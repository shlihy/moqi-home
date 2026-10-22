"""In-memory PCM and the synthesis/playback boundary. No model is imported here."""
from dataclasses import dataclass
from datetime import datetime,timezone
import json

from . import speech_policy
from .contexts import dt,latest


@dataclass(frozen=True)
class PCM:
    data: bytes
    sample_rate: int

    def __post_init__(self):
        if not isinstance(self.data,bytes) or not self.data or len(self.data)%2:
            raise ValueError('PCM must contain nonempty mono signed 16-bit samples')
        if type(self.sample_rate) is not int or self.sample_rate<=0:
            raise ValueError('PCM sample rate must be a positive integer')

    @property
    def duration(self):
        return len(self.data)/(2*self.sample_rate)


def occupancy(memory,room,at):
    rows=[]
    for entity,spec in memory.config['entities'].items():
        if spec['room']==room and spec['role']=='presence' and not spec.get('boundary'):
            row=latest(memory.store.events(),entity,at)
            if row and row['new_state'] in spec.get('present_states',['on','home']):
                rows.append(dt(row['occurred_at']))
    return (True,max(rows)) if rows else (False,None)


class SpeechBroker:
    def __init__(self,memory,synthesizer,player,*,clock=None):
        self.memory,self.synthesizer,self.player=memory,synthesizer,player
        self.clock=clock or (lambda:datetime.now(timezone.utc))
        self.prepared={}

    def _audio(self,text,room,at):
        from zoneinfo import ZoneInfo
        hour=at.astimezone(ZoneInfo(self.memory.config.get('timezone','Asia/Shanghai'))).hour
        occupied,seen=occupancy(self.memory,room,at)
        if hour>=23 or hour<7:
            return None,{'allowed':False,'reason':'night_silence'}
        if getattr(self.player,'busy',False):
            return None,{'allowed':False,'reason':'audio_output_busy'}
        if not occupied or not 0<=(at-seen).total_seconds()<=60:
            return None,{'allowed':False,'reason':'occupancy_not_verified'}
        audio=self.synthesizer.synthesize(text)
        if not isinstance(audio,PCM):
            raise ValueError('Synthesizer must return mono PCM16 audio')
        return audio,None

    def reply(self,result,*,room,request_id,user_engaged,at):
        if result.get('reply') is None:
            return {'allowed':False,'reason':'no_reply'}
        audio,denied=self._audio(result['reply'],room,at)
        if denied: return denied
        now=self.clock()
        occupied,seen=occupancy(self.memory,room,now)
        decision=speech_policy.reserve(self.memory.store,self.memory.config,request_id=request_id,at=now,
            room=room,kind=result['reply_kind'],duration_seconds=audio.duration,
            occupied=occupied,occupancy_at=seen,user_engaged=user_engaged)
        if decision['allowed']:
            self.player.play(audio)
        return dict(decision,playback_attempted=decision['allowed'])

    def _announcement(self,proposal):
        room=self.memory.config['entities'][proposal['entity_id']]['room']
        name=(self.memory.config['entities'][proposal['entity_id']].get('aliases') or [proposal['entity_id']])[0]
        verb={'turn_on':'打开','turn_off':'关闭','set_temperature':'调整温度','set_hvac_mode':'切换模式'}[proposal['action']]
        return room,'我已按习惯'+verb+name+'。不合适就手动改回来。'

    def prepare_action(self,proposal,at):
        room,text=self._announcement(proposal)
        audio,denied=self._audio(text,room,at)
        if denied: return denied
        now=self.clock()
        occupied,seen=occupancy(self.memory,room,now)
        identifier='execution-'+proposal['id']
        kind='safety' if proposal['risk']=='high' else 'new_habit'
        result=speech_policy.reserve(self.memory.store,self.memory.config,request_id=identifier,at=now,room=room,
            kind=kind,duration_seconds=audio.duration,occupied=occupied,occupancy_at=seen,
            habit_id=proposal['habit_id'] if kind=='new_habit' else None,execution_id=proposal['id'])
        if result['allowed']:
            self.prepared[proposal['id']]=(identifier,room,audio)
        return result

    def announce(self,execution_id,at):
        value=self.prepared.get(execution_id)
        if value is None:
            recovered=self._restore(execution_id,at)
            if not recovered['allowed']: return recovered
            value=self.prepared[execution_id]
        identifier,room,audio=value
        occupied,seen=occupancy(self.memory,room,at)
        result=speech_policy.claim_prepared(self.memory.store,self.memory.config,identifier,at,
                                          occupied=occupied,occupancy_at=seen)
        if result['allowed']:
            try:
                self.player.play(audio)
            except Exception:
                with self.memory.store.db:
                    self.memory.store.db.execute("UPDATE prepared_speech SET status='playback_failed' WHERE id=?",(identifier,))
                self.prepared.pop(execution_id,None)
                raise
            with self.memory.store.db:
                self.memory.store.db.execute("UPDATE prepared_speech SET status='playback_attempted' WHERE id=?",(identifier,))
        if result['allowed'] or result['reason']!='execution_not_observed':
            self.prepared.pop(execution_id,None)
        return dict(result,playback_attempted=result['allowed'])

    def _restore(self,execution_id,at):
        """Rebuild an unclaimed buffer once, using the original reservation.

        A crash after playback was claimed has an uncertain audible outcome;
        that announcement is held for diagnosis and is never automatically replayed.
        """
        store=self.memory.store
        row=store.db.execute('''SELECT p.id,p.status,s.room,s.at,s.duration_seconds,d.body
            FROM prepared_speech p JOIN speech_reservations s ON s.id=p.id
            JOIN execution_dispatch d ON d.id=p.execution_id WHERE p.execution_id=?''',(execution_id,)).fetchone()
        if not row:
            return dict(allowed=False,reason='announcement_not_required')
        identifier,status,room,reserved,duration,body=row
        if status!='prepared':
            return dict(allowed=False,reason='announcement_already_attempted_or_held')
        proposal=json.loads(body)['proposal']
        spec=self.memory.config['entities'].get(proposal['entity_id'])
        if not spec or spec['room']!=room or not 0<=(at-dt(reserved)).total_seconds()<=60:
            with store.db: store.db.execute("UPDATE prepared_speech SET status='abandoned' WHERE id=?",(identifier,))
            return dict(allowed=False,reason='prepared_audio_expired_or_scope_changed')
        _,text=self._announcement(proposal)
        audio,denied=self._audio(text,room,at)
        if denied: return denied
        if audio.duration>duration:
            with store.db: store.db.execute("UPDATE prepared_speech SET status='abandoned' WHERE id=?",(identifier,))
            return dict(allowed=False,reason='restored_audio_exceeds_reservation')
        self.prepared[execution_id]=(identifier,room,audio)
        return dict(allowed=True,reason='prepared_audio_reconstructed')
