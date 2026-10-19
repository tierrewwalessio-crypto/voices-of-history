from pathlib import Path
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import copy
import json
import queue
import sys
import tempfile
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'middleware')]
import telemetry
from md_compat import is_oem, is_regular_md
from proposals import Proposals, aircraft_order
from safe_actions import ActionJournal, command, validate, response_schema
from chatgpt_client import ChatGPTPlanClient
from unified_runtime import UnifiedRuntime
from unified_mod import CONFIGS, render, install_or_compile


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.state = dict(profile=telemetry.OEM, tag='USA', gid='1234', day='20', live=True)
        self.line = ('AIDIPLO_TELEMETRY|v=1|profile=millennium_dawn_oem|tag=USA|gid=1,234|day=20|date=2000.1.20'
                     '|divisions=20|low_strength=4|low_org=2|planes=1,234|ships=12|fuel_k=1,234.500'
                     '|stock.infantry_weapons_type=10|army.infantry_weapons_type=100|need.infantry_weapons_type=125'
                     '|has_gdp=1|econ.gdp=1000.000|has_balance=1|econ.balance=-1.250|has_growth=1|econ.growth=0.025')

    def test_units_deficits_growth_missing_and_negative_balance(self):
        packet = telemetry.parse(self.line)
        self.assertTrue(telemetry.matches(packet, self.state))
        self.state['telemetry'] = packet
        text = telemetry.context(self.state)
        for required in ('net deficit after reserves 15', 'fielded-army deficit 25', '2.5 percent',
                         '-1.25 billion USD/week', 'treasury: unavailable', 'counts, not weighted averages'):
            self.assertIn(required, text)
        self.assertNotIn('OEM treasury: 0', text)
        self.assertEqual(telemetry.number('1.234,500'), 1234.5)
        self.assertEqual(telemetry.number('1,234', True), 1234)

    def test_bad_packets_campaign_profile_future_and_stale_cannot_supply_context(self):
        for bad in (self.line+'|divisions=9', self.line.replace('v=1', 'v=2'),
                    self.line.replace('low_org=2','low_org=-1'), self.line.replace('econ.gdp=1000.000','econ.gdp=nan'),
                    self.line.replace('has_gdp=1','has_gdp=2'), self.line.replace('millennium_dawn_oem','vanilla')):
            self.assertIsNone(telemetry.parse(bad))
        for changes in ({'gid':'999'}, {'tag':'SOV'}, {'profile':'millennium_dawn'}, {'day':'19'}, {'day':'22'}):
            state = dict(self.state, **changes)
            state['telemetry'] = telemetry.parse(self.line)
            self.assertIn('unavailable or stale', telemetry.context(state))

    def test_native_export_is_opt_in_player_only_and_economy_is_oem_only(self):
        for profile in CONFIGS:
            default = render(profile)
            self.assertNotIn('common/scripted_effects/voices_telemetry.txt', default)
            opted = render(profile, telemetry=True)
            native = opted['common/scripted_effects/voices_telemetry.txt'].decode()
            export = opted['common/scripted_effects/ai_diplo_export.txt'].decode('utf-8-sig')
            self.assertEqual(export.count('voices_export_telemetry = yes'), 1)
            self.assertIn('every_country_division', native)
            self.assertNotIn('add_equipment_to_stockpile', native)
            self.assertNotIn('add_equipment_production', native)
            self.assertEqual('has_variable = gdp_total' in native, profile == telemetry.OEM)
            self.assertIn('num_equipment_in_armies@', native)
            self.assertIn('num_target_equipment_in_armies@', native)

    def test_optional_export_can_be_removed_without_orphan_files(self):
        with tempfile.TemporaryDirectory() as temp:
            install_or_compile(temp, 'vanilla', initial=True, telemetry=True)
            install_or_compile(temp, 'vanilla', telemetry=False)
            target = Path(temp) / 'mod/HOI4_AI_Voices_Universal'
            self.assertFalse((target / 'common/scripted_effects/voices_telemetry.txt').exists())
            install_or_compile(temp, 'vanilla', telemetry=False)

    def test_real_descriptor_shape_and_distinct_namespaces(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'descriptor.mod'
            path.write_text('name="Millennium Dawn OEM Local"\nversion="2.0.2"\nsupported_version="1.19.*"\n')
            self.assertTrue(is_oem(path))
            self.assertFalse(is_regular_md(path))
        cfg = json.loads((ROOT/'middleware/config-unified-md-oem.json').read_text())
        self.assertTrue(cfg['injection']['dry_run'])
        self.assertFalse(cfg['actions']['live_approved'])
        self.assertFalse(cfg['events']['enabled'])
        self.assertIn('md_oem', cfg['paths']['memory_db'])
        self.assertIn('millennium_dawn_oem', cfg['strategist']['settings_file'])

    def test_ingestion_rejects_wrong_profile_and_retains_previous_valid_snapshot(self):
        app = UnifiedRuntime()
        app.cfg = {'telemetry': {'enabled':True}}
        app.latest_state = self.state
        packet = telemetry.parse(self.line)
        app._on_telemetry(packet)
        self.assertEqual(app.latest_state['telemetry'], packet)
        app._on_telemetry(dict(packet, profile='millennium_dawn'))
        self.assertEqual(app.latest_state['telemetry'], packet)
        app._on_telemetry(dict(packet, day='19'))
        self.assertEqual(app.latest_state['telemetry'], packet)


class ProposalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.journal = ActionJournal(Path(self.temp.name)/'actions.sqlite3')
        self.store = Proposals(self.journal)
        self.state = dict(profile=telemetry.OEM, tag='USA', gid='17', day='20', live=True, leader='Player')
        self.world = {'SOV': {'leader':'Representative'}}
        self.channel, self.profile = 'leader:SOV', telemetry.OEM
        self.action = {'name':'propose_equipment', 'args': {'sender':'SOV','receiver':'USA',
                      'equipment_type':'infantry_weapons_type','quantity':None}}

    def create(self):
        return self.store.create(self.action, 'Please send us rifles', self.state, self.world, self.channel, self.profile)

    def test_proposal_schema_is_opt_in_and_negotiation_catalog_has_no_conflict(self):
        default = response_schema(self.profile)
        opted = response_schema(self.profile, proposals=True)
        self.assertNotIn('propose_equipment',json.dumps(default))
        self.assertIn('propose_equipment',json.dumps(opted))
        cfg = {'unified':True,'game_profile':self.profile,'proposals':{'enabled':True}}
        self.assertIn('propose_equipment',json.dumps(ChatGPTPlanClient(cfg).response_schema))
        app = UnifiedRuntime()
        app.cfg,app._lang_directive = cfg,lambda _sample:''
        system = app._unified_system(self.channel,self.state,self.world)
        self.assertIn('Negotiation may contain only propose_equipment drafts',system)
        self.assertNotIn('Refusal/negotiation/information has',system)

    def reply(self, text):
        return self.store.reply(text, self.state, self.world, self.channel, self.profile)

    def test_quantity_only_survives_restart_and_confirmation_is_exactly_once(self):
        old, _ = self.create()
        self.store = Proposals(ActionJournal(self.journal.path))
        message, action, current = self.reply('25')
        self.assertIsNone(action)
        self.assertNotEqual(old, current)
        self.assertIn('25 infantry_weapons_type', message)
        self.assertIsNone(self.reply('confirm '+old)[1])
        self.assertIsNone(self.reply('confirm')[1])
        message, normalized, proposal_id = self.reply('confirm '+current)
        self.assertEqual(normalized['quantity'],25)
        tx = self.journal.create(normalized,self.state,self.profile,False)
        self.store.bind(proposal_id,tx)
        self.assertEqual(self.store.recent(self.profile,self.state,self.channel)[0]['state'],'pending')
        self.journal.receive(f'VOICES_ACTION_id_{tx}_gid_17_status_executed')
        self.assertEqual(self.store.recent(self.profile,self.state,self.channel)[0]['state'],'executed')
        self.assertIsNone(self.reply('confirm '+current)[1])

    def test_change_invalid_quantity_cancellation_and_narrative_are_safe(self):
        old, _ = self.create()
        self.assertIsNone(self.reply('Tell me more'))
        self.reply('0')
        self.assertEqual(self.store.pending(self.state,self.world,self.channel,self.profile)[0][0], old)
        _, _, current = self.reply('make that 40')
        self.assertNotEqual(old,current)
        self.assertIn('cancelled', self.reply('cancel')[0])
        self.assertIsNone(self.reply('confirm '+current)[1])
        with closing(self.journal.connect()) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM actions').fetchone()[0], 0)

    def test_campaign_tag_profile_channel_leader_rollback_and_age_boundaries(self):
        for field, value in (('gid','18'),('tag','GER'),('day','19'),('day','23')):
            self.create()
            state = dict(self.state, **{field:value})
            self.assertFalse(self.store.pending(state,self.world,self.channel,self.profile))
        self.create()
        self.assertFalse(self.store.pending(self.state,{'SOV':{'leader':'New'}},self.channel,self.profile))
        self.create()
        self.assertFalse(self.store.pending(self.state,self.world,'leader:ENG',self.profile))
        self.assertFalse(self.store.pending(self.state,self.world,self.channel,'millennium_dawn'))

    def test_third_country_unsupported_aircraft_and_text_only_cannot_create_drafts(self):
        for action,text in ((dict(self.action,args=dict(self.action['args'],sender='ENG')),'send rifles'),
                            (dict(self.action,args=dict(self.action['args'],equipment_type='F-22A Raptor')),'send equipment'),
                            (self.action,'hypothetical send rifles')):
            with self.assertRaises(ValueError):
                self.store.create(action,text,self.state,self.world,self.channel,self.profile)
        with self.assertRaisesRegex(ValueError, 'different item'):
            self.store.create(self.action,'Send artillery',self.state,self.world,self.channel,self.profile)

    def test_concurrent_confirmation_claims_only_once(self):
        self.create()
        _,_,current = self.reply('25')
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.reply('confirm '+current), range(2)))
        self.assertEqual(sum(r[1] is not None for r in results),1)

    def test_runtime_quantity_and_confirm_use_no_model_and_dry_run_never_delivers(self):
        self.create()
        app = UnifiedRuntime()
        app.cfg = {'game_profile':self.profile,'actions':{'live_approved':False}}
        app.latest_state, app.world, app.proposals = self.state,self.world,self.store
        app.memory, app.llm, app.injector = Mock(),Mock(),Mock(dry_run=True)
        app.q,app.journal,app.auto_exec = queue.Queue(),self.journal,True
        app._log,app._deliver_unified = Mock(),Mock()
        app._unified_turn('25',self.channel,'Representative')
        current = self.store.pending(self.state,self.world,self.channel,self.profile)[0][0]
        app._unified_turn('confirm '+current,self.channel,'Representative')
        app.llm.interactive_chat.assert_not_called()
        app._deliver_unified.assert_not_called()
        self.assertEqual(self.store.recent(self.profile,self.state,self.channel)[0]['state'],'preview')
        app._unified_turn('confirm '+current,self.channel,'Representative')
        self.assertEqual(len(self.journal.recent(self.profile,17)),1)

    def test_aircraft_orders_are_persistently_unsupported_without_a_model_or_command(self):
        self.assertTrue(aircraft_order('Order 25 F-22s from Lockheed'))
        self.assertFalse(aircraft_order('How many F-22s do we have?'))
        self.assertFalse(aircraft_order('hypothetical: order 25 F-22s'))
        message = self.store.unsupported('Order 25 F-22s',self.state,self.world,'decree',self.profile)
        self.assertIn('per-order delivery accounting',message)
        self.assertEqual(self.store.recent(self.profile,self.state,'decree')[0]['state'],'unsupported')
        self.assertEqual(self.journal.recent(self.profile,17),[])

    def test_generated_opt_in_blocks_are_balanced_and_no_free_resource_effects_exist(self):
        import re
        for profile in CONFIGS:
            for key, data in render(profile,telemetry=True).items():
                if key.endswith('.txt'):
                    text = re.sub(r'"[^"\n]*"|#[^\n]*', '', data.decode('utf-8-sig'))
                    self.assertEqual(text.count('{'),text.count('}'),key)

    def test_invented_quantity_is_rejected_and_status_uses_matching_engine_result(self):
        with self.assertRaisesRegex(ValueError, 'player must supply'):
            self.store.create(dict(self.action,args=dict(self.action['args'],quantity=25)),
                              'Send rifles',self.state,self.world,self.channel,self.profile)
        self.create()
        _,_,current = self.reply('25')
        _,action,_ = self.reply('confirm '+current)
        tx = self.journal.create(action,self.state,self.profile,False)
        self.store.bind(current,tx)
        self.assertIn('pending',self.reply('status '+current)[0])
        self.journal.receive(f'VOICES_ACTION_id_{tx}_gid_18_status_executed')
        self.assertIn('pending',self.reply('status '+current)[0])
        self.journal.receive(f'VOICES_ACTION_id_{tx}_gid_17_status_executed')
        self.assertIn('Engine effect branch executed',self.reply('status '+current)[0])


if __name__ == '__main__':
    unittest.main()
