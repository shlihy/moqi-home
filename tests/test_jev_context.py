"""Context contracts with a tiny fixture tokenizer; no model is loaded."""
import unittest

from moqi.jev_context import ContextLimit,bounded_state


class Encoder:
    def ids(self,text): return list(range(len(text)))


class Agent:
    enc=Encoder()
    cfg=dict(max_len=400,head_max_len=100,mask_token='<mask>')


QUESTION={'intent':dict(instructions='choose',criteria={'yes':'agrees','no':'disagrees'})}


class JevContextTests(unittest.TestCase):
    def test_whole_habits_are_selected_without_cutting_the_utterance(self):
        text='Keep the full request and its final exception.'
        facts=[dict(id=str(i),entity_id='light.lamp',context={'description':'x'*150}) for i in range(2)]
        state,used,budget=bounded_state(Agent(),text,facts,QUESTION)
        self.assertEqual(state['utterance'],text)
        self.assertEqual(len(used),1)
        self.assertEqual(used[0],facts[0])
        self.assertLessEqual(budget['state_size'],budget['state_limit'])
        self.assertFalse(budget['utterance_truncated'])

    def test_overlong_utterance_or_question_is_rejected_instead_of_silently_truncated(self):
        with self.assertRaises(ContextLimit): bounded_state(Agent(),'x'*500,[],QUESTION)
        question={'intent':dict(instructions='choose',criteria={'yes':'x'*60})}
        with self.assertRaises(ContextLimit): bounded_state(Agent(),'request',[],question)

    def test_fixture_byte_estimates_are_not_reported_as_measured_tokens(self):
        state,used,budget=bounded_state(object(),'request',[],QUESTION)
        self.assertEqual(state,'request')
        self.assertEqual(budget['count_method'],'utf8_upper_estimate')


if __name__=='__main__': unittest.main()
