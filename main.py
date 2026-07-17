import asyncio
import json
import logging
import mimetypes
import re
import socket
import subprocess
import urllib.parse
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import aiofiles
import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from cloud import send_via_cloud, receive_via_cloud
from state import AppState, STAGING_DIR, LONGPOLL_GRACE_SEC, DEFAULT_LONG_POLL_TIMEOUT_SEC


# Routine status polling (repainting the partner list, the client heartbeat) would
# otherwise spam the terminal on every tick and drown out the discover/scan logs
# that actually matter — drop just those two paths from uvicorn's access log.
class _QuietAccessFilter(logging.Filter):
    _quiet = ('"GET /api/partners HTTP/', '"GET /api/partners/ping HTTP/')

    def filter(self, record: logging.LogRecord) -> bool:
        return not any(q in record.getMessage() for q in self._quiet)


logging.getLogger("uvicorn.access").addFilter(_QuietAccessFilter())


def _log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [localsend] {msg}", flush=True)


async def _hello_check(client: httpx.AsyncClient, ip: str, port: int) -> Optional[dict]:
    """GETs /api/peer/hello at ip:port, self-identifying with our own name/port
    (and, if we're a client, our ping frequency so the callee can register us).
    Returns the parsed reply, or None if unreachable."""
    params = {"name": state.config["device_name"], "port": state.config["port"]}
    if state.config.get("role") == "client":
        params["freq"] = state.config.get("ping_frequency_sec")
    try:
        r = await client.get(
            f"http://{ip}:{port}/api/peer/hello",
            params=params,
            headers={"X-Device-ID": state.device_id},
        )
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


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


class _DiscoveryResponder(asyncio.DatagramProtocol):
    """Answers UDP broadcast discovery queries with this device's identity."""

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr):
        try:
            msg = json.loads(data.decode())
        except Exception:
            return
        if msg.get("type") != "localsend_discover":
            return
        _log(f"discovery: got broadcast query from {addr[0]}:{addr[1]} — replying")
        caller_id = msg.get("from")
        if caller_id:
            caller = state.get_partner_by_device_id(caller_id)
            if caller:
                _touch(caller)
        reply = json.dumps({
            "type": "localsend_hello",
            "device_id": state.device_id,
            "name": state.config["device_name"],
            "port": state.config["port"],
        }).encode()
        self.transport.sendto(reply, addr)


class _DiscoveryClientProtocol(asyncio.DatagramProtocol):
    def __init__(self):
        self.replies: list[tuple[dict, tuple]] = []

    def datagram_received(self, data: bytes, addr):
        try:
            self.replies.append((json.loads(data.decode()), addr))
        except Exception:
            pass


async def _udp_discover(target_port: int, timeout: float = 1.5) -> list[tuple[dict, tuple]]:
    """Broadcasts a discovery query on target_port and collects replies for `timeout` seconds."""
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        _DiscoveryClientProtocol,
        local_addr=("0.0.0.0", 0),
        allow_broadcast=True,
    )
    try:
        _log(f"discover: broadcasting on 255.255.255.255:{target_port}, listening {timeout}s")
        msg = json.dumps({"type": "localsend_discover", "from": state.device_id}).encode()
        transport.sendto(msg, ("255.255.255.255", target_port))
        await asyncio.sleep(timeout)
        summary = ", ".join(f"{addr[0]} ({reply.get('name')})" for reply, addr in protocol.replies)
        _log(f"discover: broadcast got {len(protocol.replies)} reply(ies){': ' + summary if summary else ''}")
        return protocol.replies
    finally:
        transport.close()


# ── long-poll (client holds a connection open on a server partner for instant
#    work notification, instead of waiting for its next short heartbeat) ───────


def _set_reachable(partner: dict, online: bool):
    """Update our own (client-role) view of a partner's reachability and push
    it live, same pattern as the active-probe endpoints (ping_partner(s)). Only
    logs on an actual transition, not every retry attempt — a clear marker for
    when the pipe actually went down or came back, not just noise."""
    if partner.get("reachable") == online:
        return
    _log(f"longpoll: {partner['name']} connection {'resumed' if online else 'lost'}")
    partner["reachable"] = online
    state._save_partners()
    status = state.partner_status(partner)
    asyncio.create_task(state.broadcast("partner_active", {"id": partner["id"], "status": status}))


async def _longpoll_loop(partner_id: str):
    """Runs for the lifetime of a client→server pairing. Holds a GET open on the
    partner; an immediate response means either new work or a dropped connection —
    either way we just reconnect. This loop is a backend asyncio task, not a
    browser timer — unlike the JS heartbeat (setInterval, which browsers throttle
    hard on a backgrounded tab, sometimes to once an hour), it keeps running and
    retrying every 5s regardless of whether any tab is even open, so it's a much
    faster and more reliable "is the partner actually reachable" signal than
    waiting on the next heartbeat tick to notice and update reachable."""
    while True:
        if state.config.get("role") != "client":
            return  # role changed away from client — stop
        partner = state.get_partner(partner_id)
        if not partner:
            return  # partner removed — stop

        timeout = max(10, min(int(state.config.get("long_poll_timeout_sec") or DEFAULT_LONG_POLL_TIMEOUT_SEC), 3600))
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=10, read=timeout + 15, write=10, pool=10)
            ) as client:
                r = await client.get(
                    f"http://{partner['ip']}:{partner['port']}/api/peer/wait",
                    params={"timeout": timeout},
                    headers={"X-Device-ID": state.device_id},
                )
            if r.status_code != 200:
                raise httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)
            _set_reachable(partner, True)
            if r.json().get("work"):
                _log(f"longpoll: {partner['name']} signaled work — pulling")
                # Await, don't fire-and-forget: the outbox entry isn't cleared until
                # this finishes, so reconnecting immediately would just re-poll into
                # the same still-in-flight transfer over and over.
                await receive_from_partner(partner)
            # else: clean timeout, nothing queued — just reconnect immediately
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _set_reachable(partner, False)  # logs "connection lost" only on the actual transition
            reason = str(e) or type(e).__name__  # some exceptions (e.g. a bare ConnectError) stringify to ""
            _log(f"longpoll: {partner['name']} retrying in 5s ({reason})")
            await asyncio.sleep(5)


def _start_longpoll(partner: dict):
    task = state.longpoll_tasks.get(partner["id"])
    if task and not task.done():
        return
    state.longpoll_tasks[partner["id"]] = asyncio.create_task(_longpoll_loop(partner["id"]))


def _stop_longpoll(partner_id: str):
    task = state.longpoll_tasks.pop(partner_id, None)
    if task and not task.done():
        task.cancel()


def _sync_longpoll_tasks():
    """Start/stop long-poll loops so they match the current role and partner list."""
    if state.config.get("role") != "client":
        for pid in list(state.longpoll_tasks):
            _stop_longpoll(pid)
        return
    live_ids = {p["id"] for p in state.partners}
    for pid in list(state.longpoll_tasks):
        if pid not in live_ids:
            _stop_longpoll(pid)
    for p in state.partners:
        _start_longpoll(p)


@app.on_event("startup")
async def startup():
    Path(state.config["receive_dir"]).mkdir(parents=True, exist_ok=True)
    loop = asyncio.get_running_loop()
    try:
        await loop.create_datagram_endpoint(
            _DiscoveryResponder,
            local_addr=("0.0.0.0", state.config["port"]),
        )
    except OSError:
        pass  # UDP port unavailable — discovery replies just won't work, HTTP server is unaffected
    _sync_longpoll_tasks()

    # Fire-and-forget: try to relocate any partner explicitly configured for
    # subnet scanning (a wildcard IP), so a roamed device is found promptly
    # after a restart instead of waiting for the next failed sync attempt.
    for p in state.partners:
        if p.get("allow_scan"):
            asyncio.create_task(_discover_partner(p))


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
    role: Optional[str] = None
    ping_frequency_sec: Optional[int] = None
    long_poll_timeout_sec: Optional[int] = None


@app.put("/api/config")
async def put_config(body: ConfigUpdate):
    if body.role is not None and body.role not in ("server", "client"):
        raise HTTPException(400, "role must be 'server' or 'client'")
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    cfg = state.update_config(updates)
    Path(cfg["receive_dir"]).mkdir(parents=True, exist_ok=True)
    _sync_longpoll_tasks()
    await state.broadcast("config_update", cfg)
    return cfg


# ── partners ──────────────────────────────────────────────────────────────────


@app.get("/api/partners")
async def list_partners():
    return [{**p, "status": state.partner_status(p)} for p in state.partners]


# A trailing ".*" last octet (e.g. "192.168.1.*") is the explicit opt-in for
# subnet scanning — a bare IP never triggers it (see _discover_partner below).
_WILDCARD_IP_RE = re.compile(r"^(\d{1,3}\.\d{1,3}\.\d{1,3})\.\*$")


class AddPartner(BaseModel):
    ip: str
    port: int


@app.post("/api/partners")
async def add_partner(body: AddPartner):
    ip = body.ip.strip()

    if _WILDCARD_IP_RE.match(ip):
        # No concrete address to dial yet — broadcast for it instead, and
        # require an unambiguous single reply since there's no name/device_id
        # to match against on a first-ever add.
        replies = await _udp_discover(body.port)
        candidates = {}
        for remote, addr in replies:
            if remote.get("type") != "localsend_hello":
                continue
            candidates[remote.get("device_id") or addr[0]] = {
                "ip": addr[0], "port": remote.get("port", body.port), **remote,
            }
        if not candidates:
            raise HTTPException(400, f"No device answered a broadcast on port {body.port} — make sure it's running and reachable")
        if len(candidates) > 1:
            names = ", ".join(f"{c.get('name')} ({c['ip']})" for c in candidates.values())
            raise HTTPException(400, f"Found more than one device on port {body.port} ({names}) — add by its exact IP instead so there's no ambiguity")
        remote = next(iter(candidates.values()))
        resolved_ip, resolved_port = remote["ip"], remote["port"]
        allow_scan = True
    else:
        async with httpx.AsyncClient(timeout=5) as client:
            remote = await _hello_check(client, ip, body.port)
        if not remote:
            raise HTTPException(400, "Couldn't reach that address — check the IP, port, and that it's running LocalSend")
        resolved_ip, resolved_port = ip, body.port
        allow_scan = False

    if state.get_partner_by_ip_port(resolved_ip, resolved_port):
        raise HTTPException(409, f"Partner at {resolved_ip}:{resolved_port} already exists")

    remote_device_id = remote.get("device_id")
    remote_name = remote.get("name") or "unknown"

    if remote_device_id and state.get_partner_by_device_id(remote_device_id):
        raise HTTPException(409, "Partner already added (device already known)")

    partner = state.add_partner(
        remote_name, resolved_ip, resolved_port, remote_device_id,
        reachable=True, mode="server", allow_scan=allow_scan,
    )
    _touch(partner)
    _sync_longpoll_tasks()
    await state.broadcast("partners_update", state.partners)
    return {**partner, "status": state.partner_status(partner)}


class PartnerEdit(BaseModel):
    ip: Optional[str] = None
    port: Optional[int] = None
    route: Optional[str] = None  # "local" | "auto" | "cloud"


@app.put("/api/partners/{partner_id}")
async def edit_partner(partner_id: str, body: PartnerEdit):
    if body.route is not None and body.route not in ("local", "auto", "cloud"):
        raise HTTPException(400, "route must be 'local', 'auto', or 'cloud'")

    updates = {}
    if body.ip is not None:
        ip = body.ip.strip()
        if _WILDCARD_IP_RE.match(ip):
            # Toggle scanning on; keep the last-known concrete IP untouched —
            # something still has to be dialed until the next scan updates it.
            updates["allow_scan"] = True
        else:
            updates["ip"] = ip
            updates["allow_scan"] = False
    if body.port is not None:
        updates["port"] = body.port
    if body.route is not None:
        updates["route"] = body.route
    if not updates:
        raise HTTPException(400, "Nothing to update")

    partner = state.update_partner(partner_id, updates)
    if not partner:
        raise HTTPException(404, "Partner not found")
    await state.broadcast("partners_update", state.partners)
    return {**partner, "status": state.partner_status(partner)}


@app.delete("/api/partners/{partner_id}")
async def remove_partner(partner_id: str):
    partner = state.get_partner(partner_id)
    if partner:
        # Best-effort: tell the other side to drop its record of us too.
        try:
            async with httpx.AsyncClient(timeout=2) as client:
                await client.post(
                    f"http://{partner['ip']}:{partner['port']}/api/peer/forget",
                    headers={"X-Device-ID": state.device_id},
                )
        except Exception:
            pass
    state.remove_partner(partner_id)
    _stop_longpoll(partner_id)
    await state.broadcast("partners_update", state.partners)
    return {"ok": True}


async def _scan_subnet(port: int, matches, start_octet: int) -> Optional[dict]:
    """Sequentially probes the local /24 on `port`, nearest to start_octet first, so a
    one-hop IP change (a common DHCP-lease bump) is usually found within the first
    couple of tries. Last-resort fallback for when broadcast discovery gets no
    replies (AP/client isolation, a firewall silently dropping the UDP query, etc)."""
    local_ip = get_local_ip()
    prefix = ".".join(local_ip.split(".")[:3])
    order = sorted(range(1, 255), key=lambda i: abs(i - start_octet))

    _log(f"discover: broadcast got no match — falling back to a sequential scan of "
         f"{prefix}.0/24 on port {port}, nearest to .{start_octet} first ({len(order)} hosts)")

    sem = asyncio.Semaphore(24)
    stop = asyncio.Event()
    found: dict = {}
    tried = {"n": 0}

    async def probe(i: int):
        if stop.is_set():
            return
        ip = f"{prefix}.{i}"
        if ip == local_ip:
            return
        async with sem:
            if stop.is_set():
                return
            async with httpx.AsyncClient(timeout=0.6) as client:
                remote = await _hello_check(client, ip, port)
            tried["n"] += 1
            if remote and matches(remote):
                _log(f"discover: scan matched {ip}:{port} ({remote.get('name')}) after {tried['n']} hosts tried")
                found["hit"] = {"ip": ip, "port": port, **remote}
                stop.set()

    await asyncio.gather(*[probe(i) for i in order], return_exceptions=True)
    if "hit" not in found:
        _log(f"discover: scan exhausted {tried['n']} hosts on {prefix}.0/24:{port} — not found")
    return found.get("hit")


async def _discover_partner(partner: dict) -> dict:
    """Re-locate a partner whose IP may have changed: recheck its last known
    address, then try a UDP broadcast on its known port. Only if this partner
    has scanning explicitly enabled (a wildcard IP like 192.168.1.* was
    configured for it — see _WILDCARD_IP_RE) do we fall back further to
    actually probing every host on the local subnet; otherwise a partner with
    a fixed IP that doesn't answer is just reported unreachable, not scanned."""
    _log(f"discover: starting for '{partner['name']}', last known {partner['ip']}:{partner['port']}")

    target_device_id = partner.get("device_id")
    target_name = partner["name"].strip().lower()

    def matches(remote: dict) -> bool:
        remote_id = remote.get("device_id")
        remote_name = (remote.get("name") or "").strip().lower()
        return (target_device_id and remote_id == target_device_id) or remote_name == target_name

    hit = None

    # Fast path: maybe it's still (or again) at its last known address
    async with httpx.AsyncClient(timeout=1.5) as client:
        remote = await _hello_check(client, partner["ip"], partner["port"])
    if remote and matches(remote):
        _log(f"discover: still at its last known address {partner['ip']}:{partner['port']}")
        hit = {"ip": partner["ip"], "port": partner["port"], **remote}
    elif remote:
        _log(f"discover: {partner['ip']}:{partner['port']} answered but as a different device — ignoring")
    else:
        _log(f"discover: {partner['ip']}:{partner['port']} unreachable, trying broadcast")

    # Fallback: broadcast a discovery query on the partner's known port and listen for replies
    if not hit:
        for remote, addr in await _udp_discover(partner["port"]):
            if remote.get("type") == "localsend_hello" and matches(remote):
                hit = {"ip": addr[0], "port": remote.get("port", partner["port"]), **remote}
                break

    # Last resort: actually probe hosts on the subnet — only for partners explicitly opted in
    if not hit and partner.get("allow_scan"):
        try:
            start_octet = int(partner["ip"].split(".")[-1])
        except ValueError:
            start_octet = 1
        hit = await _scan_subnet(partner["port"], matches, start_octet)
    elif not hit:
        _log(f"discover: scanning not enabled for '{partner['name']}' (no wildcard IP configured) — not probing the subnet")

    if not hit:
        _log(f"discover: gave up, could not locate '{partner['name']}'")
        return {"found": False}

    changed = hit["ip"] != partner["ip"] or hit["port"] != partner["port"]
    _log(f"discover: found '{partner['name']}' at {hit['ip']}:{hit['port']} (changed={changed})")
    partner["ip"] = hit["ip"]
    partner["port"] = hit["port"]
    if hit.get("device_id"):
        partner["device_id"] = hit["device_id"]
    partner["reachable"] = True
    state._save_partners()
    _touch(partner)
    await state.broadcast("partners_update", state.partners)
    return {"found": True, "ip": hit["ip"], "port": hit["port"], "changed": changed}


@app.post("/api/partners/{partner_id}/discover")
async def discover_partner(partner_id: str):
    partner = state.get_partner(partner_id)
    if not partner:
        raise HTTPException(404, "Partner not found")
    return await _discover_partner(partner)


@app.get("/api/partners/ping")
async def ping_partners():
    """Actively pings every partner right now. A client does this on its own
    heartbeat schedule; a server never calls this automatically (it just waits
    to be pinged) but the UI's manual refresh button hits this regardless of
    role, so a stale "green" can be forced to re-check instead of waiting out
    the passive freshness window."""
    changed = False
    async with httpx.AsyncClient(timeout=3) as client:
        async def ping_one(p):
            nonlocal changed
            remote = await _hello_check(client, p["ip"], p["port"])
            online = remote is not None
            if p.get("reachable") != online:
                p["reachable"] = online
                changed = True
            if online:
                _touch(p)
        await asyncio.gather(*[ping_one(p) for p in state.partners], return_exceptions=True)
    if changed:
        state._save_partners()
        await state.broadcast("partners_update", state.partners)
    return {p["id"]: {"status": state.partner_status(p)} for p in state.partners}


@app.get("/api/partners/{partner_id}/ping")
async def ping_partner(partner_id: str):
    """Quick reachability check at the partner's current stored address, used by
    the Discover flow to decide whether a network scan is actually needed."""
    partner = state.get_partner(partner_id)
    if not partner:
        raise HTTPException(404, "Partner not found")

    async with httpx.AsyncClient(timeout=3) as client:
        remote = await _hello_check(client, partner["ip"], partner["port"])
    online = remote is not None

    if partner.get("reachable") != online:
        partner["reachable"] = online
        state._save_partners()
        await state.broadcast("partners_update", state.partners)
    if online:
        _touch(partner)

    return {"online": online, "status": state.partner_status(partner)}


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
            "origin": "staged",
        })
    return results


@app.get("/api/browse")
async def browse_dir(path: str = ""):
    """List a directory on the server's own filesystem, so a file can be
    queued for sending by path without first uploading its bytes through
    the browser. If the path resolves to a file instead, report it as such
    (with its parent dir) so the caller can select it directly rather than
    navigate into it."""
    p = Path(path).expanduser() if path else Path.home()
    try:
        p = p.resolve()
    except (OSError, RuntimeError):
        raise HTTPException(400, "Invalid path")
    if not p.exists():
        raise HTTPException(404, "No such file or directory")

    if p.is_file():
        try:
            size = p.stat().st_size
        except OSError:
            raise HTTPException(403, "Permission denied")
        return {"path": str(p), "parent": str(p.parent), "is_file": True, "name": p.name, "size": size}

    if not p.is_dir():
        raise HTTPException(404, "Not a file or directory")

    entries = []
    try:
        for child in p.iterdir():
            try:
                is_dir = child.is_dir()
                entries.append({
                    "name": child.name,
                    "path": str(child),
                    "is_dir": is_dir,
                    "size": None if is_dir else child.stat().st_size,
                })
            except OSError:
                continue  # broken symlink, permission error on stat, etc.
    except PermissionError:
        raise HTTPException(403, "Permission denied")

    entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
    parent = str(p.parent) if p.parent != p else None
    return {"path": str(p), "parent": parent, "is_file": False, "entries": entries}


@app.get("/api/browse/suggest")
async def browse_suggest(path: str = ""):
    """Autocomplete helper for the browse path field: given a partial path
    being typed, return sibling entries in its directory whose name starts
    with the typed fragment (or, if the fragment already ends in a
    separator, the full contents of that directory)."""
    raw = path or ""
    expanded = Path(raw).expanduser()
    if raw.endswith("/") or raw.endswith("\\"):
        base, prefix = expanded, ""
    else:
        base, prefix = expanded.parent, expanded.name

    try:
        base = base.resolve()
    except (OSError, RuntimeError):
        return {"entries": []}
    if not base.is_dir():
        return {"entries": []}

    prefix_lower = prefix.lower()
    matches = []
    try:
        for child in base.iterdir():
            if prefix_lower and not child.name.lower().startswith(prefix_lower):
                continue
            try:
                is_dir = child.is_dir()
                matches.append({
                    "name": child.name,
                    "path": str(child),
                    "is_dir": is_dir,
                    "size": None if is_dir else child.stat().st_size,
                })
            except OSError:
                continue
    except PermissionError:
        return {"entries": []}

    matches.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
    return {"entries": matches[:20], "base": str(base)}


# ── outbox (for pull-side: partner queues here, we pull on Receive) ───────────


@app.get("/api/outbox")
async def list_outbox():
    return state.outbox


@app.delete("/api/outbox/{transfer_id}")
async def cancel_outbox_entry(transfer_id: str):
    t = state.get_transfer(transfer_id)
    if t:
        for f in t.get("files", []):
            if f.get("origin") != "browsed":
                Path(f["path"]).unlink(missing_ok=True)
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
    file_list = [
        {"path": f["path"], "name": f["name"], "size": f["size"], "origin": f.get("origin", "staged")}
        for f in body.files
    ]

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


# ── cloud fallback (manual, for when the direct pipe is down) ────────────────
# Bundles files into a zip and drops it in a shared OCI bucket (see cloud.py)
# instead of streaming peer-to-peer. Manually triggered from the UI only —
# never kicked off automatically.


class CloudSendRequest(BaseModel):
    partner_id: str
    files: list[dict]  # [{file_id, name, path, size}]


@app.post("/api/cloud/send")
async def cloud_send(body: CloudSendRequest):
    partner = state.get_partner(body.partner_id)
    if not partner:
        raise HTTPException(404, "Partner not found")
    if not partner.get("device_id"):
        raise HTTPException(400, "Partner has no known device ID yet — contact them directly at least once first")
    if not body.files:
        raise HTTPException(400, "No files to send")

    transfer_id = str(uuid.uuid4())
    file_list = [
        {"path": f["path"], "name": f["name"], "size": f["size"], "origin": f.get("origin", "staged")}
        for f in body.files
    ]
    asyncio.create_task(send_via_cloud(state, partner, file_list, transfer_id))
    return {"transfer_id": transfer_id, "ok": True}


@app.post("/api/cloud/receive/{partner_id}")
async def cloud_receive_endpoint(partner_id: str):
    partner = state.get_partner(partner_id)
    if not partner:
        raise HTTPException(404, "Partner not found")
    asyncio.create_task(receive_via_cloud(state, partner))
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


@app.delete("/api/log/{entry_id}")
async def delete_log_entry(entry_id: str):
    if not state.remove_log_entry(entry_id):
        raise HTTPException(404, "Log entry not found")
    return {"ok": True}


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


# ── clipboard copy (log entries, receive toasts) ───────────────────────────────
# Browsers can only put text or a handful of image MIME types onto the OS
# clipboard from a web page — there's no API for placing an arbitrary local
# file there as a real, pasteable file. So: text files copy their content,
# recognized image types copy actual image data, everything else falls back
# to copying the file's path (the closest useful equivalent the platform allows).
_CLIP_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
_CLIP_TEXT_MAX_BYTES = 2_000_000


@app.get("/api/file/kind")
async def file_kind(path: str):
    p = Path(path)
    if not p.exists() or not p.is_file():
        raise HTTPException(404, "File not found")

    mime, _ = mimetypes.guess_type(p.name)
    if mime in _CLIP_IMAGE_TYPES:
        return {"kind": "image", "mime": mime}

    try:
        if p.stat().st_size <= _CLIP_TEXT_MAX_BYTES:
            raw = p.read_bytes()
            if b"\x00" not in raw:
                return {"kind": "text", "content": raw.decode("utf-8")}
    except (UnicodeDecodeError, OSError):
        pass

    return {"kind": "other", "path": str(p)}


@app.get("/api/file/raw")
async def file_raw(path: str):
    p = Path(path)
    if not p.exists() or not p.is_file():
        raise HTTPException(404, "File not found")
    return FileResponse(p)


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

    _touch(partner)
    return partner


def _touch(partner: dict, ping_frequency_sec: Optional[int] = None):
    """Record a successful contact with `partner` and push the resulting status live."""
    status = state.mark_seen(partner, ping_frequency_sec)
    asyncio.create_task(state.broadcast("partner_active", {"id": partner["id"], "status": status}))


@app.get("/api/peer/hello")
async def peer_hello(
    request: Request,
    name: Optional[str] = None,
    port: Optional[int] = None,
    freq: Optional[int] = None,
):
    caller_id = request.headers.get("X-Device-ID")
    if caller_id:
        caller = state.get_partner_by_device_id(caller_id)

        # Auto-register: an unknown device pinging us as a heartbeating client
        # becomes a partner with no manual Add step — client drives its own lifecycle.
        if not caller and state.config.get("role") == "server" and name and port and freq:
            client_ip = request.client.host if request.client else None
            if client_ip:
                caller = state.add_partner(name, client_ip, port, caller_id, reachable=False, mode="client")
                await state.broadcast("partners_update", state.partners)

        if caller:
            if name and caller.get("name") != name:
                caller["name"] = name
            if port and caller.get("port") != port:
                caller["port"] = port
            _touch(caller, freq)

    return {"device_id": state.device_id, "name": state.config["device_name"]}


@app.post("/api/peer/forget")
async def peer_forget(request: Request):
    """A partner telling us it's unpairing — remove our record of them too,
    so a client-initiated delete doesn't leave a stale entry on the server."""
    caller_id = request.headers.get("X-Device-ID")
    if caller_id:
        caller = state.get_partner_by_device_id(caller_id)
        if caller:
            state.remove_partner(caller["id"])
            await state.broadcast("partners_update", state.partners)
    return {"ok": True}


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


async def _recheck_partner_soon(partner_id: str):
    """A long-poll hold just ended. A healthy client reconnects almost
    instantly, so give it LONGPOLL_GRACE_SEC to do that before checking again
    — if it hasn't, partner_status() will now read as down, and we push that
    out instead of waiting for the next explicit poll or manual refresh."""
    await asyncio.sleep(LONGPOLL_GRACE_SEC + 1)
    partner = state.get_partner(partner_id)
    if not partner:
        return
    status = state.partner_status(partner)
    await state.broadcast("partner_active", {"id": partner_id, "status": status})


@app.get("/api/peer/wait")
async def peer_wait(request: Request, timeout: int = 600):
    """Long-poll: a client holds this open on us for instant work notification
    instead of waiting for its next short heartbeat. Whether this hold is
    currently live is itself the server-role status signal (see
    state.longpoll_connected) — a healthy client immediately reopens a new
    hold the instant one ends, so a gap longer than LONGPOLL_GRACE_SEC means
    the pipe is actually down, independent of whether the connection failure
    was ever cleanly signaled. request.is_disconnected() is still checked
    each slice as a faster path for the cases that *do* get a clean signal
    (client closed, process killed)."""
    partner = _require_partner(request)
    timeout = max(10, min(timeout, 3600))
    state.mark_longpoll_start(partner["id"])

    try:
        if state.get_transfers_for_device(partner["device_id"]):
            return {"work": True}

        ev = state.get_signal_event(partner["id"])
        ev.clear()
        slice_secs = 5
        elapsed = 0.0
        while elapsed < timeout:
            if await request.is_disconnected():
                if partner.get("reachable") is not False:
                    partner["reachable"] = False
                    state._save_partners()
                    status = state.partner_status(partner)
                    asyncio.create_task(state.broadcast("partner_active", {"id": partner["id"], "status": status}))
                return {"work": False}
            this_slice = min(slice_secs, timeout - elapsed)
            try:
                await asyncio.wait_for(ev.wait(), timeout=this_slice)
                return {"work": True}
            except asyncio.TimeoutError:
                elapsed += this_slice
        return {"work": False}
    finally:
        state.mark_longpoll_end(partner["id"])
        asyncio.create_task(_recheck_partner_soon(partner["id"]))


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
    if file_info and file_info.get("path") and file_info.get("origin") != "browsed":
        Path(file_info["path"]).unlink(missing_ok=True)  # staging copy no longer needed
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
                if file_info.get("origin") != "browsed":
                    file_path.unlink(missing_ok=True)  # staging copy no longer needed

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
    if state.is_receiving_from(partner["id"]):
        return
    state.set_receiving(partner["id"], True)
    announced = False

    try:
        base_url = f"http://{partner['ip']}:{partner['port']}"
        async with httpx.AsyncClient(timeout=3) as client:
            resp = await client.get(
                f"{base_url}/api/peer/check",
                headers={"X-Device-ID": state.device_id},
            )
            if resp.status_code != 200:
                return
            transfers = resp.json().get("transfers", [])
            if not transfers:
                return

        # Only announce "receiving" now that we know there's actually something to pull
        announced = True
        await state.broadcast("status", {"receiving": True, "partner": partner["name"]})

        receive_dir = Path(state.config["receive_dir"])
        receive_dir.mkdir(parents=True, exist_ok=True)

        for transfer in transfers:
            await _receive_transfer(partner, transfer, base_url, receive_dir)

    except Exception as e:
        if announced:
            await state.broadcast("status", {"receiving": False, "error": str(e)})
    finally:
        state.set_receiving(partner["id"], False)
        if announced:
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
# Frequently-edited files served from disk (index.html/app.js) — a plain browser
# reload must always fetch the current version, not a stale cached one from
# before the last update (a plain F5 silently kept serving old JS more than
# once during development here, masking already-fixed backend behavior).


@app.middleware("http")
async def _no_cache_static(request: Request, call_next):
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
    return response


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
    # Without this, a held /api/peer/wait long-poll (up to long_poll_timeout_sec,
    # default 10min) blocks graceful shutdown indefinitely — Ctrl+C would just hang.
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False, timeout_graceful_shutdown=5)
