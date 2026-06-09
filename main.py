import asyncio
import json
import socket
import subprocess
import time
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



def get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


app = FastAPI(title="LocalSend")
state = AppState()


@app.on_event("startup")
async def startup():
    Path(state.config["receive_dir"]).mkdir(parents=True, exist_ok=True)


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
    return {**state.config, "local_ip": get_local_ip()}


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
    if state.get_partner_by_ip_port(body.ip, body.port):
        raise HTTPException(409, f"Partner at {body.ip}:{body.port} already exists")

    remote_device_id = None
    remote_name = body.name.strip()
    # reachable=True  → we can push files directly to them
    # reachable=False → they must pull from us; we pull from them via Receive button
    reachable = False

    async with httpx.AsyncClient(timeout=5) as client:
        try:
            resp = await client.get(f"http://{body.ip}:{body.port}/api/peer/hello")
            resp.raise_for_status()
            remote = resp.json()
            remote_device_id = remote.get("device_id")
            if not remote_name:
                remote_name = remote.get("name", "unknown")
            reachable = True
        except Exception:
            pass  # Save anyway; reachable=False means pull-only for this partner

    if remote_device_id and state.get_partner_by_device_id(remote_device_id):
        raise HTTPException(409, "Partner already added (device already known)")

    partner = state.add_partner(remote_name or "unknown", body.ip, body.port, remote_device_id, reachable)
    await state.broadcast("partners_update", state.partners)
    return partner


@app.delete("/api/partners/{partner_id}")
async def remove_partner(partner_id: str):
    state.remove_partner(partner_id)
    await state.broadcast("partners_update", state.partners)
    return {"ok": True}


@app.get("/api/partners/ping")
async def ping_partners():
    now = time.time()
    results = {}
    async with httpx.AsyncClient(timeout=3) as client:
        async def ping_one(p):
            try:
                r = await client.get(f"http://{p['ip']}:{p['port']}/api/peer/hello")
                online = r.status_code == 200
            except Exception:
                online = False
            last_seen = state.partner_last_seen.get(p["id"])
            results[p["id"]] = {
                "online": online,
                "last_seen_sec": round(now - last_seen) if last_seen else None,
            }
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


# ── outbox (for pull-side: partner queues here, we pull on Receive) ───────────


@app.get("/api/outbox")
async def list_outbox():
    return state.outbox


@app.delete("/api/outbox/{transfer_id}")
async def cancel_outbox_entry(transfer_id: str):
    state.remove_transfer(transfer_id)
    await state.broadcast("outbox_update", state.outbox)
    return {"ok": True}


# ── send (push directly to partner) ──────────────────────────────────────────


class SendRequest(BaseModel):
    partner_id: str
    files: list[dict]  # [{file_id, name, path, size}]


@app.post("/api/send")
async def send_to_partner(body: SendRequest):
    partner = state.get_partner(body.partner_id)
    if not partner:
        raise HTTPException(404, "Partner not found")

    transfer_id = str(uuid.uuid4())
    file_list = [{"path": f["path"], "name": f["name"], "size": f["size"]} for f in body.files]

    await state.broadcast("send_start", {
        "transfer_id": transfer_id,
        "partner_name": partner["name"],
        "files": [{"name": f["name"], "size": f["size"]} for f in file_list],
    })

    task = asyncio.create_task(push_files_to_partner(partner, file_list, transfer_id))
    state.active_tasks[transfer_id] = task
    return {"transfer_id": transfer_id, "ok": True}


# ── receive (pull from partner) ───────────────────────────────────────────────


@app.post("/api/receive/{partner_id}")
async def receive_from_partner_endpoint(partner_id: str):
    partner = state.get_partner(partner_id)
    if not partner:
        raise HTTPException(404, "Partner not found")
    asyncio.create_task(receive_from_partner(partner))
    return {"ok": True}


# ── abort ─────────────────────────────────────────────────────────────────────


@app.post("/api/abort/{transfer_id}")
async def abort_transfer(transfer_id: str):
    task = state.active_tasks.get(transfer_id)
    if task and not task.done():
        task.cancel()
    return {"ok": True}


# ── log ───────────────────────────────────────────────────────────────────────


@app.get("/api/log")
async def get_log():
    return sorted(state.log, key=lambda e: e["ts"], reverse=True)


class OpenRequest(BaseModel):
    path: str
    type: str = "file"  # "file" or "folder"


@app.post("/api/open")
async def open_path(body: OpenRequest):
    p = Path(body.path)
    if not p.exists():
        raise HTTPException(404, "Path not found")
    if body.type == "folder":
        subprocess.Popen(["open", "-R", str(p)])  # reveal in Finder
    else:
        subprocess.Popen(["open", str(p)])
    return {"ok": True}


# ── peer endpoints (machine-to-machine) ───────────────────────────────────────


def _require_partner(request: Request) -> dict:
    device_id = request.headers.get("X-Device-ID")

    partner = None
    if device_id:
        partner = state.get_partner_by_device_id(device_id)

    if not partner:
        client_ip = request.client.host if request.client else None
        if client_ip:
            candidate = state.get_partner_by_ip(client_ip)
            if candidate and not candidate.get("device_id"):
                if device_id:
                    candidate["device_id"] = device_id
                    state._save_partners()
                partner = candidate

    if not partner:
        raise HTTPException(403, "Unknown device — add this device as a partner first")

    state.partner_last_seen[partner["id"]] = time.time()
    asyncio.create_task(state.broadcast("partner_active", {"id": partner["id"]}))
    return partner


@app.get("/api/peer/hello")
async def peer_hello():
    return {"device_id": state.device_id, "name": state.config["device_name"]}


@app.post("/api/peer/push/{filename:path}")
async def peer_receive_push(filename: str, request: Request):
    """Sender streams a file directly to us."""
    partner = _require_partner(request)
    transfer_id = request.headers.get("X-Transfer-ID", str(uuid.uuid4()))
    total = int(request.headers.get("Content-Length", 0))

    receive_dir = Path(state.config["receive_dir"])
    receive_dir.mkdir(parents=True, exist_ok=True)

    dest = receive_dir / filename
    if dest.exists():
        stem, suffix = dest.stem, dest.suffix
        for i in range(1, 10000):
            dest = receive_dir / f"{stem}_{i}{suffix}"
            if not dest.exists():
                break

    await state.broadcast("receive_start", {
        "transfer_id": transfer_id,
        "partner_name": partner["name"],
        "files": [{"name": filename, "size": total}],
    })

    received = 0
    last_pct = -1
    try:
        async with aiofiles.open(dest, "wb") as f:
            async for chunk in request.stream():
                await f.write(chunk)
                received += len(chunk)
                if total > 0:
                    pct = int(received * 100 / total)
                    if pct != last_pct:
                        last_pct = pct
                        await state.broadcast("receive_progress", {
                            "transfer_id": transfer_id,
                            "filename": filename,
                            "partner_name": partner["name"],
                            "percent": pct,
                        })
    except Exception as e:
        if dest.exists() and dest.stat().st_size == 0:
            dest.unlink(missing_ok=True)
        await state.broadcast("receive_error", {
            "transfer_id": transfer_id, "filename": filename, "error": str(e)
        })
        raise HTTPException(500, str(e))

    entry = state.add_log("received", partner["name"], filename, "ok", received, path=str(dest))
    await state.broadcast("log_entry", entry)
    await state.broadcast("receive_complete", {
        "transfer_id": transfer_id,
        "filename": filename,
        "saved_as": dest.name,
        "saved_path": str(dest),
        "partner_name": partner["name"],
        "text_preview": _text_preview(dest),
    })
    return {"ok": True, "saved_as": dest.name}


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
    # Let the sender UI know the queued file was pulled successfully
    await state.broadcast("send_complete", {"transfer_id": transfer_id, "filename": filename})

    size = file_info.get("size", 0) if file_info else 0
    entry = state.add_log("sent", partner["name"], filename, "ok", size)
    await state.broadcast("log_entry", entry)
    return {"ok": True}


def _text_preview(dest: Path, max_bytes: int = 4096) -> Optional[str]:
    """Return file contents as string if it looks like small text, else None."""
    try:
        if dest.stat().st_size > max_bytes:
            return None
        raw = dest.read_bytes()
        if b"\x00" in raw:
            return None
        return raw.decode("utf-8")
    except Exception:
        return None


# ── background helpers ────────────────────────────────────────────────────────


async def push_files_to_partner(partner: dict, files: list[dict], transfer_id: str):
    """Push each file directly to partner. Falls back to outbox if unreachable."""
    try:
        base_url = f"http://{partner['ip']}:{partner['port']}"
        timeout = httpx.Timeout(connect=3.0, read=None, write=None, pool=5.0)

        for file_info in files:
            file_path = Path(file_info["path"])
            filename = file_info["name"]

            if not file_path.exists():
                await state.broadcast("send_error", {
                    "transfer_id": transfer_id, "filename": filename, "error": "File not found",
                })
                continue

            file_size = file_path.stat().st_size
            url = f"{base_url}/api/peer/push/{urllib.parse.quote(filename)}"
            sent_ref = [0]
            pct_ref = [-1]

            async def streamer(fp=file_path, fs=file_size, sr=sent_ref, pr=pct_ref):
                async with aiofiles.open(fp, "rb") as f:
                    while chunk := await f.read(65536):
                        yield chunk
                        sr[0] += len(chunk)
                        if fs > 0:
                            pct = int(sr[0] * 100 / fs)
                            if pct != pr[0]:
                                pr[0] = pct
                                await state.broadcast("send_progress", {
                                    "transfer_id": transfer_id,
                                    "filename": filename,
                                    "partner_name": partner["name"],
                                    "percent": pct,
                                })

            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.post(
                        url,
                        content=streamer(),
                        headers={
                            "X-Device-ID": state.device_id,
                            "Content-Length": str(file_size),
                            "X-Transfer-ID": transfer_id,
                        },
                    )
                    resp.raise_for_status()

                entry = state.add_log("sent", partner["name"], filename, "ok", file_size)
                await state.broadcast("log_entry", entry)
                await state.broadcast("send_complete", {
                    "transfer_id": transfer_id, "filename": filename,
                })

            except asyncio.CancelledError:
                await state.broadcast("send_cancelled", {
                    "transfer_id": transfer_id, "filename": filename,
                })
                raise

            except (httpx.NetworkError, httpx.TimeoutException):
                # Any network-level failure (ConnectError, ReadError, timeout…)
                # Queue file for them to pull when they next poll us
                t = state.add_transfer(partner["id"], [file_info])
                await state.broadcast("send_queued", {
                    "transfer_id": transfer_id,
                    "filename": filename,
                    "partner_name": partner["name"],
                    "outbox_id": t["transfer_id"],
                })

            except Exception as e:
                await state.broadcast("send_error", {
                    "transfer_id": transfer_id, "filename": filename, "error": str(e),
                })

    except asyncio.CancelledError:
        pass
    finally:
        state.active_tasks.pop(transfer_id, None)


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

            entry = state.add_log("received", partner["name"], filename, "ok", total, path=str(dest))
            await state.broadcast("log_entry", entry)
            await state.broadcast(
                "receive_complete",
                {
                    "transfer_id": transfer_id,
                    "filename": filename,
                    "saved_as": dest.name,
                    "saved_path": str(dest),
                    "partner_name": partner["name"],
                    "text_preview": _text_preview(dest),
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


# ── static ────────────────────────────────────────────────────────────────────


@app.get("/")
async def root():
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")


if __name__ == "__main__":
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=None, help="Port to listen on")
    args = parser.parse_args()

    port = args.port or int(state.config.get("port", 8765))
    state.update_config({"port": port})  # persist so the UI shows the right port
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
