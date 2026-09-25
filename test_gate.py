import json
import unittest

import os
from pathlib import Path

from gate import (
    HOP_LIMIT,
    build_envelope,
    TESTED_HERMES_COMMIT,
    ensure_schema,
    hermes_version_refusal,
    kantharos_authorized,
    lift_title_gate,
    prepare_native_call,
    require_title_hint,
    restore_title_gate,
    route_reply,
    session_title,
    stamp_ack_ids,
    suppress_widgets,
)


class Agent:
    def __init__(self, title="", protocol=True, session_id="S_from"):
        self._session_title_hint = title
        self._bot_mode_protocol = protocol
        self.session_id = session_id
        self.tools = []
        self.valid_tool_names = set()
        self._session_db = None


SCHEMA = {"type": "function", "function": {"name": "message_agent", "parameters": {}}}


class GateTests(unittest.TestCase):
    def test_title_gate_is_not_required(self):
        notes = Agent(title="Notes thread")
        bot_chat = Agent(title="Bot Chat")
        self.assertTrue(kantharos_authorized(notes, managed=True))
        self.assertTrue(kantharos_authorized(bot_chat, managed=True))
        self.assertFalse(kantharos_authorized(notes, managed=False))
        self.assertFalse(kantharos_authorized(Agent(protocol=False), managed=True))
        self.assertTrue(ensure_schema(notes, SCHEMA, managed=True))
        self.assertEqual(notes.tools[0]["function"]["name"], "message_agent")
        self.assertIn("message_agent", notes.valid_tool_names)
        self.assertFalse(ensure_schema(Agent(title="Bot Chat"), SCHEMA, managed=False))

    def test_lift_does_not_keep_the_title(self):
        agent = Agent(title="Morning notes")
        previous = lift_title_gate(agent)
        self.assertEqual(session_title(agent), "Bot Chat")
        restore_title_gate(agent, previous)
        self.assertEqual(session_title(agent), "Morning notes")
        self.assertFalse(getattr(agent, "retitle", False))

    def test_hop_silent_and_same_instance(self):
        agent = Agent()
        roster = ["default", "bot001"]
        ok = prepare_native_call(agent, managed=True, target="bot001", message="Research competitors.", roster=roster, self_profile="default")
        self.assertTrue(ok["ok"])
        self.assertEqual(ok["hop"], 1)
        self.assertFalse(ok["retitle"])
        agent._kantharos_a2a_hop = HOP_LIMIT
        blocked = prepare_native_call(agent, managed=True, target="bot001", message="Again.", roster=roster, self_profile="default")
        self.assertFalse(blocked["ok"])
        self.assertIn("Hop limit", blocked["error"])
        silent = prepare_native_call(agent, managed=True, target="bot001", message="[SILENT]", roster=roster, self_profile="default")
        self.assertFalse(silent["ok"])
        ack = prepare_native_call(
            Agent(),
            managed=True,
            target="bot001",
            message=json.dumps({"status": "queued", "delivery_id": "d1"}),
            roster=roster,
            self_profile="default",
        )
        self.assertFalse(ack["ok"])
        peer = prepare_native_call(Agent(), managed=True, target="spark/researcher", message="Hi", roster=roster, self_profile="default")
        self.assertIn("Same-instance", peer["error"])
        relay = prepare_native_call(Agent(), managed=True, target="peer@host", message="Hi", roster=roster, self_profile="default")
        self.assertIn("Same-instance", relay["error"])
        self_msg = prepare_native_call(Agent(), managed=True, target="default", message="Hi", roster=roster, self_profile="default")
        self.assertIn("yourself", self_msg["error"])

    def test_reply_uses_from_session_id(self):
        class DB:
            def __init__(self):
                self.rows = []

            def append_message(self, session_id, role, content="", display_kind="", display_metadata=None):
                self.rows.append((session_id, role, content, display_kind, display_metadata))

        envelope = build_envelope(
            kind="request",
            from_profile="default",
            from_display_name="Kantharos",
            from_session_id="S_from",
            to_profile="bot001",
            to_session_id="S_peer",
            on_behalf_of="Tomasz",
            message="Research competitors.",
            hop=1,
            delivery_id="d1",
        )
        db = DB()
        landed = route_reply(db, envelope, "Message from 🤖 Peer (@bot001): three competitors.")
        self.assertEqual(landed, "S_from")
        self.assertEqual(db.rows[0][0], "S_from")
        self.assertNotEqual(db.rows[0][0], "S_peer")
        self.assertEqual(db.rows[0][1], "user")
        self.assertEqual(db.rows[0][3], "process_complete")
        self.assertEqual(db.rows[0][4]["kantharos_a2a"], "reply")

    def test_ack_fills_delivery_and_target_session(self):
        envelope = build_envelope(
            kind="request",
            from_profile="default",
            from_display_name="Kantharos",
            from_session_id="S_from",
            to_profile="bot001",
            message="Research competitors.",
        )
        stamped = stamp_ack_ids(envelope, {
            "status": "queued",
            "delivery_id": "del-9",
            "process_id": "proc-not-a-session",
            "to_session_id": "S_bot_chat",
        })
        self.assertEqual(stamped["delivery_id"], "del-9")
        self.assertEqual(stamped["to_session_id"], "S_bot_chat")
        untouched = stamp_ack_ids(envelope, {"status": "queued", "delivery_id": "del-9", "process_id": "proc"})
        self.assertEqual(untouched["to_session_id"], "")
        self.assertNotEqual(untouched.get("to_session_id"), "proc")

    def test_private_title_attr_is_hint_only(self):
        agent = Agent(title="Morning notes")
        previous = lift_title_gate(agent)
        private = [name for name in vars(agent) if name.startswith("_session_title") and name != "_session_title_hint"]
        self.assertEqual(
            private,
            [],
            "loud private-attr failure: title gate wrote %s; only _session_title_hint is allowed" % private,
        )
        self.assertEqual(agent._session_title_hint, "Bot Chat")
        restore_title_gate(agent, previous)
        self.assertNotIn("_session_title", vars(agent))

    def test_agent_origin_suppresses_widgets(self):
        self.assertTrue(suppress_widgets({"is_bot": True, "id": "bot:default"}))
        self.assertTrue(suppress_widgets(text="Message from 🤖 Kantharos (@default): research"))
        self.assertTrue(suppress_widgets(envelope={"kind": "request"}))
        self.assertFalse(suppress_widgets(text="Ask the peer to research our competitors"))

    def test_docker_image_without_git_reads_baked_build_sha(self):
        import sys
        import types
        from unittest import mock

        import gate as gate_mod

        pkg = types.ModuleType("hermes_cli")
        info = types.ModuleType("hermes_cli.build_info")
        info.get_code_identity = lambda refresh=False: {"sha": TESTED_HERMES_COMMIT, "source": "build-file"}
        info.get_build_sha = lambda short=8: TESTED_HERMES_COMMIT
        pkg.build_info = info
        with mock.patch.dict(sys.modules, {"hermes_cli": pkg, "hermes_cli.build_info": info}), \
                mock.patch.object(gate_mod, "_git_install_commit", return_value=""):
            self.assertEqual(gate_mod.hermes_install_commit(), TESTED_HERMES_COMMIT)
            self.assertEqual(gate_mod.hermes_version_refusal(""), "")
            info.get_code_identity = lambda refresh=False: {"sha": "1" * 40, "source": "build-file"}
            self.assertIn("untested hermes-agent", gate_mod.hermes_version_refusal(""))

    def test_untested_hermes_commit_refuses_enable(self):
        self.assertEqual(hermes_version_refusal(TESTED_HERMES_COMMIT), "")
        other = hermes_version_refusal("0" * 40)
        self.assertIn("refused to enable", other)
        self.assertIn("untested hermes-agent", other)
        unknown = hermes_version_refusal("")
        # Empty commit falls through to a live git read. Either it matches the
        # pin on a checkout of that commit, or it refuses. It must not stay silent.
        self.assertTrue(unknown == "" or "refused to enable" in unknown)

    def test_title_hint_fail_closed_when_upstream_ignores_it(self):
        import sys
        import types

        bot = types.ModuleType("tools.bot_mode_dm")

        def _session_title(agent):
            return str(getattr(agent, "session_title", "") or "")

        bot._session_title = _session_title
        tools = sys.modules.get("tools") or types.ModuleType("tools")
        sys.modules["tools"] = tools
        sys.modules["tools.bot_mode_dm"] = bot
        agent = Agent(title="Morning notes")
        result = require_title_hint(agent)
        self.assertFalse(result["ok"])
        self.assertIn("fail closed", result["error"])
        self.assertIn("_session_title_hint", result["error"])
        self.assertNotEqual(getattr(agent, "_session_title_hint", None), "Bot Chat")

    def test_upstream_title_gate_contract_fails_loud(self):
        excerpt = Path(__file__).with_name("tested_title_gate.txt").read_text(encoding="utf-8")
        self.assertIn(TESTED_HERMES_COMMIT, excerpt)
        self._assert_title_gate_source(excerpt, "pinned excerpt")
        live = Path(os.environ.get("HERMES_BOT_MODE_DM") or "/tmp/hermes-src/bot_mode_dm.py")
        if not live.is_file():
            self.fail(
                "LOUD: tools/bot_mode_dm.py is not available to check the message_agent title gate. "
                f"Set HERMES_BOT_MODE_DM. Tested commit {TESTED_HERMES_COMMIT}."
            )
        self._assert_title_gate_source(live.read_text(encoding="utf-8"), str(live))

    def _assert_title_gate_source(self, source, label):
        if "_session_title_hint" not in source:
            self.fail(
                f"LOUD: {label} renamed or removed the private _session_title_hint attribute. "
                "kantharos-a2a must fail closed instead of calling message_agent."
            )
        gate = "_session_title(agent) == BOT_CHAT_TITLE"
        if gate not in source:
            self.fail(
                f"LOUD: {label} changed the message_agent title gate in tools/bot_mode_dm.py. "
                f"Expected `{gate}`."
            )


if __name__ == "__main__":
    unittest.main()
