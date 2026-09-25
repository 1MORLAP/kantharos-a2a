# kantharos-a2a

> **Read-only public mirror.** The source of truth for this plugin lives in a
> private repository. This mirror is published for Hermes
> `agent-plugins/install` only. Pull requests are not accepted here; changes
> land upstream and are synced one-way into this tree.

Thin Hermes plugin. It makes the native `message_agent` tool available in every
Kantharos session, not only in a session titled `Bot Chat`. It registers no
tool, toolset, or hook of its own; native `tools/bot_mode_dm.py` keeps the
schema, injection, attribution, same-instance delivery, and the reply wake.

Pinned to hermes-agent commit `f97608f178d1ffeca59860195ab7da295f7c8e5f`
(`plugin.yaml` `tested_hermes_commit`).

## What it does at register

1. `pin.py`: reads the live Hermes commit (git checkout, else the baked
   `.hermes_build_sha`) and refuses on any mismatch or if neither is readable.
2. `contract.py`: verifies the native gate symbols, their signatures, and the
   `Bot Chat` title predicate. Any change refuses with a logged reason and
   nothing is wrapped.
3. `guard.py`: wraps `message_agent_authorized`, `_session_title`, and
   `message_agent_tool` in `tools.bot_mode_dm`. The title requirement is
   dropped inside those gates only when the install is Bot-Mode-managed,
   `agent.bot_mode_protocol` is true, and the platform is `tui`, `cli`, or
   `api_server`. `message_agent_tool` also runs the loop guard below and
   refuses peer and other-machine targets, unknown targets, self, and pure
   acks / `[SILENT]`.
4. `reap.py`: in a process that hosts UI chats, wraps only
   `tui_gateway.server._session_has_active_delegations` so a detached session
   that owns a live native delivery runner (`bot_mode_dm.py --run-delivery`,
   1440 s cap, or `--wait-reply`, 3240 s cap, owned via
   `running_owned_by(session_key)`) is not reaped before the reply wakes it.
   It refuses with `dashboard.turn_isolation` on. Known limit: a gateway
   restart mid-delivery still loses the wake.

## Loop guard

In the wrap of the native `message_agent_tool`, before delivery:

1. `reply_via_completion`: in a turn authored by bot X (`agent._turn_author`),
   a send to X is refused. X's final answer already goes back automatically.
2. Duplicate: an identical (sender, target, body) send within 900 s.
3. Hop limit 3.
4. Pair cap: 6 admitted sends per ordered pair per rolling 60 minutes.

Refusals return `{"error", "reason"}`, say not to retry, and do not count
toward the cap. Ledgers hold hashes and ids only.

## Install identifier

Plugin files live at the repository root:

```text
https://github.com/1MORLAP/kantharos-a2a.git
```

Always pin a full 40-hex `ref`.

## Tests

```bash
HERMES_SRC=/path/to/hermes-agent-at-pinned-commit python3 -m unittest test_gate
```
