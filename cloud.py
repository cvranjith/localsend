import asyncio
import io
import json
import os
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx

# ── config ────────────────────────────────────────────────────────────────────
# Falls back to the same OCI bucket/PAR already used by the screentime project
# (see ~/Documents/code/claude/screentime/push_data.py) under a new prefix, so
# no separate bucket/PAR has to be provisioned just for this feature. Override
# either with env vars if the PAR ever rotates or a dedicated bucket is wanted.
DEFAULT_OCI_PAR = (
    "https://objectstorage.ap-seoul-1.oraclecloud.com"
    "/p/XLOKpCj0N9ArpDK4xyDsuNAmPwt1JczeaCumz_EcSGvwfW4qRE1NbKS1MLj5XXQa"
    "/n/cnvubmbktlyh/b/obj-store/o/"
)
OCI_PAR = os.environ.get("LOCALSEND_OCI_PAR", DEFAULT_OCI_PAR)
OCI_PREFIX = os.environ.get("LOCALSEND_OCI_PREFIX", "fit/localsend/")
MANIFEST_PATH = "manifest.json"
MANIFEST_RETRIES = 5


def _url(path: str) -> str:
    return OCI_PAR + OCI_PREFIX + path


async def _oci_put(path: str, data: bytes, content_type: str):
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.put(_url(path), content=data, headers={"Content-Type": content_type})
        r.raise_for_status()


async def _oci_get(path: str) -> Optional[bytes]:
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.get(_url(path))
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.content


async def _get_manifest() -> dict:
    raw = await _oci_get(MANIFEST_PATH)
    if not raw:
        return {"slots": {}}
    try:
        data = json.loads(raw)
    except Exception:
        return {"slots": {}}
    data.setdefault("slots", {})
    return data


async def _put_manifest(manifest: dict):
    body = json.dumps(manifest, ensure_ascii=False, indent=2).encode()
    await _oci_put(MANIFEST_PATH, body, "application/json")


def _free_slot(manifest: dict) -> str:
    """Lowest slot number not currently in use — freed slots (their manifest
    entry removed on receive) get reused, so the bucket doesn't grow forever."""
    used = manifest.get("slots", {})
    i = 1
    while str(i) in used:
        i += 1
    return str(i)


def _build_zip(files: list[dict]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(f["path"], arcname=f["name"])
    return buf.getvalue()


def _dest_for(receive_dir: Path, name: str) -> Path:
    """Same collision-avoidance rename scheme used by the direct-transfer paths."""
    dest = receive_dir / name
    if not dest.exists():
        return dest
    stem, suffix = dest.stem, dest.suffix
    for i in range(1, 10000):
        candidate = receive_dir / f"{stem}_{i}{suffix}"
        if not candidate.exists():
            return candidate
    return dest


def _extract_zip(data: bytes, receive_dir: Path) -> list[dict]:
    extracted = []
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = Path(info.filename).name  # strip any path components — no zip-slip
            if not name:
                continue
            dest = _dest_for(receive_dir, name)
            with zf.open(info) as src, open(dest, "wb") as out:
                out.write(src.read())
            extracted.append({"name": name, "saved_path": str(dest), "size": info.file_size})
    return extracted


async def send_via_cloud(state, partner: dict, files: list[dict], transfer_id: str):
    """Zips `files`, drops the bundle into the next free numbered slot in the
    shared OCI bucket, and records it in manifest.json addressed to `partner`.
    Slot numbers get reused once the recipient pulls and clears their entry
    (receive_via_cloud below), so the bucket doesn't grow without bound."""
    partner_label = f"{partner['name']} (cloud)"
    await state.broadcast("send_start", {
        "transfer_id": transfer_id,
        "partner_name": partner_label,
        "files": [{"name": f["name"], "size": f["size"]} for f in files],
    })

    try:
        for f in files:
            if not Path(f["path"]).exists():
                raise FileNotFoundError(f"{f['name']} not found")

        zip_bytes = _build_zip(files)

        slot = None
        for _attempt in range(MANIFEST_RETRIES):
            manifest = await _get_manifest()
            slot = _free_slot(manifest)
            manifest["slots"][slot] = {
                "from_device_id": state.device_id,
                "from_name": state.config["device_name"],
                "to_device_id": partner["device_id"],
                "to_name": partner["name"],
                "files": [{"name": f["name"], "size": f["size"]} for f in files],
                "created_at": datetime.utcnow().isoformat(),
            }
            try:
                await _put_manifest(manifest)
                break
            except Exception:
                slot = None
                await asyncio.sleep(0.3)
        if slot is None:
            raise RuntimeError("Could not reserve a cloud slot after several attempts")

        # Upload after the manifest write so a receiver never sees a manifest
        # entry pointing at a zip that isn't there yet.
        await _oci_put(f"{slot}.zip", zip_bytes, "application/zip")

        for f in files:
            entry = state.add_log("sent", partner_label, f["name"], "ok", f["size"])
            await state.broadcast("log_entry", entry)
            if f.get("origin") != "browsed":
                Path(f["path"]).unlink(missing_ok=True)  # staging copy no longer needed
            await state.broadcast("send_complete", {"transfer_id": transfer_id, "filename": f["name"]})

    except Exception as e:
        for f in files:
            await state.broadcast("send_error", {
                "transfer_id": transfer_id, "filename": f["name"], "error": str(e),
            })


async def receive_via_cloud(state, partner: dict):
    """Checks manifest.json for bundles addressed to us from `partner`, pulls
    and unzips any that are found, then clears those entries so their slot
    numbers can be reused by the next send."""
    if state.is_receiving_from(partner["id"]):
        return
    state.set_receiving(partner["id"], True)
    partner_label = f"{partner['name']} (cloud)"
    announced = False

    try:
        manifest = await _get_manifest()
        pending = [
            (slot, entry) for slot, entry in manifest.get("slots", {}).items()
            if entry.get("to_device_id") == state.device_id
            and entry.get("from_device_id") == partner.get("device_id")
        ]
        if not pending:
            return

        announced = True
        await state.broadcast("status", {"receiving": True, "partner": partner_label})

        receive_dir = Path(state.config["receive_dir"])
        receive_dir.mkdir(parents=True, exist_ok=True)

        consumed = []
        for slot, entry in pending:
            transfer_id = str(uuid.uuid4())
            await state.broadcast("receive_start", {
                "transfer_id": transfer_id,
                "partner_name": partner_label,
                "files": entry.get("files", []),
            })

            zip_bytes = await _oci_get(f"{slot}.zip")
            if zip_bytes is None:
                # Sender reserved the slot but hasn't finished uploading yet —
                # leave the manifest entry alone, we'll pick it up next check.
                await state.broadcast("receive_error", {
                    "transfer_id": transfer_id, "filename": "(bundle)",
                    "error": "Bundle not ready yet — try again shortly",
                })
                continue

            try:
                extracted = _extract_zip(zip_bytes, receive_dir)
            except Exception as e:
                await state.broadcast("receive_error", {
                    "transfer_id": transfer_id, "filename": "(bundle)", "error": str(e),
                })
                continue

            for item in extracted:
                log_entry = state.add_log(
                    "received", partner_label, item["name"], "ok", item["size"], path=item["saved_path"]
                )
                await state.broadcast("log_entry", log_entry)
                await state.broadcast("receive_complete", {
                    "transfer_id": transfer_id,
                    "filename": item["name"],
                    "saved_as": Path(item["saved_path"]).name,
                    "saved_path": item["saved_path"],
                    "partner_name": partner_label,
                    "text_preview": None,
                })
            consumed.append(slot)

        if consumed:
            for _attempt in range(MANIFEST_RETRIES):
                fresh = await _get_manifest()
                for slot in consumed:
                    fresh["slots"].pop(slot, None)
                try:
                    await _put_manifest(fresh)
                    break
                except Exception:
                    await asyncio.sleep(0.3)

    finally:
        state.set_receiving(partner["id"], False)
        if announced:
            await state.broadcast("status", {"receiving": False})
