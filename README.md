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
   `api_server`. `message_agent_tool` also refuses hop 4+ (limit 3), peer and
   other-machine targets, unknown targets, self, and pure acks / `[SILENT]`.

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
