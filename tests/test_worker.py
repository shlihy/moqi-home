"""Background scheduling/concurrency; no network or model inference."""
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone
from pathlib import Path

from moqi.api import HomeMemory,Observation
from moqi.core import Store
from moqi.reflection import DeepSeekError
from moqi.worker import run_due,run_local_due

AT = datetime(2026,10,22,19,5,tzinfo=timezone.utc)  # 03:05 household time.
CONFIG = dict(site='SYNTHETIC-WORKER',timezone='Asia/Shanghai',reflection={'enabled':True},
              entities={'switch.lamp':dict(role='actuator',room='dining')})


class Client:
    def __init__(self,error=False):
        self.calls = 0
        self.error = error

    def hypotheses(self,summary):
        self.calls += 1
        if self.error:
            raise DeepSeekError('synthetic unavailable provider')
        return {'schema_version':1,'hypotheses':[]},{'provider':'fixture'}


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name)/'memory.sqlite3')
        with HomeMemory(CONFIG,self.path) as memory:
            memory.ingest(Observation('switch.lamp',AT-timedelta(hours=1),'on','off',
                source='human',evidence='Synthetic verified physical operation'))

    def tearDown(self):
        self.temp.cleanup()

    def test_household_clock_and_restart_allow_one_daily_call(self):
        client = Client()
        store = Store(self.path)
        self.assertEqual(run_due(store,CONFIG,AT-timedelta(minutes=1),client=client)['status'],'not_due')
        self.assertEqual(run_due(store,CONFIG,AT,client=client)['status'],'shadow_only')
        store.close()
        store = Store(self.path)
        try:
            self.assertEqual(run_due(store,CONFIG,AT+timedelta(minutes=1),client=client)['status'],'already_claimed')
            self.assertEqual(client.calls,1)
            self.assertEqual(run_due(store,CONFIG,AT+timedelta(days=1),client=client)['status'],'shadow_only')
            self.assertEqual(client.calls,2)
        finally:
            store.close()

    def test_two_workers_cannot_duplicate_a_paid_request(self):
        client = Client()
        def attempt(_):
            store = Store(self.path)
            try:
                return run_due(store,CONFIG,AT,client=client)['status']
            finally:
                store.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt,range(2)))
        self.assertEqual(sorted(results),['already_claimed','shadow_only'])
        self.assertEqual(client.calls,1)

    def test_failure_stays_visible_and_does_not_repeat_within_the_day(self):
        client = Client(error=True)
        store = Store(self.path)
        try:
            result = run_due(store,CONFIG,AT,client=client)
            self.assertEqual(result['status'],'failed')
            self.assertIn('synthetic unavailable',result['error'])
            result = run_due(store,CONFIG,AT+timedelta(minutes=1),client=client)
            self.assertEqual(result['previous_status'],'failed')
            self.assertEqual(client.calls,1)
        finally:
            store.close()

    def test_disabled_reflection_never_calls_provider(self):
        client = Client()
        store = Store(self.path)
        try:
            result = run_due(store,dict(CONFIG,reflection={'enabled':False}),AT,client=client)
            self.assertEqual(result['status'],'disabled')
            self.assertEqual(client.calls,0)
        finally:
            store.close()

    def test_local_journal_runs_without_cloud_configuration_once_per_household_day(self):
        config=dict(CONFIG,reflection={'enabled':False},memory_directory=str(Path(self.temp.name)/'memory'))
        store=Store(self.path)
        try:
            result=run_local_due(store,config,AT)
            self.assertEqual(result['status'],'complete')
            self.assertTrue(result['revision'])
            self.assertEqual(run_local_due(store,config,AT+timedelta(minutes=1))['status'],'not_due')
            self.assertTrue((Path(config['memory_directory'])/'memory-state.json').is_file())
            self.assertEqual(store.db.execute('SELECT COUNT(*) FROM cloud_calls').fetchone()[0],0)
        finally: store.close()


if __name__=='__main__':
    unittest.main()
