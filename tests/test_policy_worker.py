"""Automatic release wiring with synthetic independent-label protocol fixtures."""
import copy
import hashlib
import json
import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path

from moqi.core import Store
from moqi import jev_policy
from moqi.jev_questions import QUESTIONS
from moqi.memory_store import canonical
from moqi.policy_worker import run_due

AT=datetime(2026,10,22,12,tzinfo=timezone.utc)


class PolicyWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.store=Store(self.root/'memory.sqlite3')
        self.settings=dict(enabled=True)
        self.config=dict(site='SYNTHETIC-POLICY-WORKER',timezone='Asia/Shanghai',jev_policy=self.settings)
        proposal=dict(schema_version=1,parent_id=jev_policy.BUILTIN,question_updates={
            'scope':dict(instructions='Synthetic fixture wording',criteria=copy.deepcopy(QUESTIONS['scope']['criteria']))},
            habit_ids=[],evidence_ids=['fixture-evidence'],rationale='Fixture release flow')
        value=jev_policy.validate_candidate(self.store,proposal,{'fixture-evidence'},AT)
        with self.store.db: self.candidate=jev_policy.insert_candidate(self.store,value,{'provider':'fixture'})

    def tearDown(self): self.store.close(); self.temp.cleanup()

    def dataset(self,name,count,start,frozen,*,label_source='human_review',weak_labels=False):
        cases=[dict(id=name+str(i),at=(start+timedelta(seconds=i)).isoformat(),text='Synthetic independent fixture '+str(i),
            expected=({'status':'needs_clarification'} if i%5==0 else
                {'kind':'constraint','entity_id':'light.lamp','action':'turn_off','scope':'temporary'})) for i in range(count)]
        if weak_labels:
            for case in cases: case['expected']={'status':'interpreted'}
        raw=('\n'.join(canonical(case) for case in cases)+'\n').encode()
        source=self.root/(name+'.jsonl')
        source.write_bytes(raw)
        manifest=self.root/(name+'.json')
        manifest.write_text(json.dumps(dict(schema_version=1,dataset_id='fixture-'+name,labels_from=label_source,
            cases_file=source.name,sha256=hashlib.sha256(raw).hexdigest(),frozen_at=frozen.isoformat())),encoding='utf-8')
        return str(manifest)

    def judge(self,version,case):
        if version['id']==jev_policy.BUILTIN and case['id'].endswith('2'):
            return {'status':'needs_clarification'}
        return dict(status='interpreted',**case['expected']) if 'kind' in case['expected'] else case['expected']

    def test_validation_then_only_prospective_shadow_can_activate_without_a_manual_apply(self):
        self.settings['validation_manifest']=self.dataset('validation',200,AT-timedelta(days=1),AT-timedelta(hours=1))
        first=run_due(self.store,self.config,AT+timedelta(minutes=1),judge=self.judge)
        self.assertEqual(first['status'],'awaiting_prospective_shadow')
        self.assertEqual(jev_policy.active(self.store)['id'],jev_policy.BUILTIN)
        self.settings['shadow_manifest']=self.settings['validation_manifest']
        self.assertEqual(run_due(self.store,self.config,AT+timedelta(minutes=2),judge=self.judge)['status'],
                         'awaiting_enough_shadow_data')
        self.settings['shadow_manifest']=self.dataset('shadow',50,AT+timedelta(minutes=3),AT+timedelta(minutes=5))
        result=run_due(self.store,self.config,AT+timedelta(hours=1),judge=self.judge)
        self.assertEqual(result['status'],'activated')
        self.assertEqual(jev_policy.active(self.store)['id'],self.candidate['id'])

    def test_missing_model_generated_or_incomplete_labels_never_count_as_validation(self):
        self.assertEqual(run_due(self.store,self.config,AT,judge=self.judge)['status'],
                         'awaiting_independent_validation_data')
        for source,weak in [('model_output',False),('human_review',True)]:
            self.settings['validation_manifest']=self.dataset('invalid',200,AT-timedelta(days=1),AT-timedelta(hours=1),
                                                             label_source=source,weak_labels=weak)
            self.assertEqual(run_due(self.store,self.config,AT,judge=self.judge)['status'],'invalid_policy_data')
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM jev_policy_evaluations').fetchone()[0],0)

    def test_no_quality_gain_rejects_the_candidate_and_never_calls_cloud(self):
        self.settings['validation_manifest']=self.dataset('no-gain',200,AT-timedelta(days=1),AT-timedelta(hours=1))
        def perfect(version,case):
            return dict(status='interpreted',**case['expected']) if 'kind' in case['expected'] else case['expected']
        result=run_due(self.store,self.config,AT+timedelta(minutes=1),judge=perfect)
        self.assertEqual(result['status'],'rejected')
        self.assertEqual(jev_policy.active(self.store)['id'],jev_policy.BUILTIN)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM cloud_calls').fetchone()[0],0)


if __name__=='__main__': unittest.main()
