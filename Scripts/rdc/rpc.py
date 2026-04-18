# -*- coding: utf-8 -*-
"""RPC communication layer and shared utilities for rdc-cli interaction."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
from pathlib import Path

from shared import unwrap

# ─────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────

SESSION_PREFIX = "rdc-collect"
MAIN_SESSION = f"{SESSION_PREFIX}-main"
RDC_BAT = str(Path(__file__).resolve().parent.parent.parent / "rdc-portable" / "rdc.bat")


# ─────────────────────────────────────────────────────────────────────
# RPC helpers
# ─────────────────────────────────────────────────────────────────────

def run_rdc(*args: str, session: str | None = None, timeout: int = 120) -> tuple[str, str, int]:
    """Execute a single rdc command. Returns (stdout, stderr, returncode).

    If *session* is given, the command runs within that named daemon session
    (thread-safe for parallel workers).
    """
    cmd = [RDC_BAT]
    if session:
        cmd += ["--session", session]
    cmd.extend(args)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip(), r.stderr.strip(), r.returncode
    except subprocess.TimeoutExpired:
        return "", "TIMEOUT after {}s".format(timeout), -1


def run_rdc_json(*args: str, session: str | None = None, timeout: int = 120) -> dict | list | None:
    """Execute rdc command with --json flag and return parsed JSON, or None on error."""
    stdout, stderr, rc = run_rdc(*args, "--json", session=session, timeout=timeout)
    if rc != 0 or not stdout:
        return None
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return None


def _session_file(session: str) -> Path:
    """Return session JSON path for a named rdc session."""
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path.home() / ".local" / "share"
    return base / "rdc" / "sessions" / f"{session}.json"


def _rpc_call(session: str, method: str, params: dict | None = None, timeout: float = 30.0) -> dict | None:
    """Send JSON-RPC request directly to daemon, bypassing CLI's 30s socket timeout.

    Returns the 'result' dict on success, None on error.
    """
    sf = _session_file(session)
    if not sf.exists():
        return None
    try:
        sdata = json.loads(sf.read_text())
        host, port, token = sdata["host"], int(sdata["port"]), sdata["token"]
    except (json.JSONDecodeError, KeyError, ValueError):
        return None

    payload = {
        "jsonrpc": "2.0",
        "method": method,
        "id": 1,
        "params": {"_token": token, **(params or {})},
    }
    data = (json.dumps(payload) + "\n").encode("utf-8")
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.sendall(data)
            chunks: list[bytes] = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                if b"\n" in chunk:
                    break
            if not chunks:
                return None
            line = b"".join(chunks).split(b"\n", 1)[0].decode("utf-8")
            resp = json.loads(line)
            if "error" in resp:
                return None
            return resp.get("result")
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _unwrap(data: dict | list | None, key: str) -> list | dict | None:
    """Unwrap rdc JSON output: {'draws': [...]} -> [...], or return as-is if already a list.

    Thin wrapper around shared.unwrap for single-key call pattern used in collect.
    """
    return unwrap(data, key)


# ─────────────────────────────────────────────────────────────────────
# Utility classes
# ─────────────────────────────────────────────────────────────────────

class Progress:
    """Thread-safe progress printer."""

    def __init__(self, total: int, phase: str) -> None:
        self.total = total
        self.current = 0
        self.phase = phase
        self.t0 = time.time()
        self._lock = threading.Lock()

    def tick(self, label: str = "") -> None:
        with self._lock:
            self.current += 1
            current = self.current
        elapsed = time.time() - self.t0
        rate = current / elapsed if elapsed > 0 else 0
        eta = (self.total - current) / rate if rate > 0 else 0
        pct = 100 * current / self.total if self.total else 100
        msg = f"\r  [{current}/{self.total}] {self.phase}: {label}"
        msg += f" ({pct:.0f}%, ~{eta:.0f}s left)    "
        print(msg, end="", flush=True)

    def done(self) -> float:
        elapsed = time.time() - self.t0
        print(f"\n  Done: {self.phase} - {elapsed:.1f}s")
        return elapsed


class ErrorCollector:
    """Thread-safe error accumulator."""

    def __init__(self) -> None:
        self._errors: list[dict] = []
        self._lock = threading.Lock()

    def append(self, error: dict) -> None:
        with self._lock:
            self._errors.append(error)

    @property
    def errors(self) -> list[dict]:
        with self._lock:
            return list(self._errors)

    def __len__(self) -> int:
        with self._lock:
            return len(self._errors)
