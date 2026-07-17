#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener, urlopen
from zipfile import ZipFile


INSTALLER_LABEL = "application"
INSTALLER_VERSION = 1
AUTO_UPDATE_CHECK_MINUTES = 60
PRESERVE_TOP_LEVEL = {"venv", "_backups"}
DEPS_PROFILE_CORE = "core"
DEPS_PROFILE_ADVANCED = "advanced-keybert"


class Reporter:
    def __init__(self) -> None:
        self._tty = bool(sys.stdout.isatty())
        self._use_color = self._tty and not os.environ.get("NO_COLOR")
        encoding = str(getattr(sys.stdout, "encoding", "") or "").lower()
        force_ascii = str(os.environ.get("INSTALLER_ASCII") or "").strip().lower() in {"1", "true", "yes", "y", "on"}
        self._use_unicode = (not force_ascii) and ("utf" in encoding)
        self._truecolor = os.environ.get("COLORTERM", "").lower() in {"truecolor", "24bit"}
        self._styles = {
            "reset": "\033[0m" if self._use_color else "",
            "info": "",
            "warn": "",
            "error": "",
            "dim": "\033[2m" if self._use_color else "",
            "good": "",
        }
        if self._use_color:
            if self._truecolor:
                self._styles["info"] = "\033[38;2;74;122;168m"
                self._styles["warn"] = "\033[38;2;168;128;74m"
                self._styles["error"] = "\033[38;2;168;82;96m"
                self._styles["good"] = "\033[38;2;84;150;128m"
            else:
                self._styles["info"] = "\033[38;5;67m"
                self._styles["warn"] = "\033[38;5;137m"
                self._styles["error"] = "\033[38;5;131m"
                self._styles["good"] = "\033[38;5;72m"

    def _icon(self, kind: str) -> str:
        if self._use_unicode:
            return {
                "info": "●",
                "warn": "▲",
                "error": "✖",
                "good": "✓",
            }.get(kind, "•")
        return {
            "info": "INFO",
            "warn": "WARN",
            "error": "ERROR",
            "good": "OK",
        }.get(kind, "INFO")

    def _emit(self, level: str, message: str, *, stream) -> None:
        icon = self._icon(level)
        color = self._styles.get(level, "")
        reset = self._styles["reset"]
        if self._use_color:
            print(f"{color}{icon}{reset} {message}", file=stream, flush=True)
        else:
            print(f"[{icon}] {message}", file=stream, flush=True)

    def info(self, message: str) -> None:
        self._emit("info", message, stream=sys.stdout)

    def warn(self, message: str) -> None:
        self._emit("warn", message, stream=sys.stderr)

    def error(self, message: str) -> None:
        self._emit("error", message, stream=sys.stderr)


class SilentReporter(Reporter):
    def info(self, message: str) -> None:
        return

    def warn(self, message: str) -> None:
        return

    def error(self, message: str) -> None:
        return


class MemoryReporter(Reporter):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[dict[str, str]] = []

    def info(self, message: str) -> None:
        self.messages.append({"level": "info", "message": message})

    def warn(self, message: str) -> None:
        self.messages.append({"level": "warn", "message": message})

    def error(self, message: str) -> None:
        self.messages.append({"level": "error", "message": message})


class UpdateError(RuntimeError):
    pass


@dataclass
class ManifestEntry:
    version: str
    app_url: str
    app_sha256: str
    installer_min: int
    build_time_utc: str
    build_label: str


@dataclass
class UpdateContext:
    codex_cwd: Path
    manifest_url: str
    source_app_dir: Path | None
    requested_version: str
    python_bin: str
    tmp_root: Path
    interactive: bool
    assume_yes: bool
    skip_start: bool
    start_server: bool
    with_advanced_extraction: bool
    reporter: Reporter

    @property
    def config_file(self) -> Path:
        return self.codex_cwd / "config" / "config.properties"

    @property
    def state_file(self) -> Path:
        return self.codex_cwd / "config" / "install-state.json"

    @property
    def target_app_dir(self) -> Path:
        return self.codex_cwd / "vault-graph"

    @property
    def installer_base_url(self) -> str:
        manifest = str(self.manifest_url or "").strip()
        return manifest.rsplit("/", 1)[0] if "/" in manifest else manifest


def platform_summary() -> str:
    system = os.uname().sysname if hasattr(os, "uname") else sys.platform
    machine = os.uname().machine if hasattr(os, "uname") else ""
    system_text = str(system or "unknown").strip() or "unknown"
    machine_text = str(machine or "unknown").strip() or "unknown"
    return f"{system_text}-{machine_text}"


def is_windows() -> bool:
    return os.name == "nt"


def ensure_python_bin(explicit: str = "") -> str:
    if explicit:
        return explicit
    running_python = str(getattr(sys, "executable", "") or "").strip()
    if running_python and Path(running_python).is_file():
        return running_python
    for name in ("python3", "python"):
        found = shutil.which(name)
        if found:
            return found
    raise UpdateError("Python is required for the updater. Install Python 3 and rerun.")


def require_command(name: str) -> None:
    if not shutil.which(name):
        raise UpdateError(f"Required command not found: {name}")


def run_ok(cmd: list[str]) -> bool:
    try:
        actual_cmd = list(cmd)
        if is_windows() and actual_cmd:
            resolved = shutil.which(actual_cmd[0])
            if resolved:
                if Path(resolved).suffix.lower() in {".cmd", ".bat"}:
                    actual_cmd = ["cmd", "/d", "/c", subprocess.list2cmdline([resolved, *actual_cmd[1:]])]
                else:
                    actual_cmd[0] = resolved
        proc = subprocess.run(actual_cmd, capture_output=True, text=True, check=False)
    except Exception:
        return False
    return proc.returncode == 0


def check_prereqs(ctx: UpdateContext) -> None:
    ctx.reporter.info("Checking prerequisites")
    if not run_ok([ctx.python_bin, "-m", "venv", "--help"]):
        raise UpdateError("Python venv support is required on this machine.")
    if shutil.which("git"):
        if not run_ok(["git", "--version"]):
            ctx.reporter.warn("git is present but not responding. Some git-backed features may not work.")
    else:
        ctx.reporter.warn("git was not found. Some git-backed features may not work.")
    if shutil.which("codex"):
        if not run_ok(["codex", "--version"]):
            ctx.reporter.warn("Codex CLI is present but not responding. In-app Codex features may not work until it is configured.")
        elif not (Path.home() / ".codex").is_dir():
            ctx.reporter.warn("~/.codex was not found. In-app Codex features may not work until Codex CLI is configured.")
    else:
        ctx.reporter.warn("Codex CLI was not found. In-app Codex features may not work until it is installed.")


def load_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def deps_profile_files(app_dir: Path, profile: str) -> list[Path]:
    files = [app_dir / "requirements.txt"]
    if profile == DEPS_PROFILE_ADVANCED:
        files.append(app_dir / "requirements-keybert.txt")
    return files


def deps_sha_from_files(files: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in files:
        if not path.is_file():
            raise UpdateError(f"Missing dependency file: {path}")
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def _optional_string(value: object) -> str:
    text = str(value or "").strip()
    if not text or text.lower() == "null":
        return ""
    return text


def _parse_iso_datetime(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None


def default_manifest_url_from_env() -> str:
    explicit_manifest = (
        _optional_string(os.environ.get("INSTALLER_MANIFEST_URL"))
        or _optional_string(os.environ.get("ANTIRAG_MANIFEST_URL"))
    )
    if explicit_manifest:
        return explicit_manifest
    base_url = (
        _optional_string(os.environ.get("INSTALLER_BASE_URL"))
        or _optional_string(os.environ.get("ANTIRAG_BASE_URL"))
    )
    if not base_url:
        return ""
    return f"{base_url.rstrip('/')}/manifest.json"


def resolve_artifact_url(manifest_url: str, app_block: dict[str, object]) -> str:
    explicit_url = _optional_string(app_block.get("url"))
    if explicit_url:
        return explicit_url
    relpath = _optional_string(app_block.get("path"))
    if not relpath:
        return ""
    base = _optional_string(manifest_url)
    if not base:
        return ""
    return urljoin(base, relpath)


def _read_update_check_block(state: dict) -> dict[str, object]:
    block = state.get("update_check")
    return dict(block) if isinstance(block, dict) else {}


def read_update_check_state(state_file: Path) -> dict[str, object]:
    state = load_json(state_file)
    block = _read_update_check_block(state)
    return {
        "last_checked_at": _optional_string(block.get("last_checked_at")),
        "available": bool(block.get("available")),
        "current_version": _optional_string(block.get("current_version")) or None,
        "current_build_time_utc": _optional_string(block.get("current_build_time_utc")) or None,
        "current_build_label": _optional_string(block.get("current_build_label")) or None,
        "target_version": _optional_string(block.get("target_version")) or None,
        "target_build_time_utc": _optional_string(block.get("target_build_time_utc")) or None,
        "target_build_label": _optional_string(block.get("target_build_label")) or None,
        "manifest_url": _optional_string(block.get("manifest_url")) or None,
    }


def write_update_check_state(
    state_file: Path,
    *,
    manifest_url: str,
    available: bool,
    current_version: str | None,
    current_build_time_utc: str | None,
    current_build_label: str | None,
    target_version: str | None,
    target_build_time_utc: str | None,
    target_build_label: str | None,
    checked_at: str | None = None,
) -> dict[str, object]:
    state = load_json(state_file)
    state["update_check"] = {
        "last_checked_at": checked_at or datetime.now(timezone.utc).isoformat(),
        "available": bool(available),
        "current_version": _optional_string(current_version) or None,
        "current_build_time_utc": _optional_string(current_build_time_utc) or None,
        "current_build_label": _optional_string(current_build_label) or None,
        "target_version": _optional_string(target_version) or None,
        "target_build_time_utc": _optional_string(target_build_time_utc) or None,
        "target_build_label": _optional_string(target_build_label) or None,
        "manifest_url": _optional_string(manifest_url) or None,
    }
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return read_update_check_state(state_file)


def write_install_state(
    ctx: UpdateContext,
    *,
    version: str,
    build_time_utc: str,
    build_label: str,
    app_sha: str,
    deps_sha: str,
    app_url: str,
    python_version: str,
) -> None:
    ctx.state_file.parent.mkdir(parents=True, exist_ok=True)
    existing = load_json(ctx.state_file)
    existing_update_check = _read_update_check_block(existing)
    payload = {
        "app": "application",
        "version": version,
        "build_time_utc": build_time_utc,
        "build_label": build_label,
        "deps_profile": str(getattr(ctx, "_resolved_deps_profile", DEPS_PROFILE_CORE) or DEPS_PROFILE_CORE),
        "platform": platform_summary(),
        "app_sha256": app_sha,
        "deps_sha256": deps_sha,
        "app_url": app_url,
        "python_version": python_version,
        "deps_source": "pip",
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "update_check": {
            "last_checked_at": _optional_string(existing_update_check.get("last_checked_at")) or None,
            "available": False,
            "current_version": version,
            "current_build_time_utc": build_time_utc,
            "current_build_label": build_label,
            "target_version": version,
            "target_build_time_utc": build_time_utc,
            "target_build_label": build_label,
            "manifest_url": _optional_string(existing_update_check.get("manifest_url")) or None,
        },
    }
    ctx.state_file.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_manifest_entry(manifest: dict, requested_version: str, *, manifest_url: str = "") -> ManifestEntry:
    version = requested_version if requested_version and requested_version != "latest" else str(manifest.get("latest") or "")
    versions = manifest.get("versions") or {}
    if version not in versions:
        available = ", ".join(sorted(str(x) for x in versions.keys()))
        raise UpdateError(f"Requested version was not found in manifest: {version} (available: {available})")
    version_row = versions.get(version) or {}
    app = version_row.get("app_zip") or {}
    app_url = resolve_artifact_url(manifest_url, dict(app) if isinstance(app, dict) else {})
    app_sha = str(app.get("sha256") or "").strip()
    if not app_url or not app_sha:
        raise UpdateError("Manifest is missing app_zip.path/url or app_zip.sha256")
    return ManifestEntry(
        version=version,
        app_url=app_url,
        app_sha256=app_sha,
        installer_min=int(manifest.get("installer_version") or 1),
        build_time_utc=str(version_row.get("build_time_utc") or "").strip(),
        build_label=str(version_row.get("build_label") or "").strip(),
    )


def app_tree_sha256(app_dir: Path) -> str:
    digest = hashlib.sha256()
    skip_top_level = set(PRESERVE_TOP_LEVEL) | {".DS_Store"}
    skip_names = {"__pycache__", ".pytest_cache"}
    for path in sorted(app_dir.rglob("*")):
        rel = path.relative_to(app_dir)
        parts = rel.parts
        if not parts:
            continue
        if parts[0] in skip_top_level:
            continue
        if any(part in skip_names for part in parts):
            continue
        if path.is_dir():
            continue
        if path.suffix in {".pyc", ".pyo"}:
            continue
        digest.update(rel.as_posix().encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def _read_local_version(source_app_dir: Path) -> str:
    version_file = source_app_dir / "VERSION.TXT"
    try:
        raw = version_file.read_text(encoding="utf-8", errors="ignore").strip()
    except Exception:
        raw = ""
    if not raw:
        return "local"
    parts = raw.split()
    return parts[-1].strip() if parts else "local"


def local_source_manifest_entry(source_app_dir: Path) -> ManifestEntry:
    if not source_app_dir.is_dir():
        raise UpdateError(f"Local source app directory was not found: {source_app_dir}")
    if not (source_app_dir / "run_server.py").is_file():
        raise UpdateError(f"Local source app directory is missing run_server.py: {source_app_dir}")
    if not (source_app_dir / "server_supervisor.py").is_file():
        raise UpdateError(f"Local source app directory is missing server_supervisor.py: {source_app_dir}")
    timestamp = datetime.fromtimestamp(source_app_dir.stat().st_mtime, timezone.utc)
    return ManifestEntry(
        version=_read_local_version(source_app_dir),
        app_url=str(source_app_dir),
        app_sha256=app_tree_sha256(source_app_dir),
        installer_min=INSTALLER_VERSION,
        build_time_utc=timestamp.isoformat(),
        build_label="Local source",
    )


def _human_bytes(num: float) -> str:
    units = ["B", "KB", "MB", "GB"]
    value = float(num)
    for unit in units:
        if value < 1024.0 or unit == units[-1]:
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024.0
    return f"{num:.1f}B"


def _truncate_text(text: str, limit: int) -> str:
    s = str(text or "").strip()
    if len(s) <= limit:
        return s
    return s[: max(1, limit - 1)] + "…"


def _render_progress_line(*, label: str, downloaded: int, total: int, start: float, tty: bool, use_unicode: bool, use_color: bool) -> str:
    elapsed = max(time.time() - start, 0.001)
    rate = downloaded / elapsed
    remaining = max(total - downloaded, 0)
    eta = int(remaining / rate) if rate > 1 else 0
    pct = min(100.0, (downloaded * 100.0) / total) if total > 0 else 0.0
    width = 28 if tty else 18
    filled = int(round((pct / 100.0) * width))
    filled = max(0, min(width, filled))
    empty = width - filled
    if use_unicode:
        full_char = "█"
        empty_char = "░"
    else:
        full_char = "#"
        empty_char = "-"
    bar = f"{full_char * filled}{empty_char * empty}"
    if use_color:
        bar = f"\033[36m{bar}\033[0m"
    label_text = _truncate_text(label, 22 if tty else 16)
    return f"{label_text:<22} [{bar}] {pct:5.1f}%  {_human_bytes(downloaded)}/{_human_bytes(total)}  {eta:>3}s"


def download_to_path(url: str, dest: Path, *, reporter: Reporter, expected_sha: str = "") -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and expected_sha:
        if sha256_file(dest) == expected_sha:
            reporter.info(f"Using cached file: {dest.name}")
            return
        dest.unlink()

    reporter.info(f"Downloading {dest.name}")
    request = urlopen(url, timeout=60)
    total = 0
    try:
        total = int(request.headers.get("Content-Length") or "0")
    except Exception:
        total = 0
    start = time.time()
    downloaded = 0
    last_draw = 0.0
    tty = bool(sys.stdout.isatty())
    use_color = bool(getattr(reporter, "_use_color", False))
    use_unicode = bool(getattr(reporter, "_use_unicode", False))
    label = dest.name
    with dest.open("wb") as fh:
        while True:
            chunk = request.read(1024 * 256)
            if not chunk:
                break
            fh.write(chunk)
            downloaded += len(chunk)
            now = time.time()
            if tty and total and (now - last_draw >= 0.12 or downloaded >= total):
                line = _render_progress_line(
                    label=label,
                    downloaded=downloaded,
                    total=total,
                    start=start,
                    tty=tty,
                    use_unicode=use_unicode,
                    use_color=use_color,
                )
                sys.stdout.write(f"\r\033[2K{line}")
                sys.stdout.flush()
                last_draw = now
    if tty and total:
        sys.stdout.write("\r\033[2K")
        complete_line = _render_progress_line(
            label=label,
            downloaded=downloaded,
            total=total,
            start=start,
            tty=tty,
            use_unicode=use_unicode,
            use_color=use_color,
        )
        done_icon = "done" if not use_unicode else "✓"
        if use_color:
            done_icon = f"\033[32m{done_icon}\033[0m"
        sys.stdout.write(f"{complete_line}  {done_icon}\n")
        sys.stdout.flush()
    request.close()
    if expected_sha:
        actual = sha256_file(dest)
        if actual != expected_sha:
            raise UpdateError(f"Checksum mismatch for {dest.name} (expected {expected_sha}, got {actual})")


def fetch_manifest(manifest_url: str, dest: Path, reporter: Reporter) -> dict:
    download_to_path(manifest_url, dest, reporter=reporter)
    try:
        data = json.loads(dest.read_text(encoding="utf-8"))
    except Exception as exc:
        raise UpdateError(f"Could not parse manifest: {exc}") from exc
    if not isinstance(data, dict):
        raise UpdateError("Manifest was not a JSON object.")
    return data


def extract_zip(zip_path: Path, dest_dir: Path) -> None:
    if dest_dir.exists():
        shutil.rmtree(dest_dir, ignore_errors=True)
    dest_dir.mkdir(parents=True, exist_ok=True)
    with ZipFile(zip_path, "r") as zf:
        zf.extractall(dest_dir)


def parse_properties(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def write_config_key(config_path: Path, key_name: str, new_value: str) -> None:
    lines = config_path.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    updated = False
    for raw in lines:
        line = raw.strip()
        if line and not line.startswith("#") and "=" in raw:
            key, _value = raw.split("=", 1)
            if key.strip() == key_name:
                out.append(f"{key_name}={new_value}")
                updated = True
                continue
        out.append(raw)
    if not updated:
        if out and out[-1].strip():
            out.append("")
        out.append(f"{key_name}={new_value}")
    config_path.write_text("\n".join(out) + "\n", encoding="utf-8")


def seed_config_if_missing(ctx: UpdateContext, app_source_dir: Path) -> None:
    template_file = app_source_dir / "config" / "default-config.properties"
    if ctx.config_file.is_file():
        return
    if not template_file.is_file():
        raise UpdateError("Could not seed config/config.properties from bundled default config.")
    ctx.config_file.parent.mkdir(parents=True, exist_ok=True)
    ctx.reporter.info("Seeding config/config.properties")
    shutil.copy2(template_file, ctx.config_file)
    write_config_key(ctx.config_file, "codex_cwd", str(ctx.codex_cwd))
    if ctx.installer_base_url:
        write_config_key(ctx.config_file, "installer_base_url", ctx.installer_base_url)


def populate_installer_base_url_if_missing(ctx: UpdateContext) -> None:
    if not ctx.config_file.is_file() or not ctx.installer_base_url:
        return
    props = parse_properties(ctx.config_file)
    current = str(props.get("installer_base_url") or "").strip()
    if current and current.lower() != "null":
        return
    write_config_key(ctx.config_file, "installer_base_url", ctx.installer_base_url)
    ctx.reporter.info("Populated installer_base_url in config/config.properties")


def _property_key(raw: str) -> str | None:
    stripped = raw.strip()
    if not stripped or stripped.startswith("#") or stripped.startswith(";") or "=" not in raw:
        return None
    return raw.split("=", 1)[0].strip()


def merge_config_defaults(ctx: UpdateContext, app_source_dir: Path) -> int:
    template_file = app_source_dir / "config" / "default-config.properties"
    if not ctx.config_file.is_file():
        raise UpdateError("Local config/config.properties is missing.")
    if not template_file.is_file():
        raise UpdateError("App payload is missing vault-graph/config/default-config.properties.")
    template_lines = template_file.read_text(encoding="utf-8").splitlines()
    target_lines = ctx.config_file.read_text(encoding="utf-8").splitlines()
    existing_keys = {key for line in target_lines if (key := _property_key(line))}
    missing_lines: list[str] = []
    seen_missing: set[str] = set()
    for raw in template_lines:
        key = _property_key(raw)
        if not key or key in existing_keys or key in seen_missing:
            continue
        missing_lines.append(raw)
        seen_missing.add(key)
    if not missing_lines:
        return 0
    out = list(target_lines)
    if out and out[-1].strip():
        out.append("")
    out.append("# Added by installer from newer default config")
    out.extend(missing_lines)
    ctx.config_file.write_text("\n".join(out) + "\n", encoding="utf-8")
    ctx.reporter.info(f"Merged {len(missing_lines)} new config default(s) into config/config.properties")
    return len(missing_lines)


def _sync_tree(src: Path, dest: Path, *, top_level: bool = True) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    src_names = {child.name for child in src.iterdir()}
    for child in src.iterdir():
        target = dest / child.name
        if child.is_dir():
            if target.exists() and not target.is_dir():
                if target.is_file():
                    target.unlink()
                else:
                    shutil.rmtree(target, ignore_errors=True)
            _sync_tree(child, target, top_level=False)
        else:
            if target.exists() and target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            shutil.copy2(child, target)
    for child in list(dest.iterdir()):
        if child.name in src_names:
            continue
        if top_level and child.name in PRESERVE_TOP_LEVEL:
            continue
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)


def venv_python_path(target_app_dir: Path) -> Path:
    if is_windows():
        return target_app_dir / "venv" / "Scripts" / "python.exe"
    return target_app_dir / "venv" / "bin" / "python"


def sync_local_dependencies(ctx: UpdateContext) -> str:
    profile = str(getattr(ctx, "_resolved_deps_profile", DEPS_PROFILE_CORE) or DEPS_PROFILE_CORE)
    requirement_files = deps_profile_files(ctx.target_app_dir, profile)
    pip_env = prepare_pip_env(ctx)
    venv_python = venv_python_path(ctx.target_app_dir)
    if not venv_python.is_file():
        ctx.reporter.info("Creating virtual environment")
        shutil.rmtree(ctx.target_app_dir / "venv", ignore_errors=True)
        subprocess.run([ctx.python_bin, "-m", "venv", str(ctx.target_app_dir / "venv")], check=True)
        venv_python = venv_python_path(ctx.target_app_dir)
    if profile == DEPS_PROFILE_ADVANCED:
        ctx.reporter.info("Installing Python dependencies from requirements.txt + requirements-keybert.txt")
    else:
        ctx.reporter.info("Installing Python dependencies from requirements.txt")
    ensure_pip_index_reachable(ctx, pip_env)
    subprocess.run([str(venv_python), "-m", "pip", "install", "-U", "pip"], check=True, env=pip_env)
    for requirement_file in requirement_files:
        if not requirement_file.is_file():
            raise UpdateError(f"Missing dependency file: {requirement_file}")
        subprocess.run([str(venv_python), "-m", "pip", "install", "-r", str(requirement_file)], check=True, env=pip_env)
    proc = subprocess.run([str(venv_python), "-V"], capture_output=True, text=True, check=False)
    return (proc.stdout or proc.stderr or "").strip()


def config_port(config_path: Path) -> int:
    props = parse_properties(config_path)
    try:
        return int(str(props.get("port") or "8000").strip() or "8000")
    except Exception:
        return 8000


def port_is_open(port: int) -> bool:
    sock = socket.socket()
    sock.settimeout(0.5)
    try:
        sock.connect(("127.0.0.1", int(port)))
    except OSError:
        return False
    finally:
        try:
            sock.close()
        except OSError:
            pass
    return True


def suggest_free_port(start_port: int) -> int | None:
    start = max(1024, int(start_port) + 1)
    for port in range(start, min(start + 200, 65535)):
        sock = socket.socket()
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            continue
        finally:
            try:
                sock.close()
            except OSError:
                pass
        return port
    return None


def warn_codex_cwd_whitespace(ctx: UpdateContext) -> None:
    value = str(ctx.codex_cwd)
    if any(ch.isspace() for ch in value):
        ctx.reporter.warn(f"CODEX_CWD contains whitespace: {value}")
        ctx.reporter.warn("This usually works, but avoiding spaces in install paths is recommended.")


def ensure_new_install_port_available(ctx: UpdateContext, *, first_install: bool) -> None:
    if not first_install or not ctx.start_server:
        return
    port = config_port(ctx.config_file)
    if not port_is_open(port):
        return
    suggested = suggest_free_port(port)
    if not ctx.interactive:
        suggestion = f" (suggested free port: {suggested})" if suggested else ""
        raise UpdateError(f"Configured port {port} is already in use for a new install. Free that port or update {ctx.config_file}{suggestion}.")
    while True:
        print(f"\nPort {port} from {ctx.config_file} is already in use.")
        if suggested:
            answer = input(
                f"Choose an option:\n1) Stop the existing service and retry\n2) Use free port {suggested} and update config\n3) Skip auto-start\nSelection [1/2/3]: "
            ).strip()
        else:
            answer = input("Choose an option:\n1) Stop the existing service and retry\n3) Skip auto-start\nSelection [1/3]: ").strip()
        if answer == "1":
            if not port_is_open(port):
                return
            print(f"Port {port} is still busy. Stop the existing service first.")
        elif answer == "2" and suggested:
            write_config_key(ctx.config_file, "port", str(suggested))
            ctx.reporter.info(f"Updated config port to {suggested} for this new install.")
            return
        elif answer == "3":
            ctx.skip_start = True
            ctx.reporter.info("Skipping auto-start because the configured port is already in use.")
            return
        else:
            print("Please choose one of the listed options.")


def start_server(ctx: UpdateContext) -> int:
    if ctx.skip_start or not ctx.start_server:
        ctx.reporter.info("Install finished. Server start was skipped.")
        return 0
    if not ctx.config_file.is_file():
        raise UpdateError(f"Config file not found: {ctx.config_file}")
    port = config_port(ctx.config_file)
    if port_is_open(port):
        ctx.reporter.warn(f"Port {port} is already in use. Assuming the server is already running; skipping auto-start.")
        return 0
    python_path = venv_python_path(ctx.target_app_dir)
    if not python_path.is_file():
        raise UpdateError(f"Installed runtime is missing: {python_path}")
    ctx.reporter.info(f"Starting server from {ctx.codex_cwd}")
    cmd = [str(python_path), str(ctx.target_app_dir / "server_supervisor.py"), "--config", str(ctx.config_file)]
    if is_windows():
        proc = subprocess.run(cmd, check=False)
        return int(proc.returncode or 0)
    os.execv(str(python_path), cmd)
    return 0


def determine_local_state(ctx: UpdateContext) -> dict[str, str]:
    state = load_json(ctx.state_file)
    return {
        "app_sha256": str(state.get("app_sha256") or "").strip(),
        "deps_sha256": str(state.get("deps_sha256") or state.get("runtime_sha256") or "").strip(),
        "deps_profile": str(state.get("deps_profile") or DEPS_PROFILE_CORE).strip() or DEPS_PROFILE_CORE,
        "version": str(state.get("version") or "").strip(),
        "build_time_utc": str(state.get("build_time_utc") or "").strip(),
        "build_label": str(state.get("build_label") or "").strip(),
    }


def resolve_deps_profile(ctx: UpdateContext, state: dict[str, str]) -> str:
    if ctx.with_advanced_extraction:
        return DEPS_PROFILE_ADVANCED
    existing = str(state.get("deps_profile") or "").strip()
    if existing == DEPS_PROFILE_ADVANCED:
        return DEPS_PROFILE_ADVANCED
    return DEPS_PROFILE_CORE


def load_normalized_config(config_path: Path, app_dir: Path) -> dict[str, str]:
    loader_path = app_dir / "config_loader.py"
    if not loader_path.is_file() or not config_path.is_file():
        return {}
    try:
        spec = importlib.util.spec_from_file_location("vault_graph_config_loader_runtime", loader_path)
        if spec is None or spec.loader is None:
            return {}
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        normalize = getattr(module, "normalize_config", None)
        if callable(normalize):
            data = normalize(config_path)
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
    except Exception:
        return {}
    return {}


def pip_proxy_env(ctx: UpdateContext) -> dict[str, str]:
    cfg = load_normalized_config(ctx.config_file, ctx.target_app_dir)
    env = dict(os.environ)
    pip_index_url = _optional_string(env.get("PIP_INDEX_URL")) or _optional_string(cfg.get("pip_index_url"))
    pip_extra_index_url = _optional_string(env.get("PIP_EXTRA_INDEX_URL")) or _optional_string(cfg.get("pip_extra_index_url"))
    pip_trusted_host = _optional_string(env.get("PIP_TRUSTED_HOST")) or _optional_string(cfg.get("pip_trusted_host"))
    http_proxy = (
        _optional_string(env.get("https_proxy"))
        or _optional_string(env.get("HTTPS_PROXY"))
        or _optional_string(env.get("http_proxy"))
        or _optional_string(env.get("HTTP_PROXY"))
        or _optional_string(cfg.get("pip_https_proxy"))
        or _optional_string(cfg.get("https_proxy"))
        or _optional_string(cfg.get("pip_http_proxy"))
        or _optional_string(cfg.get("http_proxy"))
    )
    https_proxy = (
        _optional_string(env.get("https_proxy"))
        or _optional_string(env.get("HTTPS_PROXY"))
        or _optional_string(cfg.get("pip_https_proxy"))
        or _optional_string(cfg.get("https_proxy"))
        or http_proxy
    )
    if http_proxy:
        env["http_proxy"] = http_proxy
        env["HTTP_PROXY"] = http_proxy
    if https_proxy:
        env["https_proxy"] = https_proxy
        env["HTTPS_PROXY"] = https_proxy
    no_proxy = (
        _optional_string(env.get("no_proxy"))
        or _optional_string(env.get("NO_PROXY"))
        or _optional_string(cfg.get("pip_no_proxy"))
        or _optional_string(cfg.get("no_proxy"))
    )
    if no_proxy:
        env["no_proxy"] = no_proxy
        env["NO_PROXY"] = no_proxy
    if pip_index_url:
        env["PIP_INDEX_URL"] = pip_index_url
    if pip_extra_index_url:
        env["PIP_EXTRA_INDEX_URL"] = pip_extra_index_url
    if pip_trusted_host:
        env["PIP_TRUSTED_HOST"] = pip_trusted_host
    return env


def pip_proxy_configured(env: dict[str, str]) -> bool:
    return bool(
        _optional_string(env.get("http_proxy"))
        or _optional_string(env.get("HTTP_PROXY"))
        or _optional_string(env.get("https_proxy"))
        or _optional_string(env.get("HTTPS_PROXY"))
    )


def pip_proxy_fallback_env(base_env: dict[str, str]) -> dict[str, str]:
    env = dict(base_env)
    http_proxy = _optional_string(env.get("INSTALLER_FALLBACK_HTTP_PROXY") or env.get("ANTIRAG_FALLBACK_HTTP_PROXY"))
    https_proxy = _optional_string(env.get("INSTALLER_FALLBACK_HTTPS_PROXY") or env.get("ANTIRAG_FALLBACK_HTTPS_PROXY")) or http_proxy
    no_proxy = _optional_string(env.get("INSTALLER_FALLBACK_NO_PROXY") or env.get("ANTIRAG_FALLBACK_NO_PROXY"))
    if http_proxy:
        env["http_proxy"] = http_proxy
        env["HTTP_PROXY"] = http_proxy
    if https_proxy:
        env["https_proxy"] = https_proxy
        env["HTTPS_PROXY"] = https_proxy
    if no_proxy:
        env["no_proxy"] = no_proxy
        env["NO_PROXY"] = no_proxy
    return env


def _pip_index_probe_urls(env: dict[str, str]) -> list[str]:
    candidates = [
        _optional_string(env.get("PIP_INDEX_URL")),
        "https://pypi.org/simple/pip/",
    ]
    out: list[str] = []
    seen: set[str] = set()
    for url in candidates:
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out


def _probe_index_once(url: str, *, proxies: dict[str, str]) -> str | None:
    opener = build_opener(ProxyHandler(proxies)) if proxies else build_opener()
    parsed = urlparse(url)
    host = parsed.hostname or ""
    port = int(parsed.port or (443 if parsed.scheme == "https" else 80))
    proxy_in_use = bool(proxies.get(parsed.scheme))
    if host and not proxy_in_use:
        with socket.create_connection((host, port), timeout=1.2):
            pass
    request = Request(url, headers={"User-Agent": f"bootstrap-installer/{INSTALLER_VERSION}"}, method="HEAD")
    with opener.open(request, timeout=1.8) as response:
        status = int(getattr(response, "status", 200) or 200)
        if 200 <= status < 500:
            return None
        return f"{url} returned HTTP {status}"


def _probe_index_with_deadline(url: str, *, proxies: dict[str, str], deadline_seconds: float = 2.5) -> str | None:
    result: dict[str, str | None] = {"error": None}

    def _worker() -> None:
        try:
            result["error"] = _probe_index_once(url, proxies=proxies)
        except Exception as exc:
            result["error"] = f"{url}: {exc}"

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    thread.join(timeout=max(0.1, float(deadline_seconds)))
    if thread.is_alive():
        return f"{url}: timed out after {deadline_seconds:.1f}s"
    return result["error"]


def ensure_pip_index_reachable(ctx: UpdateContext, env: dict[str, str]) -> None:
    urls = _pip_index_probe_urls(env)
    if not urls:
        return
    http_proxy = _optional_string(env.get("http_proxy") or env.get("HTTP_PROXY"))
    https_proxy = _optional_string(env.get("https_proxy") or env.get("HTTPS_PROXY"))
    proxies: dict[str, str] = {}
    if http_proxy:
        proxies["http"] = http_proxy
    if https_proxy:
        proxies["https"] = https_proxy
    errors: list[str] = []
    for url in urls:
        ctx.reporter.info("Checking Python package index reachability")
        error = _probe_index_with_deadline(url, proxies=proxies, deadline_seconds=2.5)
        if error is None:
            return
        errors.append(error)
    proxy_hint = (
        "A proxy is configured, but the package index is still unreachable."
        if (http_proxy or https_proxy)
        else "No proxy is configured for pip."
    )
    raise UpdateError(
        "Python package downloads cannot reach the configured package index.\n"
        f"{proxy_hint}\n"
        "If you are on VPN, either disconnect from VPN or configure pip/http proxy settings before retrying.\n"
        "You can set HTTP_PROXY / HTTPS_PROXY in the shell, or add pip_http_proxy / pip_https_proxy to config/config.properties.\n"
        f"Probe details: {'; '.join(errors)}"
    )


def prepare_pip_env(ctx: UpdateContext) -> dict[str, str]:
    env = pip_proxy_env(ctx)
    try:
        ensure_pip_index_reachable(ctx, env)
        return env
    except UpdateError:
        if pip_proxy_configured(env):
            raise
        fallback_env = pip_proxy_fallback_env(env)
        if not pip_proxy_configured(fallback_env):
            raise
        ctx.reporter.warn("Direct pip index access failed. Retrying with the configured fallback proxy.")
        urls = _pip_index_probe_urls(fallback_env)
        if not urls:
            raise
        http_proxy = _optional_string(fallback_env.get("http_proxy") or fallback_env.get("HTTP_PROXY"))
        https_proxy = _optional_string(fallback_env.get("https_proxy") or fallback_env.get("HTTPS_PROXY"))
        proxies: dict[str, str] = {}
        if http_proxy:
            proxies["http"] = http_proxy
        if https_proxy:
            proxies["https"] = https_proxy
        errors: list[str] = []
        for url in urls:
            ctx.reporter.info("Checking Python package index reachability")
            error = _probe_index_with_deadline(url, proxies=proxies, deadline_seconds=6.0)
            if error is None:
                ctx.reporter.info("Using fallback proxy for Python package downloads.")
                return fallback_env
            errors.append(error)
        raise UpdateError(
            "Python package downloads cannot reach the configured package index.\n"
            "A proxy is configured, but the package index is still unreachable.\n"
            "If you are on VPN, either disconnect from VPN or configure pip/http proxy settings before retrying.\n"
            "You can set HTTP_PROXY / HTTPS_PROXY in the shell, or add pip_http_proxy / pip_https_proxy to config/config.properties.\n"
            f"Probe details: {'; '.join(errors)}"
        )


def ensure_advanced_extraction_assets(ctx: UpdateContext, profile: str) -> None:
    if profile != DEPS_PROFILE_ADVANCED:
        return
    cfg = load_normalized_config(ctx.config_file, ctx.target_app_dir)
    model_name = str(cfg.get("keybert_model_name") or "").strip()
    model_path_raw = str(cfg.get("keybert_model_path") or "").strip()
    if not model_name:
        ctx.reporter.warn("Advanced extraction deps were installed, but keybert_model_name is not configured.")
        return
    if not model_path_raw or model_path_raw.lower() == "null":
        ctx.reporter.warn("Advanced extraction deps were installed, but keybert_model_path is not configured. The MiniLM model was not pre-pulled.")
        return
    model_path = Path(model_path_raw).expanduser()
    if model_path.exists():
        ctx.reporter.info(f"Advanced extraction model already present: {model_path}")
        return
    ctx.reporter.info(f"Downloading advanced extraction model '{model_name}'")
    model_path.parent.mkdir(parents=True, exist_ok=True)
    venv_python = venv_python_path(ctx.target_app_dir)
    env = pip_proxy_env(ctx)
    subprocess.run(
        [
            str(venv_python),
            "-c",
            (
                "from sentence_transformers import SentenceTransformer; "
                f"m = SentenceTransformer({model_name!r}, local_files_only=False); "
                f"m.save({str(model_path)!r})"
            ),
        ],
        check=True,
        env=env,
    )


def check_for_update(*, codex_cwd: Path, manifest_url: str, requested_version: str = "latest", reporter: Reporter | None = None) -> dict[str, object]:
    reporter = reporter or SilentReporter()
    python_bin = ensure_python_bin()
    tmp_root = codex_cwd / ".bootstrap-installer-tmp"
    run_dir = tmp_root / f"check.{int(time.time() * 1000)}"
    download_dir = run_dir / "downloads"
    download_dir.mkdir(parents=True, exist_ok=True)
    try:
        manifest = fetch_manifest(manifest_url, download_dir / "manifest.json", reporter)
        entry = read_manifest_entry(manifest, requested_version, manifest_url=manifest_url)
        state = determine_local_state(
            UpdateContext(
                codex_cwd=codex_cwd,
                manifest_url=manifest_url,
                source_app_dir=None,
                requested_version=requested_version,
                python_bin=python_bin,
                tmp_root=tmp_root,
                interactive=False,
                assume_yes=True,
                skip_start=True,
                start_server=False,
                with_advanced_extraction=False,
                reporter=reporter,
            )
        )
        profile = resolve_deps_profile(
            UpdateContext(
                codex_cwd=codex_cwd,
                manifest_url=manifest_url,
                source_app_dir=None,
                requested_version=requested_version,
                python_bin=python_bin,
                tmp_root=tmp_root,
                interactive=False,
                assume_yes=True,
                skip_start=True,
                start_server=False,
                with_advanced_extraction=False,
                reporter=reporter,
            ),
            state,
        )
        target_app_dir = codex_cwd / "vault-graph"
        config_file = codex_cwd / "config" / "config.properties"
        app_matches = (
            state["app_sha256"] == entry.app_sha256
            and (target_app_dir / "run_server.py").is_file()
            and (target_app_dir / "server_supervisor.py").is_file()
            and config_file.is_file()
        )
        deps_stale = True
        requirement_files = deps_profile_files(target_app_dir, profile)
        if all(path.is_file() for path in requirement_files):
            deps_sha = deps_sha_from_files(requirement_files)
            deps_stale = not (state["deps_sha256"] == deps_sha and venv_python_path(target_app_dir).is_file())
        result = {
            "configured": True,
            "available": (not app_matches) or deps_stale,
            "deps_profile": profile,
            "current_version": state["version"] or None,
            "current_build_time_utc": state["build_time_utc"] or None,
            "current_build_label": state["build_label"] or None,
            "target_version": entry.version,
            "target_build_time_utc": entry.build_time_utc or None,
            "target_build_label": entry.build_label or None,
            "app_update_required": not app_matches,
            "deps_update_required": deps_stale,
            "manifest_url": manifest_url,
        }
        persisted = write_update_check_state(
            codex_cwd / "config" / "install-state.json",
            manifest_url=manifest_url,
            available=bool(result["available"]),
            current_version=result.get("current_version"),
            current_build_time_utc=result.get("current_build_time_utc"),
            current_build_label=result.get("current_build_label"),
            target_version=result.get("target_version"),
            target_build_time_utc=result.get("target_build_time_utc"),
            target_build_label=result.get("target_build_label"),
        )
        result["last_checked_at"] = persisted.get("last_checked_at")
        return result
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)
        tmp_root.mkdir(parents=True, exist_ok=True)
        try:
            tmp_root.rmdir()
        except OSError:
            pass


def apply_update(ctx: UpdateContext) -> dict[str, object]:
    check_prereqs(ctx)
    warn_codex_cwd_whitespace(ctx)
    ctx.codex_cwd.mkdir(parents=True, exist_ok=True)
    first_install = not ctx.target_app_dir.is_dir() and not ctx.config_file.is_file()

    run_dir = Path(tempfile_mkdtemp(ctx.tmp_root))
    download_dir = run_dir / "downloads"
    extract_dir = run_dir / "extract"
    download_dir.mkdir(parents=True, exist_ok=True)
    extract_dir.mkdir(parents=True, exist_ok=True)

    try:
        ctx.reporter.info(f"CODEX_CWD: {ctx.codex_cwd}")
        ctx.reporter.info(f"Platform: {platform_summary()}")
        source_app_dir = ctx.source_app_dir.resolve() if ctx.source_app_dir else None
        if source_app_dir is not None:
            ctx.reporter.info(f"Local source: {source_app_dir}")
            entry = local_source_manifest_entry(source_app_dir)
        else:
            ctx.reporter.info(f"Manifest: {ctx.manifest_url}")
            manifest = fetch_manifest(ctx.manifest_url, download_dir / "manifest.json", ctx.reporter)
            entry = read_manifest_entry(manifest, ctx.requested_version, manifest_url=ctx.manifest_url)
            if entry.installer_min > INSTALLER_VERSION:
                raise UpdateError(f"This manifest requires installer version {entry.installer_min}+.")

        state = determine_local_state(ctx)
        profile = resolve_deps_profile(ctx, state)
        ctx._resolved_deps_profile = profile  # type: ignore[attr-defined]
        need_app = not (
            state["app_sha256"] == entry.app_sha256
            and (ctx.target_app_dir / "run_server.py").is_file()
            and (ctx.target_app_dir / "server_supervisor.py").is_file()
            and ctx.config_file.is_file()
        )
        staged_requirements_sha = ""
        app_source_dir = source_app_dir
        if need_app:
            if source_app_dir is not None:
                staged_requirements_sha = deps_sha_from_files(deps_profile_files(source_app_dir, profile))
            else:
                app_zip = download_dir / Path(entry.app_url).name
                download_to_path(entry.app_url, app_zip, reporter=ctx.reporter, expected_sha=entry.app_sha256)
                ctx.reporter.info("Extracting app payload")
                app_extract = extract_dir / "app"
                extract_zip(app_zip, app_extract)
                app_source_dir = app_extract / "vault-graph"
                staged_requirements_sha = deps_sha_from_files(deps_profile_files(app_source_dir, profile))
        else:
            if source_app_dir is not None:
                ctx.reporter.info("Skipping local file sync; installed app checksum already matches local source.")
            else:
                ctx.reporter.info("Skipping app download; installed app checksum already matches manifest.")
            staged_requirements_sha = deps_sha_from_files(deps_profile_files(ctx.target_app_dir, profile))

        venv_python = venv_python_path(ctx.target_app_dir)
        need_deps = not (venv_python.is_file() and state["deps_sha256"] == staged_requirements_sha)

        ctx.reporter.info(f"Selected version: {entry.version}")
        if need_app:
            if app_source_dir is None:
                raise UpdateError("No application source is available for install.")
            seed_config_if_missing(ctx, app_source_dir)
            ensure_new_install_port_available(ctx, first_install=first_install)
            merge_config_defaults(ctx, app_source_dir)
            if app_source_dir.resolve() != ctx.target_app_dir.resolve():
                _sync_tree(app_source_dir, ctx.target_app_dir)
        populate_installer_base_url_if_missing(ctx)

        if need_deps:
            python_version = sync_local_dependencies(ctx)
            ensure_advanced_extraction_assets(ctx, profile)
        else:
            ctx.reporter.info("Skipping dependency install; requirements checksum already matches local state.")
            proc = subprocess.run([str(venv_python_path(ctx.target_app_dir)), "-V"], capture_output=True, text=True, check=False)
            python_version = (proc.stdout or proc.stderr or "").strip()

        write_install_state(
            ctx,
            version=entry.version,
            build_time_utc=entry.build_time_utc,
            build_label=entry.build_label,
            app_sha=entry.app_sha256,
            deps_sha=staged_requirements_sha,
            app_url=entry.app_url,
            python_version=python_version,
        )
        return {
            "status": "ok",
            "changed": bool(need_app or need_deps),
            "current_version": state["version"] or None,
            "current_build_time_utc": state["build_time_utc"] or None,
            "current_build_label": state["build_label"] or None,
            "target_version": entry.version,
            "target_build_time_utc": entry.build_time_utc or None,
            "target_build_label": entry.build_label or None,
            "app_updated": need_app,
            "deps_updated": need_deps,
            "deps_profile": profile,
            "manifest_url": ctx.manifest_url,
        }
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)
        ctx.tmp_root.mkdir(parents=True, exist_ok=True)
        try:
            ctx.tmp_root.rmdir()
        except OSError:
            pass


def tempfile_mkdtemp(root: Path) -> str:
    root.mkdir(parents=True, exist_ok=True)
    suffix = f"run.{int(time.time() * 1000)}.{os.getpid()}"
    candidate = root / suffix
    counter = 0
    while candidate.exists():
        counter += 1
        candidate = root / f"{suffix}.{counter}"
    candidate.mkdir(parents=True, exist_ok=False)
    return str(candidate)


def build_context(args: argparse.Namespace, reporter: Reporter) -> UpdateContext:
    codex_cwd = Path(args.codex_cwd or os.getcwd()).expanduser().resolve()
    tmp_root = Path(args.tmp_root or (codex_cwd / ".bootstrap-installer-tmp")).expanduser().resolve()
    source_app_dir_raw = str(getattr(args, "source_app_dir", "") or "").strip()
    manifest_url_raw = str(getattr(args, "manifest_url", "") or "").strip()
    if not manifest_url_raw and not source_app_dir_raw:
        manifest_url_raw = default_manifest_url_from_env()
    if not manifest_url_raw and not source_app_dir_raw:
        raise UpdateError("No manifest source was provided. Set INSTALLER_BASE_URL or INSTALLER_MANIFEST_URL, pass --manifest-url, or use --source-app-dir.")
    return UpdateContext(
        codex_cwd=codex_cwd,
        manifest_url=manifest_url_raw,
        source_app_dir=Path(source_app_dir_raw).expanduser().resolve() if source_app_dir_raw else None,
        requested_version=str(args.version or "latest").strip() or "latest",
        python_bin=ensure_python_bin(getattr(args, "python_bin", "") or ""),
        tmp_root=tmp_root,
        interactive=sys.stdin.isatty() and sys.stdout.isatty() and not getattr(args, "non_interactive", False),
        assume_yes=bool(getattr(args, "yes", False)),
        skip_start=bool(getattr(args, "skip_start", False)),
        start_server=bool(getattr(args, "start_server", False)),
        with_advanced_extraction=bool(getattr(args, "with_advanced_extraction", False)),
        reporter=reporter,
    )


def cli() -> int:
    parser = argparse.ArgumentParser(description="Shared installer and self-updater.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--manifest-url", default="")
    common.add_argument("--version", default="latest")
    common.add_argument("--codex-cwd", default=os.getcwd())
    common.add_argument("--tmp-root", default="")
    common.add_argument("--source-app-dir", default="")
    common.add_argument("--python-bin", default="")
    common.add_argument("--non-interactive", action="store_true")
    common.add_argument("--yes", action="store_true")
    common.add_argument("--with-advanced-extraction", "--with-keybert", dest="with_advanced_extraction", action="store_true")

    check_p = sub.add_parser("check", parents=[common], help="Check whether an update is available.")
    check_p.add_argument("--json", action="store_true")

    apply_p = sub.add_parser("apply", parents=[common], help="Install or upgrade the application.")
    apply_p.add_argument("--skip-start", action="store_true")
    apply_p.add_argument("--start-server", action="store_true")
    apply_p.add_argument("--json", action="store_true")

    args = parser.parse_args()
    reporter: Reporter = SilentReporter() if getattr(args, "json", False) else Reporter()
    ctx = build_context(args, reporter)
    try:
        if args.cmd == "check":
            result = check_for_update(codex_cwd=ctx.codex_cwd, manifest_url=ctx.manifest_url, requested_version=ctx.requested_version, reporter=ctx.reporter)
            if getattr(args, "json", False):
                print(json.dumps(result, indent=2, sort_keys=True))
            return 0

        result = apply_update(ctx)
        if getattr(args, "json", False):
            print(json.dumps(result, indent=2, sort_keys=True))
        if ctx.start_server and not ctx.skip_start:
            ctx.reporter.info("Install complete.")
            return start_server(ctx)
        ctx.reporter.info("Install complete.")
        return 0
    except (UpdateError, HTTPError, URLError, subprocess.CalledProcessError) as exc:
        if getattr(args, "json", False):
            print(json.dumps({"status": "error", "detail": str(exc)}, indent=2, sort_keys=True))
        else:
            reporter.error(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(cli())
