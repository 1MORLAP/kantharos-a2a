"""Hop accounting for message_agent chains (hop limit 3).

A user turn is hop 0. Each message_agent send is the caller's hop plus one.
Every queued send is written to a small ledger on the instance, keyed by the
message body and by the ids in the native ack, so a later turn can find its
own hop:

- a teammate turn whose user row is the native ``Message from 🤖 … (@…): body``
  looks up that body;
- a sender turn woken by the delivery's completion notification
  (``Background process <id> …``) looks up that process id.

An agent-originated turn the ledger does not know is counted as hop 1.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

HOP_LIMIT = 3
LEDGER_TTL_SECONDS = 24 * 60 * 60
LEDGER_MAX_ENTRIES = 2000

_DM_RE = re.compile(r"Message from 🤖 [^\n]*?\(@[^)\s]+\): ", re.S)
_PROC_RE = re.compile(r"Background process ([A-Za-z0-9_.:-]+)")


def body_key(body: str) -> str:
    digest = hashlib.sha256(str(body or "").strip().encode("utf-8")).hexdigest()
    return f"body:{digest}"


def ack_keys(ack: Any) -> list[str]:
    keys: list[str] = []
    if isinstance(ack, dict):
        for field, prefix in (("delivery_id", "delivery"), ("process_id", "proc")):
            value = str(ack.get(field) or "").strip()
            if value:
                keys.append(f"{prefix}:{value}")
    return keys


def inbound_hop(text: str, lookup: Callable[[str], Optional[int]]) -> tuple[int, str]:
    """Hop of the turn whose triggering user row is ``text``, and how it was found."""
    body = str(text or "")
    match = _DM_RE.search(body)
    if match:
        found = lookup(body_key(body[match.end():]))
        if found is not None:
            return int(found), "teammate message"
        return 1, "teammate message not in ledger"
    hops = [lookup(f"proc:{pid}") for pid in _PROC_RE.findall(body)]
    hops = [int(h) for h in hops if h is not None]
    if hops:
        return max(hops), "teammate reply"
    return 0, "user turn"


def hop_refusal(hop: int) -> str:
    if hop <= HOP_LIMIT:
        return ""
    return (
        f"Hop limit reached: this message would be hop {hop} and the limit is {HOP_LIMIT}. "
        "It was not sent. Do not retry; tell the user the teammate chain stopped here."
    )


def message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
            elif isinstance(part, str):
                parts.append(part)
        return "".join(parts)
    return ""


def latest_user_text(agent: Any) -> str:
    """The current turn's triggering user row, from the session store. Empty if unknown."""
    override = getattr(agent, "_persist_user_message_override", None)
    if isinstance(override, str) and override.strip():
        return override
    db = getattr(agent, "_session_db", None)
    sid = getattr(agent, "session_id", None)
    getter = getattr(db, "get_messages", None)
    if not callable(getter) or not sid:
        return ""
    try:
        rows = getter(sid, latest=True, limit=12)
    except TypeError:
        rows = getter(sid)
    except Exception:
        return ""
    for row in reversed(list(rows or [])):
        if isinstance(row, dict) and row.get("role") == "user":
            return message_text(row.get("content"))
    return ""


class HopLedger:
    """JSON ledger under ``<hermes root>/plugin-data/kantharos-a2a``. Holds hashes and ids only."""

    def __init__(self, directory: Path, *, now: Callable[[], float] = time.time):
        self.directory = Path(directory)
        self.path = self.directory / "hops.json"
        self._now = now

    @contextlib.contextmanager
    def _locked(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = self.directory / ".hops.lock"
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX)
            except ImportError:  # pragma: no cover - non-POSIX
                pass
            yield
        finally:
            os.close(fd)

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _fresh(self, data: dict) -> dict:
        cutoff = self._now() - LEDGER_TTL_SECONDS
        rows = {k: v for k, v in data.items() if isinstance(v, list) and len(v) == 2 and v[1] >= cutoff}
        if len(rows) > LEDGER_MAX_ENTRIES:
            keep = sorted(rows.items(), key=lambda item: item[1][1])[-LEDGER_MAX_ENTRIES:]
            rows = dict(keep)
        return rows

    def lookup(self, key: str) -> Optional[int]:
        with self._locked():
            row = self._fresh(self._read()).get(key)
        return int(row[0]) if row else None

    def record(self, keys: Iterable[str], hop: int) -> None:
        keys = [k for k in keys if k]
        if not keys:
            return
        with self._locked():
            data = self._fresh(self._read())
            stamp = self._now()
            for key in keys:
                data[key] = [int(hop), stamp]
            tmp = self.path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream)
            os.replace(tmp, self.path)
