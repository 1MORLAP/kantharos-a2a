"""Thin enablement for native ``message_agent`` on every Kantharos session.

Reuses ``tools.bot_mode_dm`` schema, delivery, and completion wake. The Bot
Chat title gate is lifted only for the duration of the native call. Sessions
are not renamed, and there is no forever-chat fallback.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .gate import (
    ATTRIBUTION_PREFIX,
    MESSAGE_AGENT_TOOL_NAME,
    build_envelope,
    ensure_schema,
    hermes_version_refusal,
    prepare_native_call,
    require_title_hint,
    restore_title_gate,
    route_reply,
    stamp_ack_ids,
    suppress_widgets,
)

logger = logging.getLogger(__name__)


def _managed(agent: Any) -> bool:
    try:
        from tools.bot_mode_probe import is_bot_mode_managed
        from tools.bot_mode_dm import _agent_home

        return bool(is_bot_mode_managed(_agent_home(agent)))
    except Exception:
        logger.debug("kantharos-a2a managed check failed", exc_info=True)
        return False


def _roster(agent: Any) -> tuple[list[str], str]:
    try:
        from pathlib import Path

        from tools.bot_mode_dm import _agent_home
        from tools.bot_mode_probe import _hermes_root, _profile_name, _roster

        home = Path(_agent_home(agent))
        root = _hermes_root(home)
        names = list(_roster(root))
        return names, _profile_name(home)
    except Exception:
        logger.debug("kantharos-a2a roster check failed", exc_info=True)
        return [], ""


def _native_schema() -> dict:
    from tools.bot_mode_dm import message_agent_tool_schema

    return message_agent_tool_schema()


def inject_agent(agent: Any) -> bool:
    if agent is None:
        return False
    try:
        return ensure_schema(agent, _native_schema(), managed=_managed(agent))
    except Exception:
        logger.debug("kantharos-a2a inject failed", exc_info=True)
        return False


def _on_session_start(**kwargs: Any) -> None:
    inject_agent(kwargs.get("agent"))


def _on_pre_llm_call(**kwargs: Any) -> None:
    inject_agent(kwargs.get("agent"))
    return None


def _on_pre_tool_call(tool_name: str = "", args: Any = None, **kwargs: Any) -> dict | None:
    """Block ADR-0012 widgets on agent-origin turns. Do not reimplement the tool."""
    author = kwargs.get("author") if isinstance(kwargs.get("author"), dict) else None
    text = ""
    if isinstance(args, dict):
        text = str(args.get("message") or args.get("prompt") or "")
    if tool_name in {"approval", "clarify", "sudo", "secret"} and suppress_widgets(author, text):
        return {"action": "block", "message": "Agent-originated turns do not ask the user. Reply to the sender instead."}
    return None


def handle_message_agent(args: dict | None = None, **kwargs: Any) -> str:
    """Guardrails, then the native tool. Reply wake stays on the calling session."""
    payload = args if isinstance(args, dict) else {}
    agent = kwargs.get("agent")
    target = str(payload.get("target") or "")
    message = str(payload.get("message") or "")
    roster, me = _roster(agent) if agent is not None else ([], "")
    plan = prepare_native_call(
        agent,
        managed=_managed(agent) if agent is not None else False,
        target=target,
        message=message,
        roster=roster,
        self_profile=me,
    )
    if not plan.get("ok"):
        return json.dumps({"error": plan.get("error") or "message_agent refused", "reason": "kantharos-a2a"})
    from tools.bot_mode_dm import message_agent_tool

    hinted = require_title_hint(agent)
    if not hinted.get("ok"):
        return json.dumps({"error": hinted.get("error") or "message_agent title gate failed closed", "reason": "kantharos-a2a"})
    try:
        raw = message_agent_tool(target=plan["target"], message=plan["message"], agent=agent)
    finally:
        restore_title_gate(agent, hinted.get("previous"))
    agent._kantharos_a2a_hop = plan["hop"]
    envelope = build_envelope(
        kind="request",
        from_profile=me,
        from_display_name=str(getattr(agent, "_kantharos_display_name", "") or me),
        from_session_id=str(getattr(agent, "session_id", "") or ""),
        to_profile=str(plan["target"]),
        on_behalf_of=str(getattr(agent, "_kantharos_on_behalf_of", "") or ""),
        message=message,
        hop=int(plan["hop"]),
    )
    try:
        ack = json.loads(raw)
    except (ValueError, TypeError):
        ack = {"status": "queued", "detail": raw}
    if isinstance(ack, dict) and ack.get("status") == "queued":
        envelope = stamp_ack_ids(envelope, ack)
        ack["kantharos_envelope"] = envelope
        if not str(ack.get("to") or "").startswith(ATTRIBUTION_PREFIX):
            ack.setdefault("to", f"@{plan['target']}")
    return json.dumps(ack)


def register(ctx: Any) -> None:
    refusal = hermes_version_refusal()
    if refusal:
        logger.error(refusal)
        return
    try:
        schema = _native_schema()
    except Exception:
        logger.debug("kantharos-a2a native schema unavailable at register", exc_info=True)
        schema = {
            "type": "function",
            "function": {
                "name": MESSAGE_AGENT_TOOL_NAME,
                "description": "Send a composed message to another agent on this install.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "target": {"type": "string"},
                        "message": {"type": "string"},
                    },
                    "required": ["target", "message"],
                },
            },
        }
    ctx.register_tool(
        name=MESSAGE_AGENT_TOOL_NAME,
        toolset="kantharos-a2a",
        schema=schema,
        handler=handle_message_agent,
        description="Native message_agent on every Kantharos profile session.",
        emoji="",
    )
    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)


__all__ = ["handle_message_agent", "inject_agent", "register", "route_reply"]
