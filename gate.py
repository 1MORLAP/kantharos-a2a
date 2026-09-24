"""Pure guardrails for kantharos-a2a.

Native ``tools/bot_mode_dm.py`` injects ``message_agent`` only when the session
title is ``Bot Chat`` and the install is Bot-Mode-managed. Kantharos sessions
are arbitrary (ADR-0009). This module drops the title check and adds the
product guardrails. It does not launch delivery; ``__init__.py`` calls the
native tool after these checks pass.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Mapping, Optional

HOP_LIMIT = 3
MESSAGE_AGENT_TOOL_NAME = "message_agent"
BOT_CHAT_TITLE = "Bot Chat"
# NousResearch/hermes-agent main fetched 2026-09-24. Refuse any other revision.
TESTED_HERMES_COMMIT = "f97608f178d1ffeca59860195ab7da295f7c8e5f"
ATTRIBUTION_PREFIX = "Message from \U0001f916 "
SILENT_MARK = "[SILENT]"
ACK_STATUSES = frozenset({"queued", "ack", "acknowledged"})

_HINT_RE = re.compile(r"\[@([^\]]+?)\s*\u2192\s*profile:([^\]]+)\]")
_PEER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")


def session_title(agent: Any) -> str:
    hint = str(getattr(agent, "_session_title_hint", "") or "").strip()
    if hint:
        return hint
    db = getattr(agent, "_session_db", None)
    sid = getattr(agent, "session_id", None)
    getter = getattr(db, "get_session_title", None)
    if callable(getter) and sid:
        try:
            return str(getter(sid) or "").strip()
        except Exception:
            return ""
    return ""


def kantharos_authorized(agent: Any, *, managed: bool) -> bool:
    """Managed install + protocol. Session title is ignored, including ``Bot Chat``."""
    if agent is None or not managed:
        return False
    return bool(getattr(agent, "_bot_mode_protocol", True))


def ensure_schema(agent: Any, schema: Mapping[str, Any], *, managed: bool) -> bool:
    """Same inject as ``ensure_message_agent_tool``, without the title gate."""
    if not kantharos_authorized(agent, managed=managed):
        return False
    tools = getattr(agent, "tools", None)
    name = MESSAGE_AGENT_TOOL_NAME
    present = bool(tools) and any(
        isinstance(tool, dict) and (tool.get("function") or {}).get("name") == name for tool in tools
    )
    if not present:
        if getattr(agent, "tools", None) is None:
            agent.tools = []
        agent.tools.append(dict(schema))
    valid = getattr(agent, "valid_tool_names", None)
    if isinstance(valid, set):
        valid.add(name)
    return True


def is_silent_or_ack(message: str) -> bool:
    text = str(message or "").strip()
    if not text or text == SILENT_MARK:
        return True
    if text.upper() == SILENT_MARK:
        return True
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        payload = None
    if isinstance(payload, dict):
        status = str(payload.get("status") or "").strip().lower()
        if status in ACK_STATUSES and not str(payload.get("message") or "").strip():
            return True
    lowered = text.lower()
    if lowered in {"ok", "ack", "queued", "received"}:
        return True
    return False


def same_instance_target(target: str) -> Optional[str]:
    """Return an error when the target leaves this Hermes instance."""
    raw = str(target or "").strip().lstrip("@")
    if not raw:
        return "target is required."
    if _PEER_RE.match(raw) or "/" in raw:
        return "Same-instance roster only. Peer targets are not available."
    if "@" in raw:
        return "Same-instance roster only. Connection-qualified targets are not available."
    return None


def resolve_local_target(target: str, roster: list[str], self_profile: str) -> tuple[Optional[str], Optional[str]]:
    """Folder id match, case-insensitive. ``hermes`` means ``default``. Unknown and self fail."""
    scope = same_instance_target(target)
    if scope:
        return None, scope
    want = str(target or "").strip().lstrip("@").lower()
    names = [str(name) for name in roster if str(name).strip()]
    if want == "hermes":
        if "default" in names and self_profile != "default":
            return "default", None
        if "default" == self_profile:
            return None, "You can't message yourself."
        return None, "No teammate named 'hermes' on this instance."
    hits = [name for name in names if name.lower() == want]
    if len(hits) != 1:
        return None, f"No teammate named '{target}' on this instance. Pick a profile from the roster."
    if hits[0] == self_profile:
        return None, "You can't message yourself."
    return hits[0], None


def current_hop(agent: Any, message: str) -> int:
    stored = getattr(agent, "_kantharos_a2a_hop", None)
    try:
        hop = int(stored) if stored is not None else 0
    except (TypeError, ValueError):
        hop = 0
    match = re.search(r"\[kantharos-a2a hop=(\d+)\]", str(message or ""))
    if match:
        hop = max(hop, int(match.group(1)))
    return hop


def outbound_hop(agent: Any, message: str) -> tuple[int, Optional[str]]:
    """A new request consumes one hop. Refuse above 3. Replies do not call this."""
    hop = current_hop(agent, message) + 1
    if hop > HOP_LIMIT:
        return hop, f"Hop limit is {HOP_LIMIT}. This request would be hop {hop}."
    return hop, None


def build_envelope(
    *,
    kind: str,
    from_profile: str,
    from_display_name: str,
    from_session_id: str,
    to_profile: str,
    to_session_id: str = "",
    on_behalf_of: str = "",
    message: str,
    hop: int = 1,
    correlation_id: str = "",
    delivery_id: str = "",
) -> dict[str, Any]:
    return {
        "kind": kind,
        "correlation_id": correlation_id or str(uuid.uuid4()),
        "hop": int(hop),
        "from_profile": from_profile,
        "from_display_name": from_display_name,
        "from_session_id": from_session_id,
        "to_profile": to_profile,
        "to_session_id": to_session_id,
        "on_behalf_of": on_behalf_of,
        "delivery_id": delivery_id,
        "message": message,
    }


def stamp_hop(message: str, hop: int) -> str:
    body = str(message or "").strip()
    body = re.sub(r"\s*\[kantharos-a2a hop=\d+\]\s*", " ", body).strip()
    return f"{body}\n[kantharos-a2a hop={int(hop)}]".strip()


def reply_session_id(envelope: Mapping[str, Any]) -> str:
    """Reply injection target. The originating sender session, never the focused chat."""
    if str(envelope.get("kind") or "") == "reply":
        return str(envelope.get("to_session_id") or envelope.get("from_session_id") or "").strip()
    return str(envelope.get("from_session_id") or "").strip()


def stamp_ack_ids(envelope: Mapping[str, Any], ack: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    """Fill ``delivery_id`` and ``to_session_id`` once the native ack names them.

    The native ack carries ``delivery_id``. It does not carry the target Bot Chat
    session id (``process_id`` is the delivery process, not that session). A
    ``to_session_id`` / ``bot_chat_session_id`` / ``target_session_id`` field is
    copied when present and left blank otherwise.
    """
    stamped = dict(envelope)
    if not isinstance(ack, Mapping):
        return stamped
    delivery = str(ack.get("delivery_id") or "").strip()
    if delivery:
        stamped["delivery_id"] = delivery
    session = str(
        ack.get("to_session_id") or ack.get("bot_chat_session_id") or ack.get("target_session_id") or ""
    ).strip()
    if session:
        stamped["to_session_id"] = session
    return stamped


def route_reply(session_db: Any, envelope: Mapping[str, Any], text: str) -> str:
    """Append the reply onto the sender session and return that id.

    Role stays ``user`` with ``display_kind`` ``process_complete`` because that is
    the row ``tools/bot_mode_dm.py`` ``_persist_reply_when_done`` writes, and the
    native completion wake reads that shape. The client must paint it as an agent
    delivery, never a user bubble.
    """
    session_id = reply_session_id(envelope)
    if not session_id:
        raise ValueError("reply is missing from_session_id")
    append = getattr(session_db, "append_message", None)
    if not callable(append):
        raise ValueError("session store cannot append")
    content = str(text or "").strip()
    append(
        session_id,
        "user",
        content=content,
        display_kind="process_complete",
        display_metadata={"display_text": content, "kantharos_a2a": "reply"},
    )
    return session_id


def agent_origin(author: Optional[Mapping[str, Any]] = None, text: str = "", envelope: Optional[Mapping[str, Any]] = None) -> bool:
    if isinstance(author, Mapping) and author.get("is_bot") is True:
        return True
    if isinstance(envelope, Mapping) and str(envelope.get("kind") or "") in {"request", "reply"}:
        return True
    body = str(text or "")
    if body.startswith(ATTRIBUTION_PREFIX) or ATTRIBUTION_PREFIX in body[:240]:
        return True
    return False


def suppress_widgets(author: Optional[Mapping[str, Any]] = None, text: str = "", envelope: Optional[Mapping[str, Any]] = None) -> bool:
    """Agent-origin turns must not emit ADR-0012 approval, clarify, sudo, or secret."""
    return agent_origin(author, text, envelope)


WIDGETS = ("approval", "clarify", "sudo", "secret")


def prepare_native_call(agent: Any, *, managed: bool, target: str, message: str, roster: list[str], self_profile: str) -> dict[str, Any]:
    """Validate one outbound call. Does not deliver. Title is not rewritten."""
    if is_silent_or_ack(message):
        return {"ok": False, "error": "Not sending. Pure acknowledgements and [SILENT] do not message a teammate."}
    if not kantharos_authorized(agent, managed=managed):
        return {"ok": False, "error": "message_agent is unavailable on this install."}
    resolved, error = resolve_local_target(target, roster, self_profile)
    if error:
        return {"ok": False, "error": error}
    hop, hop_error = outbound_hop(agent, message)
    if hop_error:
        return {"ok": False, "error": hop_error}
    title_before = session_title(agent)
    return {
        "ok": True,
        "target": resolved,
        "message": stamp_hop(message, hop),
        "hop": hop,
        "title_before": title_before,
        "retitle": False,
    }


def hermes_install_commit() -> str:
    """HEAD of the hermes-agent tree that provides ``tools.bot_mode_dm``. Empty if unknown."""
    import subprocess
    from pathlib import Path

    try:
        import tools.bot_mode_dm as mod
    except Exception:
        return ""
    start = Path(getattr(mod, "__file__", "") or "").resolve()
    if not start:
        return ""
    for parent in (start, *start.parents):
        if not (parent / ".git").exists():
            continue
        try:
            proc = subprocess.run(
                ["git", "-C", str(parent), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except Exception:
            return ""
        if proc.returncode == 0:
            return str(proc.stdout or "").strip()
        return ""
    return ""


def hermes_version_refusal(commit: str = "") -> str:
    """Empty when this install matches the tested commit. Otherwise a fail-closed reason."""
    found = str(commit or "").strip() or hermes_install_commit()
    if not found:
        return (
            "kantharos-a2a refused to enable: the hermes-agent commit could not be read. "
            f"This plugin was tested against {TESTED_HERMES_COMMIT}."
        )
    if found != TESTED_HERMES_COMMIT:
        return (
            "kantharos-a2a refused to enable: untested hermes-agent "
            f"{found}. Tested commit is {TESTED_HERMES_COMMIT}."
        )
    return ""


def require_title_hint(agent: Any) -> dict[str, Any]:
    """Set the private hint and confirm upstream ``_session_title`` still honors it.

    If the attribute is ignored or the helper is gone, restore the hint and
    return an error. Callers must not invoke ``message_agent_tool``.
    """
    previous = lift_title_gate(agent)
    try:
        from tools.bot_mode_dm import _session_title
    except Exception as exc:
        restore_title_gate(agent, previous)
        return {
            "ok": False,
            "error": (
                "kantharos-a2a fail closed: tools.bot_mode_dm._session_title is unavailable "
                f"({exc}). message_agent was not called."
            ),
        }
    try:
        seen = str(_session_title(agent) or "").strip()
    except Exception as exc:
        restore_title_gate(agent, previous)
        return {
            "ok": False,
            "error": (
                "kantharos-a2a fail closed: reading the message_agent title gate failed "
                f"({exc}). message_agent was not called."
            ),
        }
    if seen != BOT_CHAT_TITLE:
        restore_title_gate(agent, previous)
        return {
            "ok": False,
            "error": (
                "kantharos-a2a fail closed: _session_title returned "
                f"{seen!r} after _session_title_hint was set to {BOT_CHAT_TITLE!r}. "
                "Upstream no longer honors that private attribute. message_agent was not called."
            ),
        }
    return {"ok": True, "previous": previous}


def lift_title_gate(agent: Any):
    """Let the native tool pass its title check without writing the session title.

    Upstream ``message_agent_tool`` returns an error unless ``_session_title``
    is ``Bot Chat``. The hint is restored by ``restore_title_gate``. The
    session row is not updated.
    """
    previous = getattr(agent, "_session_title_hint", None)
    agent._session_title_hint = BOT_CHAT_TITLE
    return previous


def restore_title_gate(agent: Any, previous: Any) -> None:
    if previous is None:
        try:
            delattr(agent, "_session_title_hint")
        except Exception:
            agent._session_title_hint = ""
        return
    agent._session_title_hint = previous


def native_schema_name(schema: Mapping[str, Any]) -> str:
    return str((schema.get("function") or {}).get("name") or "")
