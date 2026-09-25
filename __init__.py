"""kantharos-a2a: native ``message_agent`` on every Kantharos session.

At register the plugin checks the Hermes commit pin and the native contracts, then
wraps ``tools.bot_mode_dm`` so the ``Bot Chat`` title requirement is dropped for
Bot-Mode-managed Kantharos sessions and every ``message_agent`` call passes the
Kantharos loop guard (see ``guard.py``). In a process that hosts UI chats it also
wraps ``tui_gateway.server._session_has_active_delegations`` so a detached session
that owns a live delivery runner is not reaped before the reply wakes it (see
``reap.py``). It registers no tool, toolset, or hook of its own.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from typing import Any

from .contract import contract_refusal, server_predicate_problem, verify_native_contract, verify_reap_contract
from .guard import NativeGateWrap, lift_applies
from .pin import hermes_version_refusal
from .reap import KNOWN_LIMIT, PREDICATE, ReapExemptionWrap, turn_isolation_refusal

logger = logging.getLogger(__name__)

_reap_lock = threading.Lock()
_reap_state: dict[str, Any] = {"wrap": None, "installed": False, "refused": False}


def install_reap_exemption() -> bool:
    """Wrap the reap predicate once this process hosts chats (tui_gateway.server loaded and bound).
    Only the ``tui_gateway.server`` binding is wrapped: both reapers resolve it there (C-1).
    Safe to call repeatedly. Returns True once installed."""
    if _reap_state["installed"] or _reap_state["refused"] or _reap_state["wrap"] is None:
        return bool(_reap_state["installed"])
    server = sys.modules.get("tui_gateway.server")
    if server is None or not hasattr(server, PREDICATE):
        return False
    with _reap_lock:
        if _reap_state["installed"] or _reap_state["refused"]:
            return bool(_reap_state["installed"])
        import tui_gateway.session_lifecycle as lifecycle

        problem = server_predicate_problem(server, lifecycle) or turn_isolation_refusal(server)
        if problem:
            _reap_state["refused"] = True
            logger.error("kantharos-a2a refused the reap exemption: %s", problem)
            return False
        _reap_state["wrap"].install(server)
        _reap_state["installed"] = True
        logger.info(
            "kantharos-a2a: wrapped native tui_gateway.server.%s pid=%s; a detached session that owns a live "
            "message_agent delivery runner is not reaped. Known limit: %s",
            PREDICATE, os.getpid(), KNOWN_LIMIT,
        )
        return True


def register(ctx: Any) -> None:
    refusal = hermes_version_refusal()
    if refusal:
        logger.error(refusal)
        return
    refusal = contract_refusal(verify_native_contract() + verify_reap_contract())
    if refusal:
        logger.error(refusal)
        return

    import tools.bot_mode_dm as dm
    import tools.bot_mode_probe as probe
    import tools.process_registry as registry_mod

    gate = NativeGateWrap(dm, probe, on_gate=install_reap_exemption)
    if _reap_state["wrap"] is None:
        _reap_state["wrap"] = ReapExemptionWrap(
            registry_mod=registry_mod,
            lift=lambda agent: lift_applies(agent, managed=gate.managed(agent)),
        )
    if gate.install():
        logger.info(
            "kantharos-a2a: wrapped native message_agent gate (message_agent_authorized, _session_title, "
            "message_agent_tool) pid=%s; Bot Chat title requirement dropped for tui/cli/api_server "
            "sessions with bot_mode_protocol on a Bot-Mode-managed install; loop guard on "
            "(reply_via_completion, duplicate 900 s, hop limit 3, pair cap 6/60 min)",
            os.getpid(),
        )
    else:
        logger.info("kantharos-a2a: native message_agent gate already wrapped pid=%s", os.getpid())
    install_reap_exemption()


__all__ = ["register", "install_reap_exemption"]
