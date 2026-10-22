"""Code-first dispatch regressions. Injected agents are protocol fixtures only."""
import unittest
from unittest.mock import patch

from moqi.interpretation import MemoryInterpreter


CONFIG = {'entities':{'switch.lamp':{'role':'actuator','room':'dining','aliases':['餐厅灯']}}}


class InterpretationRoutingTests(unittest.TestCase):
    def test_explicit_commands_work_without_loading_or_installing_a_model(self):
        with patch('moqi.interpretation.load_agent',side_effect=AssertionError('must bypass model')) as loader:
            parser = MemoryInterpreter(CONFIG,mode='hybrid',model_path='missing-intentionally')
            parsed = parser.interpret('今晚别自动关餐厅灯')
            self.assertEqual(parsed['kind'],'constraint')
            self.assertEqual(parsed['scope'],'temporary')
            self.assertEqual(parsed['provenance']['engine'],'rules')
            self.assertEqual(parser.interpret('撤销刚才那条')['kind'],'undo')
            loader.assert_not_called()

    def test_unconfigured_unresolved_semantics_are_unavailable_not_guessed(self):
        result = MemoryInterpreter(CONFIG,mode='hybrid').interpret('我睡下以后能不能让它一直亮着')
        self.assertEqual(result['status'],'unavailable')
        self.assertNotIn('entity_id',result)

    def test_cancelled_instruction_never_loads_model_or_creates_change(self):
        with patch('moqi.interpretation.load_agent',side_effect=AssertionError('must bypass model')):
            result = MemoryInterpreter(CONFIG,model_path='missing').interpret('算了，今晚别自动关餐厅灯')
            self.assertEqual(result['status'],'needs_clarification')
            self.assertNotIn('kind',result)


if __name__=='__main__':
    unittest.main()
