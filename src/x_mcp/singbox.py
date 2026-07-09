"""Singbox management: download binary, build config, start/stop process.

Used by set_proxy() to turn non-HTTP proxy protocols (trojan/anytls/ss/etc)
into a local HTTP proxy that twikit/httpx can consume directly.

Design:
- Binary downloaded to ~/.x-mcp/singbox-bin/sing-box on first use and kept
  cached for reuse; set_proxy(None) or MCP server exit does not delete it.
- Config file is tmp, deleted in stop_singbox() (not immediately after
  Popen — sing-box may still be reading it).
- HTTP inbound listens on 127.0.0.1:0 (OS picks a free port); we read
  the actual port from singbox's stderr.
- Process is killed on cleanup: terminate() -> 5s -> kill().

Download: uses httpx (already a transitive dep via twikit) instead of
urllib.request — handles GitHub's 302 redirects and TLS more reliably.
"""
from __future__ import annotations

import json
import logging
import os
import platform
import re
import select
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

SINGBOX_VERSION = "1.13.14"
SINGBOX_DIR = Path.home() / ".x-mcp" / "singbox-bin"
SINGBOX_BIN = SINGBOX_DIR / "sing-box"

# Two download sources, tried in order.
GITHUB_RELEASES = "https://github.com/SagerNet/sing-box/releases/download"
# Common GitHub mirrors that mirror release assets. We try them in order if
# github.com is unreachable.
MIRRORS = [
    "https://github.moeyy.xyz/https://github.com/SagerNet/sing-box/releases/download",
    "https://gh-proxy.com/https://github.com/SagerNet/sing-box/releases/download",
    "https://ghproxy.net/https://github.com/SagerNet/sing-box/releases/download",
]

# In-process state. _proc is the running singbox subprocess; _is_managed
# records whether we downloaded the binary ourselves (so we know it's safe
# to delete on cleanup). _cfg_path is the temp config file, cleaned up
# alongside the process in stop_singbox() — we can't delete it immediately
# after Popen because sing-box may still be reading it (Popen is async).
_proc: Optional[subprocess.Popen] = None
_is_managed: bool = False
_active_proxy: Optional[str] = None  # e.g. "http://127.0.0.1:54321"
_cfg_path: Optional[str] = None


def _platform_asset() -> str:
    """Return the asset name for the current platform, e.g. 'darwin-arm64'."""
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "darwin":
        arch = "arm64" if machine in ("arm64", "aarch64") else "amd64"
        return f"darwin-{arch}"
    if system == "linux":
        arch = "arm64" if machine in ("arm64", "aarch64") else "amd64"
        return f"linux-{arch}"
    if system == "windows":
        arch = "arm64" if machine in ("arm64", "aarch64") else "amd64"
        return f"windows-{arch}"
    raise RuntimeError(f"Unsupported platform: {system}/{machine}")


def _download_urls(ver: str, asset: str) -> list[str]:
    """Build candidate download URLs (github + mirrors)."""
    fname = f"sing-box-{ver}-{asset}.tar.gz"
    urls = [f"{GITHUB_RELEASES}/v{ver}/{fname}"]
    for m in MIRRORS:
        urls.append(f"{m}/v{ver}/{fname}")
    return urls


def _download_binary() -> Path:
    """Download and extract sing-box binary. Returns path to the binary."""
    if SINGBOX_BIN.exists():
        return SINGBOX_BIN

    asset = _platform_asset()
    urls = _download_urls(SINGBOX_VERSION, asset)
    SINGBOX_DIR.mkdir(parents=True, exist_ok=True)

    last_err: Optional[str] = None
    for url in urls:
        try:
            logger.info(f"[singbox] downloading from {url}")
            with httpx.Client(follow_redirects=True, timeout=120.0) as http:
                resp = http.get(url)
                resp.raise_for_status()
                data = resp.content
            tmp_tar = SINGBOX_DIR / "sing-box.tar.gz"
            tmp_tar.write_bytes(data)
            with tarfile.open(tmp_tar, "r:gz") as tar:
                # Find the sing-box binary inside the archive.
                member = next(
                    (m for m in tar.getmembers() if m.name.endswith("sing-box") and m.isfile()),
                    None,
                )
                if member is None:
                    raise RuntimeError("sing-box binary not found in archive")
                # Extract to a tmp name to avoid path traversal issues.
                member.name = "sing-box"
                tar.extract(member, SINGBOX_DIR)
            tmp_tar.unlink()
            os.chmod(SINGBOX_BIN, 0o755)
            logger.info(f"[singbox] binary at {SINGBOX_BIN}")
            return SINGBOX_BIN
        except Exception as e:
            last_err = f"{url}: {e}"
            logger.warning(f"[singbox] download failed {last_err}")
            continue

    raise RuntimeError(
        f"Failed to download sing-box binary from all sources. "
        f"Last error: {last_err}. Please install manually: "
        f"brew install sing-box (macOS) or download from "
        f"https://github.com/SagerNet/sing-box/releases and put it in PATH."
    )


def _find_system_singbox() -> Optional[str]:
    """Find sing-box in PATH if already installed."""
    return shutil.which("sing-box")


def _resolve_binary() -> tuple[str, bool]:
    """Return (binary path, is_managed). Tries PATH first, then downloads."""
    global _is_managed
    system_bin = _find_system_singbox()
    if system_bin:
        _is_managed = False
        return system_bin, False
    binary = str(_download_binary())
    _is_managed = True
    return binary, True


def _build_config(outbound: dict) -> tuple[dict, int]:
    """Build a full singbox config wrapping the user's outbound.

    Returns (config_dict, listen_port). listen_port=0 means OS picks a port.
    """
    if "tag" not in outbound:
        outbound["tag"] = "proxy"
    tag = outbound["tag"]

    config = {
        "inbounds": [{
            "type": "http",
            "tag": "http-in",
            "listen": "127.0.0.1",
            "listen_port": 0,
        }],
        "outbounds": [outbound, {"type": "direct", "tag": "direct"}],
        "route": {
            "rules": [{"action": "route", "outbound": tag}],
            "final": tag,
        },
    }
    return config, 0


def _read_port_from_stderr(proc: subprocess.Popen, timeout: float = 30.0) -> int:
    """Read the actual listen port from singbox stderr.

    We read directly from the raw file descriptor with os.read(). Using the
    buffered reader's read1() can return empty bytes on some platforms even
    when select() says the fd is readable, causing the port detection to time
    out even though sing-box has started successfully.

    Timeout is generous (30s) because some deployment machines are slow or
    remote, and because the first download may have just finished.
    """
    fd = proc.stderr.fileno()
    start = time.time()
    buf = b""
    while time.time() - start < timeout:
        if proc.poll() is not None:
            remaining = b""
            try:
                remaining = os.read(fd, 65536)
            except OSError:
                pass
            raise RuntimeError(
                f"sing-box exited early. stderr: {(buf + remaining).decode(errors='replace')}"
            )
        # Read directly from the raw fd; the buffered reader's read1() is
        # unreliable on pipes across platforms.
        r, _, _ = select.select([fd], [], [], 0.5)
        if r:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                chunk = b""
            buf += chunk
            text = buf.decode(errors="replace")
            # Strip ANSI colour codes and log useful lines for debugging.
            for line in text.splitlines():
                line_clean = re.sub(r"\x1b\[[0-9;]*m", "", line).strip()
                if line_clean and any(k in line_clean for k in ("FATAL", "ERROR", "listen", "started", "sing-box started")):
                    logger.info("[singbox] %s", line_clean)
            # sing-box 1.13+ logs like:
            # INFO[0000] inbound/http[http-in]: tcp server started at 127.0.0.1:54321
            m = re.search(r"inbound/http\[[^\]]+\]: tcp server started at .*:(\d+)", text)
            if not m:
                m = re.search(r"listen[_=](?:address=127\.0\.0\.1[_=])?(?:port=|:)(\d+)", text)
            if not m:
                m = re.search(r"127\.0\.0\.1:(\d+)", text)
            if m:
                return int(m.group(1))
    raise RuntimeError(
        f"sing-box HTTP port not ready in {timeout}s. stderr so far: "
        f"{buf.decode(errors='replace')}"
    )


def start_singbox(outbound_json: str) -> str:
    """Start singbox with the given outbound config. Returns proxy URL.

    outbound_json: JSON string of a singbox outbound (trojan/anytls/ss/etc).
    """
    global _proc, _is_managed, _active_proxy

    # If something is already running, stop it first.
    if _proc is not None:
        stop_singbox()

    try:
        outbound = json.loads(outbound_json)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Invalid outbound JSON: {e}") from e
    if not isinstance(outbound, dict):
        raise RuntimeError("outbound must be a JSON object")

    config, _ = _build_config(outbound)

    binary, _is_managed = _resolve_binary()

    # Write config to tmp file. NOTE: we cannot delete it right after
    # Popen because subprocess.Popen returns immediately — sing-box may
    # still be reading the file. Instead, keep the path in _cfg_path and
    # clean it up in stop_singbox() (or on MCP server exit via atexit).
    global _cfg_path
    fd, cfg_path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(config, f)
    _cfg_path = cfg_path

    _proc = subprocess.Popen(
        [binary, "run", "-c", cfg_path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        bufsize=0,
    )

    try:
        port = _read_port_from_stderr(
            _proc,
            timeout=float(os.environ.get("X_MCP_SINGBOX_START_TIMEOUT", "30.0")),
        )
    except Exception:
        stop_singbox()
        raise

    _active_proxy = f"http://127.0.0.1:{port}"
    logger.info(f"[singbox] proxy at {_active_proxy}")
    return _active_proxy


def stop_singbox() -> None:
    """Stop singbox process + delete config file.

    We keep the downloaded binary in ~/.x-mcp/singbox-bin/ so that the next
    set_proxy() call does not have to re-download it. If you need to force a
    re-download, delete that directory manually.
    """
    global _proc, _is_managed, _active_proxy, _cfg_path
    if _proc is not None:
        try:
            _proc.terminate()
            try:
                _proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _proc.kill()
                _proc.wait(timeout=2)
        except Exception as e:
            logger.warning(f"[singbox] failed to stop process cleanly: {e}")
        _proc = None

    if _is_managed and SINGBOX_BIN.exists():
        # Keep the managed binary around as a cache; re-downloading on every
        # get_cookie() cycle is too slow on machines with poor GitHub connectivity.
        logger.info(f"[singbox] keeping managed binary at {SINGBOX_BIN}")
    _is_managed = False

    if _cfg_path is not None:
        try:
            os.unlink(_cfg_path)
        except OSError:
            pass
        _cfg_path = None

    _active_proxy = None


def get_active_proxy() -> Optional[str]:
    """Return the proxy URL of the currently running singbox, or None."""
    return _active_proxy
