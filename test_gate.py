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


class ContractTests(unittest.TestCase):
    """Fail loudly if upstream renames the gate or changes a signature."""

    def importer(self, dm, probe):
        mods = {"tools.bot_mode_dm": dm, "tools.bot_mode_probe": probe}
        its = types.ModuleType("agent.inline_tool_executors")
        its.INLINE_TOOL_EXECUTORS = {"message_agent": object()}
        mods["agent.inline_tool_executors"] = its
        mods["agent.turn_context"] = types.ModuleType("agent.turn_context")
        sources = {
            "agent.inline_tool_executors": '"tools.bot_mode_dm", "message_agent_tool"',
            "agent.turn_context": "ensure_message_agent_tool(agent)",
        }
        real_source = contract._source

        def fake_source(obj):
            for name, text in sources.items():
                if obj is mods.get(name):
                    return text
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

    PREFIXES = ("tools", "agent")

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
