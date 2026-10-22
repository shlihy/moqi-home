"""Release mechanics with labelled synthetic fixtures, not model accuracy."""
import copy
import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path

from moqi.core import Store
from moqi import jev_policy as policy
from moqi.jev_questions import QUESTIONS
from moqi.memory_store import objects,put,state,restore_state

AT = datetime(2026,10,22,12,tzinfo=timezone.utc)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name)/'memory.sqlite3')

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def candidate(self,alter=None):
        value = dict(schema_version=1,parent_id=policy.active(self.store)['id'],
            question_updates={'scope':dict(instructions='合成模板，用于验证发布机制。',criteria=copy.deepcopy(QUESTIONS['scope']['criteria']))},
            habit_ids=[],evidence_ids=['retained-evidence'],rationale='Synthetic independently evaluated proposal')
        if alter:
            alter(value)
        validated = policy.validate_candidate(self.store,value,{'retained-evidence'},AT)
        with self.store.db:
            return policy.insert_candidate(self.store,validated,{'provider':'fixture-not-real-model'})

    def cases(self,count,start,prefix):
        return [dict(id=prefix+str(i),at=(start+timedelta(seconds=i)).isoformat(),text='Synthetic case '+str(i),
            expected={'kind':'constraint','entity_id':'switch.lamp','scope':'temporary','action':'turn_off'}
                     if i%5 else {'status':'needs_clarification'},mandatory=i==1) for i in range(count)]

    def judge(self,version,case):
        expected = case['expected']
        if version['id']==policy.BUILTIN and case['id'].endswith('2'):
            return {'status':'needs_clarification'}
        return {'status':'interpreted',**expected} if 'kind' in expected else dict(expected)

    def validate_and_shadow(self,value):
        report = policy.evaluate(self.store,value['id'],self.cases(200,AT-timedelta(days=1),'validation-'),
                                 self.judge,AT+timedelta(minutes=1),phase='validation')
        self.assertTrue(report['passed'])
        report = policy.evaluate(self.store,value['id'],self.cases(50,AT+timedelta(minutes=2),'shadow-'),
                                 self.judge,AT+timedelta(hours=1),phase='shadow')
        self.assertTrue(report['passed'])

    def test_candidate_cannot_expand_answer_domain_or_invent_evidence(self):
        for field in ('answer','evidence','permissions'):
            def alter(value):
                if field=='answer': value['question_updates']['scope']['criteria']['execute_anything']='Unsafe'
                elif field=='evidence': value['evidence_ids']=['invented']
                else: value['allowed_devices']=['lock.front']
            with self.subTest(field=field),self.assertRaises(ValueError):
                self.candidate(alter)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM jev_policies').fetchone()[0],0)

    def test_proposal_validation_and_prospective_shadow_are_all_required(self):
        value = self.candidate()
        with self.assertRaises(ValueError):
            policy.activate(self.store,value['id'],AT)
        validation = self.cases(200,AT-timedelta(days=1),'validation-')
        report = policy.evaluate(self.store,value['id'],validation,self.judge,AT+timedelta(minutes=1),phase='validation')
        self.assertTrue(report['passed'])
        with self.assertRaisesRegex(ValueError,'prospective'):
            policy.activate(self.store,value['id'],AT+timedelta(minutes=1))
        with self.assertRaisesRegex(ValueError,'new observations'):
            policy.evaluate(self.store,value['id'],validation,self.judge,AT+timedelta(minutes=2),phase='shadow')
        policy.evaluate(self.store,value['id'],self.cases(50,AT+timedelta(minutes=2),'shadow-'),
                        self.judge,AT+timedelta(hours=1),phase='shadow')
        self.assertEqual(policy.activate(self.store,value['id'],AT+timedelta(hours=2))['id'],value['id'])
        self.assertEqual(policy.active(self.store)['id'],value['id'])

    def test_regression_cannot_be_hidden_by_abstention_or_reusing_an_old_pass(self):
        value = self.candidate()
        self.validate_and_shadow(value)
        cases = self.cases(50,AT+timedelta(hours=2),'regression-')
        report = policy.evaluate(self.store,value['id'],cases,lambda version,case:{'status':'needs_clarification'},
                                 AT+timedelta(hours=3),phase='shadow')
        self.assertFalse(report['passed'])
        with self.assertRaises(ValueError):
            policy.activate(self.store,value['id'],AT+timedelta(hours=4))

    def test_feedback_rollback_is_idempotent_and_preserves_memory_and_ledgers(self):
        value = self.candidate()
        self.validate_and_shadow(value)
        policy.activate(self.store,value['id'],AT+timedelta(hours=2))
        before = state(self.store)
        when = AT+timedelta(hours=3)
        policy.feedback(self.store,value['id'],'request-one','rejected',when)
        policy.feedback(self.store,value['id'],'request-one','rejected',when)
        self.assertEqual(policy.active(self.store)['id'],value['id'])
        policy.feedback(self.store,value['id'],'request-two','rejected',when)
        self.assertEqual(policy.active(self.store)['id'],policy.BUILTIN)
        self.assertEqual(state(self.store),before)
        restore_state(self.store,before)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM jev_policy_feedback').fetchone()[0],2)

    def test_replay_never_injects_current_statistics_into_past_cases(self):
        habit = dict(id='actual-habit',created_at=(AT-timedelta(days=3)).isoformat(),status='active',
            entity_id='switch.lamp',property='state',target='on',context={'room':'dining'},
            successes=999,failures=0,trust_level=3)
        with self.store.db:
            put(self.store,'memory_habits',habit)
        value = self.candidate(lambda v:v.update(habit_ids=['actual-habit']))
        self.assertEqual(policy.prepare(self.store,value,AT)['habit_facts'][0]['successes'],999)
        seen = []
        def judge(version,case):
            seen.extend(version['habit_facts'])
            return self.judge(version,case)
        policy.evaluate(self.store,value['id'],self.cases(200,AT-timedelta(days=1),'old-'),
                        judge,AT+timedelta(minutes=1),phase='validation')
        self.assertEqual(seen,[])
        cases = self.cases(1,AT-timedelta(days=1),'future-context-')
        cases[0]['habit_context'] = {'captured_at':AT.isoformat(),'habits':[habit]}
        with self.assertRaisesRegex(ValueError,'captured'):
            policy.evaluate(self.store,value['id'],cases,self.judge,AT+timedelta(minutes=2),phase='validation')


if __name__=='__main__':
    unittest.main()
