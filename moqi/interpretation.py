"""Bounded household-language interpretation for explicit memory changes."""
import math
import re
import time

from .decision import parse_correction
from .jev_runtime import load_agent
from .memory_store import identity
from .jev_policy import BUILTIN, questions as policy_questions
from .jev_context import ContextLimit,bounded_state
DIGITS = dict(zip('零〇一二两三四五六七八九',[0,0,1,2,2,3,4,5,6,7,8,9]))
TEMPERATURE_PATTERN = r'(-?\d+(?:\.\d+)?|[零〇一二两三四五六七八九十]+(?:点[零一二三四五六七八九]+)?)\s*(摄氏度|华氏度|°\s*[CFcf]|度)'


def _chinese_number(value):
    whole,_,decimal = value.partition('点')
    if '十' in whole:
        tens,ones = whole.split('十')
        number = (DIGITS[tens] if tens else 1)*10+(DIGITS[ones] if ones else 0)
    elif len(whole) == 1:
        number = DIGITS[whole]
    else:
        raise ValueError('Ambiguous written temperature')
    return number+(float('0.'+''.join(str(DIGITS[c]) for c in decimal)) if decimal else 0)


def temperature(text):
    matches = re.findall(TEMPERATURE_PATTERN,text)
    if len(matches) != 1:
        raise ValueError('State one exact temperature and its unit')
    raw,unit = matches[0]
    value = float(raw) if re.fullmatch(r'-?\d+(?:\.\d+)?',raw) else _chinese_number(raw)
    if unit == '华氏度' or unit.upper().replace(' ','') == '°F':
        value = (value-32)*5/9
    return value


def _clarify(reason, **details):
    return {'status':'needs_clarification','reason':reason,**details}


class MemoryInterpreter:
    def __init__(self,config,*,model_path=None,agent=None,mode='hybrid',policy=None):
        if mode not in {'hybrid','model','rules'}:
            raise ValueError('Interpreter mode must be hybrid, model or rules')
        self.config,self.mode = config,mode
        self.agent = None if mode == 'rules' else agent
        self.model_path = model_path
        self.policy = policy or dict(id=BUILTIN,question_updates={},habit_facts=[])

    def _ask(self,text,names,trace):
        questions = policy_questions(self.policy,names)
        named={entity for entity,spec in self.config['entities'].items() if spec['role']=='actuator' and
               any(alias in text for alias in [entity,*spec.get('aliases',[])])}
        facts=[fact for fact in self.policy['habit_facts'] if not named or fact['entity_id'] in named]
        state,used,budget=bounded_state(self.agent,text,facts,questions)
        trace.setdefault('local_context',[]).append(dict(questions=names,**budget))
        captured={fact['id']:fact for fact in trace['habit_context']['habits']}
        captured.update({fact['id']:fact for fact in used})
        trace['habit_context']['habits']=list(captured.values())
        started = time.perf_counter()
        response = self.agent.system_one(state,questions)
        trace['model_ms'] += (time.perf_counter()-started)*1000
        trace['model'] = response.get('model')
        selected = {}
        for name,question in questions.items():
            answer = response['answers'][name]
            choice = answer['choice']
            probability = answer['probabilities'][choice]
            if (choice not in question['criteria'] or isinstance(probability,bool) or
                    not isinstance(probability,(int,float)) or not math.isfinite(probability) or
                    not 0 <= probability <= 1):
                raise ValueError('Invalid typed model output')
            selected[name] = choice
            trace['scores'][name] = {'choice':choice,'selected_score':probability}
        trace['question_sets'].append(identity(questions))
        return selected

    def _rule(self,text):
        if text in {'撤销刚才那条','撤销刚才的修改','撤销刚才的设置'}:
            return {'status':'interpreted','kind':'undo','provenance':{'engine':'rules'}}
        scope = None
        translated = text
        for prefix,kind in [('工作日晚上','weekday_night'),('今晚','temporary'),('今天晚上','temporary')]:
            if text.startswith(prefix):
                translated,scope = text[len(prefix):],kind
                break
        result = parse_correction(translated,self.config)
        if result['intent'] == 'unknown':
            # Exact, bounded preferences are code, not mandatory model calls.
            for entity,spec in self.config['entities'].items():
                if not (entity.startswith('climate.') or spec.get('category')=='hvac'):
                    continue
                for alias in [entity,*spec.get('aliases',[])]:
                    match = re.fullmatch(
                        r'(?:我)?(?P<activity>工作|办公|睡觉)(?:的时候|时|期间)[，,\s]*'+re.escape(alias)+
                        r'(?:设到|设为|调到)(?P<temperature>'+TEMPERATURE_PATTERN+r')(?:更舒服|就好)?[。.!！]?',text)
                    if match:
                        try:
                            value = temperature(match['temperature'])
                        except ValueError:
                            return None
                        return dict(status='interpreted',kind='preference',entity_id=entity,
                            activity='sleeping' if match['activity']=='睡觉' else 'working',value=value,
                            unit='°C',provenance={'engine':'rules'})
            return None
        kind = result['intent']
        value = {'status':'interpreted','kind':kind,'provenance':{'engine':'rules'}}
        if kind == 'constraint':
            value.update(entity_id=result['entity_id'],action=result['deny_actions'][0],scope=scope or result['scope'])
        return value

    def interpret(self,text,*,selected_event=None):
        if not isinstance(text,str) or not 1 <= len(text.strip()) <= 500:
            raise ValueError('Enter a statement of 1–500 characters')
        text = text.strip()
        if re.search(r'取消|算了|不用改|不是要求你|先别确定',text):
            return _clarify('The statement is withdrawn or does not authorize a memory change')
        if self.mode != 'model':
            rule = self._rule(text)
            if rule is not None:
                return rule
            if self.mode == 'rules':
                return _clarify('No bounded rule matched',provenance={'engine':'rules'})
        trace = {'engine':'edgejev','model_ms':0.,'scores':{},'question_sets':[],
                 'policy_id':self.policy['id'],'habit_context':{
                     'captured_at':self.policy.get('captured_at'),'habits':[]}}
        try:
            # Deterministic requests neither load the model nor require its files.
            if self.agent is None:
                if self.model_path is None:
                    return {'status':'unavailable','reason':'No local JEV configured for unresolved semantics',
                            'provenance':trace}
                self.agent = load_agent(self.model_path)
            kind = self._ask(text,['intent'],trace)['intent']
            if kind == 'unknown':
                return _clarify('No explicit supported memory request',provenance=trace)
            result = {'status':'interpreted','kind':kind,'provenance':trace}
            if kind in {'explain','memory_changes'}:
                return result
            if kind in {'exception','attribution'}:
                if not selected_event:
                    return _clarify('Select the event whose evidence you want to correct',provenance=trace)
                result['event_key'] = selected_event
                if kind == 'attribution':
                    source = self._ask(text,['source'],trace)['source']
                    if source == 'unknown':
                        return _clarify('The event source is not stated clearly',provenance=trace)
                    result['source'] = source
                return result
            devices = {entity:spec for entity,spec in self.config['entities'].items() if spec['role']=='actuator'}
            # A bounded entity must be named by a configured alias, never guessed from “that”.
            named = [entity for entity,spec in devices.items()
                     if any(alias in text for alias in [entity,*spec.get('aliases',[])])]
            if len(named) != 1:
                return _clarify('Name one configured device',provenance=trace)
            result['entity_id'] = named[0]
            if kind == 'constraint':
                selected = self._ask(text,['action','scope'],trace)
                if 'unknown' in selected.values():
                    return _clarify('Clarify the prohibited action or time scope',provenance=trace)
                result.update(selected)
            elif kind == 'preference':
                activity = self._ask(text,['activity'],trace)['activity']
                if activity == 'unknown':
                    return _clarify('State working or sleeping as the activity',provenance=trace)
                try:
                    result.update(activity=activity,value=temperature(text),unit='°C')
                except (ValueError,KeyError):
                    return _clarify('State one exact temperature and its unit',provenance=trace)
            return result
        except ContextLimit as exc:
            return _clarify(str(exc),provenance=trace)
        except (KeyError,TypeError,ValueError,RuntimeError) as exc:
            return {'status':'failed','reason':'Local model inference failed or returned invalid types',
                    'error':str(exc),'provenance':trace}
