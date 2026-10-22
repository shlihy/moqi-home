"""Voice conversation state and memory operations; no audio or web interaction.

Adapters provide a trusted wake-session ID, room and ASR result. Reply text is a
template for a downstream speech gate, not evidence that audio was played.
"""
import json
import re
from datetime import timedelta
from zoneinfo import ZoneInfo

from .contexts import dt
from .corrections import preview,request as change_request
from .interpretation import MemoryInterpreter
from .memory_store import canonical
from . import jev_policy

YES = {'是','是的','对','对的','确认','没错'}
NO = {'不对','不是','取消','算了','不用改'}
EXCEPTION = {'刚才来了客人','刚才是来了客人','刚才那次是临时情况'}


class VoiceConversation:
    def __init__(self,memory,interpreter=None):
        self.memory = memory
        self.config = memory.config
        self.interpreter = interpreter or MemoryInterpreter(self.config)

    def _load(self,room):
        row = self.memory.store.db.execute('SELECT body FROM voice_sessions WHERE room=?',(room,)).fetchone()
        return json.loads(row[0]) if row else {}

    def _save(self,room,state,at):
        state['updated_at'] = at.isoformat()
        with self.memory.store.db:
            self.memory.store.db.execute('INSERT OR REPLACE INTO voice_sessions VALUES (?,?)',(room,canonical(state)))

    def discard_pending(self,room,at):
        """A prompt that could not be played cannot authorize a later yes."""
        state=self._load(room)
        self._cancel(state,at)
        self._save(room,state,at)

    def _event(self,text,room,at):
        named = [entity for entity,spec in self.config['entities'].items()
                 if spec['role']=='actuator' and any(alias in text for alias in [entity,*spec.get('aliases',[])])]
        if len(named)>1:
            return None
        rows = [row for row in self.memory.store.events() if row['role']=='actuator' and
                row['room']==room and row['kind'] in {'transition','attributes'} and
                at-timedelta(minutes=5)<=dt(row['occurred_at'])<at and row['raw_json'] and row['raw_json']!='{}' and
                (not named or row['entity_id']==named[0])]
        if not rows or len({row['entity_id'] for row in rows})!=1:
            return None
        return rows[-1]['event_key']

    def _cancel(self,state,at):
        if state.get('pending_change'):
            value = change_request(self.memory.store,state['pending_change'])
            if value['status']=='pending':
                self.memory.cancel_change(value['id'],at)
            elif value['status']=='applied':
                # Recover a crash between memory commit and conversation save.
                state['last_change'] = value['id']
        for key in ('pending_change','pending_until','pending_kind','session_id'):
            state.pop(key,None)

    def _description(self,parsed):
        entity = parsed.get('entity_id')
        spec = self.config['entities'].get(entity,{})
        name = (spec.get('aliases') or [entity])[0]
        if parsed['kind']=='constraint':
            scope = {'temporary':'今晚','night':'以后每晚','weekday_night':'以后工作日晚上','always':'以后'}[parsed['scope']]
            action = '关闭' if parsed['action']=='turn_off' else '开启'
            return scope+'不自动'+action+name
        if parsed['kind']=='preference':
            return ('睡觉时' if parsed['activity']=='sleeping' else '工作时')+name+'设为'+str(parsed['value'])+'摄氏度'
        return '将刚才那次操作按你说的修正'

    def handle(self,text,*,session_id,room,at):
        at = self.memory._at(at)
        if room not in {spec['room'] for spec in self.config['entities'].values()}:
            raise ValueError('Use a configured voice room')
        if not isinstance(session_id,str) or not session_id or len(session_id)>100:
            raise ValueError('A trusted wake-session ID is required')
        if not isinstance(text,str) or not 1<=len(text.strip())<=500:
            raise ValueError('ASR text must have 1–500 characters')
        text = text.strip().rstrip('。！!？?')
        state = self._load(room)
        if state.get('updated_at') and at<dt(state['updated_at']):
            raise ValueError('Voice conversation time moved backwards')
        night = at.astimezone(ZoneInfo(self.config.get('timezone','Asia/Shanghai'))).hour in {23,0,1,2,3,4,5,6}

        def reply(status,message=None,*,kind='correction',**extra):
            self._save(room,state,at)
            return dict(status=status,reply=None if night else message,reply_kind=kind,
                        night_silence=night,physical_execution=False,**extra)

        if not text:
            return reply('unchanged')

        if state.get('pending_until') and (at>=dt(state['pending_until']) or state.get('session_id')!=session_id):
            self._cancel(state,at)
        if text in NO:
            if text in {'不对','不是'} and state.get('pending_change'):
                value = change_request(self.memory.store,state['pending_change'])
                identifier = value['interpretation']['provenance'].get('policy_id')
                if identifier:
                    jev_policy.feedback(self.memory.store,identifier,value['id'],'rejected',at)
            self._cancel(state,at)
            return reply('cancelled','好，不修改。')
        if text in YES:
            identifier = state.get('pending_change')
            if not identifier or night:
                return reply('unchanged','现在没有待确认的修改。')
            result = self.memory.apply_change(identifier,at)
            policy_id = result['interpretation']['provenance'].get('policy_id')
            if policy_id:
                jev_policy.feedback(self.memory.store,policy_id,identifier,'confirmed',at)
            self._cancel_applied(state)
            state['last_change'] = identifier
            return reply('applied','好，记住了。',change_id=result['id'])
        if text in {'撤销刚才那条','撤销刚才的修改','撤销刚才的设置'}:
            self._cancel(state,at)
            identifier = state.get('last_change')
            if not identifier:
                return reply('unchanged','这里没有可撤销的语音修改。')
            self.memory.undo_change(identifier,at)
            return reply('undone','已经撤销刚才的修改。',change_id=identifier)
        if text in EXCEPTION:
            self._cancel(state,at)
        following_reference = state.get('pending_kind')=='exception'
        if following_reference:
            aliases = [alias for entity,spec in self.config['entities'].items() if spec['role']=='actuator'
                       for alias in [entity,*spec.get('aliases',[])]]
            if not any(re.fullmatch(re.escape(alias)+r'(?:刚才的?那次|那次)?',text) for alias in aliases):
                self._cancel(state,at)
                following_reference = False
        if text in EXCEPTION or following_reference:
            event_key = self._event(text,room,at)
            if event_key is None:
                if night:
                    return reply('needs_clarification')
                if following_reference:
                    self._cancel(state,at)
                    return reply('unchanged','还不能确定是哪次，先不修改。')
                state.update(pending_kind='exception',pending_until=(at+timedelta(seconds=60)).isoformat(),session_id=session_id)
                return reply('needs_clarification','你说的是哪台设备刚才的操作？',kind='clarification')
            self._cancel(state,at)
            parsed = dict(status='interpreted',kind='exception',event_key=event_key,provenance={'engine':'rules'})
        else:
            self._cancel(state,at)
            # Night output is impossible; do not run a model that could only
            # produce a clarification/confirmation we cannot voice then.
            parser = MemoryInterpreter(self.config,mode='rules') if night else self.interpreter
            parser.policy = jev_policy.prepare(self.memory.store,jev_policy.active(self.memory.store),at)
            parsed = parser.interpret(text,selected_event=self._event(text,room,at))
        if parsed.get('status')!='interpreted':
            return reply(parsed['status'],'这句话我还不能确定，暂时不改。')
        if parsed['kind']=='memory_changes':
            habits = self.memory.inspect(at)['habits']
            count = sum(h['context']['room']==room for h in habits)
            return reply('read_only',f'这个房间目前有{count}条习惯线索。',kind='memory_changes')
        if parsed['kind']=='explain':
            named = [entity for entity,spec in self.config['entities'].items()
                     if any(alias in text for alias in [entity,*spec.get('aliases',[])])]
            rows = [(identifier,entity,issued,habit) for identifier,entity,issued,habit in
                self.memory.store.db.execute("SELECT id,entity_id,issued_at,habit_id FROM execution_ledger WHERE status='acknowledged'")
                if self.config['entities'].get(entity,{}).get('room')==room and
                at-timedelta(minutes=5)<=dt(issued)<at and (not named or len(named)==1 and entity==named[0])]
            if rows and len({entity for identifier,entity,issued,habit in rows})==1:
                execution = max(rows,key=lambda row:dt(row[2]))
                habit = next((h for h in self.memory.inspect(at)['habits'] if h['id']==execution[3]),None)
                if habit:
                    return reply('read_only',f"这次依据已学到的习惯，目前有{habit['successes']}条支持记录。",
                                 kind='explanation',execution_id=execution[0],habit_id=habit['id'])
            # Without a verified execution reference, a habit is not a reason
            # that a particular physical action happened.
            return reply('read_only','我还没有定位到这次操作的执行记录。',kind='explanation')
        request = preview(self.memory.store,self.config,parsed,text,at,input_source='voice')
        if parsed.get('provenance',{}).get('engine')=='edgejev':
            state.update(pending_change=request['id'],pending_until=(at+timedelta(seconds=60)).isoformat(),session_id=session_id)
            return reply('needs_confirmation','你是说'+self._description(parsed)+'，对吗？',kind='confirmation',change_id=request['id'])
        result = self.memory.apply_change(request['id'],at)
        state['last_change'] = result['id']
        return reply('applied','好，已记住。',change_id=result['id'])

    @staticmethod
    def _cancel_applied(state):
        for key in ('pending_change','pending_until','pending_kind','session_id'):
            state.pop(key,None)
