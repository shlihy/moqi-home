"""Integrated event-to-learning replay, without a model, microphone or live HA."""
import importlib.util
import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path

from moqi.api import HomeMemory,Observation
from moqi.memory_store import objects
from moqi.runtime import Runtime

AT=datetime(2026,10,22,10,tzinfo=timezone.utc)
CONFIG=dict(site='SYNTHETIC-RUNTIME',timezone='Asia/Shanghai',entities={
    'light.lamp':dict(role='actuator',room='dining',category='light',risk='low'),
    'binary_sensor.motion':dict(role='motion',room='dining')})
PROFILE=dict(schema_version=1,revision='fixture-area-one',execution_enabled=False,
    observed_entities=list(CONFIG['entities']),control_entities=['light.lamp'],
    entities=CONFIG['entities'],timezone='Asia/Shanghai',policy=dict(devices={}),issues=[])

spec=importlib.util.spec_from_file_location('fixture_ha_profile',Path(__file__).parents[1]/'custom_components/moqi/profile.py')
profile_module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(profile_module)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.memory=HomeMemory(CONFIG,str(Path(self.temp.name)/'memory.sqlite3'))
        self.runtime=Runtime(self.memory)

    def tearDown(self): self.memory.close(); self.temp.cleanup()

    def train(self):
        for day in range(8,0,-1):
            start=AT-timedelta(days=day)
            self.runtime.observe(Observation('light.lamp',start,'off'))
            self.runtime.tick(start+timedelta(minutes=1))
            self.runtime.observe(Observation('light.lamp',start+timedelta(minutes=5),'on','off',
                source='human',evidence='Synthetic independently verified physical operation'))
            self.memory.observe_interval(start,start+timedelta(minutes=16))
            self.runtime.tick(start+timedelta(minutes=17))

    def test_passive_behavior_forms_habits_without_teaching_or_cloud_requests(self):
        self.train()
        habits=objects(self.memory.store,'memory_habits')
        self.assertTrue(habits)
        self.assertTrue(any(h['successes']>=4 for h in habits))
        self.assertEqual(self.memory.store.db.execute('SELECT COUNT(*) FROM correction_requests').fetchone()[0],0)
        self.assertEqual(self.memory.store.db.execute('SELECT COUNT(*) FROM cloud_calls').fetchone()[0],0)
        self.assertEqual(self.memory.store.db.execute('SELECT COUNT(*) FROM execution_ledger').fetchone()[0],0)
        self.assertTrue(self.memory.store.db.execute('SELECT COUNT(*) FROM forecasts').fetchone()[0])

    def test_revoked_selection_preserves_history_and_does_not_reuse_room_habits(self):
        self.train()
        self.runtime.apply_profile(PROFILE,AT)
        count=self.memory.store.db.execute('SELECT COUNT(*) FROM events').fetchone()[0]
        changed=dict(PROFILE,revision='fixture-area-two',entities={
            'binary_sensor.motion':dict(role='motion',room='dining')},
            observed_entities=['binary_sensor.motion'],control_entities=[])
        self.runtime.apply_profile(changed,AT+timedelta(seconds=1))
        self.assertEqual(self.runtime.tick(AT+timedelta(seconds=2))['decisions'],[])
        self.assertGreaterEqual(self.memory.store.db.execute('SELECT COUNT(*) FROM events').fetchone()[0],count)
        self.assertTrue(objects(self.memory.store,'memory_habits'))

    def test_event_cutoff_duplicate_and_clock_reversal_do_not_invent_learning(self):
        value=Observation('light.lamp',AT,'on','off',source='unknown')
        self.assertTrue(self.runtime.observe(value))
        self.assertFalse(self.runtime.observe(value))
        self.runtime.tick(AT)
        self.runtime.tick(AT+timedelta(seconds=1))
        self.assertEqual(objects(self.memory.store,'memory_habits'),[])
        self.assertEqual(self.runtime.tick(AT)['status'],'clock_moved_backwards')

    def test_ha_profile_requires_area_load_classification_and_reliable_occupancy(self):
        current=dict(schema_version=1,revision='fixture',execution_enabled=False,
            observed_entities=['switch.plug','binary_sensor.pir','sensor.humidity','light.no_area'],
            control_entities=['switch.plug'])
        metadata={
            'switch.plug':dict(room='dining',labels=[]),
            'binary_sensor.pir':dict(room='dining',labels=[],device_class='motion'),
            'sensor.humidity':dict(room='dining',labels=[],device_class='humidity',unit='%'),
            'light.no_area':dict(labels=[])}
        value=profile_module.build_profile(current,metadata,'Asia/Shanghai')
        self.assertEqual(value['entities']['switch.plug']['risk'],'unclassified')
        self.assertEqual(value['entities']['binary_sensor.pir']['role'],'motion')
        self.assertEqual(value['entities']['sensor.humidity']['role'],'humidity')
        self.assertNotIn('light.no_area',value['entities'])
        metadata['switch.plug']['labels']=['默契：灯具']
        metadata['binary_sensor.pir']['labels']=['默契：可靠在场']
        value=profile_module.build_profile(current,metadata,'Asia/Shanghai')
        self.assertEqual(value['entities']['switch.plug']['risk'],'low')
        self.assertEqual(value['entities']['binary_sensor.pir']['role'],'presence')


if __name__=='__main__': unittest.main()
