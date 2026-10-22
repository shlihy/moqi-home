"""Paid-call regressions using captured HTTP fixtures; no external requests."""
import io
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone
from pathlib import Path
from urllib.error import URLError

from moqi.api import HomeMemory,Observation
from moqi.core import Store
from moqi.llm_cost import bounded_summary,input_bound,report,usage_cost
from moqi.memory_store import restore_state,state
from moqi.reflection import DeepSeek,DeepSeekError,SYSTEM,reflect

AT = datetime(2026,10,22,12,tzinfo=timezone.utc)
CONFIG = dict(site='SYNTHETIC-COST',timezone='Asia/Shanghai',entities={
    'switch.lamp':dict(role='actuator',room='dining')})


class BillingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name)/'memory.sqlite3')
        self.memory = HomeMemory(CONFIG,self.path)
        self.memory.ingest(Observation('switch.lamp',AT-timedelta(hours=1),'on','off',
                                      source='human',evidence='Synthetic physical control'))
        self.calls = []

    def tearDown(self):
        self.memory.close()
        self.temp.cleanup()

    def client(self,*,usage=True,content=None,finish='stop'):
        def opener(request,timeout):
            self.calls.append(json.loads(request.data))
            body = dict(id='fixture',model='deepseek-flash',choices=[dict(
                finish_reason=finish,message=dict(content=content if content is not None else
                    '{"schema_version":1,"hypotheses":[]}'))])
            if usage:
                body['usage'] = dict(prompt_tokens=1000,completion_tokens=100,
                    prompt_cache_hit_tokens=800,prompt_cache_miss_tokens=200)
            return io.BytesIO(json.dumps(body).encode())
        return DeepSeek(api_key='synthetic-secret',opener=opener)

    def test_actual_usage_savings_and_unchanged_evidence_survive_restart_and_memory_rollback(self):
        before = state(self.memory.store)
        result = reflect(self.memory.store,CONFIG,AT,self.client())
        self.assertEqual(result['status'],'shadow_only')
        charged = report(self.memory.store,CONFIG,AT)
        self.assertAlmostEqual(charged['estimated_spend_including_reservations'],0.0001848)
        self.assertEqual(charged['measured_cache_hit_ratio'],0.8)
        restore_state(self.memory.store,before)
        self.memory.store.db.commit()
        self.memory.close()
        self.memory = HomeMemory(CONFIG,self.path)
        result = reflect(self.memory.store,CONFIG,AT+timedelta(days=1),self.client())
        self.assertEqual(result['status'],'unchanged_evidence')
        self.assertEqual(len(self.calls),1)
        self.assertEqual(report(self.memory.store,CONFIG,AT)['calls'],1)

    def test_budget_zero_and_unknown_model_never_send_a_request(self):
        config = dict(CONFIG,reflection=dict(monthly_budget_usd=0))
        self.assertEqual(reflect(self.memory.store,config,AT,self.client())['status'],'monthly_budget')
        client = self.client()
        client.model = 'unpriced-expensive-model'
        self.assertEqual(reflect(self.memory.store,CONFIG,AT,client)['status'],'unpriced_model')
        self.assertEqual(self.calls,[])

    def test_unknown_network_outcome_retains_reservation_and_prevents_paid_retry(self):
        def fail(request,timeout):
            self.calls.append('attempt')
            raise URLError('synthetic disconnected after send')
        client = DeepSeek(api_key='synthetic-secret',opener=fail)
        with self.assertRaises(DeepSeekError):
            reflect(self.memory.store,CONFIG,AT,client)
        row = self.memory.store.db.execute('SELECT reserved_nusd,charged_nusd,status FROM cloud_calls').fetchone()
        self.assertEqual(row[0],row[1])
        self.assertEqual(row[2],'unknown')
        self.assertEqual(reflect(self.memory.store,CONFIG,AT,client)['status'],'daily_call_limit')
        self.assertEqual(len(self.calls),1)

    def test_truncated_or_invalid_output_still_accounts_for_billed_tokens(self):
        with self.assertRaises(DeepSeekError):
            reflect(self.memory.store,CONFIG,AT,self.client(content='',finish='length'))
        result = report(self.memory.store,CONFIG,AT)
        self.assertEqual(result['completion_tokens'],100)
        self.assertEqual(result['unsettled_calls'],0)

    def test_missing_usage_keeps_worst_case_and_does_not_invent_a_cache_rate(self):
        reflect(self.memory.store,CONFIG,AT,self.client(usage=False))
        result = report(self.memory.store,CONFIG,AT)
        self.assertEqual(result['unsettled_calls'],1)
        self.assertIsNone(result['measured_cache_hit_ratio'])
        self.assertGreater(result['estimated_spend_including_reservations'],0)
        self.assertEqual(usage_cost(dict(prompt_tokens=100,completion_tokens=2))['charged_nusd'],32400)
        self.assertFalse(usage_cost(dict(prompt_tokens=100,completion_tokens=2,
            prompt_cache_hit_tokens=999,prompt_cache_miss_tokens=0))['cache_usage_reported'])

    def test_two_processes_reserve_shared_budget_before_either_http_call(self):
        # Each request needs ~0.004 USD; 0.005 admits one, not both.
        def attempt(index):
            store = Store(self.path)
            try:
                config = dict(CONFIG,site='fixture-'+str(index),reflection=dict(monthly_budget_usd=0.005))
                return reflect(store,config,AT,self.client(usage=False))['status']
            finally:
                store.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt,range(2)))
        self.assertEqual(sorted(results),['monthly_budget','shadow_only'])
        self.assertEqual(len(self.calls),1)

    def test_summary_stays_structured_and_bounded_with_stable_system_prefix(self):
        cells = [dict(id=str(i),group=str(i//3),feature='weekday',value=i%7,human_actions=i+1)
                 for i in range(1000)]
        summary = dict(cells=cells,habits=[],feedback=[],current_policy=dict(id='fixture',question_updates={}))
        selected = bounded_summary(summary,SYSTEM,CONFIG)
        self.assertLessEqual(input_bound(DeepSeek.messages(selected)),8000)
        self.assertLessEqual(len(selected['cells']),24)
        self.assertTrue(all(cell in cells for cell in selected['cells']))
        self.assertEqual(summary['cells'],cells)
        self.assertEqual(DeepSeek.messages(selected)[0],DeepSeek.messages(dict(selected,cells=[]))[0])
        self.assertEqual(selected['selection']['available']['cells'],1000)


if __name__=='__main__':
    unittest.main()
