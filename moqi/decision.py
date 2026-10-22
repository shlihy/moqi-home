"""Plan §7 typed decisions and deterministic evidence explanations."""
import math
import re
from dataclasses import asdict,dataclass
from datetime import datetime
from zoneinfo import ZoneInfo
from .contexts import number
from .memory import predict, candidates
from .memory_store import objects
from .constraint_scopes import applies as constraint_applies


@dataclass(frozen=True)
class Proposal:
    id: str
    entity_id: str
    action: str
    value: object
    habit_id: str
    issued_at: str
    probability: float
    risk: str
    announce: bool

    def __post_init__(self):
        if self.action not in {'turn_on','turn_off','set_temperature','set_hvac_mode'}:
            raise ValueError('Action outside typed domain')
        if isinstance(self.probability,bool) or not isinstance(self.probability,(int,float)) or not math.isfinite(self.probability) or not 0<=self.probability<=1:
            raise ValueError('Invalid probability')
        if self.action=='set_temperature' and number(self.value) is None:
            raise ValueError('Temperature must be finite')
        if self.action=='set_hvac_mode' and self.value not in {'heat','cool','off','auto','dry','fan_only'}:
            raise ValueError('Unknown HVAC mode')


def explain(h):
    return (f"{h['entity_id']} → {h['target']}: {h['successes']} positive observations, "
            f"{h['failures']} negative opportunities; posterior {h['probability']:.3f}, "
            f"trust {h['trust_level']}, risk {h['risk']}; context {h['context']}.")


def announcement_required(store,habit):
    if habit['risk']=='high':
        return True
    prior=store.db.execute("SELECT 1 FROM execution_ledger WHERE habit_id=? AND status='acknowledged' LIMIT 1",(habit['id'],)).fetchone()
    return habit['trust_level']>=2 and prior is None


def decide(store,config,at,activity=None,*,record_forecasts=True):
    from .memory_store import identity
    result=[]
    constraints=objects(store,'memory_constraints')
    for h in (predict(store,config,at) if record_forecasts else candidates(store,config,at)):
        threshold={'low':.7,'medium':.85,'high':.95}.get(h['risk'])
        if h['property']=='temperature': action,value='set_temperature',number(h['target'])
        elif h['target'] in {'on','off'}: action,value='turn_'+h['target'],None
        else: action,value='set_hvac_mode',h['target']
        denied=any(constraint_applies(c,h['entity_id'],action,at,config) for c in constraints)
        reason='constraint' if denied else 'unclassified_or_prohibited' if threshold is None else (
            'trust_not_earned' if h['trust_level']<2 else 'risk_threshold' if h['probability']<threshold else 'safety_review_required')
        item=dict(habit_id=h['id'],entity_id=h['entity_id'],property=h['property'],target=h['target'],
                  probability=h['probability'],status='shadow',reason=reason,explanation=explain(h),proposal=None)
        if reason=='safety_review_required':
            p=Proposal(identity([h['id'],at.isoformat()]),h['entity_id'],action,value,h['id'],at.isoformat(),
                       h['probability'],h['risk'],announcement_required(store,h))
            item['proposal']=asdict(p)
        result.append(item)
    from .preferences import apply_to_shadow
    return apply_to_shadow(result,store,config,at,activity)


def parse_correction(text,config):
    """Rules accept only bounded intents. Ambiguity yields Noul, never a guessed device."""
    text=text.strip()
    if text in {'你最近学到了什么','你最近学到了什么？','最近学到了什么','what have you learned'}:
        return dict(intent='memory_changes',choice='Choice',confidence=1.)
    if re.fullmatch(r'为什么.*[?？]?',text):
        return dict(intent='explain',choice='Choice',confidence=1.)
    devices=[]
    for entity,spec in config['entities'].items():
        if spec['role']=='actuator' and any(alias in text for alias in [entity,*spec.get('aliases',[])]): devices.append(entity)
    if len(devices)!=1: return dict(intent='unknown',choice='Noul',reason='device ambiguous or absent')
    pattern=r'(?:以后)?(?P<scope>晚上|夜间|一直)?(?:别|不要)(?:自动)?(?P<verb>关|开)(?:掉|闭|启)?(?P<name>.+?)[。.!！]?'
    match=re.fullmatch(pattern,text)
    if not match: return dict(intent='unknown',choice='Noul',reason='no bounded correction rule matched')
    spec=config['entities'][devices[0]]
    if match['name'].strip() not in [devices[0],*spec.get('aliases',[])]:
        return dict(intent='unknown',choice='Noul',reason='device phrase contains unsupported qualifiers')
    return dict(intent='constraint',choice='Choice',confidence=1.,entity_id=devices[0],
                deny_actions=['turn_off' if match['verb']=='关' else 'turn_on'],
                scope='night' if match['scope'] in {'晚上','夜间'} else 'always',reason=text)
