"""Reap exemption for live message_agent delivery runners (ADR-0016 reap-exemption amendment).

Native tui_gateway reaps a detached UI session ``ws_orphan_reap_grace_s`` after its
client leaves, and closing the agent kills every background process the session
owns. That includes the ``message_agent`` delivery runner, so a teammate's reply
never wakes a sender whose app was closed. Native exempts a session from the reap
only while ``_session_has_active_delegations`` is true (async delegations).

``session_lifecycle.register`` and ``session_reaper.register`` rebind their helpers
onto ``tui_gateway.server`` (``method_ctx.bind_module``), so the live ``_reap`` and
``_session_is_lru_evictable`` both resolve the predicate from ``tui_gateway.server``.
That single binding is wrapped (C-1). The wrap is also true while the UI session
owns a live native delivery runner, meaning all of these hold (C-2..C-4):

- the process is in ``process_registry.running_owned_by(session["session_key"])``
  (TUI turns register processes with owner_task_id = session_key);
- ``notify_on_complete`` is true and it has not exited;
- its argv carries ``bot_mode_dm.py --run-delivery`` (local runner) or
  ``bot_mode_dm.py --wait-reply`` (relay waiter);
- it is younger than 1440 s (runner) or 3240 s (waiter).

The gating matches the gate-wrap: Bot-Mode-managed install, ``bot_mode_protocol``
on, platform tui / cli / api_server. With ``dashboard.turn_isolation`` on, runners
live in a child registry this wrap cannot see, so the plugin does not install it
(C-5). Native Hermes still owns the reap, the wake and the reply. Known limit: a
gateway restart mid-delivery still loses the wake; there is no workaround here.
"""

from __future__ import annotations

import functools
import logging
import shlex
import time
from pathlib import PurePath
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

PREDICATE = "_session_has_active_delegations"
DELIVERY_MODES = frozenset({"--run-delivery", "--wait-reply"})
# C-4 age caps: local runner 1440 s; relay waiter 3240 s (REPLY_WAIT_SECONDS ~3120 + headroom).
AGE_CAP_SECONDS = {"--run-delivery": 1440.0, "--wait-reply": 3240.0}
KNOWN_LIMIT = ("a gateway restart mid-delivery still loses the wake (the runner dies with its host process); "
               "no workaround")


def delivery_mode(command: str) -> str:
    """``--run-delivery`` / ``--wait-reply`` when ``command`` runs the native tools/bot_mode_dm.py runner."""
    text = str(command or "")
    try:
        parts = shlex.split(text)
    except ValueError:
        parts = text.split()
    for i, part in enumerate(parts[:-1]):
        path = PurePath(part)
        if path.name == "bot_mode_dm.py" and path.parent.name == "tools" and parts[i + 1] in DELIVERY_MODES:
            return parts[i + 1]
    return ""


class ReapExemptionWrap:
    """Wraps the reap-exemption predicate in the namespaces the reapers resolve it from."""

    def __init__(self, *, registry_mod: Any, lift: Callable[[Any], bool], now: Callable[[], float] = time.time):
        self.registry_mod = registry_mod
        self.lift = lift
        self.now = now
        self.originals: dict[int, tuple[Any, Any]] = {}
        self._logged: set[str] = set()

    def live_delivery(self, session: Optional[dict]) -> Optional[Any]:
        """The first live native delivery runner the UI session owns, else None."""
        if not isinstance(session, dict):
            return None
        agent = session.get("agent")
        if agent is None or not self.lift(agent):
            return None
        owner = str(session.get("session_key") or "")
        if not owner:
            return None
        now = self.now()
        for proc in self.registry_mod.process_registry.running_owned_by(owner):
            if getattr(proc, "owner_task_id", None) != owner:
                continue
            if not getattr(proc, "notify_on_complete", False) or getattr(proc, "exited", True):
                continue
            mode = delivery_mode(getattr(proc, "command", ""))
            if not mode:
                continue
            started = float(getattr(proc, "started_at", 0.0) or 0.0)
            if not started or now - started > AGE_CAP_SECONDS[mode]:
                continue
            return proc
        return None

    def _wrap(self, original: Callable[..., bool], sessions: Callable[[str], Optional[dict]]) -> Callable[..., bool]:
        @functools.wraps(original)
        def wrapped(sid: str, session: dict | None = None) -> bool:
            if original(sid, session):
                return True
            try:
                current = session if session is not None else sessions(sid)
                proc = self.live_delivery(current)
            except Exception:
                logger.debug("kantharos-a2a reap exemption check failed for %s", sid, exc_info=True)
                return False
            if proc is None:
                return False
            key = f"{sid}:{getattr(proc, 'id', '')}"
            if key not in self._logged:
                self._logged.add(key)
                logger.info(
                    "kantharos-a2a: kept detached session %s alive for message_agent delivery runner %s "
                    "(reap exemption; native wake delivers the reply)", sid, getattr(proc, "id", ""))
            return True

        return wrapped

    # install --------------------------------------------------------------
    def install(self, *namespaces: Any) -> int:
        """Wrap ``PREDICATE`` on each namespace (module) once. Returns how many were newly wrapped."""
        done = 0
        for ns in namespaces:
            if ns is None:
                continue
            original = getattr(ns, PREDICATE)
            if getattr(original, "__kantharos_a2a_original__", None) is not None:
                continue

            def sessions(sid: str, _ns: Any = ns) -> Optional[dict]:
                lock, table = getattr(_ns, "_sessions_lock", None), getattr(_ns, "_sessions", None)
                if lock is None or not isinstance(table, dict):
                    return None
                with lock:
                    return table.get(sid)

            wrapped = self._wrap(original, sessions)
            wrapped.__kantharos_a2a_original__ = original
            setattr(ns, PREDICATE, wrapped)
            self.originals[id(ns)] = (ns, original)
            done += 1
        return done

    def uninstall(self) -> None:
        for ns, original in self.originals.values():
            setattr(ns, PREDICATE, original)
        self.originals.clear()


def turn_isolation_refusal(server: Any) -> str:
    """C-5: with ``dashboard.turn_isolation`` on, turns run in a compute-host child whose process
    registry this wrap cannot see. Refuse (loudly) instead of wrapping a predicate that never fires."""
    loader = getattr(server, "_load_dashboard_process_isolation_config", None)
    if not callable(loader):
        return ("tui_gateway.server._load_dashboard_process_isolation_config is missing, so "
                "dashboard.turn_isolation cannot be checked")
    try:
        on = bool((loader() or {}).get("turn_isolation"))
    except Exception as exc:
        return f"dashboard.turn_isolation could not be read ({type(exc).__name__}: {exc})"
    if on:
        return ("dashboard.turn_isolation is on: delivery runners live in the compute-host child, so the "
                "app-closed reply wake is not supported. Turn dashboard.turn_isolation off to use it")
    return ""
