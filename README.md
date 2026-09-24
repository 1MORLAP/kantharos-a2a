# kantharos-a2a

> **Read-only public mirror.** The source of truth for this plugin lives in a
> private repository. This mirror is published for Hermes
> `agent-plugins/install` only. Pull requests are not accepted here; changes
> land upstream and are synced one-way into this tree.

Thin Hermes plugin. It exposes the native `message_agent` tool on every
Kantharos profile session by dropping the `Bot Chat` title gate. It does not
reimplement delivery or the completion-notification wake. Those stay in
upstream `tools/bot_mode_dm.py`.

Pinned to hermes-agent commit
`f97608f178d1ffeca59860195ab7da295f7c8e5f`
(`plugin.yaml` `tested_hermes_commit`). `register` refuses to enable on any
other commit, and the tool call fails closed if `_session_title` stops honoring
`_session_title_hint`.

## What it does

- Injects `message_agent_tool_schema()` when the install is Bot-Mode-managed and
  `agent.bot_mode_protocol` is on. Session title is ignored.
- For the native call only, sets `_session_title_hint` to `Bot Chat`, then
  restores it. The session row is not renamed.
- Refuses peer targets, `@connection` targets, self, unknown names, hop greater
  than 3, and pure acks / `[SILENT]`.
- Stamps `[kantharos-a2a hop=N]` on the outbound body.
- Asks the native tool to deliver and to wake the calling session
  (`from_session_id` on the sender agent).
- `pre_tool_call` blocks `approval`, `clarify`, `sudo`, and `secret` when the
  turn author is a bot.

## Layout / install identifier

Plugin files (`plugin.yaml`, `__init__.py`, …) live at the **repository root**.
Hermes `agent-plugins/install` therefore accepts a repo-root identifier with no
`#subdir` fragment, for example:

```text
https://github.com/<org>/<repo>.git
```

or the shorthand `<org>/<repo>`. Pin with a full 40-hex `ref`.

A `#subdir` identifier also works if the plugin is kept under a subdirectory
(Hermes parses `#…` as an optional subdirectory). This mirror uses the root
layout so the public identifier does not need a fragment.

## Install (Connected instance dashboard)

Kantharos Settings → Hermes Cloud → Enable bot-to-bot messaging, in order:

1. `POST /api/dashboard/agent-plugins/install` with
   `{ identifier, enable: true, ref }` where `ref` is a full 40-hex SHA of this
   mirror and `identifier` is the public GitHub URL above. `enable` writes
   `plugins.enabled`.
2. `PUT /api/config` for `agent.bot_mode_protocol` and
   `plugins.entries.kantharos-a2a.allow_tool_override`.
3. `POST /api/files/upload` of `profile.yaml` when that path is under the
   managed files root, setting `ui_meta.hermes-bots: {}` on one profile.
4. `POST /api/gateway/restart`.

Public GitHub clones do not require a deploy token for a public repository.
Do not put credentials in the identifier URL.

## Install (shell copy)

1. Copy this directory to the instance plugin folder:

```bash
mkdir -p "$HERMES_HOME/plugins"
cp -a . "$HERMES_HOME/plugins/kantharos-a2a"
```

`$HERMES_HOME` is the Hermes data directory for that install (often
`~/.hermes` inside the instance).

2. Enable it in `$HERMES_HOME/config.yaml`:

```yaml
agent:
  bot_mode_protocol: true
plugins:
  enabled:
    - kantharos-a2a
  entries:
    kantharos-a2a:
      allow_tool_override: true
```

`allow_tool_override` is only required if a future Hermes build registers
`message_agent` as a built-in. The tool is injected, not toolset-registered, on
current `main`.

3. Mark the install Bot-Mode-managed. `is_bot_mode_managed` is true when any
   profile's `profile.yaml` contains a `ui_meta.hermes-bots` object. One profile
   is enough for the install. Example, on the default profile:

```yaml
ui_meta:
  hermes-bots: {}
```

Do not retitle user sessions to `Bot Chat`.

4. Restart the Hermes gateway on that instance so the plugin loads.

5. Confirm a non-Bot-Chat session lists `message_agent` in its tools. Sending
   from Kantharos still uses the current session only.

No secrets belong in this directory or in `config.yaml` examples.

## License

MIT — see `LICENSE`. Copyright (c) 2026 Kantharos.
