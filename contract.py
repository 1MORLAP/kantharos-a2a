"""Loud-fail contract for the native symbols kantharos-a2a wraps.

``verify_native_contract`` runs at register. Any missing symbol, changed
signature, changed constant, or gate that no longer reads the session title the
way f97608f does is a refusal: the plugin logs the reason and wraps nothing.
"""

from __future__ import annotations

import importlib
import inspect
from typing import Any, Callable

# (module, attribute, exact parameter names, source fragments that must be present)
FUNCTIONS: tuple[tuple[str, str, tuple[str, ...], tuple[str, ...]], ...] = (
    (
        "tools.bot_mode_dm",
        "message_agent_authorized",
        ("agent",),
        ("_session_title(agent) == BOT_CHAT_TITLE", "is_bot_mode_managed(_agent_home(agent))"),
    ),
    ("tools.bot_mode_dm", "ensure_message_agent_tool", ("agent",), ("message_agent_authorized(agent)",)),
    (
        "tools.bot_mode_dm",
        "message_agent_tool",
        ("target", "message", "task_id", "agent"),
        ("_session_title(agent) != BOT_CHAT_TITLE", "is_bot_mode_managed(home)"),
    ),
    ("tools.bot_mode_dm", "_session_title", ("agent",), ("_session_title_hint",)),
    ("tools.bot_mode_dm", "_agent_home", ("agent",), ()),
    ("tools.bot_mode_dm", "_resolve_local_name", ("target", "roster", "root"), ()),
    ("tools.bot_mode_probe", "is_bot_mode_managed", ("home",), ()),
    ("tools.bot_mode_probe", "_hermes_root", ("home",), ()),
    ("tools.bot_mode_probe", "_profile_name", ("home",), ()),
    ("tools.bot_mode_probe", "_roster", ("root",), ()),
    ("tools.bot_mode_probe", "_peers", ("root",), ()),
)

CONSTANTS: tuple[tuple[str, str, Any], ...] = (
    ("tools.bot_mode_probe", "BOT_CHAT_TITLE", "Bot Chat"),
    ("tools.bot_mode_dm", "MESSAGE_AGENT_TOOL_NAME", "message_agent"),
)

# Callers that must still route through the wrapped module attributes.
MODULE_SOURCES: tuple[tuple[str, str], ...] = (
    ("agent.turn_context", "ensure_message_agent_tool(agent)"),
    ("agent.inline_tool_executors", '"tools.bot_mode_dm", "message_agent_tool"'),
)


def _source(obj: Any) -> str:
    try:
        return inspect.getsource(obj)
    except (OSError, TypeError):
        return ""


def _unwrapped(fn: Any) -> Any:
    return getattr(fn, "__kantharos_a2a_original__", fn)


def verify_native_contract(import_module: Callable[[str], Any] = importlib.import_module) -> list[str]:
    """Problems with the native gate, as readable reasons. Empty means safe to wrap."""
    problems: list[str] = []
    modules: dict[str, Any] = {}

    def module(name: str) -> Any:
        if name not in modules:
            try:
                modules[name] = import_module(name)
            except Exception as exc:
                modules[name] = None
                problems.append(f"{name} could not be imported ({type(exc).__name__}: {exc})")
        return modules[name]

    for mod_name, attr, params, fragments in FUNCTIONS:
        mod = module(mod_name)
        if mod is None:
            continue
        fn = _unwrapped(getattr(mod, attr, None))
        if not callable(fn):
            problems.append(f"{mod_name}.{attr} is missing")
            continue
        try:
            found = tuple(inspect.signature(fn).parameters)
        except (TypeError, ValueError):
            found = ()
        if found != params:
            problems.append(f"{mod_name}.{attr} signature changed: expected {params}, found {found}")
        if fragments:
            text = _source(fn)
            if not text:
                problems.append(f"{mod_name}.{attr} source is unreadable, so the gate cannot be verified")
            for fragment in fragments:
                if text and fragment not in text:
                    problems.append(f"{mod_name}.{attr} no longer contains {fragment!r}")

    for mod_name, attr, expected in CONSTANTS:
        mod = module(mod_name)
        if mod is None:
            continue
        if not hasattr(mod, attr):
            problems.append(f"{mod_name}.{attr} is missing")
        elif getattr(mod, attr) != expected:
            problems.append(f"{mod_name}.{attr} changed: expected {expected!r}, found {getattr(mod, attr)!r}")

    for mod_name, fragment in MODULE_SOURCES:
        mod = module(mod_name)
        if mod is None:
            continue
        text = _source(mod)
        if not text:
            problems.append(f"{mod_name} source is unreadable, so its message_agent call site cannot be verified")
        elif fragment not in text:
            problems.append(f"{mod_name} no longer contains {fragment!r}")

    executors = getattr(module("agent.inline_tool_executors"), "INLINE_TOOL_EXECUTORS", None)
    if module("agent.inline_tool_executors") is not None:
        if not isinstance(executors, dict) or "message_agent" not in executors:
            problems.append("agent.inline_tool_executors.INLINE_TOOL_EXECUTORS has no 'message_agent' entry")
    return problems


def contract_refusal(problems: list[str]) -> str:
    if not problems:
        return ""
    return (
        "kantharos-a2a refused to enable: the native message_agent gate changed, so nothing was wrapped. "
        + "; ".join(problems)
    )
