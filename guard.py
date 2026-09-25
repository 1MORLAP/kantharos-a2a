"""Load-time wrap of the native message_agent gate (ADR-0016 gate-wrap amendment).

Native ``tools.bot_mode_dm`` gates both injection (``message_agent_authorized``,
called by ``ensure_message_agent_tool``) and dispatch (``message_agent_tool``)
on ``_session_title(agent) == "Bot Chat"``. The wrappers here drop only that
title requirement, and only while one of those two gates is running, when:

- the install is Bot-Mode-managed (native ``is_bot_mode_managed``),
- ``agent._bot_mode_protocol`` is True (config ``agent.bot_mode_protocol``),
- ``agent.platform`` is ``tui``, ``cli`` or ``api_server``.

Every other native check still runs. Native Hermes keeps the schema, injection,
attribution, same-instance delivery, the reply into the sender session, and
deny mode on the target. ``message_agent_tool`` also gets the Kantharos
loop guard (ADR-0016 loop-guard revision), checked in this order:

1. no pure acks or ``[SILENT]``;
2. ``reply_via_completion``: in a turn authored by bot X (``agent._turn_author``,
   set natively at turn start), ``message_agent`` to X is refused. X's final
   answer already reaches the asker through the delivery completion;
3. same instance only;
4. duplicate: an identical (sender, target, body) send within 900 s;
5. hop limit 3;
6. pair cap: 6 admitted sends per ordered pair per rolling 60 minutes.

Refusals do not count toward the cap, and there is no retry loop here.
"""

from __future__ import annotations

import contextvars
import functools
import json
import logging
from pathlib import Path
from typing import Any, Callable, Optional

from .hops import (
    PAIR_CAP, HopLedger, SendLedger, ack_keys, body_digest, body_key, duplicate_refusal, hop_refusal,
    inbound_hop, latest_user_text, pair_cap_refusal,
)

logger = logging.getLogger(__name__)

BOT_CHAT_TITLE = "Bot Chat"
LIFT_PLATFORMS = frozenset({"tui", "cli", "api_server"})
ACK_STATUSES = frozenset({"queued", "claimed", "settled"})
SILENT_MARK = "[SILENT]"

_IN_GATE: contextvars.ContextVar[bool] = contextvars.ContextVar("kantharos_a2a_in_gate", default=False)


def lift_applies(agent: Any, *, managed: bool) -> bool:
    """All three conditions for dropping the Bot Chat title requirement."""
    if agent is None or not managed:
        return False
    if getattr(agent, "_bot_mode_protocol", None) is not True:
        return False
    platform = str(getattr(agent, "platform", "") or "").strip().lower()
    return platform in LIFT_PLATFORMS


def is_silent_or_ack(message: str) -> bool:
    text = str(message or "").strip()
    if not text or text.upper() == SILENT_MARK:
        return True
    if text.lower() in {"ok", "ack", "queued", "received"}:
        return True
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return False
    if isinstance(payload, dict):
        status = str(payload.get("status") or "").strip().lower()
        return status in ACK_STATUSES | {"ack", "acknowledged"} and not str(payload.get("message") or "").strip()
    return False


def error_json(message: str, reason: str) -> str:
    return json.dumps({"error": message, "reason": reason})


def parse_bot_author(author: Any) -> Optional[tuple[str, str]]:
    """``(origin, profile)`` for a bot turn author: ``bot:<profile>`` is ``("", profile)``;
    ``bot:<connection>/<profile>`` and ``bot:<hostname>/<profile>`` carry the origin. None otherwise."""
    if not isinstance(author, dict) or not author.get("is_bot"):
        return None
    raw = str(author.get("id") or "").strip()
    if not raw.startswith("bot:") or len(raw) <= 4:
        return None
    rest = raw[4:]
    origin, _, profile = rest.rpartition("/")
    profile = profile.strip()
    return (origin.strip(), profile) if profile else None


def _target_origin_profile(raw: str) -> Optional[tuple[str, str]]:
    if "/" in raw:
        origin, _, profile = raw.partition("/")
    elif "@" in raw:
        profile, _, origin = raw.rpartition("@")
    else:
        return None
    return (origin.strip(), profile.strip()) if origin.strip() and profile.strip() else None


def reply_target(author: Any, target: str, resolve: Callable[[str], Optional[str]]) -> str:
    """The author's name when ``target`` resolves to the bot that authored this turn, else "".
    Same profile and, for a relayed or peer author, the same connection or peer."""
    parsed = parse_bot_author(author)
    raw = str(target or "").strip().lstrip("@")
    if parsed is None or not raw:
        return ""
    origin, profile = parsed
    name = str((author or {}).get("name") or profile)
    if not origin:
        if _target_origin_profile(raw) is not None:
            return ""
        resolved = resolve(raw)
        hit = resolved == profile if resolved is not None else raw.lower() == profile.lower()
        return name if hit else ""
    other = _target_origin_profile(raw)
    if other is None:
        return ""
    return name if (other[0].lower(), other[1].lower()) == (origin.lower(), profile.lower()) else ""


def reply_refusal(name: str) -> str:
    return f"Your final answer is delivered to {name} automatically. Do not call message_agent to reply."


def target_refusal(target: str, *, roster: list[str], peers: list[str], me: str,
                   resolve: Callable[[str], Optional[str]]) -> tuple[str, str]:
    """(message, reason) when ``target`` is not one teammate profile on this instance."""
    raw = str(target or "").strip().lstrip("@")
    if not raw:
        return "target is required.", "unknown_target"
    if "/" in raw or "@" in raw or raw.lower() in {p.lower() for p in peers}:
        return ("Same instance only: peer gateways and other connected machines are not reachable. "
                "Pick a teammate from this instance."), "not_same_instance"
    resolved = resolve(raw)
    if resolved is None:
        return (f"No teammate named '{raw}' on this instance. Pick a teammate from the roster. "
                "Do not substitute delegate_task or a subagent."), "unknown_target"
    if resolved == me:
        return "You can't message yourself. Pick a teammate from the roster.", "self_target"
    return "", ""


class NativeGateWrap:
    """Installs and removes the wrappers on ``tools.bot_mode_dm``."""

    def __init__(self, dm: Any, probe: Any, on_gate: Optional[Callable[[], None]] = None):
        self.dm = dm
        self.probe = probe
        self.originals: dict[str, Any] = {}
        # Called whenever a gate runs: the chat host installs its reap exemption lazily.
        self.on_gate = on_gate

    def _touch(self) -> None:
        if self.on_gate is None:
            return
        try:
            self.on_gate()
        except Exception:
            logger.warning("kantharos-a2a: on_gate hook failed", exc_info=True)

    # native helpers -------------------------------------------------------
    def _home(self, agent: Any) -> str:
        return str(self.dm._agent_home(agent))

    def managed(self, agent: Any) -> bool:
        try:
            return bool(self.probe.is_bot_mode_managed(self._home(agent)))
        except Exception:
            logger.debug("kantharos-a2a managed check failed", exc_info=True)
            return False

    def _data_dir(self, agent: Any) -> Path:
        root = Path(self.probe._hermes_root(Path(self._home(agent))))
        return root / "plugin-data" / "kantharos-a2a"

    def ledger(self, agent: Any) -> HopLedger:
        return HopLedger(self._data_dir(agent))

    def sends(self, agent: Any) -> SendLedger:
        return SendLedger(self._data_dir(agent))

    # wrappers -------------------------------------------------------------
    def _session_title(self, original: Callable[[Any], str]) -> Callable[[Any], str]:
        @functools.wraps(original)
        def wrapped(agent: Any) -> str:
            title = original(agent)
            if title == BOT_CHAT_TITLE or not _IN_GATE.get():
                return title
            if lift_applies(agent, managed=self.managed(agent)):
                return BOT_CHAT_TITLE
            return title

        return wrapped

    def _authorized(self, original: Callable[[Any], bool]) -> Callable[[Any], bool]:
        @functools.wraps(original)
        def wrapped(agent: Any) -> bool:
            self._touch()
            token = _IN_GATE.set(True)
            try:
                allowed = bool(original(agent))
            finally:
                _IN_GATE.reset(token)
            if allowed and not getattr(agent, "_kantharos_a2a_logged", False):
                title = self.originals["_session_title"](agent)
                if title != BOT_CHAT_TITLE:
                    try:
                        agent._kantharos_a2a_logged = True
                    except Exception:
                        pass
                    logger.info(
                        "kantharos-a2a: message_agent injected (Bot Chat title gate lifted) session=%s platform=%s title=%r",
                        getattr(agent, "session_id", ""), getattr(agent, "platform", ""), title,
                    )
            return allowed

        return wrapped

    def _tool(self, original: Callable[..., str]) -> Callable[..., str]:
        @functools.wraps(original)
        def wrapped(target: str = "", message: str = "", task_id: Optional[str] = None, agent: Any = None) -> str:
            self._touch()
            session = getattr(agent, "session_id", "")
            if is_silent_or_ack(message):
                logger.info("kantharos-a2a: refused message_agent session=%s reason=silent_ack", session)
                return error_json("Not sent: pure acknowledgements and [SILENT] do not message a teammate.",
                                  "silent_ack")
            try:
                home = Path(self._home(agent))
                root = self.probe._hermes_root(home)
                roster = [name for name, _ in self.probe._roster(root)]
                peers = list(self.probe._peers(root))
                me = self.probe._profile_name(home)

                def resolve(raw: str) -> Optional[str]:
                    return self.dm._resolve_local_name(raw, roster, root)

                author = getattr(agent, "_turn_author", None)
                reply_to = reply_target(author, target, resolve)
                problem, reason = target_refusal(target, roster=roster, peers=peers, me=me, resolve=resolve)
                resolved = resolve(str(target or "").strip().lstrip("@")) if not problem else None
                ledger = self.ledger(agent)
                sends = self.sends(agent)
                hop_in, source = inbound_hop(latest_user_text(agent), ledger.lookup)
            except Exception as exc:
                logger.warning("kantharos-a2a: refused message_agent session=%s reason=guard_error %s", session, exc)
                return error_json(f"kantharos-a2a could not check this message ({exc}). It was not sent.",
                                  "guard_error")
            if reply_to:
                logger.warning(
                    "kantharos-a2a: refused message_agent reason=reply_via_completion session=%s profile=%s "
                    "target=%r author=%s", session, me, target, (author or {}).get("id"))
                return error_json(reply_refusal(reply_to), "reply_via_completion")
            if problem:
                logger.info("kantharos-a2a: refused message_agent session=%s target=%r reason=%s",
                            session, target, reason)
                return error_json(problem, reason)
            digest = body_digest(message)
            try:
                pair_count, dup_age = sends.check(me, resolved, digest)
            except Exception as exc:
                logger.warning("kantharos-a2a: refused message_agent session=%s reason=guard_error %s", session, exc)
                return error_json(f"kantharos-a2a could not check this message ({exc}). It was not sent.",
                                  "guard_error")
            refusal = duplicate_refusal(dup_age, f"@{resolved}")
            if refusal:
                logger.warning("kantharos-a2a: refused message_agent reason=duplicate age=%ds session=%s "
                               "profile=%s target=%s", int(dup_age or 0), session, me, resolved)
                return error_json(refusal, "duplicate")
            hop = hop_in + 1
            refusal = hop_refusal(hop)
            if refusal:
                logger.warning(
                    "kantharos-a2a: refused message_agent hop=%d limit=3 session=%s profile=%s target=%r (%s)",
                    hop, session, me, target, source,
                )
                return error_json(refusal, "hop_limit")
            refusal = pair_cap_refusal(pair_count, f"@{resolved}")
            if refusal:
                logger.warning("kantharos-a2a: refused message_agent reason=pair_cap count=%d limit=%d "
                               "session=%s profile=%s target=%s", pair_count, PAIR_CAP, session, me, resolved)
                return error_json(refusal, "pair_cap")
            token = _IN_GATE.set(True)
            try:
                raw = original(target=target, message=message, task_id=task_id, agent=agent)
            finally:
                _IN_GATE.reset(token)
            try:
                ack = json.loads(raw)
            except (ValueError, TypeError):
                ack = None
            if isinstance(ack, dict) and not ack.get("error") and str(ack.get("status") or "") in ACK_STATUSES:
                try:
                    ledger.record([body_key(message), *ack_keys(ack)], hop)
                    sends.admit(me, resolved, digest, str(ack.get("delivery_id") or ""))
                except Exception:
                    logger.warning("kantharos-a2a: loop-guard ledger write failed", exc_info=True)
                ack["hop"] = hop
                logger.info(
                    "kantharos-a2a: message_agent %s hop=%d pair=%d/%d session=%s profile=%s to=%s delivery_id=%s "
                    "process_id=%s (%s)",
                    ack.get("status"), hop, pair_count + 1, PAIR_CAP, session, me, ack.get("to"),
                    ack.get("delivery_id"), ack.get("process_id", ""), source,
                )
                return json.dumps(ack)
            logger.info("kantharos-a2a: native message_agent returned no ack session=%s: %s",
                        session, str(raw)[:300])
            return raw

        return wrapped

    # install --------------------------------------------------------------
    def install(self) -> bool:
        """Wrap once. Returns False when this module is already wrapped."""
        if getattr(self.dm.message_agent_tool, "__kantharos_a2a_original__", None) is not None:
            return False
        for name, factory in (
            ("_session_title", self._session_title),
            ("message_agent_authorized", self._authorized),
            ("message_agent_tool", self._tool),
        ):
            original = getattr(self.dm, name)
            self.originals[name] = original
            wrapped = factory(original)
            wrapped.__kantharos_a2a_original__ = original
            setattr(self.dm, name, wrapped)
        return True

    def uninstall(self) -> None:
        for name, original in self.originals.items():
            setattr(self.dm, name, original)
        self.originals.clear()
