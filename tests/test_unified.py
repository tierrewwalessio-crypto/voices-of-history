from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import copy
import json
import queue
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "middleware"))

from unified_mod import BASES, CONFIGS, ENTRY, FOLDER, install_or_compile, render, select_unified, descriptor_matches
from safe_actions import ActionJournal, command, text_only, validate
from strategist import Strategist
from usage_budget import BudgetClient, BudgetExceeded, UsageBudget
from unified_runtime import UnifiedRuntime, compact_context
from main import App, load_realism_settings, saved_realism_mode
from overlay import ChatOverlay
from chatgpt_client import ChatGPTPlanClient
from test_chatgpt import FakeResponse
from memory import Memory


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "usage.sqlite3"
        self.now = [100000.0]
        self.cfg = {"calls_per_day": 20, "tokens_per_day": 100000,
                    "automatic_calls_per_day": 6, "automatic_tokens_per_day": 20000}
        self.budget = UsageBudget(self.path, self.cfg, lambda: self.now[0])
        self.messages = [{"role": "user", "content": "Review the country."}]

    def tearDown(self):
        self.temp.cleanup()

    def request(self, purpose="automatic", profile="vanilla"):
        tx = self.budget.reserve(self.messages, purpose, profile)
        self.budget.finish(tx, {"input_tokens": 100, "output_tokens": 100, "total_tokens": 200})

    def test_rolling_burst_and_daily_limits_survive_restarts_and_profile_switch(self):
        for profile in ("vanilla", "the_fire_rises", "cwic_dev"):
            self.request(profile=profile)
        with self.assertRaises(BudgetExceeded):
            self.request()
        self.now[0] += 601
        self.budget = UsageBudget(self.path, self.cfg, lambda: self.now[0])
        for _ in range(3):
            self.request(profile="millennium_dawn")
        self.now[0] += 601
        with self.assertRaises(BudgetExceeded):
            self.request()
        self.request(purpose="chat")
        self.assertEqual(self.budget.snapshot()["automatic_calls"], 6)
        self.now[0] += 86401
        self.request()
        self.assertEqual(self.budget.snapshot()["calls"], 1)

    def test_concurrent_reservations_cannot_overrun_global_allowance(self):
        budget = UsageBudget(self.path, {"calls_per_day": 3})
        def attempt(_):
            try:
                budget.reserve(self.messages, "chat")
                return True
            except BudgetExceeded:
                return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(attempt, range(12))), 3)

    def test_failed_or_crashed_request_still_uses_allowance_and_reported_usage_is_used(self):
        tx = self.budget.reserve(self.messages, "chat")
        estimate = self.budget.snapshot()["tokens"]
        self.assertGreater(estimate, 0)
        self.budget.finish(tx, failed=True)
        self.assertEqual(self.budget.snapshot()["tokens"], estimate)
        self.request("chat")
        self.assertEqual(self.budget.snapshot()["tokens"], estimate + 200)
        with self.assertRaises(BudgetExceeded):
            UsageBudget(self.path, {"tokens_per_day": 100}).reserve(self.messages, "chat")

    def test_quota_error_pauses_without_retry_or_provider_call_after_cap(self):
        client = Mock()
        client.last_usage = None
        client.chat.side_effect = RuntimeError("HTTP 429")
        cfg = {"usage": {"ledger": str(self.path)}, "game_profile": "vanilla"}
        wrapped = BudgetClient(client, cfg)
        for _ in range(2):
            with self.assertRaises((BudgetExceeded, RuntimeError)):
                wrapped.interactive_chat(self.messages)
        self.assertEqual(client.chat.call_count, 1)
        self.assertTrue(wrapped.budget.snapshot()["provider_paused"])

    def test_server_token_usage_and_bounded_reasoning_request_are_preserved(self):
        cfg = {"unified": True, "chatgpt_plan": {"model": "gpt-6.1-sol"},
               "usage": {"ledger": str(self.path)}}
        provider = ChatGPTPlanClient(cfg)
        provider.auth = Mock()
        provider.auth.get_access_token.return_value = "fake-test-token"
        usage = {"input_tokens": 100, "output_tokens": 200, "total_tokens": 300,
                 "output_tokens_details": {"reasoning_tokens": 150}}
        response = FakeResponse(lines=["data: " + json.dumps({"type": "response.completed",
                    "response": {"usage": usage, "output": [{"content": [{"type": "output_text", "text": "Test reply"}]}]}})])
        with patch("chatgpt_client.requests.post", return_value=response) as post:
            client = BudgetClient(provider, cfg)
            self.assertEqual(client.interactive_chat(self.messages), "Test reply")
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["max_output_tokens"], 1200)
        self.assertEqual(body["reasoning"], {"effort": "low"})
        self.assertFalse(body["store"])
        self.assertNotIn("service_tier", body)
        self.assertEqual(client.budget.snapshot()["tokens"], 300)
        self.assertEqual(provider.last_usage["output_tokens_details"]["reasoning_tokens"], 150)


class UnifiedNativeTests(unittest.TestCase):
    def test_launcher_descriptor_formatting_survives_repeated_profile_compilation(self):
        with tempfile.TemporaryDirectory() as temp:
            userdir = Path(temp)
            install_or_compile(userdir, "the_fire_rises", initial=True)
            path = userdir / "mod" / (FOLDER + ".mod")
            canonical = path.read_bytes()
            # Paradox also moves dependencies before supported_version during discovery.
            reordered = canonical.replace(b'dependencies={ "The Fire Rises" }\n', b'').replace(
                b'supported_version=', b'dependencies={ "The Fire Rises" }\n' + b'supported_version=')
            formatted = b'\xef\xbb\xbf' + reordered.replace(b'tags={ "Gameplay" "Events" }',
                b'tags={\r\n\t"Gameplay"\r\n\t"Events"\r\n}').replace(b'\n', b'\r\n')
            self.assertTrue(descriptor_matches(formatted, canonical))
            path.write_bytes(formatted)
            for _ in range(2):
                install_or_compile(userdir, "the_fire_rises")
                self.assertEqual(path.read_bytes(), formatted)
            for profile in ("vanilla", "millennium_dawn", "cwic_dev", "the_fire_rises"):
                install_or_compile(userdir, profile)
                expected = render(profile)["descriptor.mod"] + f'path="{(userdir / "mod" / FOLDER).resolve().as_posix()}"\n'.encode()
                self.assertTrue(descriptor_matches(path.read_bytes(), expected))

    def test_descriptor_guard_rejects_meaningful_edits_without_overwriting_them(self):
        for change in (lambda b: b.replace(b"Unified ChatGPT", b"Different mod"),
                       lambda b: b.replace(b"Gameplay", b"Other tag"),
                       lambda b: b + b'\nreplace_path="common/ideologies"\n',
                       lambda b: b.replace(b'path="', b'path="elsewhere/'),
                       lambda b: b.replace(b"1.19.*", b"1.18.*"),
                       lambda b: b.replace(b"The Fire Rises", b"Another overhaul"),
                       lambda b: b + b'name="Voices of History - Unified ChatGPT"\n',
                       lambda b: b + b'dependencies={ "The Fire Rises" }\n'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temp:
                userdir = Path(temp)
                install_or_compile(userdir, "the_fire_rises", initial=True)
                path = userdir / "mod" / (FOLDER + ".mod")
                edited = change(path.read_bytes())
                path.write_bytes(edited)
                with self.assertRaises(RuntimeError):
                    install_or_compile(userdir, "vanilla")
                self.assertEqual(path.read_bytes(), edited)

    def test_descriptor_guard_rejects_malformed_blocks_and_duplicate_fields(self):
        expected = render("the_fire_rises")["descriptor.mod"]
        for actual in (expected + b"junk", expected.replace(b'}', b'', 1),
                       expected + b'version="0.2.0-local"\n',
                       expected.replace(b'"Gameplay" "Events"', b'"Events" "Gameplay"')):
            self.assertFalse(descriptor_matches(actual, expected))

    def test_selection_all_four_and_conflicts(self):
        self.assertEqual(select_unified([ENTRY]), "vanilla")
        for base, profile in BASES.items():
            self.assertEqual(select_unified([base, ENTRY]), profile)
        for enabled in ([ENTRY, ENTRY], [ENTRY, "mod/unknown.mod"],
                        [ENTRY, *BASES], [ENTRY, "mod/HOI4_AI_Voices_TFR.mod"]):
            with self.assertRaises(RuntimeError):
                select_unified(enabled)

    def test_only_selected_ideologies_and_native_priority_expiry(self):
        expected = {"vanilla": {"fascism", "communism", "democratic", "neutrality"},
                    "millennium_dawn": {"fascism", "communism", "democratic", "neutrality", "nationalist"}}
        import re
        from profiles import TFR_GROUPS, CWIC_GROUPS
        expected["the_fire_rises"] = {k for k, _ in TFR_GROUPS}
        expected["cwic_dev"] = {k for k, _ in CWIC_GROUPS}
        expected["millennium_dawn_oem"] = expected["millennium_dawn"]
        for profile in CONFIGS:
            files = render(profile)
            dependency = {"millennium_dawn_oem": "Millennium Dawn OEM Local", "millennium_dawn": "Millennium Dawn: A Modern Day Mod", "cwic_dev": "Cold War Iron Curtain Dev Build", "the_fire_rises": "The Fire Rises"}.get(profile)
            if dependency:
                self.assertIn(f'dependencies={{ "{dependency}" }}'.encode(), files["descriptor.mod"])
            else:
                self.assertNotIn(b"dependencies", files["descriptor.mod"])
            code = b"\n".join(v for k, v in files.items() if k.endswith(".txt"))
            governments = set(re.findall(rb"has_government\s*=\s*(\w+)", code))
            self.assertEqual({s.decode() for s in governments}, expected[profile])
            self.assertIn(b"VOICES_TICK|", files["common/on_actions/zzz_ai_diplo_on_actions.txt"])
            strategy = files["common/ai_strategy/voices_priorities.txt"]
            self.assertIn(b"abort_when_not_enabled = yes", strategy)
            self.assertNotIn(b"add_ai_strategy", strategy)

    def test_installation_preserves_existing_mods_playsets_and_refuses_external_edits(self):
        with tempfile.TemporaryDirectory() as temp:
            userdir = Path(temp)
            (userdir / "mod").mkdir()
            protected = userdir / "mod/existing.mod"
            protected.write_bytes(b"preserve")
            playset = userdir / "dlc_load.json"
            playset.write_text('{"enabled_mods":["mod/existing.mod"]}')
            manifest = install_or_compile(userdir, "the_fire_rises", initial=True)
            self.assertEqual(protected.read_bytes(), b"preserve")
            self.assertIn("existing.mod", playset.read_text())
            with self.assertRaises(RuntimeError):
                install_or_compile(userdir, "vanilla", initial=True)
            changed = userdir / "mod" / FOLDER / "interface/ai_diplo.gui"
            changed.write_text("User's external edit")
            with self.assertRaises(RuntimeError):
                install_or_compile(userdir, "vanilla")
            self.assertEqual(changed.read_text(), "User's external edit")
            self.assertEqual(manifest["profile"], "the_fire_rises")


class ActionTests(unittest.TestCase):
    def setUp(self):
        self.state = {"profile": "the_fire_rises", "tag": "USA", "gid": "17", "day": "20"}
        self.world = {"SOV": {"leader": "Current representative", "maj": "1"}}

    def action(self, action, text, **kwargs):
        return validate(action, self.state, self.world, kwargs.get("channel", "leader:SOV"),
                        kwargs.get("decision", "accept"), text, "the_fire_rises", kwargs.get("automatic", False))

    def test_no_actions_negotiation_and_third_country_authority(self):
        action = {"name": "send_equipment", "args": {"sender": "SOV", "receiver": "USA", "equipment_type": "infantry_equipment", "quantity": 100}}
        for text in ("Take no actions. Send rifles.", "Hypothetically send rifles", "Do nothing; discuss equipment", "Preview only: send equipment"):
            with self.assertRaises(ValueError):
                self.action(action, text)
        with self.assertRaises(ValueError):
            self.action(action, "Send rifles", decision="negotiate")
        action["args"]["sender"] = "GER"
        with self.assertRaises(ValueError):
            self.action(action, "Send rifles")

    def test_transfer_guard_is_atomic_removal_precedes_receipt_and_no_raw_scripts(self):
        action = self.action({"name": "send_equipment", "args": {"sender": "SOV", "receiver": "USA", "equipment_type": "infantry_equipment", "quantity": 100}}, "Send 100 rifles")
        code = command(action, "a" * 32, 17)
        self.assertIn("has_equipment = { infantry_equipment > 99 }", code)
        self.assertLess(code.index("amount = -100"), code.index("amount = 100"))
        self.assertNotIn("amount = -100 producer", code)  # Removal covers all producers.
        self.assertIn("status_executed", code)
        for name in ("raw_console", "raw_effect", "declare_war", "add_factories"):
            with self.assertRaises(ValueError):
                self.action({"name": name, "args": {}}, "Please act")

    def test_pact_guard_postcondition_and_military_access_consent(self):
        pact = self.action({"name": "non_aggression_pact", "args": {"country_a": "SOV", "country_b": "USA"}}, "Can we sign a non-aggression pact?")
        code = command(pact, "b" * 32, 17)
        self.assertIn("NOT = { has_war_with = USA }", code)
        self.assertIn("has_non_aggression_pact_with = USA", code)
        self.assertIn("has_country_flag = voices_tx_", code)
        scoped = command(pact, "b" * 32, 17, day=20)
        self.assertIn("check_variable = { aidiplo_day = 20 }", scoped)
        with self.assertRaises(ValueError):
            self.action({"name": "military_access", "args": {"country_a": "SOV", "country_b": "USA"}}, "Give us military access")

    def test_pilot_has_no_resource_grants_and_priority_expires(self):
        action = self.action({"name": "ai_priority", "args": {"target": "SOV", "priority": "infantry"}}, "Weekly review", automatic=True, channel="strategy:SOV")
        code = command(action, "c" * 32, 17)
        self.assertIn("days = 14", code)
        self.assertIn("flag = voices_priority_infantry value = 1 days = 14", code)
        self.assertIn("clr_country_flag = voices_priority_mobile", code)
        with self.assertRaises(ValueError):
            self.action({"name": "add_political_power", "args": {"target": "SOV", "amount": 5}}, "Weekly review", automatic=True, channel="strategy:SOV")

    def test_result_journal_requires_matching_native_id_and_gid_and_never_confirms_preview(self):
        with tempfile.TemporaryDirectory() as temp:
            journal = ActionJournal(Path(temp) / "actions.sqlite3")
            action = {"name": "ai_priority", "actor": "SOV", "player": "USA", "priority": "hold"}
            tx = journal.create(action, self.state, "the_fire_rises", preview=False)
            self.assertIsNone(journal.receive(f"VOICES_ACTION|id={tx}|gid=18|status=applied"))
            result = journal.receive(f"VOICES_ACTION|id={tx}|gid=17|status=applied")
            self.assertEqual(result["status"], "applied")
            self.assertIsNone(journal.receive(f"VOICES_ACTION|id={tx}|gid=17|status=applied"))
            preview = journal.create(action, self.state, "the_fire_rises", preview=True)
            self.assertIsNone(journal.receive(f"VOICES_ACTION|id={preview}|gid=17|status=applied"))

    def test_inline_console_result_markers_survive_quote_removal(self):
        import re
        action = {"name": "improve_relations", "actor": "SOV", "player": "USA", "a": "SOV", "b": "USA"}
        with tempfile.TemporaryDirectory() as temp:
            journal = ActionJournal(Path(temp) / "actions.sqlite3")
            tx = journal.create(action, self.state, "the_fire_rises", preview=False)
            code = command(action, tx, 17, day=20)
            values = re.findall(r'log = (\S+)', code.replace('"', ''))
            self.assertEqual(len(values), 4)
            self.assertTrue(all(re.fullmatch(r"[A-Za-z0-9_]+", value) for value in values))
            self.assertIsNone(journal.receive(f"VOICES_ACTION_id_{tx}_gid_18_status_applied"))
            result = journal.receive(f"[12:00][2000.01.01.12]: VOICES_ACTION_id_{tx}_gid_17_status_applied")
            self.assertEqual(result["status"], "applied")
            self.assertIsNone(journal.receive(f"VOICES_ACTION_id_{tx}_gid_17_status_applied"))

    def test_opinion_branch_reports_execution_without_claiming_pair_verification(self):
        action = {"name": "improve_relations", "actor": "SOV", "player": "USA", "a": "SOV", "b": "USA"}
        code = command(action, "d" * 32, 17, day=20)
        self.assertIn("add_opinion_modifier = { target = USA modifier = voices_dialogue_goodwill }", code)
        self.assertIn("flag = voices_signal_USA value = 1 days = 30", code)
        self.assertIn("_status_executed", code)
        self.assertNotIn("_status_applied", code)
        self.assertNotIn("has_opinion_modifier", code)


class StrategistTests(unittest.TestCase):
    def test_pause_catchup_reload_weekly_and_country_scoping(self):
        with tempfile.TemporaryDirectory() as temp:
            pilot = Strategist("the_fire_rises", Path(temp) / "pilot.json", Path(temp) / "ledger.sqlite3")
            state = {"tag": "USA", "name": "United States", "gid": "17", "day": "20", "date": "2020.1.19"}
            world = {t: {"tag": t, "name": t, "maj": "1", "cap": "0", "date": state["date"]} for t in ("SOV", "CHI", "GER")}
            pilot.save(list(world), True, state, world)
            for _ in range(20):
                self.assertIsNone(pilot.due(state, world))  # Paused exports.
            for day in range(21, 27):
                state["day"] = str(day)
                self.assertIsNone(pilot.due(state, world))
            state["day"] = "27"
            review = pilot.due(state, world)
            self.assertIsNotNone(review)
            pilot.finish(review, "Own country review")
            self.assertEqual(pilot.history(17, review["actor"], 27)[0]["summary"], "Own country review")
            self.assertEqual(pilot.history(18, review["actor"], 27), [])
            self.assertEqual(pilot.history(17, "OTHER", 27), [])
            self.assertIsNone(pilot.due(state, world))
            state["day"] = "200"
            self.assertIsNone(pilot.due(state, world))  # No load catch-up burst.
            pilot.crisis("SOV declared war on USA")
            self.assertIsNone(pilot.due(state, world))  # Crisis while paused waits for next day.
            state["day"] = "201"
            crisis = pilot.due(state, world)
            self.assertIn("Crisis", crisis["reason"])
            restarted = Strategist("the_fire_rises", pilot.path, pilot.ledger)
            self.assertIsNone(restarted.due(state, world))

    def test_invalid_pilot_selection_and_stale_nation_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            pilot = Strategist("vanilla", Path(temp) / "pilot.json", Path(temp) / "ledger.sqlite3")
            state = {"tag": "USA", "gid": "1", "day": "1", "date": "1936.1.1"}
            world = {t: {"maj": "1", "date": "old"} for t in ("GER", "JAP", "SOV")}
            with self.assertRaises(ValueError):
                pilot.save(["USA", "SOV", "GER"], True, state, world)
            pilot.save(list(world), True, state, world)
            for day in range(2, 12):
                state["day"] = str(day)
                self.assertIsNone(pilot.due(state, world))


class UnifiedTurnTests(unittest.TestCase):
    def test_foreign_voice_uses_current_named_speaker_and_current_message_language(self):
        with tempfile.TemporaryDirectory() as temp:
            app = App.__new__(App)
            app.cfg = {"unified": True, "game_profile": "millennium_dawn", "usage": {"ledger": str(Path(temp) / "usage.sqlite3")}, "actions": {"live_approved": False}}
            app.latest_state = {"profile": "millennium_dawn", "tag": "USA", "name": "United States", "leader": "Bill Clinton", "gid": "17", "day": "1", "date": "2000.1.1", "live": True}
            app.world = {"SOV": {"tag": "SOV", "leader": "Current Representative", "name": "Russia"}}
            provider = Mock()
            provider.last_usage = None
            provider.chat.return_value = '{"message":"Let us talk.","decision":"inform","actions":[]}'
            app.llm = BudgetClient(provider, app.cfg)
            app.memory = Mock()
            app.memory.get_history.return_value = [{"role": "assistant", "content": "Old leader's earlier reply."}]
            app.memory.facts_block.return_value = ""
            app._lang_directive = Mock(return_value="Reply in the current player's language.")
            app.injector, app.journal, app.q = Mock(), Mock(), queue.Queue()
            app.journal.recent.return_value = []
            app.injector.dry_run = True
            app.lore, app.auto_exec, app._log = None, True, Mock()
            text = "Bonjour. Take no actions."
            app._unified_turn(text, "leader:SOV", "Old Representative")
            messages = provider.chat.call_args.args[0]
            self.assertIn("You are Current Representative", messages[0]["content"])
            self.assertIn("2000.1.1", messages[0]["content"])
            self.assertNotIn("You are Old Representative", messages[0]["content"])
            app._lang_directive.assert_called_once_with(text)
            self.assertEqual(provider.chat.call_count, 1)
            app.journal.create.assert_not_called()
            app.injector.execute.assert_not_called()

    def test_plain_acceptance_or_malformed_action_record_cannot_become_an_effect(self):
        app = App.__new__(App)
        app.q, app._log = queue.Queue(), Mock()
        for raw in ('Russia agrees. The action below is a preview.',
                    '{"message":"Russia accepts.","actions":[{"name":"improve_relations"}]}',
                    '{"message":"Russia accepts.","decision":"accept","actions":"improve_relations"}'):
            parsed = app._parse_unified_reply(raw, "leader:SOV")
            self.assertEqual(parsed["decision"], "inform")
            self.assertEqual(parsed["actions"], [])
            self.assertIn("No game effect was queued", app.q.get_nowait()[1])
        parsed = app._parse_unified_reply('{"message":"Agreed in principle.","decision":"accept","actions":[]}', "leader:SOV")
        self.assertEqual(parsed["actions"], [])
        self.assertIn("no supported game action", app.q.get_nowait()[1])

    def test_saved_profile_style_skips_startup_selector_and_invalid_style_still_prompts(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "mode-tfr.txt"
            cfg = {"behavior": {"realism_mode_file": str(path), "default_realism_mode": "plausible"}}
            for contents, expected in ((None, None), ("plausible", "plausible"),
                                       ("sandbox", "sandbox"), ("invalid", None)):
                if contents is not None:
                    path.write_text(contents, encoding="utf-8")
                saved = saved_realism_mode(path)
                self.assertEqual(saved, expected)
                self.assertEqual(load_realism_settings(cfg)[1], expected or "plausible")
                overlay = Mock()
                ChatOverlay._show_startup(overlay, saved is None)
                if expected:
                    overlay.hide_panel.assert_called_once_with()
                    overlay.settings_show.assert_not_called()
                else:
                    overlay.settings_show.assert_called_once_with()
                    overlay.hide_panel.assert_not_called()

    def test_new_game_log_invalidates_cached_state_without_deleting_saved_memory(self):
        app = App.__new__(App)
        app.latest_state, app.world, app.states = {"gid": "17"}, {"SOV": {}}, {"1": {}}
        app.diplomatic_contacts = {"SOV": {}}
        app.memory, app.ui, app._log = Mock(), Mock(), Mock()
        app.strategist = Mock()
        app.plan_slots = [{"cached": True}] * 6
        app._reset_log_session()
        self.assertEqual((app.gid, app.day), (0, 0))
        self.assertEqual(app.latest_state, {})
        self.assertEqual(app.world, {})
        app.memory.set_context.assert_called_once_with(0, 0)
        app.memory.forget_future.assert_not_called()
        self.assertIsNone(app.strategist.previous)

    def test_historical_catchup_snapshot_cannot_make_a_unified_model_request(self):
        app = App.__new__(App)
        app.cfg = {"game_profile": "the_fire_rises"}
        app.latest_state = {"gid": "17", "profile": "the_fire_rises", "live": False}
        app.world, app.llm = {}, Mock()
        with self.assertRaises(ValueError):
            app._unified_turn("Greetings", "decree", "Office")
        app.llm.interactive_chat.assert_not_called()

    def test_captured_turn_memory_does_not_change_after_campaign_context_switch(self):
        with tempfile.TemporaryDirectory() as temp:
            memory = Memory(str(Path(temp) / "memory.sqlite3"))
            memory.set_context(18, 30)
            memory.add_turn("leader:SOV", "Past briefing", "Past reply", 17, 20)
            self.assertEqual(memory.get_history("leader:SOV"), [])
            self.assertEqual(len(memory.get_history("leader:SOV", gameid=17, day=20)), 2)
            self.assertEqual(memory.get_history("leader:SOV", gameid=17, day=19), [])
            self.assertEqual((memory.gameid, memory.day), (18, 30))
            memory.conn.close()

    def test_text_only_ignores_model_actions_without_second_request_or_game_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            app = App.__new__(App)
            app.cfg = {"unified": True, "game_profile": "the_fire_rises", "usage": {"ledger": str(Path(temp) / "usage.sqlite3")}, "actions": {"live_approved": False}}
            app.latest_state = {"profile": "the_fire_rises", "tag": "USA", "name": "United States", "leader": "Donald Trump", "gid": "17", "day": "20", "date": "2020.1.19", "live": True}
            app.world = {"SOV": {"tag": "SOV", "leader": "Vladimir Putin", "name": "Russian Federation"}}
            provider = Mock()
            provider.last_usage = None
            provider.chat.return_value = '{"message":"A hypothetical discussion.","decision":"accept","actions":[{"name":"send_equipment","args":{}}]}'
            app.llm = BudgetClient(provider, app.cfg)
            app.memory = Mock()
            app.memory.get_history.return_value = []
            app.memory.facts_block.return_value = ""
            app._lang_directive = lambda *_: "Reply in English."
            app.injector, app.journal, app.q = Mock(), Mock(), queue.Queue()
            app.journal.recent.return_value = []
            app.injector.dry_run = True
            app.lore, app.auto_exec = None, True
            app._log = Mock()
            app._unified_turn("Take no actions. Discuss TFR lore.", "leader:SOV", "Putin")
            self.assertEqual(provider.chat.call_count, 1)
            app.journal.create.assert_not_called()
            app.injector.write_run_file.assert_not_called()
            app.injector.execute.assert_not_called()
            app.memory.add_turn.assert_called_once_with("leader:SOV", "Take no actions. Discuss TFR lore.", "A hypothetical discussion.", 17, 20)

    def test_compact_context_does_not_send_full_state_dictionary_or_every_country(self):
        state = {"tag": "USA", "name": "United States", "date": "2020.1.19"}
        world = {f"D{i:02}": {"tag": f"D{i:02}", "name": f"Country{i}", "maj": "0"} for i in range(100)}
        context = compact_context(state, world, "D99")
        self.assertIn("Country99", context)
        self.assertNotIn("Country98", context)
        self.assertNotIn("KNOWN STATES", context)


if __name__ == "__main__":
    unittest.main()
