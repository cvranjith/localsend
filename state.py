import asyncio
import json
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

DATA_DIR = Path("data")
STAGING_DIR = DATA_DIR / "staging"
CONFIG_FILE = DATA_DIR / "config.json"
PARTNERS_FILE = DATA_DIR / "partners.json"
OUTBOX_FILE = DATA_DIR / "outbox.json"
LOG_FILE = DATA_DIR / "log.json"

LOG_RETENTION_DAYS = 7
DEFAULT_PING_FREQUENCY_SEC = 60
# Short on purpose: a healthy long-poll cycles (ends, reconnects) at least this
# often, and that cycling — not last_ping_at freshness — is what server-role
# status now trusts. A long timeout would mean a perfectly healthy multi-minute
# hold looks "stale" long before it naturally ends, and a genuinely dead
# connection with no clean close (silent WiFi drop) wouldn't be caught until
# this many seconds pass regardless — so shorter directly means faster detection.
DEFAULT_LONG_POLL_TIMEOUT_SEC = 20
LONGPOLL_GRACE_SEC = 8  # buffer for the normal end-of-cycle -> reconnect gap


class AppState:
    def __init__(self):
        DATA_DIR.mkdir(exist_ok=True)
        STAGING_DIR.mkdir(exist_ok=True)
        self._load_config()
        self._load_partners()
        self._load_outbox()
        self._load_log()
        self.file_semaphore = asyncio.Semaphore(6)
        self._receiving_partners: set[str] = set()
        self.sse_queues: list[asyncio.Queue] = []
        self.active_tasks: dict[str, asyncio.Task] = {}      # transfer_id → task
        self.longpoll_tasks: dict[str, asyncio.Task] = {}    # partner_id → our long-poll loop against them
        self._partner_signals: dict[str, asyncio.Event] = {}  # partner_id → "new work queued" wake-up
        self._longpoll_live: dict[str, bool] = {}    # partner_id → a /api/peer/wait request from them is currently being held open
        self._longpoll_last_seen: dict[str, float] = {}  # partner_id → epoch time the last hold started or ended

    # ── config ────────────────────────────────────────────────────────────────

    def _load_config(self):
        self.config: dict = json.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {}
        self.config.setdefault("device_id", str(uuid.uuid4()))
        self.config.setdefault("device_name", f"device-{self.config['device_id'][:6]}")
        self.config.setdefault("port", 8765)
        self.config.setdefault("receive_dir", str(Path.home() / "Downloads" / "localsend-recv"))
        self.config.setdefault("role", "server")  # "server" = never pings out; "client" = pings its partners
        self.config.setdefault("ping_frequency_sec", DEFAULT_PING_FREQUENCY_SEC)
        # The old default (600s) meant a healthy long-poll hold looked stale
        # long before it ended — a config still sitting at that untouched old
        # default gets tightened too, not just fresh installs.
        if self.config.get("long_poll_timeout_sec") == 600:
            self.config["long_poll_timeout_sec"] = DEFAULT_LONG_POLL_TIMEOUT_SEC
        self.config.setdefault("long_poll_timeout_sec", DEFAULT_LONG_POLL_TIMEOUT_SEC)
        self.device_id: str = self.config["device_id"]
        self._save_config()

    def _save_config(self):
        CONFIG_FILE.write_text(json.dumps(self.config, indent=2))

    def update_config(self, updates: dict) -> dict:
        for k, v in updates.items():
            if k != "device_id":
                self.config[k] = v
        self._save_config()
        return self.config

    # ── partners ──────────────────────────────────────────────────────────────

    def _load_partners(self):
        self.partners: list[dict] = (
            json.loads(PARTNERS_FILE.read_text()) if PARTNERS_FILE.exists() else []
        )
        # Backfill "mode" for partner records saved before it existed. Only a
        # client-role pinger ever supplies ping_frequency_sec (see peer_hello),
        # so its presence reliably tells apart the two ways a partner record
        # can come to exist, without depending on the (volatile) reachable flag.
        # Backfill "route" too — folds the old separate global auto_cloud_fallback
        # setting and per-partner force_cloud flag into one three-way choice.
        legacy_auto = self.config.get("auto_cloud_fallback")
        changed = False
        for p in self.partners:
            if "mode" not in p:
                p["mode"] = "client" if p.get("ping_frequency_sec") else "server"
                changed = True
            if "route" not in p:
                legacy_force = p.pop("force_cloud", None)
                p["route"] = "cloud" if legacy_force else ("auto" if legacy_auto else "local")
                changed = True
            elif "force_cloud" in p:
                p.pop("force_cloud", None)
                changed = True
            if "allow_scan" not in p:
                # Subnet scanning used to run unconditionally for any unreachable
                # partner; it's now opt-in only (a wildcard IP), so existing
                # records default to off rather than silently keep scanning.
                p["allow_scan"] = False
                changed = True
        if changed:
            self._save_partners()

    def _save_partners(self):
        PARTNERS_FILE.write_text(json.dumps(self.partners, indent=2))

    def add_partner(
        self, name: str, ip: str, port: int, remote_device_id: str,
        reachable: bool = False, mode: str = "server", allow_scan: bool = False,
    ) -> dict:
        p = {
            "id": str(uuid.uuid4()),
            "name": name,
            "ip": ip,
            "port": port,
            "device_id": remote_device_id,
            "reachable": reachable,  # True = we can push; False = pull-only
            "mode": mode,  # "server" (we dialed into them) or "client" (they dialed into us) — fixed at creation, not touched by later reachability checks
            "last_ping_at": None,          # epoch seconds, persisted so status survives a restart
            "ping_frequency_sec": None,    # declared by the partner itself when it pings us as a client
            "route": "local",  # "local" (always direct) | "auto" (direct, cloud fallback if down) | "cloud" (always cloud) — user-editable, only one active at a time
            "allow_scan": allow_scan,  # explicit opt-in (wildcard IP, e.g. 192.168.1.*) for the last-resort full-subnet probe when unreachable
        }
        self.partners.append(p)
        self._save_partners()
        return p

    def update_partner(self, partner_id: str, updates: dict) -> dict | None:
        partner = self.get_partner(partner_id)
        if not partner:
            return None
        for k, v in updates.items():
            partner[k] = v
        self._save_partners()
        return partner

    def mark_seen(self, partner: dict, ping_frequency_sec: int | None = None) -> str:
        """Record a successful contact from/to `partner` and return its freshly computed status."""
        partner["last_ping_at"] = time.time()
        partner["reachable"] = True  # successful contact just now is direct proof of reachability
        if ping_frequency_sec:
            partner["ping_frequency_sec"] = ping_frequency_sec
        self._save_partners()
        return self.partner_status(partner)

    # ── long-poll liveness (server-role's real-time "is the pipe up" signal) ───

    def mark_longpoll_start(self, partner_id: str):
        self._longpoll_live[partner_id] = True
        self._longpoll_last_seen[partner_id] = time.time()

    def mark_longpoll_end(self, partner_id: str):
        self._longpoll_live[partner_id] = False
        self._longpoll_last_seen[partner_id] = time.time()

    def longpoll_connected(self, partner_id: str) -> bool:
        """True while a /api/peer/wait hold from this partner is currently open,
        or ended recently enough that it's within the normal reconnect gap. A
        healthy client-role peer holds one of these continuously — it ends and
        immediately re-opens a new one — so this is a direct, real-time proxy
        for "the pipe is up right now", unlike last_ping_at (which only ticks
        at the *start* of a hold that can run for a while before naturally
        cycling, so a perfectly healthy hold would otherwise look stale)."""
        if self._longpoll_live.get(partner_id):
            return True
        last = self._longpoll_last_seen.get(partner_id)
        return bool(last and (time.time() - last) <= LONGPOLL_GRACE_SEC)

    def partner_status(self, partner: dict) -> str:
        """green/red/grey, computed from persisted state only — the UI just paints this,
        it never re-derives freshness from its own timers."""
        last_ping_at = partner.get("last_ping_at")
        if not last_ping_at:
            return "grey"  # never contacted
        if self.config.get("role") == "client":
            # We are the one pinging; reachable reflects the outcome of our most recent attempt.
            return "green" if partner.get("reachable") else "red"
        # We are the server: partner is a client that must ping *us*. An explicit probe
        # (manual refresh) that just failed is definitive — don't wait out anything else.
        if partner.get("reachable") is False:
            return "red"
        if self.longpoll_connected(partner["id"]):
            return "green"
        # Bootstrap grace: last_ping_at was just set (e.g. the initial hello
        # that registered them) but their first long-poll hasn't landed yet —
        # give it the same short window before calling it down.
        if (time.time() - last_ping_at) <= LONGPOLL_GRACE_SEC:
            return "green"
        return "red"

    def remove_partner(self, partner_id: str):
        self.partners = [p for p in self.partners if p["id"] != partner_id]
        self._save_partners()

    def get_partner(self, partner_id: str) -> dict | None:
        return next((p for p in self.partners if p["id"] == partner_id), None)

    def get_partner_by_device_id(self, device_id: str) -> dict | None:
        return next((p for p in self.partners if p.get("device_id") == device_id), None)

    def get_partner_by_ip(self, ip: str) -> dict | None:
        return next((p for p in self.partners if p.get("ip") == ip), None)

    def get_partner_by_ip_port(self, ip: str, port: int) -> dict | None:
        return next((p for p in self.partners if p.get("ip") == ip and p.get("port") == port), None)

    # ── outbox ────────────────────────────────────────────────────────────────

    def _load_outbox(self):
        self.outbox: list[dict] = (
            json.loads(OUTBOX_FILE.read_text()) if OUTBOX_FILE.exists() else []
        )

    def _save_outbox(self):
        OUTBOX_FILE.write_text(json.dumps(self.outbox, indent=2))

    def add_transfer(self, partner_id: str, files: list[dict]) -> dict:
        t = {
            "transfer_id": str(uuid.uuid4()),
            "partner_id": partner_id,
            "created_at": datetime.utcnow().isoformat(),
            "files": files,  # [{path, name, size}]
        }
        self.outbox.append(t)
        self._save_outbox()
        self.signal_partner(partner_id)  # wake any long-poll holding open for them
        return t

    # ── long-poll wake signal ────────────────────────────────────────────────

    def get_signal_event(self, partner_id: str) -> asyncio.Event:
        ev = self._partner_signals.get(partner_id)
        if ev is None:
            ev = self._partner_signals[partner_id] = asyncio.Event()
        return ev

    def signal_partner(self, partner_id: str):
        self.get_signal_event(partner_id).set()

    def get_transfer(self, transfer_id: str) -> dict | None:
        return next((t for t in self.outbox if t["transfer_id"] == transfer_id), None)

    def get_transfers_for_device(self, device_id: str) -> list[dict]:
        partner = self.get_partner_by_device_id(device_id)
        if not partner:
            return []
        return [t for t in self.outbox if t["partner_id"] == partner["id"]]

    def ack_file(self, transfer_id: str, filename: str) -> dict | None:
        t = self.get_transfer(transfer_id)
        if not t:
            return None
        matched = next((f for f in t["files"] if f["name"] == filename), None)
        t["files"] = [f for f in t["files"] if f["name"] != filename]
        if not t["files"]:
            self.outbox = [x for x in self.outbox if x["transfer_id"] != transfer_id]
        self._save_outbox()
        return matched

    def remove_transfer(self, transfer_id: str):
        self.outbox = [t for t in self.outbox if t["transfer_id"] != transfer_id]
        self._save_outbox()

    # ── log ───────────────────────────────────────────────────────────────────

    def _load_log(self):
        if LOG_FILE.exists():
            entries = json.loads(LOG_FILE.read_text())
            cutoff = (datetime.utcnow() - timedelta(days=LOG_RETENTION_DAYS)).isoformat()
            self.log: list[dict] = [e for e in entries if e.get("ts", "") >= cutoff]
        else:
            self.log = []

    def _save_log(self):
        LOG_FILE.write_text(json.dumps(self.log[-500:], indent=2))

    def remove_log_entry(self, entry_id: str) -> bool:
        before = len(self.log)
        self.log = [e for e in self.log if e["id"] != entry_id]
        if len(self.log) < before:
            self._save_log()
            return True
        return False

    def add_log(
        self, direction: str, partner_name: str, filename: str, status: str, size: int = 0, path: str = None
    ) -> dict:
        entry = {
            "id": str(uuid.uuid4()),
            "ts": datetime.utcnow().isoformat(),
            "direction": direction,  # "sent" | "received"
            "partner_name": partner_name,
            "filename": filename,
            "status": status,  # "ok" | "error"
            "size": size,
            "path": path,
        }
        self.log.append(entry)
        self._save_log()
        return entry

    # ── busy flag ─────────────────────────────────────────────────────────────

    def is_receiving_from(self, partner_id: str) -> bool:
        return partner_id in self._receiving_partners

    def set_receiving(self, partner_id: str, val: bool):
        if val:
            self._receiving_partners.add(partner_id)
        else:
            self._receiving_partners.discard(partner_id)

    # ── SSE ───────────────────────────────────────────────────────────────────

    async def broadcast(self, event_type: str, data: Any):
        payload = json.dumps({"type": event_type, "data": data})
        dead = []
        for q in self.sse_queues:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            try:
                self.sse_queues.remove(q)
            except ValueError:
                pass
