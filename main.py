import asyncio
import json
import urllib.parse
import uuid
from pathlib import Path
from typing import Optional

import aiofiles
import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from state import AppState, STAGING_DIR

POLL_INTERVAL = 30  # seconds

app = FastAPI(title="LocalSend")
state = AppState()


@app.on_event("startup")
async def startup():
    Path(state.config["receive_dir"]).mkdir(parents=True, exist_ok=True)
    asyncio.create_task(poll_loop())


# ── SSE ───────────────────────────────────────────────────────────────────────


@app.get("/api/events")
async def sse_events(request: Request):
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    state.sse_queues.append(q)

    async def generator():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=25)
                    yield {"data": msg}
                except asyncio.TimeoutError:
                    yield {"data": json.dumps({"type": "ping"})}
        finally:
            try:
                state.sse_queues.remove(q)
            except ValueError:
                pass

    return EventSourceResponse(generator())


# ── config ────────────────────────────────────────────────────────────────────


@app.get("/api/config")
async def get_config():
    return state.config


class ConfigUpdate(BaseModel):
    device_name: Optional[str] = None
    receive_dir: Optional[str] = None


@app.put("/api/config")
async def put_config(body: ConfigUpdate):
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    cfg = state.update_config(updates)
    Path(cfg["receive_dir"]).mkdir(parents=True, exist_ok=True)
    await state.broadcast("config_update", cfg)
    return cfg


# ── partners ──────────────────────────────────────────────────────────────────


@app.get("/api/partners")
async def list_partners():
    return state.partners


class AddPartner(BaseModel):
    name: str
    ip: str
    port: int


@app.post("/api/partners")
async def add_partner(body: AddPartner):
    async with httpx.AsyncClient(timeout=5) as client:
        try:
            resp = await client.get(f"http://{body.ip}:{body.port}/api/peer/hello")
            resp.raise_for_status()
            remote = resp.json()
        except Exception as e:
            raise HTTPException(400, f"Could not reach {body.ip}:{body.port} — {e}")

    remote_device_id = remote.get("device_id")
    if not remote_device_id:
        raise HTTPException(400, "Remote did not return a device_id")
    if state.get_partner_by_device_id(remote_device_id):
        raise HTTPException(409, "Partner already added (device already known)")

    name = body.name.strip() or remote.get("name", "unknown")
    partner = state.add_partner(name, body.ip, body.port, remote_device_id)
    await state.broadcast("partners_update", state.partners)
    return partner


@app.delete("/api/partners/{partner_id}")
async def remove_partner(partner_id: str):
    state.remove_partner(partner_id)
    await state.broadcast("partners_update", state.partners)
    return {"ok": True}


@app.get("/api/partners/ping")
async def ping_partners():
    results = {}
    async with httpx.AsyncClient(timeout=3) as client:
        async def ping_one(p):
            try:
                r = await client.get(f"http://{p['ip']}:{p['port']}/api/peer/hello")
                results[p["id"]] = r.status_code == 200
            except Exception:
                results[p["id"]] = False

        await asyncio.gather(*[ping_one(p) for p in state.partners], return_exceptions=True)
    return results


# ── file staging / upload ─────────────────────────────────────────────────────


@app.post("/api/upload")
async def upload_files(files: list[UploadFile] = File(...)):
    results = []
    for f in files:
        file_id = str(uuid.uuid4())
        safe_name = Path(f.filename or "file").name
        dest = STAGING_DIR / f"{file_id}_{safe_name}"
        async with aiofiles.open(dest, "wb") as out:
            while chunk := await f.read(65536):
                await out.write(chunk)
        results.append({
            "file_id": file_id,
            "name": safe_name,
            "path": str(dest),
            "size": dest.stat().st_size,
        })
    return results


# ── outbox ────────────────────────────────────────────────────────────────────


@app.get("/api/outbox")
async def list_outbox():
    return state.outbox


class QueueRequest(BaseModel):
    partner_id: str
    files: list[dict]  # [{file_id, name, path, size}]


@app.post("/api/queue")
async def queue_for_send(body: QueueRequest, background_tasks: BackgroundTasks):
    partner = state.get_partner(body.partner_id)
    if not partner:
        raise HTTPException(404, "Partner not found")

    file_list = [{"path": f["path"], "name": f["name"], "size": f["size"]} for f in body.files]
    transfer = state.add_transfer(body.partner_id, file_list)
    await state.broadcast("outbox_update", state.outbox)
    background_tasks.add_task(notify_partner, partner, transfer["transfer_id"], len(file_list))
    return transfer


@app.delete("/api/outbox/{transfer_id}")
async def cancel_transfer(transfer_id: str):
    state.remove_transfer(transfer_id)
    await state.broadcast("outbox_update", state.outbox)
    return {"ok": True}


# ── manual trigger ────────────────────────────────────────────────────────────


@app.post("/api/trigger/{partner_id}")
async def manual_trigger(partner_id: str, background_tasks: BackgroundTasks):
    partner = state.get_partner(partner_id)
    if not partner:
        raise HTTPException(404, "Partner not found")

    pending = [t for t in state.outbox if t["partner_id"] == partner_id]
    for t in pending:
        background_tasks.add_task(notify_partner, partner, t["transfer_id"], len(t["files"]))

    background_tasks.add_task(receive_from_partner, partner)
    return {"ok": True, "pending_sends": len(pending)}


# ── log ───────────────────────────────────────────────────────────────────────


@app.get("/api/log")
async def get_log():
    return sorted(state.log, key=lambda e: e["ts"], reverse=True)


# ── peer endpoints (machine-to-machine) ───────────────────────────────────────


def _require_partner(request: Request) -> dict:
    device_id = request.headers.get("X-Device-ID")
    if not device_id:
        raise HTTPException(403, "Missing X-Device-ID header")
    partner = state.get_partner_by_device_id(device_id)
    if not partner:
        raise HTTPException(403, "Unknown device — add this device as a partner first")
    return partner


@app.get("/api/peer/hello")
async def peer_hello():
    return {"device_id": state.device_id, "name": state.config["device_name"]}


@app.post("/api/peer/notify")
async def peer_notify(request: Request, background_tasks: BackgroundTasks):
    partner = _require_partner(request)
    background_tasks.add_task(receive_from_partner, partner)
    return {"ok": True}


@app.get("/api/peer/check")
async def peer_check(request: Request):
    partner = _require_partner(request)
    transfers = state.get_transfers_for_device(partner["device_id"])
    return {
        "transfers": [
            {
                "transfer_id": t["transfer_id"],
                "files": [{"name": f["name"], "size": f["size"]} for f in t["files"]],
            }
            for t in transfers
        ]
    }


@app.get("/api/peer/transfer/{transfer_id}/list")
async def peer_transfer_list(transfer_id: str, request: Request):
    _require_partner(request)
    t = state.get_transfer(transfer_id)
    if not t:
        raise HTTPException(404, "Transfer not found")
    return {
        "transfer_id": transfer_id,
        "files": [{"name": f["name"], "size": f["size"]} for f in t["files"]],
    }


@app.get("/api/peer/transfer/{transfer_id}/file/{filename:path}")
async def peer_serve_file(transfer_id: str, filename: str, request: Request):
    partner = _require_partner(request)
    t = state.get_transfer(transfer_id)
    if not t:
        raise HTTPException(404, "Transfer not found")

    # filename arrives already decoded by FastAPI
    file_info = next((f for f in t["files"] if f["name"] == filename), None)
    if not file_info:
        raise HTTPException(404, "File not in transfer")

    file_path = Path(file_info["path"])
    if not file_path.exists():
        raise HTTPException(410, "File no longer available on disk")

    file_size = file_path.stat().st_size

    async def file_stream():
        sent = 0
        last_pct = -1
        async with aiofiles.open(file_path, "rb") as f:
            while chunk := await f.read(65536):
                yield chunk
                sent += len(chunk)
                if file_size > 0:
                    pct = int(sent * 100 / file_size)
                    if pct != last_pct:
                        last_pct = pct
                        await state.broadcast(
                            "send_progress",
                            {
                                "transfer_id": transfer_id,
                                "filename": filename,
                                "partner_name": partner["name"],
                                "percent": pct,
                            },
                        )

    encoded = urllib.parse.quote(filename)
    return StreamingResponse(
        file_stream(),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{encoded}",
            "Content-Length": str(file_size),
        },
    )


@app.post("/api/peer/transfer/{transfer_id}/ack/{filename:path}")
async def peer_ack_file(transfer_id: str, filename: str, request: Request):
    partner = _require_partner(request)
    file_info = state.ack_file(transfer_id, filename)
    await state.broadcast("outbox_update", state.outbox)

    size = file_info.get("size", 0) if file_info else 0
    entry = state.add_log("sent", partner["name"], filename, "ok", size)
    await state.broadcast("log_entry", entry)
    return {"ok": True}


# ── background helpers ────────────────────────────────────────────────────────


async def notify_partner(partner: dict, transfer_id: str, file_count: int):
    url = f"http://{partner['ip']}:{partner['port']}/api/peer/notify"
    async with httpx.AsyncClient(timeout=3) as client:
        try:
            await client.post(
                url,
                json={"transfer_id": transfer_id, "file_count": file_count},
                headers={"X-Device-ID": state.device_id},
            )
        except Exception:
            pass  # poll fallback will catch it


async def receive_from_partner(partner: dict):
    # No await between check and set — safe in single-threaded asyncio
    if state.is_receiving:
        return
    state.set_receiving(True)
    await state.broadcast("status", {"receiving": True, "partner": partner["name"]})

    try:
        base_url = f"http://{partner['ip']}:{partner['port']}"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{base_url}/api/peer/check",
                headers={"X-Device-ID": state.device_id},
            )
            if resp.status_code != 200:
                return
            transfers = resp.json().get("transfers", [])
            if not transfers:
                return

        receive_dir = Path(state.config["receive_dir"])
        receive_dir.mkdir(parents=True, exist_ok=True)

        for transfer in transfers:
            await _receive_transfer(partner, transfer, base_url, receive_dir)

    except Exception as e:
        await state.broadcast("status", {"receiving": False, "error": str(e)})
    finally:
        state.set_receiving(False)
        await state.broadcast("status", {"receiving": False})


async def _receive_transfer(partner: dict, transfer: dict, base_url: str, receive_dir: Path):
    transfer_id = transfer["transfer_id"]
    files = transfer["files"]

    await state.broadcast(
        "receive_start",
        {
            "transfer_id": transfer_id,
            "partner_name": partner["name"],
            "files": files,
        },
    )

    tasks = [
        _download_file(partner, transfer_id, f, base_url, receive_dir) for f in files
    ]
    await asyncio.gather(*tasks, return_exceptions=True)


async def _download_file(
    partner: dict, transfer_id: str, file_info: dict, base_url: str, receive_dir: Path
):
    async with state.file_semaphore:
        filename = file_info["name"]
        total = file_info.get("size", 0)

        dest = receive_dir / filename
        if dest.exists():
            stem, suffix = dest.stem, dest.suffix
            for i in range(1, 10000):
                dest = receive_dir / f"{stem}_{i}{suffix}"
                if not dest.exists():
                    break

        url = f"{base_url}/api/peer/transfer/{transfer_id}/file/{urllib.parse.quote(filename)}"

        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream(
                    "GET", url, headers={"X-Device-ID": state.device_id}
                ) as resp:
                    resp.raise_for_status()
                    received = 0
                    last_pct = -1
                    async with aiofiles.open(dest, "wb") as f:
                        async for chunk in resp.aiter_bytes(65536):
                            await f.write(chunk)
                            received += len(chunk)
                            if total > 0:
                                pct = int(received * 100 / total)
                                if pct != last_pct:
                                    last_pct = pct
                                    await state.broadcast(
                                        "receive_progress",
                                        {
                                            "transfer_id": transfer_id,
                                            "filename": filename,
                                            "partner_name": partner["name"],
                                            "percent": pct,
                                            "received": received,
                                            "total": total,
                                        },
                                    )

            # ACK to let sender untrack this file
            async with httpx.AsyncClient(timeout=5) as client:
                await client.post(
                    f"{base_url}/api/peer/transfer/{transfer_id}/ack/{urllib.parse.quote(filename)}",
                    headers={"X-Device-ID": state.device_id},
                )

            entry = state.add_log("received", partner["name"], filename, "ok", total)
            await state.broadcast("log_entry", entry)
            await state.broadcast(
                "receive_complete",
                {
                    "transfer_id": transfer_id,
                    "filename": filename,
                    "saved_as": dest.name,
                    "partner_name": partner["name"],
                },
            )

        except Exception as e:
            if dest.exists() and dest.stat().st_size == 0:
                dest.unlink(missing_ok=True)
            state.add_log("received", partner["name"], filename, "error")
            await state.broadcast(
                "receive_error",
                {
                    "transfer_id": transfer_id,
                    "filename": filename,
                    "error": str(e),
                },
            )


async def poll_loop():
    while True:
        await asyncio.sleep(POLL_INTERVAL)
        if state.is_receiving:
            continue
        for partner in list(state.partners):
            try:
                await receive_from_partner(partner)
            except Exception:
                pass


# ── static ────────────────────────────────────────────────────────────────────


@app.get("/")
async def root():
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")


if __name__ == "__main__":
    import uvicorn

    port = int(state.config.get("port", 8765))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
