"""Persistent speech-text orchestration; not ASR/TTS or real JEV acceptance."""
import tempfile
import unittest
import re
from datetime import datetime,timedelta,timezone
from pathlib import Path

from moqi.api import HomeMemory,Observation
from moqi.conversation import VoiceConversation
from moqi.interpretation import MemoryInterpreter
from moqi.memory_store import objects

AT = datetime(2026,10,22,12,tzinfo=timezone.utc)
CONFIG = dict(site='SYNTHETIC-VOICE',timezone='Asia/Shanghai',entities={
    'switch.lamp':dict(role='actuator',room='dining',category='light',risk='low',aliases=['餐厅灯']),
    'switch.fan':dict(role='actuator',room='dining',category='ventilation',risk='low',aliases=['风扇']),
    'climate.study':dict(role='actuator',room='study',category='hvac',risk='medium',aliases=['书房空调'])})


class SemanticFixture:
    # The real laya agent exposes its tokenizer and artifact limits. This
    # protocol fixture tokenizes Latin words and Chinese characters, no model.
    class Encoder:
        def ids(self,text): return re.findall(r'[A-Za-z_]+|[^A-Za-z_\s]',text)
    enc=Encoder()
    cfg=dict(max_len=1024,head_max_len=256)

    def system_one(self,text,questions):
        choices={'intent':'constraint','action':'turn_off','scope':'temporary'}
        return {'model':'fixture-not-real-model','answers':{key:
            {'choice':choices[key],'probabilities':{choices[key]:.9}} for key in questions}}


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name)/'memory.sqlite3')
        self.memory = HomeMemory(CONFIG,self.path)
        self.voice = VoiceConversation(self.memory,MemoryInterpreter(CONFIG))

    def tearDown(self):
        self.memory.close()
        self.temp.cleanup()

    def say(self,text,at=AT,room='dining',session='wake-1'):
        return self.voice.handle(text,session_id=session,room=room,at=at)

    def reopen(self,model=False):
        self.memory.close()
        self.memory = HomeMemory(CONFIG,self.path)
        parser = MemoryInterpreter(CONFIG,agent=SemanticFixture()) if model else MemoryInterpreter(CONFIG)
        self.voice = VoiceConversation(self.memory,parser)

    def test_rule_constraint_and_restart_undo_need_no_web_or_model(self):
        result = self.say('今晚别自动关餐厅灯')
        self.assertEqual(result['status'],'applied')
        self.assertEqual(objects(self.memory.store,'memory_constraints')[0]['source'],'voice')
        self.reopen()
        result = self.say('撤销刚才那条',AT+timedelta(seconds=10),session='wake-2')
        self.assertEqual(result['status'],'undone')
        self.assertEqual(objects(self.memory.store,'memory_constraints'),[])
        # A repeated undo does not undo an earlier, unrelated change.
        self.assertEqual(self.say('撤销刚才那条',AT+timedelta(seconds=11))['status'],'undone')

    def test_clear_preference_uses_code_and_separate_empirical_baseline(self):
        result = self.say('我工作的时候，书房空调设到23°C更舒服。',room='study')
        self.assertEqual(result['status'],'applied')
        value = objects(self.memory.store,'memory_preferences')[0]
        self.assertEqual(value['value'],23)
        self.assertEqual(value['source'],'voice')
        self.assertEqual(objects(self.memory.store,'memory_models'),[])
        self.assertEqual(self.memory.store.events(),[])

    def test_unplayed_confirmation_cannot_be_authorized_by_a_later_yes(self):
        self.reopen(model=True)
        result=self.say('餐厅灯今晚就让它亮着，别自己灭了')
        self.assertEqual(result['status'],'needs_confirmation')
        self.voice.discard_pending('dining',AT+timedelta(seconds=1))
        self.assertEqual(self.say('确认',AT+timedelta(seconds=2))['status'],'unchanged')
        self.assertEqual(objects(self.memory.store,'memory_constraints'),[])

    def test_model_only_change_is_confirmed_in_voice_and_persists_restart(self):
        self.reopen(model=True)
        result = self.say('餐厅灯今晚就让它亮着，别自己灭了')
        self.assertEqual(result['status'],'needs_confirmation')
        self.assertEqual(result['reply_kind'],'confirmation')
        self.assertEqual(objects(self.memory.store,'memory_constraints'),[])
        self.reopen(model=True)
        self.assertEqual(self.say('对',AT+timedelta(seconds=5))['status'],'applied')
        self.assertEqual(len(objects(self.memory.store,'memory_constraints')),1)

    def test_expired_or_different_wake_session_cannot_confirm_old_change(self):
        self.reopen(model=True)
        self.say('餐厅灯今晚就让它亮着，别自己灭了')
        self.assertEqual(self.say('确认',AT+timedelta(seconds=61))['status'],'unchanged')
        self.assertEqual(objects(self.memory.store,'memory_constraints'),[])
        self.say('餐厅灯今晚就让它亮着，别自己灭了',AT+timedelta(seconds=62))
        self.assertEqual(self.say('确认',AT+timedelta(seconds=63),session='unrelated-wake')['status'],'unchanged')

    def test_extra_preference_qualifiers_are_not_silently_dropped_by_code(self):
        result = self.say('我工作时书房空调设到23°C，睡觉时也这样',room='study')
        self.assertEqual(result['status'],'unavailable')
        self.assertEqual(objects(self.memory.store,'memory_preferences'),[])

    def test_two_recent_devices_are_resolved_by_spoken_reference(self):
        for entity in ('switch.lamp','switch.fan'):
            self.memory.ingest(Observation(entity,AT-timedelta(seconds=30),'on','off',
                source='human',evidence='Synthetic physical action'))
        raw = self.memory.store.events()
        result = self.say('刚才是来了客人')
        self.assertEqual(result['status'],'needs_clarification')
        self.assertEqual(objects(self.memory.store,'memory_exclusions'),[])
        result = self.say('餐厅灯那次',AT+timedelta(seconds=2))
        self.assertEqual(result['status'],'applied')
        self.assertEqual(objects(self.memory.store,'memory_exclusions')[0]['entity_id'],'switch.lamp')
        self.assertEqual(self.memory.store.events(),raw)

    def test_new_instruction_replaces_pending_exception_instead_of_answering_it(self):
        self.say('刚才是来了客人')
        result = self.say('今晚别自动关餐厅灯',AT+timedelta(seconds=1))
        self.assertEqual(result['status'],'applied')
        self.assertEqual(len(objects(self.memory.store,'memory_constraints')),1)
        self.assertEqual(objects(self.memory.store,'memory_exclusions'),[])

    def test_night_never_replies_or_creates_unconfirmable_model_change(self):
        self.reopen(model=True)
        night = AT.replace(hour=15)
        result = self.say('餐厅灯今晚就让它亮着，别自己灭了',night)
        self.assertIsNone(result['reply'])
        self.assertTrue(result['night_silence'])
        self.assertEqual(objects(self.memory.store,'memory_constraints'),[])
        # A clear restriction can still be recorded silently, without a prompt.
        result = self.say('今晚别自动关餐厅灯',night+timedelta(seconds=1))
        self.assertEqual(result['status'],'applied')
        self.assertIsNone(result['reply'])


if __name__=='__main__':
    unittest.main()
