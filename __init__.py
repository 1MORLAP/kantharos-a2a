"""kantharos-a2a: native ``message_agent`` on every Kantharos session.

At register the plugin checks the Hermes commit pin and the native gate
contract, then wraps ``tools.bot_mode_dm`` so the ``Bot Chat`` title
requirement is dropped for Bot-Mode-managed Kantharos sessions (see
``guard.py``). It registers no tool, toolset, or hook of its own.
"""

from __future__ import annotations

import logging
from typing import Any

from .contract import contract_refusal, verify_native_contract
from .guard import NativeGateWrap
from .pin import hermes_version_refusal

logger = logging.getLogger(__name__)


def register(ctx: Any) -> None:
    refusal = hermes_version_refusal()
    if refusal:
        logger.error(refusal)
        return
    refusal = contract_refusal(verify_native_contract())
    if refusal:
        logger.error(refusal)
        return
    import os

    import tools.bot_mode_dm as dm
    import tools.bot_mode_probe as probe

    if NativeGateWrap(dm, probe).install():
        logger.info(
            "kantharos-a2a: wrapped native message_agent gate (message_agent_authorized, _session_title, "
            "message_agent_tool) pid=%s; Bot Chat title requirement dropped for tui/cli/api_server "
            "sessions with bot_mode_protocol on a Bot-Mode-managed install; hop limit 3",
            os.getpid(),
        )
    else:
        logger.info("kantharos-a2a: native message_agent gate already wrapped pid=%s", os.getpid())


__all__ = ["register"]
