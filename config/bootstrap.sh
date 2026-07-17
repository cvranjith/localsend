#!/usr/bin/env bash
set -euo pipefail

DEFAULT_INSTALLER_BASE_URL="https://objectstorage.ap-seoul-1.oraclecloud.com/p/vqHG5Za7XyJd8hCzfB-qd1V6SevieUEXJ7LXakmysUsXTcOEkpLoG0OcNhG6xm_W/n/cnvubmbktlyh/b/artifactory/o/antirag"
BASE_URL="${INSTALLER_BASE_URL:-${ANTIRAG_BASE_URL:-$DEFAULT_INSTALLER_BASE_URL}}"
MANIFEST_URL="${INSTALLER_MANIFEST_URL:-${ANTIRAG_MANIFEST_URL:-}}"
REQUESTED_VERSION="latest"
CODEX_CWD="${PWD}"
CODEX_CWD_EXPLICIT="0"
TMP_ROOT=""
ASSUME_YES="0"
SKIP_START="0"
AUTO_CONFIRM="1"
CONFIRM_CWD="0"
WITH_ADVANCED_EXTRACTION="0"
MODE="auto"
SOURCE_APP_DIR=""
BOOTSTRAP_MODE=""
SCRIPT_PATH="${BASH_SOURCE[0]:-}"
DEFAULT_FALLBACK_HTTP_PROXY="http://www-proxy.us.oracle.com:80"
DEFAULT_FALLBACK_HTTPS_PROXY="http://www-proxy.us.oracle.com:80"
DEFAULT_FALLBACK_NO_PROXY="localhost,.oraclecorp.com,us.oracle.com,.oraclecloud.com,.in.oracle.com,nl.oracle.com,svc.cluster.local,127.0.0.1,.oraclevcn.com"
COLOR_RESET=""
COLOR_INFO=""
COLOR_WARN=""
COLOR_ERROR=""

setup_colors() {
  if [ ! -t 1 ]; then
    return
  fi
  if [ "${NO_COLOR:-}" != "" ]; then
    return
  fi
  if ! command -v tput >/dev/null 2>&1; then
    return
  fi
  local colors=""
  colors="$(tput colors 2>/dev/null || true)"
  if [ -z "$colors" ] || [ "$colors" -lt 8 ]; then
    return
  fi
  COLOR_RESET="$(printf '\033[0m')"
  if [ "${COLORTERM:-}" = "truecolor" ] || [ "${COLORTERM:-}" = "24bit" ]; then
    COLOR_INFO="$(printf '\033[38;2;74;122;168m')"
    COLOR_WARN="$(printf '\033[38;2;168;128;74m')"
    COLOR_ERROR="$(printf '\033[38;2;168;82;96m')"
  else
    COLOR_INFO="$(printf '\033[38;5;67m')"
    COLOR_WARN="$(printf '\033[38;5;137m')"
    COLOR_ERROR="$(printf '\033[38;5;131m')"
  fi
}

usage() {
  cat <<'EOF'
Usage: bootstrap.sh [options]

Installs or upgrades the application into the current directory by default.

Options:
  --version <version>   Install a specific version (default: latest)
  --codex-cwd <path>    Use a specific CODEX_CWD instead of the current directory
  --tmp-root <path>     Override the download / staging directory
  --source-app-dir <path>
                      Use a local vault-graph source directory
  --base-url <url>      Override the read-only artifact base URL
  --manifest-url <url>  Override the manifest URL
  --local               Force local bootstrap mode
  --remote              Force remote bootstrap mode
  --confirm-cwd        Prompt before using the current CODEX_CWD
  --with-advanced-extraction
                       Install optional KeyBERT + sentence-transformers deps and pre-pull the configured MiniLM model
  --with-keybert       Alias for --with-advanced-extraction
  --yes                 Skip other interactive prompts when available
  --skip-start          Install / upgrade but do not start the server
  -h, --help            Show this help text

Examples:
  bash bootstrap.sh
  bash bootstrap.sh --local
  bash bootstrap.sh --version 1.0.001
  bash bootstrap.sh --with-advanced-extraction
  curl -fsSL "<bootstrap-url>/bootstrap.sh" | bash
  curl -fsSL "<bootstrap-url>/bootstrap.sh" | bash -s -- --remote
  curl -fsSL "<bootstrap-url>/bootstrap.sh" | bash -s -- --version 1.0.001 --yes
  curl -fsSL "<bootstrap-url>/bootstrap.sh" | bash -s -- --with-advanced-extraction
EOF
}

die() {
  if [ -n "$COLOR_ERROR" ]; then
    printf '%sx%s %s\n' "$COLOR_ERROR" "$COLOR_RESET" "$1" >&2
  else
    printf '[ERROR] %s\n' "$1" >&2
  fi
  exit 1
}

info() {
  if [ -n "$COLOR_INFO" ]; then
    printf '%s●%s %s\n' "$COLOR_INFO" "$COLOR_RESET" "$1"
  else
    printf '[INFO] %s\n' "$1"
  fi
}

warn() {
  if [ -n "$COLOR_WARN" ]; then
    printf '%s▲%s %s\n' "$COLOR_WARN" "$COLOR_RESET" "$1" >&2
  else
    printf '[WARN] %s\n' "$1" >&2
  fi
}

print_banner() {
  local banner_path="$1"
  [ -f "$banner_path" ] || return
  if [ -n "$COLOR_INFO" ]; then
    "$PYTHON_BIN" - "$banner_path" <<'PY'
from pathlib import Path
import os
import sys

path = Path(sys.argv[1])
reset = "\033[0m"
truecolor = os.environ.get("COLORTERM", "").lower() in {"truecolor", "24bit"}

lines = path.read_text(encoding="utf-8", errors="replace").splitlines()

def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t

if truecolor:
    anchors = [
        (54, 100, 158),
        (70, 126, 170),
        (86, 144, 166),
        (104, 120, 170),
    ]

    def sample_gradient(t: float):
        if t <= 0:
            return anchors[0]
        if t >= 1:
            return anchors[-1]
        segments = len(anchors) - 1
        pos = t * segments
        idx = min(int(pos), segments - 1)
        frac = pos - idx
        a = anchors[idx]
        b = anchors[idx + 1]
        return (
            int(round(lerp(a[0], b[0], frac))),
            int(round(lerp(a[1], b[1], frac))),
            int(round(lerp(a[2], b[2], frac))),
        )
else:
    palette = [60, 61, 67, 68, 73, 74, 103, 109, 110]

    def sample_gradient(t: float):
        if t <= 0:
            return palette[0]
        if t >= 1:
            return palette[-1]
        idx = int(round(t * (len(palette) - 1)))
        idx = max(0, min(len(palette) - 1, idx))
        return palette[idx]

max_width = max((len(line) for line in lines), default=1)
for row, line in enumerate(lines):
    if not line:
        print("")
        continue
    out = []
    row_shift = 0.08 * row
    width = max(1, len(line) - 1)
    for col, ch in enumerate(line):
        if ch.isspace():
            out.append(ch)
            continue
        t = (col / width) if width else 0.0
        # A tiny row phase shift makes the gradient feel smoother across the whole banner.
        t = max(0.0, min(1.0, t * 0.88 + row_shift))
        sample = sample_gradient(t)
        if truecolor:
            r, g, b = sample
            out.append(f"\033[38;2;{r};{g};{b}m{ch}")
        else:
            out.append(f"\033[38;5;{sample}m{ch}")
    out.append(reset)
    print("".join(out))
print("")
PY
  else
    cat "$banner_path"
    printf '\n'
  fi
}

parse_args() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --version)
        [ "$#" -ge 2 ] || die "--version requires a value"
        REQUESTED_VERSION="$2"
        shift 2
        ;;
      --codex-cwd)
        [ "$#" -ge 2 ] || die "--codex-cwd requires a value"
        CODEX_CWD="$2"
        CODEX_CWD_EXPLICIT="1"
        shift 2
        ;;
      --tmp-root)
        [ "$#" -ge 2 ] || die "--tmp-root requires a value"
        TMP_ROOT="$2"
        shift 2
        ;;
      --source-app-dir)
        [ "$#" -ge 2 ] || die "--source-app-dir requires a value"
        SOURCE_APP_DIR="$2"
        shift 2
        ;;
      --base-url)
        [ "$#" -ge 2 ] || die "--base-url requires a value"
        BASE_URL="${2%/}"
        shift 2
        ;;
      --manifest-url)
        [ "$#" -ge 2 ] || die "--manifest-url requires a value"
        MANIFEST_URL="$2"
        shift 2
        ;;
      --confirm-cwd)
        CONFIRM_CWD="1"
        AUTO_CONFIRM="0"
        shift
        ;;
      --local)
        MODE="local"
        shift
        ;;
      --remote)
        MODE="remote"
        shift
        ;;
      --with-advanced-extraction|--with-keybert)
        WITH_ADVANCED_EXTRACTION="1"
        shift
        ;;
      --yes)
        ASSUME_YES="1"
        shift
        ;;
      --skip-start)
        SKIP_START="1"
        shift
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        die "Unknown argument: $1"
        ;;
    esac
  done
}

resolve_python() {
  if command -v python3 >/dev/null 2>&1; then
    command -v python3
    return
  fi
  if command -v python >/dev/null 2>&1; then
    command -v python
    return
  fi
  die "Python is required for the installer. Install Python 3 and rerun."
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"
}

resolve_path() {
  "$PYTHON_BIN" - "$1" <<'PY'
from pathlib import Path
import sys
print(Path(sys.argv[1]).expanduser().resolve())
PY
}

bootstrap_script_default_cwd() {
  [ -n "$SCRIPT_PATH" ] || return 0
  "$PYTHON_BIN" - "$SCRIPT_PATH" <<'PY'
from pathlib import Path
import sys

script = Path(sys.argv[1]).expanduser().resolve()
script_dir = script.parent
candidates = []
if (script_dir / "vault-graph" / "run_server.py").is_file():
    candidates.append(script_dir)
if script_dir.name == "config" and (script_dir.parent / "vault-graph" / "run_server.py").is_file():
    candidates.append(script_dir.parent)
seen = set()
for candidate in candidates:
    text = str(candidate)
    if text in seen:
        continue
    seen.add(text)
    print(text)
    break
PY
}

detect_bootstrap_mode() {
  if [ "$MODE" != "auto" ]; then
    printf '%s\n' "$MODE"
    return
  fi
  if [ -n "$SCRIPT_PATH" ] && [ -f "$SCRIPT_PATH" ]; then
    printf 'local\n'
    return
  fi
  printf 'remote\n'
}

resolve_local_source_app_dir() {
  if [ -n "$SOURCE_APP_DIR" ]; then
    if [ -f "$SOURCE_APP_DIR/run_server.py" ]; then
      resolve_path "$SOURCE_APP_DIR"
      return
    fi
    return
  fi
  if [ -f "${CODEX_CWD}/vault-graph/run_server.py" ]; then
    resolve_path "${CODEX_CWD}/vault-graph"
    return
  fi
  if [ -n "$SCRIPT_PATH" ]; then
    local script_dir=""
    script_dir="$(dirname "$SCRIPT_PATH")"
    if [ -f "${script_dir}/vault-graph/run_server.py" ]; then
      resolve_path "${script_dir}/vault-graph"
      return
    fi
    if [ -f "${script_dir}/../vault-graph/run_server.py" ]; then
      resolve_path "${script_dir}/../vault-graph"
      return
    fi
  fi
}

resolve_local_banner_path() {
  if [ -n "$SCRIPT_PATH" ]; then
    local script_dir=""
    script_dir="$(dirname "$SCRIPT_PATH")"
    if [ -f "${script_dir}/banner.txt" ]; then
      resolve_path "${script_dir}/banner.txt"
      return
    fi
  fi
  if [ -f "${CODEX_CWD}/config/banner.txt" ]; then
    resolve_path "${CODEX_CWD}/config/banner.txt"
    return
  fi
}

persist_local_bootstrap_from_file() {
  [ -n "$SCRIPT_PATH" ] || return
  mkdir -p "${CODEX_CWD}/config"
  if [ "$(resolve_path "$SCRIPT_PATH")" != "$(resolve_path "${CODEX_CWD}/config/bootstrap.sh")" ]; then
    cp "$SCRIPT_PATH" "${CODEX_CWD}/config/bootstrap.sh"
  fi
  chmod +x "${CODEX_CWD}/config/bootstrap.sh"
  local local_banner=""
  local_banner="$(resolve_local_banner_path || true)"
  if [ -n "$local_banner" ] && [ -f "$local_banner" ]; then
    if [ "$(resolve_path "$local_banner")" != "$(resolve_path "${CODEX_CWD}/config/banner.txt")" ]; then
      cp "$local_banner" "${CODEX_CWD}/config/banner.txt"
    fi
  fi
}

persist_local_bootstrap_from_remote() {
  mkdir -p "${CODEX_CWD}/config"
  curl -fsSL "${UPDATER_BASE%/}/bootstrap.sh" -o "${CODEX_CWD}/config/bootstrap.sh"
  chmod +x "${CODEX_CWD}/config/bootstrap.sh"
  curl -fsSL "${UPDATER_BASE%/}/self_update.py" -o "${CODEX_CWD}/config/self_update.py"
  chmod +x "${CODEX_CWD}/config/self_update.py"
  if [ -f "$BANNER_PATH" ]; then
    if [ "$(resolve_path "$BANNER_PATH")" != "$(resolve_path "${CODEX_CWD}/config/banner.txt")" ]; then
      cp "$BANNER_PATH" "${CODEX_CWD}/config/banner.txt"
    fi
  else
    curl -fsSL "${UPDATER_BASE%/}/banner.txt" -o "${CODEX_CWD}/config/banner.txt" >/dev/null 2>&1 || true
  fi
}

warn_codex_cwd_whitespace() {
  case "$CODEX_CWD" in
    *[[:space:]]*)
      warn "CODEX_CWD contains whitespace: $CODEX_CWD"
      warn "This usually works, but avoiding spaces in install paths is recommended."
      ;;
  esac
}

confirm_codex_cwd() {
  if [ "$CONFIRM_CWD" != "1" ]; then
    AUTO_CONFIRM="1"
    return
  fi
  if [ "$ASSUME_YES" = "1" ] || [ -f "${CODEX_CWD}/config/config.properties" ]; then
    AUTO_CONFIRM="1"
    return
  fi
  if [ ! -r /dev/tty ]; then
    info "No interactive terminal available; using current directory as CODEX_CWD: $CODEX_CWD"
    AUTO_CONFIRM="1"
    return
  fi
  printf '\nInstall into this CODEX_CWD?\n%s\n[Y/n]: ' "$CODEX_CWD" > /dev/tty
  local answer=""
  if ! read -r answer < /dev/tty; then
    answer=""
  fi
  case "$answer" in
    ""|y|Y|yes|YES)
      AUTO_CONFIRM="1"
      ;;
    *)
      die "Please cd into the desired CODEX_CWD and rerun, or pass --codex-cwd <path>."
      ;;
  esac
}

check_prereqs() {
  info "Checking prerequisites"
  if [ "$BOOTSTRAP_MODE" = "remote" ]; then
    require_command curl
  fi
  require_command mktemp
  "$PYTHON_BIN" -m venv --help >/dev/null 2>&1 || die "Python venv support is required on this machine."
  if command -v git >/dev/null 2>&1; then
    git --version >/dev/null 2>&1 || warn "git is present but not responding. Some git-backed features may not work."
  else
    warn "git was not found. Some git-backed features may not work."
  fi
  if command -v codex >/dev/null 2>&1; then
    codex --version >/dev/null 2>&1 || warn "Codex CLI is present but not responding. In-app Codex features may not work until it is configured."
    if [ ! -d "${HOME:-$PWD}/.codex" ]; then
      warn "~/.codex was not found. In-app Codex features may not work until Codex CLI is configured."
    fi
  else
    warn "Codex CLI was not found. In-app Codex features may not work until it is installed."
  fi
}

cleanup() {
  if [ -n "${TMP_ROOT:-}" ] && [ -d "${TMP_ROOT:-}" ]; then
    rmdir "$TMP_ROOT" >/dev/null 2>&1 || true
  fi
}

cleanup_downloaded_bootstrap() {
  [ "${BOOTSTRAP_MODE:-}" = "remote" ] || return 0
  [ -n "${SCRIPT_PATH:-}" ] || return 0
  local root_bootstrap=""
  root_bootstrap="$(resolve_path "${CODEX_CWD}/bootstrap.sh")"
  if [ "$(resolve_path "$SCRIPT_PATH")" = "$root_bootstrap" ]; then
    rm -f "$root_bootstrap"
  fi
}

parse_args "$@"
setup_colors
PYTHON_BIN="$(resolve_python)"
if [ -n "$SCRIPT_PATH" ] && [ ! -f "$SCRIPT_PATH" ]; then
  SCRIPT_PATH=""
fi
if [ -n "$SCRIPT_PATH" ]; then
  SCRIPT_PATH="$(resolve_path "$SCRIPT_PATH")"
fi
if [ "$CODEX_CWD_EXPLICIT" != "1" ] && [ -n "$SCRIPT_PATH" ]; then
  DETECTED_CWD="$(bootstrap_script_default_cwd || true)"
  if [ -n "${DETECTED_CWD:-}" ]; then
    CODEX_CWD="$DETECTED_CWD"
  fi
fi
CODEX_CWD="$(resolve_path "$CODEX_CWD")"
BOOTSTRAP_MODE="$(detect_bootstrap_mode)"
if [ "$BOOTSTRAP_MODE" = "remote" ]; then
  if [ -z "$BASE_URL" ] && [ -z "$MANIFEST_URL" ]; then
    die "Remote bootstrap requires INSTALLER_BASE_URL or INSTALLER_MANIFEST_URL, or pass --base-url / --manifest-url."
  fi
  if [ -z "$MANIFEST_URL" ] && [ -n "$BASE_URL" ]; then
    MANIFEST_URL="${BASE_URL%/}/manifest.json"
  fi
  command -v curl >/dev/null 2>&1 || die "curl is required for the installer."
fi
check_prereqs
warn_codex_cwd_whitespace
confirm_codex_cwd

if [ -z "$TMP_ROOT" ]; then
  TMP_ROOT="${CODEX_CWD}/.bootstrap-installer-tmp"
fi
TMP_ROOT="$("$PYTHON_BIN" - "$TMP_ROOT" <<'PY'
from pathlib import Path
import sys
print(Path(sys.argv[1]).expanduser().resolve())
PY
)"
mkdir -p "$TMP_ROOT"
trap cleanup EXIT

UPDATER_PATH=""
BANNER_PATH=""
LOCAL_SOURCE_APP_DIR=""
if [ "$BOOTSTRAP_MODE" = "local" ]; then
  LOCAL_SOURCE_APP_DIR="$(resolve_local_source_app_dir || true)"
  if [ -z "$LOCAL_SOURCE_APP_DIR" ]; then
    if [ "$MODE" = "local" ]; then
      die "Could not find a local vault-graph source directory. Pass --source-app-dir <path> or use --remote."
    fi
    warn "Local source was not found. Falling back to remote bootstrap mode."
    BOOTSTRAP_MODE="remote"
  fi
fi

if [ "$BOOTSTRAP_MODE" = "local" ]; then
  UPDATER_PATH="${LOCAL_SOURCE_APP_DIR}/scripts/self_update.py"
  [ -f "$UPDATER_PATH" ] || die "Local updater was not found: $UPDATER_PATH"
  BANNER_PATH="$(resolve_local_banner_path || true)"
  if [ -n "$BANNER_PATH" ] && [ -f "$BANNER_PATH" ]; then
    print_banner "$BANNER_PATH"
  fi
  persist_local_bootstrap_from_file
else
  UPDATER_BASE="${BASE_URL%/}"
  if [ -z "$UPDATER_BASE" ]; then
    UPDATER_BASE="${MANIFEST_URL%/*}"
  elif [ "$MANIFEST_URL" != "${BASE_URL%/}/manifest.json" ]; then
    UPDATER_BASE="${MANIFEST_URL%/*}"
  fi
  BANNER_URL="${UPDATER_BASE%/}/banner.txt"
  BANNER_PATH="${CODEX_CWD}/config/banner.txt"

  mkdir -p "${CODEX_CWD}/config"
  curl -fsSL "$BANNER_URL" -o "$BANNER_PATH" >/dev/null 2>&1 || true
  print_banner "$BANNER_PATH"

  persist_local_bootstrap_from_remote
  UPDATER_PATH="${CODEX_CWD}/config/self_update.py"
fi

CMD=(
  "$PYTHON_BIN" "$UPDATER_PATH" apply
  --version "$REQUESTED_VERSION"
  --codex-cwd "$CODEX_CWD"
  --tmp-root "$TMP_ROOT"
  --start-server
)

if [ "$BOOTSTRAP_MODE" = "local" ]; then
  CMD+=(--source-app-dir "$LOCAL_SOURCE_APP_DIR")
else
  CMD+=(--manifest-url "$MANIFEST_URL")
fi

if [ "$ASSUME_YES" = "1" ]; then
  CMD+=(--yes)
fi
if [ "$SKIP_START" = "1" ]; then
  CMD+=(--skip-start)
fi
if [ "$WITH_ADVANCED_EXTRACTION" = "1" ]; then
  CMD+=(--with-advanced-extraction)
fi

export INSTALLER_FALLBACK_HTTP_PROXY="${INSTALLER_FALLBACK_HTTP_PROXY:-${ANTIRAG_FALLBACK_HTTP_PROXY:-$DEFAULT_FALLBACK_HTTP_PROXY}}"
export INSTALLER_FALLBACK_HTTPS_PROXY="${INSTALLER_FALLBACK_HTTPS_PROXY:-${ANTIRAG_FALLBACK_HTTPS_PROXY:-$DEFAULT_FALLBACK_HTTPS_PROXY}}"
export INSTALLER_FALLBACK_NO_PROXY="${INSTALLER_FALLBACK_NO_PROXY:-${ANTIRAG_FALLBACK_NO_PROXY:-$DEFAULT_FALLBACK_NO_PROXY}}"

"${CMD[@]}"
EXIT_CODE="$?"
cleanup_downloaded_bootstrap
exit "$EXIT_CODE"
