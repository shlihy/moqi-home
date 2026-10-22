"""Isolated dispatch and PCM fixtures; never connect to HA or real audio."""
import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path

from moqi.api import HomeMemory,Observation
from moqi.execution import Executor
from moqi.ha_gateway import GatewayError
from moqi.memory_store import put
from moqi.safety import review
from moqi.speech import PCM,SpeechBroker

AT=datetime(2026,10,22,2,tzinfo=timezone.utc)
CONFIG=dict(site='SYNTHETIC-EXECUTOR',timezone='Asia/Shanghai',entities={
    'light.lamp':dict(role='actuator',room='dining',category='light',risk='low',aliases=['餐厅灯']),
    'binary_sensor.occupancy':dict(role='presence',room='dining')})
PROFILE=dict(revision='fixture',execution_enabled=True,control_entities=['light.lamp'],
             policy=dict(devices={'light.lamp':dict(actions=['turn_on','turn_off'])}))


class Reviewer:
    def review(self,memory,policy,proposal,at):
        return review(memory.store,memory.config,policy,proposal,at)


class Gateway:
    def __init__(self,*,fail=False):
        self.calls=[]
        self.receipts={}
        self.fail=fail

    def submit(self,proposal,revision):
        self.calls.append(proposal['id'])
        receipt=dict(id=proposal['id'],entity_id=proposal['entity_id'],issued_at=proposal['issued_at'],
                     context_id='fixture-context',status='service_completed')
        self.receipts[proposal['id']]=receipt
        if self.fail: raise GatewayError('synthetic POST disconnected')
        return receipt

    def receipt(self,identifier):
        return self.receipts[identifier]


class Audio:
    def __init__(self,seconds=1): self.synthesized=[]; self.played=[]; self.seconds=seconds
    def synthesize(self,text): self.synthesized.append(text); return PCM(b'\0\0'*16000*self.seconds,16000)
    def play(self,audio): self.played.append(audio)


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=str(Path(self.temp.name)/'memory.sqlite3')
        self.memory=HomeMemory(CONFIG,self.path)
        self.now=AT
        self.gateway=Gateway()
        self.audio=Audio()
        self.speech=SpeechBroker(self.memory,self.audio,self.audio,clock=lambda:self.now)
        self.habit=dict(id='fixture-earned',entity_id='light.lamp',property='state',target='on',risk='low',
                        trust_level=2,status='active',alpha=100.,beta=1.)
        put(self.memory.store,'memory_habits',self.habit)
        with self.memory.store.db:
            self.memory.store.db.execute('INSERT INTO sessions(started_at,last_seen_at,status) VALUES (?,?,?)',
                                        (AT.isoformat(),AT.isoformat(),'connected'))
        self.memory.ingest(Observation('light.lamp',AT-timedelta(seconds=1),'off'))
        self.memory.ingest(Observation('binary_sensor.occupancy',AT-timedelta(seconds=1),'on',source='sensor'))
        self.proposal=dict(id='fixture-command',entity_id='light.lamp',action='turn_on',value=None,
                           habit_id=self.habit['id'],issued_at=AT.isoformat(),probability=.99,risk='low',announce=False)

    def tearDown(self): self.memory.close(); self.temp.cleanup()

    def controller(self,**kwargs):
        return Executor(self.memory,self.gateway,enabled=True,speech=self.speech,reviewer=Reviewer(),**kwargs)

    def echo(self,at,context='fixture-context'):
        from moqi.core import normalize
        self.memory.store.add(normalize(dict(event_type='state_changed',time_fired=at.isoformat(),data=dict(
            entity_id='light.lamp',old_state={'state':'off'},new_state=dict(state='on',context={'id':context}))),CONFIG))

    def test_first_action_reserves_audio_then_waits_for_matching_observation_and_plays_once(self):
        # Exercise the actual independent process as well as the simulated HTTP.
        executor=Executor(self.memory,self.gateway,enabled=True,speech=self.speech)
        self.assertEqual(executor.dispatch(self.proposal,PROFILE,AT)['status'],'service_completed')
        self.assertEqual(self.audio.played,[])
        self.assertEqual(executor.reconcile(AT+timedelta(seconds=1)),[])
        self.echo(AT+timedelta(seconds=2),context='another-automation')
        self.assertEqual(executor.reconcile(AT+timedelta(seconds=3)),[])
        self.echo(AT+timedelta(seconds=4))
        self.assertEqual(executor.reconcile(AT+timedelta(seconds=5)),[self.proposal['id']])
        self.assertEqual(len(self.audio.played),1)
        executor.reconcile(AT+timedelta(seconds=6))
        self.assertEqual(len(self.audio.played),1)
        self.assertEqual(len(self.gateway.calls),1)

    def test_post_timeout_recovers_receipt_after_restart_without_resend(self):
        self.gateway.fail=True
        self.assertEqual(self.controller().dispatch(self.proposal,PROFILE,AT)['status'],'pending')
        self.memory.close(); self.memory=HomeMemory(CONFIG,self.path)
        executor=Executor(self.memory,self.gateway,enabled=True,reviewer=Reviewer())
        self.echo(AT+timedelta(seconds=2))
        self.assertEqual(executor.reconcile(AT+timedelta(seconds=3)),[self.proposal['id']])
        self.assertEqual(len(self.gateway.calls),1)

    def test_unclaimed_first_announcement_is_reconstructed_after_restart_without_new_budget(self):
        self.controller().dispatch(self.proposal,PROFILE,AT)
        self.memory.close(); self.memory=HomeMemory(CONFIG,self.path)
        self.speech=SpeechBroker(self.memory,self.audio,self.audio,clock=lambda:AT+timedelta(seconds=3))
        self.echo(AT+timedelta(seconds=2))
        executor=Executor(self.memory,self.gateway,enabled=True,speech=self.speech,reviewer=Reviewer())
        executor.reconcile(AT+timedelta(seconds=3))
        self.assertEqual(len(self.gateway.calls),1)
        self.assertEqual(len(self.audio.played),1)
        self.assertEqual(self.memory.store.db.execute('SELECT COUNT(*) FROM speech_reservations').fetchone()[0],1)
        self.assertEqual(executor.announcement_holds(),[])

    def test_uncertain_or_expired_announcement_holds_next_execution_without_user_negative(self):
        executor=self.controller()
        executor.dispatch(self.proposal,PROFILE,AT)
        self.echo(AT+timedelta(seconds=2))
        self.memory.close(); self.memory=HomeMemory(CONFIG,self.path)
        self.speech=SpeechBroker(self.memory,self.audio,self.audio,clock=lambda:AT+timedelta(minutes=2))
        executor=Executor(self.memory,self.gateway,enabled=True,speech=self.speech,reviewer=Reviewer())
        executor.reconcile(AT+timedelta(minutes=2))
        self.assertEqual(len(self.audio.played),0)
        self.assertTrue(executor.announcement_holds())
        result=executor.dispatch(dict(self.proposal,id='later',issued_at=(AT+timedelta(minutes=2)).isoformat()),PROFILE,
                                 AT+timedelta(minutes=2))
        self.assertEqual(result['reason'],'required_announcement_unresolved')
        self.assertEqual(self.memory.store.db.execute('SELECT feedback FROM execution_ledger WHERE id=?',(self.proposal['id'],)).fetchone()[0],None)
        from moqi.memory import feedback
        self.memory.observe_interval(AT,AT+timedelta(minutes=11))
        with self.memory.store.db: feedback(self.memory.store,CONFIG,AT+timedelta(minutes=11))
        self.assertEqual(self.memory.store.db.execute('SELECT feedback FROM execution_ledger WHERE id=?',(self.proposal['id'],)).fetchone()[0],
                         'unscored_announcement_unresolved')
        self.assertEqual(executor.dispatch(self.proposal,PROFILE,AT+timedelta(seconds=4))['status'],'shadow')
        self.assertEqual(len(self.gateway.calls),1)

    def test_required_audio_unavailable_or_too_long_prevents_dispatch(self):
        executor=Executor(self.memory,self.gateway,enabled=True,reviewer=Reviewer())
        self.assertEqual(executor.dispatch(self.proposal,PROFILE,AT)['reason'],'announcement_unavailable')
        self.audio.seconds=9
        second=dict(self.proposal,id='overlong')
        self.assertEqual(self.controller().dispatch(second,PROFILE,AT)['reason'],'invalid_or_overlong_audio')
        self.assertEqual(self.gateway.calls,[])

    def test_shadow_mode_and_ha_disabled_never_post(self):
        self.assertEqual(Executor(self.memory,self.gateway).dispatch(self.proposal,PROFILE,AT)['reason'],'runtime_shadow_mode')
        self.assertEqual(self.controller().dispatch(self.proposal,dict(PROFILE,execution_enabled=False),AT)['reason'],
                         'ha_execution_not_selected')
        self.assertEqual(self.gateway.calls,[])

    def test_recheck_silence_after_synthesis_and_after_execution(self):
        self.now=AT.replace(hour=15)  # 23:00 household time.
        self.assertFalse(self.speech.prepare_action(self.proposal,self.now)['allowed'])
        self.assertEqual(self.audio.synthesized,[])
        self.now=AT
        executor=self.controller()
        executor.dispatch(self.proposal,PROFILE,AT)
        self.echo(AT+timedelta(seconds=2))
        executor.reconcile(AT.replace(hour=15))
        self.assertEqual(self.audio.played,[])

    def test_followup_l2_action_is_not_forced_to_reannounce_first_use(self):
        with self.memory.store.db:
            self.memory.store.db.execute('INSERT INTO execution_ledger(id,entity_id,target,before_state,issued_at,habit_id,status) VALUES (?,?,?,?,?,?,?)',
                ('previous','light.lamp','on','off',(AT-timedelta(days=1)).isoformat(),self.habit['id'],'acknowledged'))
        executor=Executor(self.memory,self.gateway,enabled=True,reviewer=Reviewer())
        self.assertEqual(executor.dispatch(self.proposal,PROFILE,AT)['status'],'service_completed')
        self.assertEqual(self.audio.synthesized,[])

    def test_even_daytime_audio_may_not_extend_into_the_silent_period(self):
        self.now=AT.replace(hour=14,minute=59,second=59)  # 22:59:59 household time.
        self.memory.ingest(Observation('binary_sensor.occupancy',self.now,'on',source='sensor'))
        self.audio.seconds=2
        result=self.speech.reply(dict(reply='合成的测试回复',reply_kind='explanation'),room='dining',
                                 request_id='near-night',user_engaged=True,at=self.now)
        self.assertEqual(result['reason'],'audio_crosses_night_silence')
        self.assertEqual(self.audio.played,[])


if __name__=='__main__': unittest.main()
