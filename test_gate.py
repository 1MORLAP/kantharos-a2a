"""kantharos-a2a tests. Run from this directory: python3 -m unittest test_gate

The live-contract tests need a hermes-agent checkout at the pinned commit:
HERMES_SRC=/path/to/hermes-agent (default /workspace/kantharos/hermes-map/f97608f).
They fail loudly when it is missing or when upstream renamed or changed the gate.
"""

import importlib
import importlib.util
import json
import linecache
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
HERMES_SRC = Path(os.environ.get("HERMES_SRC") or "/workspace/kantharos/hermes-map/f97608f")


def _load_plugin():
    spec = importlib.util.spec_from_file_location(
        "kantharos_a2a_under_test", HERE / "__init__.py", submodule_search_locations=[str(HERE)])
    pkg = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = pkg
    spec.loader.exec_module(pkg)
    return pkg


plugin = _load_plugin()
pin = sys.modules["kantharos_a2a_under_test.pin"]
contract = sys.modules["kantharos_a2a_under_test.contract"]
guard = sys.modules["kantharos_a2a_under_test.guard"]
hops = sys.modules["kantharos_a2a_under_test.hops"]
TESTED = pin.TESTED_HERMES_COMMIT


class DB:
    def __init__(self, title="", rows=None):
        self.title = title
        self.rows = rows or []
        self.db_path = None

    def get_session_title(self, sid):
        return self.title

    def get_messages(self, sid, latest=False, limit=None):
        return list(self.rows)


class Agent:
    def __init__(self, title="", protocol=True, platform="tui", home="/data", rows=None, session_id="S_from"):
        self._session_title_hint = None
        self._bot_mode_protocol = protocol
        self.platform = platform
        self.session_id = session_id
        self._session_db = DB(title, rows)
        self._session_db.db_path = str(Path(home) / "state.db")
        self.tools = []
        self.valid_tool_names = set()


FAKE_DM_SOURCE = """
import json


def _session_title(agent):
    title = str(getattr(agent, "_session_title_hint", "") or "").strip()
    return title or str(agent._session_db.get_session_title(agent.session_id) or "").strip()


def message_agent_authorized(agent):
    if not getattr(agent, "_bot_mode_protocol", True):
        return False
    return _session_title(agent) == BOT_CHAT_TITLE and is_bot_mode_managed(_agent_home(agent))


def ensure_message_agent_tool(agent):
    if not message_agent_authorized(agent):
        return False
    agent.tools.append({"type": "function", "function": {"name": "message_agent"}})
    agent.valid_tool_names.add("message_agent")
    return True


def message_agent_tool(target="", message="", task_id=None, agent=None):
    home = _agent_home(agent)
    if _session_title(agent) != BOT_CHAT_TITLE:
        return json.dumps({"error": "message_agent is only available in a Bot Mode 'Bot Chat' session."})
    if not is_bot_mode_managed(home):
        return json.dumps({"error": "not managed"})
    calls.append((target, message))
    n = len(calls)
    return json.dumps({"status": "queued", "delivery_id": f"d{n}", "to": f"@{target}", "process_id": f"proc_{n}"})


def _resolve_local_name(target, roster, root=None):
    want = target.strip().lower()
    if want == "hermes":
        return "default"
    return next((n for n in roster if n.lower() == want), {"web factory": "bot002"}.get(want))
"""


def fake_native(managed=True, roster=("default", "bot001", "bot002"), peers=("spark",)):
    """Fake tools.bot_mode_dm / tools.bot_mode_probe with f97608f's gate shape."""
    calls = []
    dm = types.ModuleType("tools.bot_mode_dm")
    probe = types.ModuleType("tools.bot_mode_probe")
    probe.BOT_CHAT_TITLE = "Bot Chat"
    probe.is_bot_mode_managed = lambda home: managed
    probe._hermes_root = lambda home: Path(home).parent.parent if Path(home).parent.name == "profiles" else Path(home)
    probe._profile_name = lambda home: Path(home).name if Path(home).parent.name == "profiles" else "default"
    probe._roster = lambda root: [(n, Path(root)) for n in roster]
    probe._peers = lambda root: list(peers)
    dm.MESSAGE_AGENT_TOOL_NAME = "message_agent"
    dm._agent_home = lambda agent: str(Path(agent._session_db.db_path).parent)

    dm.BOT_CHAT_TITLE = probe.BOT_CHAT_TITLE
    dm.is_bot_mode_managed = lambda home: probe.is_bot_mode_managed(home)
    dm.calls = calls
    dm.__file__ = "<fake tools/bot_mode_dm.py>"
    linecache.cache[dm.__file__] = (len(FAKE_DM_SOURCE), None, FAKE_DM_SOURCE.splitlines(True), dm.__file__)
    exec(compile(FAKE_DM_SOURCE, dm.__file__, "exec"), dm.__dict__)
    return dm, probe, calls


class LiftTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.dm, self.probe, self.calls = fake_native()
        self.wrap = guard.NativeGateWrap(self.dm, self.probe)
        self.assertTrue(self.wrap.install())

    def tearDown(self):
        self.wrap.uninstall()
        self.tmp.cleanup()

    def agent(self, **kw):
        kw.setdefault("home", self.home)
        return Agent(**kw)

    def test_non_bot_chat_kantharos_session_gets_the_tool(self):
        agent = self.agent(title="Launch plan")
        self.assertTrue(self.dm.ensure_message_agent_tool(agent))
        self.assertIn("message_agent", agent.valid_tool_names)
        ack = json.loads(self.dm.message_agent_tool(target="bot002", message="Reply GATE95-PONG", agent=agent))
        self.assertEqual(ack["status"], "queued")
        self.assertEqual(ack["hop"], 1)

    def test_title_is_not_rewritten(self):
        agent = self.agent(title="Launch plan")
        self.dm.ensure_message_agent_tool(agent)
        self.dm.message_agent_tool(target="bot002", message="hi there", agent=agent)
        self.assertIsNone(agent._session_title_hint)
        self.assertEqual(self.dm._session_title(agent), "Launch plan")

    def test_title_is_only_lifted_inside_the_gates(self):
        self.assertEqual(self.dm._session_title(self.agent(title="Launch plan")), "Launch plan")

    def test_each_condition_is_required(self):
        for kw in ({"protocol": False}, {"platform": "telegram"}, {"platform": ""}, {"platform": "subagent"}):
            agent = self.agent(title="Launch plan", **kw)
            self.assertFalse(self.dm.ensure_message_agent_tool(agent), kw)
            self.assertIn("error", json.loads(self.dm.message_agent_tool(target="bot002", message="hello", agent=agent)))
        dm, probe, _ = fake_native(managed=False)
        wrap = guard.NativeGateWrap(dm, probe)
        wrap.install()
        try:
            self.assertFalse(dm.ensure_message_agent_tool(self.agent(title="Launch plan")))
        finally:
            wrap.uninstall()

    def test_protocol_must_be_explicitly_true(self):
        agent = self.agent(title="Launch plan")
        del agent._bot_mode_protocol
        self.assertFalse(self.dm.ensure_message_agent_tool(agent))

    def test_platforms(self):
        for platform in ("tui", "cli", "api_server"):
            self.assertTrue(self.dm.ensure_message_agent_tool(self.agent(title="x", platform=platform)), platform)

    def test_bot_chat_session_unchanged(self):
        self.assertTrue(self.dm.ensure_message_agent_tool(self.agent(title="Bot Chat", platform="telegram")))

    def test_install_is_idempotent(self):
        self.assertFalse(guard.NativeGateWrap(self.dm, self.probe).install())

    def test_same_instance_only(self):
        agent = self.agent(title="Launch plan")
        for target, reason in (("spark/researcher", "not_same_instance"), ("peer@mini", "not_same_instance"),
                               ("spark", "not_same_instance"), ("nobody", "unknown_target"),
                               ("default", "self_target"), ("hermes", "self_target")):
            out = json.loads(self.dm.message_agent_tool(target=target, message="hello", agent=agent))
            self.assertEqual(out["reason"], reason, target)
        self.assertEqual(self.calls, [])
        ok = json.loads(self.dm.message_agent_tool(target="Web Factory", message="hello", agent=agent))
        self.assertEqual(ok["status"], "queued")

    def test_silent_and_pure_acks_are_not_sent(self):
        agent = self.agent(title="Launch plan")
        for msg in ("[SILENT]", "ok", json.dumps({"status": "queued", "delivery_id": "d1"}), "  "):
            out = json.loads(self.dm.message_agent_tool(target="bot002", message=msg, agent=agent))
            self.assertEqual(out["reason"], "silent_ack")
        self.assertEqual(self.calls, [])


class HopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "profiles" / "bot002").mkdir(parents=True)
        (self.root / "profiles" / "bot001").mkdir(parents=True)
        self.dm, self.probe, self.calls = fake_native()
        self.wrap = guard.NativeGateWrap(self.dm, self.probe)
        self.wrap.install()

    def tearDown(self):
        self.wrap.uninstall()
        self.tmp.cleanup()

    def send(self, agent, target, message):
        return json.loads(self.dm.message_agent_tool(target=target, message=message, agent=agent))

    def test_hop_four_is_refused_server_side(self):
        kantharos = Agent(title="Launch plan", home=str(self.root), rows=[{"role": "user", "content": "Ask Web Factory"}])
        self.assertEqual(self.send(kantharos, "bot002", "Please ask bot001 for numbers.")["hop"], 1)
        web = Agent(title="Bot Chat", platform="cli", home=str(self.root / "profiles" / "bot002"),
                    rows=[{"role": "user", "content": "Message from 🤖 hermes (@hermes): Please ask bot001 for numbers."}])
        self.assertEqual(self.send(web, "bot001", "Kantharos wants numbers.")["hop"], 2)
        bot1 = Agent(title="Bot Chat", platform="cli", home=str(self.root / "profiles" / "bot001"),
                     rows=[{"role": "user", "content": "Message from 🤖 Web Factory (@bot002): Kantharos wants numbers."}])
        self.assertEqual(self.send(bot1, "bot002", "Numbers for Kantharos: 3.")["hop"], 3)
        web2 = Agent(title="Bot Chat", platform="cli", home=str(self.root / "profiles" / "bot002"),
                     rows=[{"role": "user", "content": "Message from 🤖 bot001 (@bot001): Numbers for Kantharos: 3."}])
        with self.assertLogs(guard.logger, "WARNING") as logs:
            fourth = self.send(web2, "bot001", "Thanks, one more question.")
        self.assertEqual(fourth["reason"], "hop_limit")
        self.assertIn("hop 4", fourth["error"])
        self.assertIn("hop=4", "\n".join(logs.output))
        self.assertEqual(len(self.calls), 3)

    def test_reply_wake_continues_the_chain(self):
        kantharos = Agent(title="Launch plan", home=str(self.root), rows=[{"role": "user", "content": "go"}])
        first = self.send(kantharos, "bot002", "Question one.")
        wake = f"[IMPORTANT: Background process {first['process_id']} exited (code 0).\nOutput:\nReply A1]"
        kantharos._session_db.rows = [{"role": "user", "content": wake}]
        self.assertEqual(self.send(kantharos, "bot002", "Question two.")["hop"], 2)

    def test_new_user_turn_resets(self):
        kantharos = Agent(title="Launch plan", home=str(self.root), rows=[{"role": "user", "content": "go"}])
        self.send(kantharos, "bot002", "Question one.")
        kantharos._session_db.rows = [{"role": "user", "content": "Different ask from the user"}]
        self.assertEqual(self.send(kantharos, "bot002", "Question two.")["hop"], 1)

    def test_unknown_agent_message_counts_as_hop_one(self):
        self.assertEqual(hops.inbound_hop("Message from 🤖 X (@x): unseen", lambda k: None)[0], 1)

    def test_ledger_holds_no_message_text(self):
        kantharos = Agent(title="Launch plan", home=str(self.root), rows=[{"role": "user", "content": "go"}])
        self.send(kantharos, "bot002", "private body text")
        raw = (self.root / "plugin-data" / "kantharos-a2a" / "hops.json").read_text()
        self.assertNotIn("private body", raw)



reap = sys.modules["kantharos_a2a_under_test.reap"]


def bot_agent(root, profile, author=None, rows=None, title="Bot Chat", platform="cli"):
    home = str(Path(root) / "profiles" / profile) if profile != "default" else str(root)
    agent = Agent(title=title, platform=platform, home=home, rows=rows or [{"role": "user", "content": "go"}])
    agent._turn_author = author
    return agent


class LoopGuardTests(unittest.TestCase):
    """ADR-0016 loop-guard revision: reply_via_completion, duplicate, hop, pair_cap."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for name in ("bot001", "bot002", "bot003"):
            (self.root / "profiles" / name).mkdir(parents=True)
        self.dm, self.probe, self.calls = fake_native(roster=("default", "bot001", "bot002", "bot003"))
        self.wrap = guard.NativeGateWrap(self.dm, self.probe)
        self.wrap.install()
        self.clock = [1_000_000.0]
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(hops.time, "time", side_effect=lambda: self.clock[0]).start()

    def tearDown(self):
        self.wrap.uninstall()
        self.tmp.cleanup()

    def send(self, agent, target, message):
        return json.loads(self.dm.message_agent_tool(target=target, message=message, agent=agent))

    # rule 1 ---------------------------------------------------------------
    def test_reply_to_the_asking_bot_is_refused_local_author(self):
        author = {"id": "bot:bot003", "name": "bot003", "is_bot": True}
        agent = bot_agent(self.root, "bot001", author)
        with self.assertLogs(guard.logger, "WARNING") as logs:
            out = self.send(agent, "bot003", "Here are the numbers you asked for: 3.")
        self.assertEqual(out["reason"], "reply_via_completion")
        self.assertEqual(out["error"], "Your final answer is delivered to bot003 automatically. "
                                       "Do not call message_agent to reply.")
        self.assertIn("reason=reply_via_completion", "\n".join(logs.output))
        self.assertEqual(self.calls, [])

    def test_questions_are_not_exempt(self):
        agent = bot_agent(self.root, "bot001", {"id": "bot:bot003", "name": "bot003", "is_bot": True})
        self.assertEqual(self.send(agent, "@bot003", "Which numbers do you mean?")["reason"], "reply_via_completion")

    def test_friendly_name_of_the_author_is_refused(self):
        agent = bot_agent(self.root, "bot001", {"id": "bot:bot002", "name": "bot002", "is_bot": True})
        self.assertEqual(self.send(agent, "Web Factory", "answer")["reason"], "reply_via_completion")

    def test_other_teammate_is_allowed_in_a_bot_authored_turn(self):
        agent = bot_agent(self.root, "bot001", {"id": "bot:bot003", "name": "bot003", "is_bot": True})
        self.assertEqual(self.send(agent, "bot002", "Can you check the draft?")["status"], "queued")

    def test_human_author_is_not_refused(self):
        agent = bot_agent(self.root, "bot001", {"id": "u123", "name": "Tomasz", "is_bot": False},
                          title="Launch plan", platform="tui")
        self.assertEqual(self.send(agent, "bot003", "Ping from the user's chat")["status"], "queued")
        agent._turn_author = None
        self.assertEqual(self.send(agent, "bot003", "Second ask")["status"], "queued")

    def test_connection_author_is_refused_only_for_the_same_connection(self):
        author = {"id": "bot:mini/bot003", "name": "bot003", "is_bot": True}
        agent = bot_agent(self.root, "bot001", author)
        self.assertEqual(self.send(agent, "mini/bot003", "answer")["reason"], "reply_via_completion")
        self.assertEqual(self.send(agent, "bot003@mini", "answer")["reason"], "reply_via_completion")
        # same profile name on this instance is a different bot
        self.assertEqual(self.send(agent, "bot003", "local teammate")["status"], "queued")
        # same profile on another connection is not the author (refused only as not same instance)
        self.assertEqual(self.send(agent, "studio/bot003", "answer")["reason"], "not_same_instance")

    def test_peer_hostname_author_is_refused_only_for_the_same_peer(self):
        author = {"id": "bot:spark/researcher", "name": "researcher", "is_bot": True}
        agent = bot_agent(self.root, "bot001", author)
        out = self.send(agent, "spark/researcher", "answer")
        self.assertEqual(out["reason"], "reply_via_completion")
        self.assertIn("delivered to researcher automatically", out["error"])
        self.assertEqual(self.send(agent, "spark/other", "answer")["reason"], "not_same_instance")

    def test_parse_bot_author_forms(self):
        self.assertEqual(guard.parse_bot_author({"id": "bot:bot003", "is_bot": True}), ("", "bot003"))
        self.assertEqual(guard.parse_bot_author({"id": "bot:mini/bot003", "is_bot": True}), ("mini", "bot003"))
        self.assertEqual(guard.parse_bot_author({"id": "bot:host.local/default", "is_bot": True}),
                         ("host.local", "default"))
        self.assertIsNone(guard.parse_bot_author({"id": "bot:bot003", "is_bot": False}))
        self.assertIsNone(guard.parse_bot_author({"id": "u1", "is_bot": True}))
        self.assertIsNone(guard.parse_bot_author(None))

    def test_default_profile_author(self):
        agent = bot_agent(self.root, "bot001", {"id": "bot:default", "name": "hermes", "is_bot": True})
        self.assertEqual(self.send(agent, "hermes", "answer")["reason"], "reply_via_completion")

    # dedup ----------------------------------------------------------------
    def test_identical_resend_within_900s_is_refused(self):
        agent = bot_agent(self.root, "bot001", None, title="Launch plan", platform="tui")
        self.assertEqual(self.send(agent, "bot002", "Status of the launch?")["status"], "queued")
        self.clock[0] += 120  # target_busy came back; the model re-sends the same body
        out = self.send(agent, "bot002", "Status of the launch?")
        self.assertEqual(out["reason"], "duplicate")
        self.assertIn("Do not retry", out["error"])
        self.assertEqual(len(self.calls), 1)
        self.clock[0] += 900
        self.assertEqual(self.send(agent, "bot002", "Status of the launch?")["status"], "queued")

    def test_duplicate_is_per_target(self):
        agent = bot_agent(self.root, "bot001", None, title="Launch plan", platform="tui")
        self.send(agent, "bot002", "Same body")
        self.assertEqual(self.send(agent, "bot003", "Same body")["status"], "queued")

    # pair cap -------------------------------------------------------------
    def test_pair_cap_six_per_rolling_hour(self):
        agent = bot_agent(self.root, "bot001", None, title="Launch plan", platform="tui")
        for i in range(6):
            self.assertEqual(self.send(agent, "bot002", f"question {i}")["status"], "queued", i)
            self.clock[0] += 60
        out = self.send(agent, "bot002", "question 7")
        self.assertEqual(out["reason"], "pair_cap")
        self.assertIn("Do not retry", out["error"])
        self.assertEqual(self.send(agent, "bot003", "other pair")["status"], "queued")
        self.clock[0] += 3600 - 6 * 60 + 1
        self.assertEqual(self.send(agent, "bot002", "question 8")["status"], "queued")

    def test_refusals_do_not_count_toward_the_cap(self):
        agent = bot_agent(self.root, "bot001", None, title="Launch plan", platform="tui")
        self.send(agent, "bot002", "q0")
        for _ in range(8):
            self.assertEqual(self.send(agent, "bot002", "q0")["reason"], "duplicate")
        for i in range(1, 6):
            self.assertEqual(self.send(agent, "bot002", f"q{i}")["status"], "queued", i)
        self.assertEqual(self.send(agent, "bot002", "q6")["reason"], "pair_cap")

    def test_forced_ack_loop_stops(self):
        """f2: a sender woken by each reply keeps messaging the same teammate: hop 3 or the cap stops it."""
        agent = bot_agent(self.root, "default", None, title="Launch plan", platform="tui")
        results = []
        out = self.send(agent, "bot002", "Round 0")
        results.append(out)
        for i in range(1, 10):
            if "process_id" in out:
                agent._session_db.rows = [{"role": "user", "content":
                    f"[IMPORTANT: Background process {out['process_id']} exited (code 0).\nOutput:\nThanks {i}]"}]
            out = self.send(agent, "bot002", f"Round {i}")
            results.append(out)
            if "error" in out:
                break
        self.assertIn(results[-1]["reason"], {"hop_limit", "pair_cap"})
        self.assertLessEqual(len(self.calls), 6)

    def test_sends_ledger_holds_no_message_text(self):
        agent = bot_agent(self.root, "bot001", None, title="Launch plan", platform="tui")
        self.send(agent, "bot002", "private body text")
        raw = (self.root / "plugin-data" / "kantharos-a2a" / "sends.json").read_text()
        self.assertNotIn("private body", raw)


class Proc:
    def __init__(self, pid, command, owner="SK1", started=0.0, notify=True, exited=False):
        self.id = pid
        self.command = command
        self.owner_task_id = owner
        self.started_at = started
        self.notify_on_complete = notify
        self.exited = exited


RUNNER = ("/opt/hermes/.venv/bin/python /opt/hermes/tools/bot_mode_dm.py --run-delivery "
          "--author '{\"id\":\"bot:default\"}' query-file /tmp/dm_x.txt --profile-home /opt/data/profiles/bot002 "
          "/opt/hermes/.venv/bin/hermes -p bot002 chat -Q")
WAITER = "/opt/hermes/.venv/bin/python /opt/hermes/tools/bot_mode_dm.py --wait-reply env_1 /opt/data/bot_relay/r.json"


class ReapTests(unittest.TestCase):
    def setUp(self):
        self.now = 10_000.0
        self.procs = []
        registry = types.SimpleNamespace(
            process_registry=types.SimpleNamespace(
                running_owned_by=lambda owner: [p for p in self.procs if p.owner_task_id == owner]))
        self.lifted = True
        self.wrap = reap.ReapExemptionWrap(registry_mod=registry, lift=lambda agent: self.lifted,
                                           now=lambda: self.now)

    def session(self, key="SK1"):
        return {"agent": object(), "session_key": key}

    def test_delivery_mode(self):
        self.assertEqual(reap.delivery_mode(RUNNER), "--run-delivery")
        self.assertEqual(reap.delivery_mode(WAITER), "--wait-reply")
        self.assertEqual(reap.delivery_mode("python tools/bot_mode_dm.py --help"), "")
        self.assertEqual(reap.delivery_mode("python other/bot_mode_dm.py --run-delivery x"), "")
        self.assertEqual(reap.delivery_mode("echo bot_mode_dm.py --run-delivery"), "")
        self.assertEqual(reap.delivery_mode("sleep 100"), "")

    def test_owner_match(self):
        self.procs = [Proc("p1", RUNNER, owner="SK2", started=self.now - 10)]
        self.assertIsNone(self.wrap.live_delivery(self.session("SK1")))
        self.procs.append(Proc("p2", RUNNER, owner="SK1", started=self.now - 10))
        self.assertEqual(self.wrap.live_delivery(self.session("SK1")).id, "p2")
        self.assertIsNone(self.wrap.live_delivery({"agent": object(), "session_key": ""}))

    def test_age_caps(self):
        self.procs = [Proc("p1", RUNNER, started=self.now - 1441)]
        self.assertIsNone(self.wrap.live_delivery(self.session()))
        self.procs = [Proc("p1", RUNNER, started=self.now - 1439)]
        self.assertIsNotNone(self.wrap.live_delivery(self.session()))
        self.procs = [Proc("w1", WAITER, started=self.now - 3239)]
        self.assertIsNotNone(self.wrap.live_delivery(self.session()))
        self.procs = [Proc("w1", WAITER, started=self.now - 3241)]
        self.assertIsNone(self.wrap.live_delivery(self.session()))

    def test_exited_notify_off_other_commands_and_lift(self):
        for proc in (Proc("a", RUNNER, started=self.now - 5, exited=True),
                     Proc("b", RUNNER, started=self.now - 5, notify=False),
                     Proc("c", "sleep 600", started=self.now - 5)):
            self.procs = [proc]
            self.assertIsNone(self.wrap.live_delivery(self.session()), proc.id)
        self.procs = [Proc("d", RUNNER, started=self.now - 5)]
        self.lifted = False
        self.assertIsNone(self.wrap.live_delivery(self.session()))

    def fake_server(self, native_result=False):
        server = types.ModuleType("fake_tui_server")
        server._sessions_lock = __import__("threading").Lock()
        server._sessions = {"sid1": self.session()}

        def _session_has_active_delegations(sid, session=None):
            return native_result

        server._session_has_active_delegations = _session_has_active_delegations
        return server

    def test_wrapper_keeps_native_true_and_adds_live_runner(self):
        server = self.fake_server(native_result=True)
        self.wrap.install(server)
        self.addCleanup(self.wrap.uninstall)
        self.assertTrue(server._session_has_active_delegations("sid1"))
        server2 = self.fake_server(native_result=False)
        wrap2 = reap.ReapExemptionWrap(registry_mod=self.wrap.registry_mod, lift=lambda a: True, now=lambda: self.now)
        wrap2.install(server2)
        self.addCleanup(wrap2.uninstall)
        self.assertFalse(server2._session_has_active_delegations("sid1"))
        self.procs = [Proc("p1", RUNNER, started=self.now - 5)]
        with self.assertLogs(reap.logger, "INFO"):
            self.assertTrue(server2._session_has_active_delegations("sid1"))
        self.assertTrue(server2._session_has_active_delegations("sid1", server2._sessions["sid1"]))

    def test_install_touches_only_the_given_namespace_and_is_idempotent(self):
        server, lifecycle = self.fake_server(), self.fake_server()
        native_lifecycle = lifecycle._session_has_active_delegations
        self.assertEqual(self.wrap.install(server), 1)
        self.addCleanup(self.wrap.uninstall)
        self.assertEqual(self.wrap.install(server), 0)
        self.assertIs(lifecycle._session_has_active_delegations, native_lifecycle)
        self.wrap.uninstall()
        self.assertIsNone(getattr(server._session_has_active_delegations, "__kantharos_a2a_original__", None))

    def test_turn_isolation_refuses(self):
        server = self.fake_server()
        server._load_dashboard_process_isolation_config = lambda: {"turn_isolation": True}
        self.assertIn("turn_isolation is on", reap.turn_isolation_refusal(server))
        server._load_dashboard_process_isolation_config = lambda: {"turn_isolation": False}
        self.assertEqual(reap.turn_isolation_refusal(server), "")
        del server._load_dashboard_process_isolation_config
        self.assertIn("missing", reap.turn_isolation_refusal(server))

    def test_install_reap_exemption_refuses_loudly_with_turn_isolation(self):
        server = self.fake_server()
        server._load_dashboard_process_isolation_config = lambda: {"turn_isolation": True}
        lifecycle = types.ModuleType("tui_gateway.session_lifecycle")
        lifecycle._session_has_active_delegations = server._session_has_active_delegations
        pkg = types.ModuleType("tui_gateway")
        pkg.session_lifecycle = lifecycle
        state = {"wrap": self.wrap, "installed": False, "refused": False}
        with mock.patch.dict(sys.modules, {"tui_gateway": pkg, "tui_gateway.server": server,
                                           "tui_gateway.session_lifecycle": lifecycle}), \
                mock.patch.dict(plugin._reap_state, state), \
                self.assertLogs(plugin.logger, "ERROR") as logs:
            self.assertFalse(plugin.install_reap_exemption())
            self.assertTrue(plugin._reap_state["refused"])
        self.assertIn("turn_isolation is on", "\n".join(logs.output))
        self.assertIsNone(getattr(server._session_has_active_delegations, "__kantharos_a2a_original__", None))

    def test_install_reap_exemption_wraps_server_only(self):
        server = self.fake_server()
        server._load_dashboard_process_isolation_config = lambda: {"turn_isolation": False}
        lifecycle = types.ModuleType("tui_gateway.session_lifecycle")
        native = server._session_has_active_delegations
        lifecycle._session_has_active_delegations = native
        pkg = types.ModuleType("tui_gateway")
        pkg.session_lifecycle = lifecycle
        state = {"wrap": self.wrap, "installed": False, "refused": False}
        with mock.patch.dict(sys.modules, {"tui_gateway": pkg, "tui_gateway.server": server,
                                           "tui_gateway.session_lifecycle": lifecycle}), \
                mock.patch.dict(plugin._reap_state, state):
            self.assertTrue(plugin.install_reap_exemption())
        self.addCleanup(self.wrap.uninstall)
        self.assertIs(server._session_has_active_delegations.__kantharos_a2a_original__, native)
        self.assertIs(lifecycle._session_has_active_delegations, native)

    def test_server_binding_must_be_the_lifecycle_predicate(self):
        server, lifecycle = self.fake_server(), types.ModuleType("lifecycle")
        lifecycle._session_has_active_delegations = lambda sid, session=None: False
        self.assertIn("is not the session_lifecycle predicate", contract.server_predicate_problem(server, lifecycle))
        lifecycle._session_has_active_delegations = server._session_has_active_delegations
        self.assertEqual(contract.server_predicate_problem(server, lifecycle), "")


class ContractTests(unittest.TestCase):
    """Fail loudly if upstream renames the gate or changes a signature."""

    def importer(self, dm, probe):
        mods = {"tools.bot_mode_dm": dm, "tools.bot_mode_probe": probe}
        its = types.ModuleType("agent.inline_tool_executors")
        its.INLINE_TOOL_EXECUTORS = {"message_agent": object()}
        mods["agent.inline_tool_executors"] = its
        mods["agent.turn_context"] = types.ModuleType("agent.turn_context")
        mods["agent.turn_author"] = types.ModuleType("agent.turn_author")
        sources = {
            "agent.inline_tool_executors": '"tools.bot_mode_dm", "message_agent_tool"',
            "agent.turn_context": "agent._turn_author = turn_author\nensure_message_agent_tool(agent)",
            "agent.turn_author": 'return f"bot:{origin}/{profile}" if origin else f"bot:{profile}"',
        }
        real_source = contract._source

        def fake_source(obj):
            for name, text in sources.items():
                if obj is mods.get(name):
                    return text
            if obj is dm:
                return real_source(obj) + '\nauthor = {"id": f"bot:{me}", "name": _handle(me), "is_bot": True}\n'
            return real_source(obj)

        self.addCleanup(setattr, contract, "_source", real_source)
        contract._source = fake_source

        def imp(name):
            if name not in mods:
                raise ImportError(name)
            return mods[name]

        return imp

    def test_f97608f_shape_passes(self):
        dm, probe, _ = fake_native()
        self.assertEqual(contract.verify_native_contract(self.importer(dm, probe)), [])

    def test_renamed_gate_refuses(self):
        dm, probe, _ = fake_native()
        del dm.message_agent_authorized
        problems = contract.verify_native_contract(self.importer(dm, probe))
        self.assertTrue(any("message_agent_authorized is missing" in p for p in problems), problems)
        self.assertIn("refused to enable", contract.contract_refusal(problems))

    def test_changed_signature_refuses(self):
        dm, probe, _ = fake_native()
        dm.message_agent_tool = lambda target="", message="", agent=None: ""
        problems = contract.verify_native_contract(self.importer(dm, probe))
        self.assertTrue(any("message_agent_tool signature changed" in p for p in problems), problems)

    def test_changed_title_predicate_refuses(self):
        dm, probe, _ = fake_native()

        def message_agent_authorized(agent):
            return dm.session_is_bot_chat(agent)

        dm.message_agent_authorized = message_agent_authorized
        problems = contract.verify_native_contract(self.importer(dm, probe))
        self.assertTrue(any("_session_title(agent) == BOT_CHAT_TITLE" in p for p in problems), problems)

    def test_changed_title_constant_refuses(self):
        dm, probe, _ = fake_native()
        probe.BOT_CHAT_TITLE = "Team Chat"
        problems = contract.verify_native_contract(self.importer(dm, probe))
        self.assertTrue(any("BOT_CHAT_TITLE changed" in p for p in problems), problems)

    def test_register_refuses_and_wraps_nothing(self):
        with mock.patch.object(plugin, "hermes_version_refusal", return_value=""), \
                mock.patch.object(plugin, "verify_native_contract",
                                  return_value=["tools.bot_mode_dm.message_agent_tool is missing"]), \
                self.assertLogs(plugin.logger, "ERROR") as logs:
            plugin.register(object())
        self.assertIn("refused to enable", "\n".join(logs.output))

    def test_register_refuses_on_untested_commit(self):
        with mock.patch.object(plugin, "hermes_version_refusal", return_value="kantharos-a2a refused to enable: untested"), \
                mock.patch.object(plugin, "verify_native_contract") as verify, \
                self.assertLogs(plugin.logger, "ERROR"):
            plugin.register(object())
        verify.assert_not_called()

    def test_missing_turn_author_assignment_refuses(self):
        dm, probe, _ = fake_native()
        imp = self.importer(dm, probe)
        real = contract._source

        def no_author(obj):
            text = real(obj)
            return text.replace("agent._turn_author = turn_author", "") if obj is imp("agent.turn_context") else text

        contract._source = no_author
        problems = contract.verify_native_contract(imp)
        self.assertTrue(any("agent._turn_author = turn_author" in p for p in problems), problems)

    def test_register_refuses_when_reap_predicate_is_renamed(self):
        """(g) a renamed tui_gateway.session_lifecycle._session_has_active_delegations refuses register."""
        lifecycle = types.ModuleType("tui_gateway.session_lifecycle")
        lifecycle._session_has_active_delegations_v2 = lambda sid, session=None: False

        def imp(name):
            if name == "tui_gateway.session_lifecycle":
                return lifecycle
            raise ImportError(name)

        problems = contract.verify_reap_contract(imp)
        self.assertTrue(any("_session_has_active_delegations is missing" in p for p in problems), problems)
        with mock.patch.object(plugin, "hermes_version_refusal", return_value=""), \
                mock.patch.object(plugin, "verify_native_contract", return_value=[]), \
                mock.patch.object(plugin, "verify_reap_contract", side_effect=lambda: contract.verify_reap_contract(imp)), \
                mock.patch.object(guard.NativeGateWrap, "install") as install, \
                self.assertLogs(plugin.logger, "ERROR") as logs:
            plugin.register(object())
        install.assert_not_called()
        self.assertIn("_session_has_active_delegations is missing", "\n".join(logs.output))

    def test_registers_no_tool_toolset_or_hook(self):
        ctx = mock.Mock()
        with mock.patch.object(plugin, "hermes_version_refusal", return_value="refused"), \
                self.assertLogs(plugin.logger, "ERROR"):
            plugin.register(ctx)
        ctx.register_tool.assert_not_called()
        ctx.register_hook.assert_not_called()
        manifest = (HERE / "plugin.yaml").read_text()
        self.assertNotIn("provides_tools", manifest)
        self.assertNotIn("provides_hooks", manifest)
        self.assertIn(f"tested_hermes_commit: {TESTED}", manifest)


class LiveContractTests(unittest.TestCase):
    """Against the real hermes-agent source at the pinned commit."""

    PREFIXES = ("tools", "agent", "tui_gateway")

    def _ours(self, name):
        return any(name == p or name.startswith(p + ".") for p in self.PREFIXES)

    def setUp(self):
        if not (HERMES_SRC / "tools" / "bot_mode_dm.py").is_file():
            self.fail(f"LOUD: hermes-agent source not found at {HERMES_SRC}. Set HERMES_SRC to a checkout of {TESTED}.")
        self._saved = {k: v for k, v in sys.modules.items() if self._ours(k)}
        for k in self._saved:
            sys.modules.pop(k, None)
        sys.path.insert(0, str(HERMES_SRC))

    def tearDown(self):
        sys.path.remove(str(HERMES_SRC))
        for k in [k for k in sys.modules if self._ours(k)]:
            sys.modules.pop(k, None)
        sys.modules.update(self._saved)

    @staticmethod
    def _import(name):
        try:
            return importlib.import_module(name)
        except ImportError:
            # agent.turn_context needs Hermes runtime deps; its call site is checked from source.
            path = HERMES_SRC / (name.replace(".", "/") + ".py")
            if not path.is_file():
                raise
            stub = types.ModuleType(name)
            stub.__file__ = str(path)
            return stub

    def test_pinned_source_matches_contract(self):
        problems = contract.verify_native_contract(self._import)
        self.assertEqual(problems, [], "LOUD: upstream changed the native message_agent gate: " + "; ".join(problems))

    def test_pinned_source_matches_reap_contract(self):
        """C-1..C-4 surface at the pin. A yaml stub stands in when PyYAML is not installed here."""
        stub = types.ModuleType("yaml")
        stub.safe_load = lambda *a, **k: {}
        stub.YAMLError = Exception
        stub.safe_dump = stub.dump = lambda *a, **k: ""
        stub.SafeLoader = stub.Loader = stub.SafeDumper = stub.Dumper = type("L", (), {})
        extra = {} if importlib.util.find_spec("yaml") else {"yaml": stub}
        with mock.patch.dict(sys.modules, extra):
            problems = contract.verify_reap_contract(self._import)
        self.assertEqual(problems, [], "LOUD: upstream changed the native reap surface: " + "; ".join(problems))

    def test_wrap_on_real_module_lifts_only_the_title(self):
        dm = importlib.import_module("tools.bot_mode_dm")
        probe = importlib.import_module("tools.bot_mode_probe")
        wrap = guard.NativeGateWrap(dm, probe)
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(probe, "is_bot_mode_managed", return_value=True):
                wrap.install()
                try:
                    agent = Agent(title="Launch plan", home=tmp)
                    self.assertTrue(dm.ensure_message_agent_tool(agent))
                    self.assertTrue(any(t["function"]["name"] == "message_agent" for t in agent.tools))
                    self.assertFalse(dm.ensure_message_agent_tool(Agent(title="Launch plan", home=tmp, platform="discord")))
                    self.assertEqual(dm._session_title(agent), "Launch plan")
                finally:
                    wrap.uninstall()
                self.assertFalse(dm.message_agent_authorized(Agent(title="Launch plan", home=tmp)))


class PinTests(unittest.TestCase):
    def test_untested_hermes_commit_refuses_enable(self):
        self.assertEqual(pin.hermes_version_refusal(TESTED), "")
        self.assertIn("untested hermes-agent", pin.hermes_version_refusal("0" * 40))

    def test_docker_image_without_git_reads_baked_build_sha(self):
        pkg = types.ModuleType("hermes_cli")
        info = types.ModuleType("hermes_cli.build_info")
        info.get_code_identity = lambda refresh=False: {"sha": TESTED, "source": "build-file"}
        info.get_build_sha = lambda short=8: TESTED
        pkg.build_info = info
        with mock.patch.dict(sys.modules, {"hermes_cli": pkg, "hermes_cli.build_info": info}), \
                mock.patch.object(pin, "_git_install_commit", return_value=""):
            self.assertEqual(pin.hermes_install_commit(), TESTED)
            self.assertEqual(pin.hermes_version_refusal(""), "")
            info.get_code_identity = lambda refresh=False: {"sha": "1" * 40, "source": "build-file"}
            self.assertIn("untested hermes-agent", pin.hermes_version_refusal(""))

    def test_neither_readable_refuses(self):
        with mock.patch.object(pin, "_git_install_commit", return_value=""), \
                mock.patch.object(pin, "_stamped_install_commit", return_value=""):
            self.assertIn("could not be read", pin.hermes_version_refusal(""))


if __name__ == "__main__":
    unittest.main()
