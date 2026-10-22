"""Real memory transactions with reference typed input; no model accuracy claim."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from moqi.api import HomeMemory, Observation
from moqi.constraint_scopes import applies
from moqi.memory_store import LEGACY_VERSIONED, objects, restore_state, state

AT = datetime(2026,10,22,12,tzinfo=timezone.utc)
CONFIG = {'site':'SYNTHETIC-CORRECTIONS','timezone':'Asia/Shanghai','entities':{
    'switch.lamp':{'role':'actuator','room':'dining','category':'light','risk':'low'},
    'binary_sensor.presence':{'role':'presence','room':'dining'},
    'climate.study':{'role':'actuator','room':'study','category':'hvac','risk':'medium'},
}}


class ReferenceInterpretation:
    def __init__(self, **fields):
        self.fields = fields

    def interpret(self, text, *, selected_event=None):
        return {'status':'interpreted','provenance':'reference_fixture',**self.fields}


class CorrectionWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = str(Path(self.temp.name)/'memory.sqlite3')
        self.engine = HomeMemory(CONFIG,self.database)

    def tearDown(self):
        self.engine.close()
        self.temp.cleanup()

    def preview(self, at=AT, **fields):
        return self.engine.propose_change('Synthetic explicit statement',at,ReferenceInterpretation(**fields))

    def seed_routine(self):
        for offset in range(8,0,-1):
            start = AT-timedelta(days=offset)
            self.engine.ingest(Observation('switch.lamp',start-timedelta(minutes=1),'off'))
            self.engine.ingest(Observation('binary_sensor.presence',start,'on'))
            self.engine.observe_interval(start,start+timedelta(minutes=30))
            self.engine.ingest(Observation('switch.lamp',start+timedelta(minutes=5),'on','off',
                                          source='human',evidence='Synthetic physical control'))
        self.engine.ingest(Observation('switch.lamp',AT-timedelta(minutes=1),'off'))
        self.engine.ingest(Observation('binary_sensor.presence',AT,'on'))
        self.engine.observe_interval(AT,AT+timedelta(minutes=30))
        self.engine.learn(AT)
        return next(r for r in self.engine.store.events() if r['attributed_source']=='human')

    def test_preview_and_cancel_leave_memory_unchanged(self):
        before = state(self.engine.store)
        request = self.preview(kind='constraint',entity_id='switch.lamp',action='turn_off',scope='temporary')
        self.assertEqual(state(self.engine.store),before)
        self.engine.cancel_change(request['id'],AT)
        self.assertEqual(state(self.engine.store),before)
        with self.assertRaisesRegex(ValueError,'pending'):
            self.engine.apply_change(request['id'],AT)

    def test_apply_restart_idempotence_expiry_and_undo(self):
        request = self.preview(kind='constraint',entity_id='switch.lamp',action='turn_off',scope='temporary')
        applied = self.engine.apply_change(request['id'],AT+timedelta(minutes=1))
        self.engine.apply_change(request['id'],AT+timedelta(minutes=2))
        self.assertEqual(len(objects(self.engine.store,'memory_constraints')),1)
        self.engine.close()
        self.engine = HomeMemory(CONFIG,self.database)
        value = objects(self.engine.store,'memory_constraints')[0]
        self.assertEqual(value['valid_from'],applied['applied_at'])
        self.assertFalse(applies(value,'switch.lamp','turn_off',AT+timedelta(hours=11),CONFIG))
        self.engine.undo_change(request['id'],AT+timedelta(minutes=3))
        self.engine.undo_change(request['id'],AT+timedelta(minutes=4))
        self.assertEqual(objects(self.engine.store,'memory_constraints'),[])
        self.assertEqual(self.engine.change_history()[0]['status'],'undone')

    def test_preference_changes_only_its_context_and_can_restore_previous_value(self):
        fields = {'kind':'preference','entity_id':'climate.study','activity':'working','unit':'°C'}
        first = self.preview(**fields,value=23)
        self.engine.apply_change(first['id'],AT)
        working = self.engine.shadow(AT,activity='working')
        self.assertEqual(working[0]['target'],23)
        self.assertIsNone(working[0]['proposal'])
        self.assertEqual(working[0]['learned_baseline'],[])
        self.assertEqual(self.engine.shadow(AT,activity='sleeping'),[])
        self.assertEqual(self.engine.shadow(AT),[])
        self.assertEqual(self.engine.store.events(),[])
        self.assertEqual(objects(self.engine.store,'memory_habits'),[])
        second = self.preview(**fields,value=24)
        self.engine.apply_change(second['id'],AT)
        with self.assertRaisesRegex(ValueError,'newer change'):
            self.engine.undo_change(first['id'],AT)
        self.engine.undo_change(second['id'],AT)
        self.assertEqual(self.engine.shadow(AT,activity='working')[0]['target'],23)

    def test_new_preference_cannot_override_a_newer_pending_preview_target(self):
        fields = {'kind':'preference','entity_id':'climate.study','activity':'working','unit':'°C'}
        old = self.preview(**fields,value=23)
        newer = self.preview(**fields,value=24)
        self.engine.apply_change(newer['id'],AT)
        with self.assertRaisesRegex(ValueError,'changed after this preview'):
            self.engine.apply_change(old['id'],AT)

    def test_exception_retracts_evidence_without_rewriting_human_history(self):
        event = self.seed_routine()
        raw_before = self.engine.store.events()
        habits_before = {h['id']:h['successes'] for h in objects(self.engine.store,'memory_habits')}
        request = self.preview(kind='exception',event_key=event['event_key'])
        self.engine.apply_change(request['id'],AT)
        after = objects(self.engine.store,'memory_habits')
        self.assertTrue(any(h['successes']<habits_before[h['id']] for h in after))
        self.assertEqual(self.engine.store.events(),raw_before)
        self.engine.learn(AT+timedelta(minutes=1))
        self.assertEqual(objects(self.engine.store,'memory_exclusions')[0]['event_key'],event['event_key'])
        self.engine.undo_change(request['id'],AT+timedelta(minutes=2))
        self.assertEqual(objects(self.engine.store,'memory_exclusions'),[])
        self.assertEqual({h['id']:h['successes'] for h in objects(self.engine.store,'memory_habits')},habits_before)
        self.assertEqual(self.engine.store.events(),raw_before)

    def test_attribution_correction_and_undo_preserve_raw_payloads_and_receipts(self):
        event = self.seed_routine()
        raw = self.engine.store.db.execute('SELECT event_key,raw_json FROM events ORDER BY event_key').fetchall()
        with self.engine.store.db:
            self.engine.store.db.execute("INSERT INTO execution_ledger(id,entity_id,target,issued_at,status) VALUES (?,?,?,?,?)",
                                         ('synthetic-receipt','switch.lamp','off',(AT-timedelta(days=20)).isoformat(),'failed'))
        receipts = self.engine.store.db.execute('SELECT * FROM execution_ledger').fetchall()
        request = self.preview(kind='attribution',event_key=event['event_key'],source='device')
        self.engine.apply_change(request['id'],AT)
        current = next(r for r in self.engine.store.events() if r['event_key']==event['event_key'])
        self.assertEqual(current['attributed_source'],'device')
        self.engine.undo_change(request['id'],AT+timedelta(minutes=1))
        current = next(r for r in self.engine.store.events() if r['event_key']==event['event_key'])
        self.assertEqual(current['attributed_source'],'human')
        self.assertEqual(self.engine.store.db.execute('SELECT event_key,raw_json FROM events ORDER BY event_key').fetchall(),raw)
        self.assertEqual(self.engine.store.db.execute('SELECT * FROM execution_ledger').fetchall(),receipts)

    def test_failed_apply_leaves_request_pending_and_memory_unchanged(self):
        event = self.seed_routine()
        request = self.preview(kind='exception',event_key=event['event_key'])
        with self.engine.store.db:
            self.engine.store.db.execute("UPDATE events SET raw_json='{}' WHERE event_key=?",(event['event_key'],))
        before = state(self.engine.store)
        with self.assertRaisesRegex(ValueError,'no longer retained'):
            self.engine.apply_change(request['id'],AT)
        self.assertEqual(state(self.engine.store),before)
        self.assertEqual(self.engine.change_history()[0]['status'],'pending')

    def test_v1_snapshot_restores_without_touching_operational_ledgers(self):
        before = {name:rows for name,rows in state(self.engine.store).items() if name in LEGACY_VERSIONED}
        request = self.preview(kind='preference',entity_id='climate.study',activity='working',unit='°C',value=23)
        self.engine.apply_change(request['id'],AT)
        restore_state(self.engine.store,before)
        self.engine.store.db.commit()
        self.assertEqual(objects(self.engine.store,'memory_preferences'),[])
        self.assertEqual(self.engine.change_history()[0]['status'],'applied')


if __name__ == '__main__':
    unittest.main()
