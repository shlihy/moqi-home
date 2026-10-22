"""Synthetic evidence and captured HTTP protocol; no live model/API acceptance."""
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError

from moqi.api import HomeMemory, Observation
from moqi.memory_store import canonical, objects, state
from moqi.reflection import DeepSeek, DeepSeekError, evidence_summary, reflect


AT = datetime(2026,10,22,12,tzinfo=timezone.utc)
CONFIG = dict(site='SYNTHETIC',timezone='Asia/Shanghai',entities={
    'switch.private_family_light':dict(role='actuator',room='private_family_room',category='light',risk='low'),
})


class ReferenceClient:
    def __init__(self, alter=None):
        self.alter = alter
        self.calls = 0

    def hypotheses(self, summary):
        self.calls += 1
        cell = next(cell for cell in summary['cells'] if cell['feature']=='weekday')
        value = dict(schema_version=1,hypotheses=[dict(kind='context_split',feature='weekday',
            value=cell['value'],rationale='Synthetic association to test after this cutoff',evidence_ids=[cell['id']])])
        if self.alter:
            self.alter(value)
        return value,dict(provider='reference_http_fixture',model='no-real-model')


class ReflectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = HomeMemory(CONFIG,str(Path(self.temp.name)/'memory.sqlite3'))

    def tearDown(self):
        self.engine.close()
        self.temp.cleanup()

    def observed(self, at, *, source='human'):
        return self.engine.ingest(Observation('switch.private_family_light',at,'on','off',
            source=source,evidence='Synthetic physical control' if source!='unknown' else ''))

    def test_summary_excludes_future_unknown_device_and_private_names(self):
        self.observed(AT-timedelta(days=1))
        self.observed(AT-timedelta(hours=2),source='device')
        self.observed(AT-timedelta(hours=1),source='unknown')
        self.observed(AT+timedelta(hours=1))
        summary = evidence_summary(self.engine.store,CONFIG,AT)
        self.assertTrue(summary['cells'])
        self.assertTrue(all(cell['human_actions']==1 for cell in summary['cells']))
        payload = canonical(summary)
        self.assertNotIn('private_family',payload)
        self.assertNotIn('2026-',payload)
        self.assertNotIn('Synthetic physical control',payload)

    def test_no_evidence_does_not_contact_provider(self):
        client = ReferenceClient()
        result = reflect(self.engine.store,CONFIG,AT,client)
        self.assertEqual(result['status'],'no_evidence')
        self.assertEqual(client.calls,0)

    def test_only_shadow_proposal_is_added_and_repeated_analysis_does_not_reset_it(self):
        self.observed(AT-timedelta(days=1))
        before = state(self.engine.store)
        raw = self.engine.store.events()
        result = reflect(self.engine.store,CONFIG,AT,ReferenceClient())
        proposal = objects(self.engine.store,'memory_proposals')[0]
        self.assertEqual(proposal['status'],'shadow')
        self.assertEqual(proposal['trust_level'],1)
        self.assertEqual(before['memory_habits'],objects(self.engine.store,'memory_habits'))
        self.assertEqual(raw,self.engine.store.events())
        second = reflect(self.engine.store,CONFIG,AT+timedelta(minutes=1),ReferenceClient())
        self.assertEqual(result['created'],second['reused'])
        self.assertEqual(second['created'],[])
        self.assertEqual(objects(self.engine.store,'memory_proposals')[0],proposal)

    def test_entire_batch_is_rejected_for_unsupported_or_invented_evidence(self):
        self.observed(AT-timedelta(days=1))
        for kind in ('invented','wrong_value','permission'):
            def alter(response):
                invalid = dict(response['hypotheses'][0])
                if kind=='invented': invalid['evidence_ids']=['invented']
                elif kind=='wrong_value': invalid['value']=(invalid['value']+1)%7
                else: invalid['allow_control']=['lock.front_door']
                response['hypotheses'].append(invalid)
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                reflect(self.engine.store,CONFIG,AT,ReferenceClient(alter))
            self.assertEqual(objects(self.engine.store,'memory_proposals'),[])

    def test_http_request_and_empty_truncated_or_invalid_outputs(self):
        valid = {'choices':[{'finish_reason':'stop','message':{'content':
            '{"schema_version":1,"hypotheses":[]}'}}], 'model':'server-reported-model','id':'fixture'}
        seen = []
        def opener(request,timeout):
            seen.append((request,timeout))
            return io.BytesIO(json.dumps(valid).encode())
        result,provenance = DeepSeek(api_key='synthetic-secret',opener=opener)._request({'cells':[]})
        request,timeout = seen[0]
        sent = json.loads(request.data)
        self.assertEqual(request.full_url,'https://api.deepseek.com/chat/completions')
        self.assertEqual(sent['response_format'],{'type':'json_object'})
        self.assertFalse(sent['stream'])
        self.assertEqual(sent['thinking'],{'type':'disabled'})
        self.assertIn('json',sent['messages'][0]['content'])
        self.assertEqual(provenance['model'],'server-reported-model')
        self.assertNotIn('synthetic-secret',canonical(provenance))
        self.assertEqual(result['hypotheses'],[])
        for content,finish in [('', 'stop'),('{}','length'),('not-json','stop')]:
            valid['choices'][0] = {'finish_reason':finish,'message':{'content':content}}
            with self.subTest(content=content,finish=finish), self.assertRaises(DeepSeekError):
                DeepSeek(api_key='synthetic-secret',opener=opener)._request({'cells':[]})

    def test_http_error_does_not_echo_provider_body_or_key(self):
        def opener(request,timeout):
            raise HTTPError(request.full_url,401,'synthetic-secret',{},io.BytesIO(b'private-response'))
        with self.assertRaisesRegex(DeepSeekError,'DeepSeek HTTP 401') as raised:
            DeepSeek(api_key='synthetic-secret',opener=opener)._request({'cells':[]})
        self.assertNotIn('synthetic-secret',str(raised.exception))
        self.assertNotIn('private-response',str(raised.exception))


if __name__=='__main__':
    unittest.main()
