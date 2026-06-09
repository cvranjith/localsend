import asyncio
import json
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


class AppState:
    def __init__(self):
        DATA_DIR.mkdir(exist_ok=True)
        STAGING_DIR.mkdir(exist_ok=True)
        self._load_config()
        self._load_partners()
        self._load_outbox()
        self._load_log()
        self.file_semaphore = asyncio.Semaphore(6)
        self._receiving = False
        self.sse_queues: list[asyncio.Queue] = []
        self.active_tasks: dict[str, asyncio.Task] = {}  # transfer_id → task

    # ── config ────────────────────────────────────────────────────────────────

    def _load_config(self):
        self.config: dict = json.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {}
        self.config.setdefault("device_id", str(uuid.uuid4()))
        self.config.setdefault("device_name", f"device-{self.config['device_id'][:6]}")
        self.config.setdefault("port", 8765)
        self.config.setdefault("receive_dir", str(Path.home() / "Downloads" / "localsend-recv"))
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

    def _save_partners(self):
        PARTNERS_FILE.write_text(json.dumps(self.partners, indent=2))

    def add_partner(self, name: str, ip: str, port: int, remote_device_id: str, reachable: bool = False) -> dict:
        p = {
            "id": str(uuid.uuid4()),
            "name": name,
            "ip": ip,
            "port": port,
            "device_id": remote_device_id,
            "reachable": reachable,  # True = we can push; False = pull-only
        }
        self.partners.append(p)
        self._save_partners()
        return p

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
        return t

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

    def add_log(
        self, direction: str, partner_name: str, filename: str, status: str, size: int = 0
    ) -> dict:
        entry = {
            "id": str(uuid.uuid4()),
            "ts": datetime.utcnow().isoformat(),
            "direction": direction,  # "sent" | "received"
            "partner_name": partner_name,
            "filename": filename,
            "status": status,  # "ok" | "error"
            "size": size,
        }
        self.log.append(entry)
        self._save_log()
        return entry

    # ── busy flag ─────────────────────────────────────────────────────────────

    @property
    def is_receiving(self) -> bool:
        return self._receiving

    def set_receiving(self, val: bool):
        self._receiving = val

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
