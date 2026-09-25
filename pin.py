"""Hermes commit pin for kantharos-a2a.

The plugin wraps private upstream symbols, so it only enables on the exact
hermes-agent commit it was tested against. The live commit comes from the git
checkout next to ``tools.bot_mode_dm`` when there is one, else from Hermes's
baked build SHA (``.hermes_build_sha``, the value the gateway stamps as
``gateway_state.code_sha``). Anything else refuses.
"""

from __future__ import annotations

# NousResearch/hermes-agent, the live code_sha on the connected instance (2026-09-24).
TESTED_HERMES_COMMIT = "f97608f178d1ffeca59860195ab7da295f7c8e5f"


def _git_install_commit() -> str:
    """HEAD of a source checkout that provides ``tools.bot_mode_dm``. Empty if none."""
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


def _stamped_install_commit() -> str:
    """Hermes's own code identity. Docker images drop ``.git`` and bake ``.hermes_build_sha``."""
    try:
        from hermes_cli.build_info import get_code_identity

        sha = str((get_code_identity() or {}).get("sha") or "").strip()
        if len(sha) == 40:
            return sha
    except Exception:
        pass
    try:
        from hermes_cli.build_info import get_build_sha

        sha = str(get_build_sha(short=0) or "").strip()
        if len(sha) == 40:
            return sha
    except Exception:
        pass
    return ""


def hermes_install_commit() -> str:
    """Commit of the running hermes-agent. Git checkout first, then the baked build SHA. Empty if unknown."""
    return _git_install_commit() or _stamped_install_commit()


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
