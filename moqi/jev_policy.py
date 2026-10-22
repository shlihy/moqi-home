"""Versioned JEV wording, evidence-grounded context and measured promotion.

Policies are operational releases, outside household-memory rollback. They do
not change answer domains, device permissions, empirical counts or trust.
"""
import json
from copy import deepcopy
from datetime import timedelta

from .contexts import dt
from .jev_questions import QUESTIONS
from .memory_store import canonical, change, identity, objects

BUILTIN = 'builtin-'+identity(QUESTIONS)
SCHEMA = '''
CREATE TABLE IF NOT EXISTS jev_policies(id TEXT PRIMARY KEY,created_at TEXT,parent_id TEXT,status TEXT,body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jev_policy_state(singleton INTEGER PRIMARY KEY CHECK(singleton=1),active_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jev_policy_evaluations(id TEXT PRIMARY KEY,policy_id TEXT,at TEXT,phase TEXT,body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jev_policy_feedback(request_id TEXT PRIMARY KEY,policy_id TEXT,at TEXT,signal TEXT);
'''


def get(store,identifier):
    if identifier==BUILTIN:
        return dict(id=BUILTIN,status='builtin',question_updates={},habit_ids=[],evidence_ids=[])
    row = store.db.execute('SELECT created_at,parent_id,status,body FROM jev_policies WHERE id=?',(identifier,)).fetchone()
    if row is None:
        raise ValueError('Unknown JEV policy version')
    return dict(json.loads(row[3]),id=identifier,created_at=row[0],parent_id=row[1],status=row[2])


def active(store):
    row = store.db.execute('SELECT active_id FROM jev_policy_state WHERE singleton=1').fetchone()
    return get(store,row[0] if row else BUILTIN)


def validate_candidate(store,value,evidence_ids,at):
    required = {'schema_version','parent_id','question_updates','habit_ids','evidence_ids','rationale'}
    if not isinstance(value,dict) or set(value)!=required or type(value['schema_version']) is not int or value['schema_version']!=1:
        raise ValueError('Invalid JEV policy contract')
    parent = active(store)
    if value['parent_id']!=parent['id']:
        raise ValueError('Policy parent is no longer active')
    patches = value['question_updates']
    if not isinstance(patches,dict) or not patches or set(patches)-set(QUESTIONS):
        raise ValueError('Only existing JEV questions may be updated')
    for name,patch in patches.items():
        if not isinstance(patch,dict) or set(patch)!={'instructions','criteria'}:
            raise ValueError('A JEV patch contains only instructions and criteria')
        if not isinstance(patch['instructions'],str) or not 1<=len(patch['instructions'])<=400:
            raise ValueError('JEV instructions must have 1-400 characters')
        if not isinstance(patch['criteria'],dict) or set(patch['criteria'])!=set(QUESTIONS[name]['criteria']):
            raise ValueError('A JEV policy cannot change possible answer IDs')
        if any(not isinstance(v,str) or not 1<=len(v)<=100 for v in patch['criteria'].values()):
            raise ValueError('JEV answer descriptions must have 1-100 characters')
    cited = value['evidence_ids']
    if (not isinstance(cited,list) or not cited or len(cited)>30 or
            any(not isinstance(key,str) or key not in evidence_ids for key in cited)):
        raise ValueError('JEV policy needs retained, known evidence references')
    known = {h['id'] for h in objects(store,'memory_habits') if h['status']=='active' and dt(h['created_at'])<=at}
    if (not isinstance(value['habit_ids'],list) or len(value['habit_ids'])>8 or
            any(not isinstance(key,str) or key not in known for key in value['habit_ids'])):
        raise ValueError('JEV habit context must reference observed active habits')
    if not isinstance(value['rationale'],str) or not 1<=len(value['rationale'])<=1000:
        raise ValueError('JEV policy needs a short rationale')
    complete = deepcopy(value)
    complete['question_updates'] = {**parent['question_updates'],**complete['question_updates']}
    return dict(complete,id=identity(complete),created_at=at.isoformat(),status='proposed')


def insert_candidate(store,value,provenance):
    """Caller owns the transaction; inserts never activate a candidate."""
    body = {k:v for k,v in value.items() if k not in {'id','created_at','status','parent_id'}}
    body['provenance'] = provenance
    store.db.execute('INSERT OR IGNORE INTO jev_policies VALUES (?,?,?,?,?)',
        (value['id'],value['created_at'],value['parent_id'],'proposed',canonical(body)))
    return get(store,value['id'])


def prepare(store,policy,at):
    """Resolve facts from current memory, not a model-authored habit narrative."""
    facts = []
    for habit in objects(store,'memory_habits'):
        if habit['id'] in policy['habit_ids'] and habit['status']=='active' and dt(habit['created_at'])<=at:
            facts.append({key:habit[key] for key in ('id','entity_id','property','target','context','successes','failures','trust_level')})
    return dict(id=policy['id'],question_updates=deepcopy(policy['question_updates']),habit_facts=facts,captured_at=at.isoformat())


def questions(policy,names):
    result = {name:deepcopy(QUESTIONS[name]) for name in names}
    for name in names:
        patch = policy.get('question_updates',{}).get(name)
        if patch:
            result[name].update(deepcopy(patch))
    return result


def _score(outputs,cases):
    supported = emitted = correct = unsafe = mandatory_failed = errors = 0
    for result,case in zip(outputs,cases):
        expected = case['expected']
        is_supported = expected.get('status')!='needs_clarification'
        ready = result.get('status')=='interpreted'
        matches = (ready and all(result.get(key)==value for key,value in expected.items())) if is_supported else result.get('status')=='needs_clarification'
        supported += is_supported
        emitted += bool(is_supported and ready)
        correct += bool(is_supported and ready and matches)
        unsafe += bool(not is_supported and ready)
        mandatory_failed += bool(case.get('mandatory',False) and not matches)
        errors += not matches
    return dict(cases=len(cases),supported=supported,emitted=emitted,correct=correct,
                precision=correct/emitted if emitted else 0.,coverage=emitted/supported if supported else 0.,
                unsafe=unsafe,mandatory_failed=mandatory_failed,errors=errors)


def validate_expected(value):
    """Labels must check the actual modification, not merely 'interpreted'."""
    if not isinstance(value,dict): raise ValueError('A case needs independent expected fields')
    if value.get('status')=='needs_clarification': return
    fields={
        'constraint':{'kind','entity_id','action','scope'},
        'preference':{'kind','entity_id','activity','value','unit'},
        'exception':{'kind','event_key'},'attribution':{'kind','event_key','source'},
        'explain':{'kind'},'memory_changes':{'kind'},'undo':{'kind'}}
    kind=value.get('kind')
    if kind not in fields or not fields[kind]<=set(value):
        raise ValueError('An expected label must specify every meaningful modification field')
    if 'entity_id' in value and (not isinstance(value['entity_id'],str) or not value['entity_id']):
        raise ValueError('Expected entity must be explicit')
    if kind=='constraint' and (value['action'] not in {'turn_on','turn_off'} or
            value['scope'] not in {'temporary','night','weekday_night','always'}):
        raise ValueError('Expected constraint is outside the supported domain')
    if kind=='attribution' and value['source'] not in {'human','device'}:
        raise ValueError('Expected source must be independently known')
    if kind=='preference':
        from .contexts import number
        if value['activity'] not in {'working','sleeping'} or value['unit']!='°C' or number(value['value']) is None:
            raise ValueError('Expected preference needs an exact Celsius value and activity')


def evaluate(store,identifier,cases,judge,at,*,phase):
    """Judge runs both fixed versions; labels must be independent of model output.

    This routine never runs a model itself. A real-model evaluator belongs on the
    authorized server. A fixture judge tests release mechanics only.
    """
    if phase not in {'validation','shadow'}:
        raise ValueError('Evaluation phase must be validation or shadow')
    policy = get(store,identifier)
    if policy['status'] not in {'proposed','shadow'}:
        raise ValueError('Evaluate an unactivated policy candidate')
    if at<dt(policy['created_at']):
        raise ValueError('Policy evaluation cannot precede its proposal')
    if not isinstance(cases,list) or not cases:
        raise ValueError('Evaluation requires labelled cases')
    identifiers = []
    for case in cases:
        if (not isinstance(case,dict) or not isinstance(case.get('id'),str) or not case['id'] or
                not isinstance(case.get('text'),str) or not case['text'] or
                not isinstance(case.get('expected'),dict) or not case['expected'] or
                'at' not in case or dt(case['at'])>at):
            raise ValueError('Each case needs a stable ID, past event time, text and independent expected fields')
        identifiers.append(case['id'])
        validate_expected(case['expected'])
        context = case.get('habit_context')
        if context is not None and (not isinstance(context,dict) or not isinstance(context.get('captured_at'),str) or
                not isinstance(context.get('habits'),list) or
                any(not isinstance(h,dict) or not isinstance(h.get('id'),str) for h in context['habits']) or
                dt(context['captured_at'])>dt(case['at'])):
            raise ValueError('Replay context must be captured no later than its case')
    if len(identifiers)!=len(set(identifiers)):
        raise ValueError('Evaluation case IDs must be unique')
    previous = _latest_evaluation(store,identifier,'validation')
    if phase=='shadow':
        if not previous or not previous['passed']:
            raise ValueError('Shadow evaluation follows a passing validation')
        if set(identifiers)&set(previous['case_ids']) or any(dt(case['at'])<=dt(previous['at']) for case in cases):
            raise ValueError('Shadow cases must be new observations after validation')
    base = get(store,policy['parent_id'])
    def frozen_context(version,case):
        # Never enrich historical cases with today's learned statistics.
        context = case.get('habit_context',{'captured_at':case['at'],'habits':[]})
        return dict(id=version['id'],question_updates=deepcopy(version['question_updates']),
                    captured_at=context['captured_at'],habit_facts=[deepcopy(h) for h in context['habits']
                        if h['id'] in version['habit_ids']])
    baseline = [judge(frozen_context(base,case),case) for case in cases]
    proposed = [judge(frozen_context(policy,case),case) for case in cases]
    old,new = _score(baseline,cases),_score(proposed,cases)
    minimum = 200 if phase=='validation' else 50
    supported_minimum=100 if phase=='validation' else 20
    ambiguous_minimum=20 if phase=='validation' else 5
    passed = (len(cases)>=minimum and new['supported']>=supported_minimum and
              len(cases)-new['supported']>=ambiguous_minimum and new['precision']>=.98 and
              new['coverage']>=.8 and new['unsafe']==0 and new['mandatory_failed']==0 and
              new['errors']<=old['errors'] and new['coverage']>=old['coverage'])
    if phase=='validation':
        passed = passed and old['errors']>0 and (old['errors']-new['errors'])/old['errors']>=.2
    report = dict(policy_id=identifier,parent_id=policy['parent_id'],at=at.isoformat(),phase=phase,
                  passed=passed,baseline=old,candidate=new,case_ids=identifiers,
                  cases_hash=identity(cases),id=identity([identifier,phase,at.isoformat(),cases]),
                  results=[dict(id=case['id'],expected=case['expected'],baseline=a,candidate=b)
                           for case,a,b in zip(cases,baseline,proposed)])
    with store.db:
        store.db.execute('INSERT INTO jev_policy_evaluations VALUES (?,?,?,?,?)',
            (report['id'],identifier,at.isoformat(),phase,canonical(report)))
        if phase=='validation' and passed:
            store.db.execute("UPDATE jev_policies SET status='shadow' WHERE id=?",(identifier,))
        change(store,at.isoformat(),'jev_policy_evaluated',report)
    return report


def _latest_evaluation(store,identifier,phase):
    row = store.db.execute('SELECT body FROM jev_policy_evaluations WHERE policy_id=? AND phase=? ORDER BY at DESC,rowid DESC LIMIT 1',
                           (identifier,phase)).fetchone()
    return json.loads(row[0]) if row else None


def activate(store,identifier,at):
    if store.db.in_transaction:
        raise ValueError('Commit the current transaction before activating a policy')
    store.db.execute('BEGIN IMMEDIATE')
    try:
        policy = get(store,identifier)
        if active(store)['id']==identifier:
            store.db.rollback()
            return policy
        if policy['status']!='shadow' or active(store)['id']!=policy['parent_id']:
            raise ValueError('Policy is not a validated successor of the active version')
        for phase in ('validation','shadow'):
            report = _latest_evaluation(store,identifier,phase)
            if not report or not report['passed'] or dt(report['at'])>at:
                raise ValueError('Policy needs passing validation and prospective shadow evidence')
        store.db.execute("UPDATE jev_policies SET status='retired' WHERE status='active'")
        store.db.execute("UPDATE jev_policies SET status='active' WHERE id=?",(identifier,))
        store.db.execute('INSERT OR REPLACE INTO jev_policy_state VALUES (1,?)',(identifier,))
        change(store,at.isoformat(),'jev_policy_activated',dict(id=identifier,parent_id=policy['parent_id']))
        store.db.commit()
    except Exception:
        store.db.rollback()
        raise
    return get(store,identifier)


def rollback(store,at,*,reason,expected_id=None):
    if store.db.in_transaction:
        raise ValueError('Commit the current transaction before rolling back a policy')
    store.db.execute('BEGIN IMMEDIATE')
    try:
        current = active(store)
        if current['id']==BUILTIN or expected_id is not None and expected_id!=current['id']:
            store.db.rollback()
            return current
        previous = get(store,current['parent_id'])
        store.db.execute("UPDATE jev_policies SET status='rolled_back' WHERE id=?",(current['id'],))
        if previous['id']!=BUILTIN:
            store.db.execute("UPDATE jev_policies SET status='active' WHERE id=?",(previous['id'],))
        store.db.execute('INSERT OR REPLACE INTO jev_policy_state VALUES (1,?)',(previous['id'],))
        change(store,at.isoformat(),'jev_policy_rolled_back',dict(id=current['id'],restored=previous['id'],reason=reason))
        store.db.commit()
    except Exception:
        store.db.rollback()
        raise
    return active(store)


def feedback(store,identifier,request_id,signal,at):
    if signal not in {'confirmed','rejected'}:
        raise ValueError('Policy feedback must be an explicit voice confirmation or rejection')
    get(store,identifier)
    with store.db:
        store.db.execute('INSERT OR IGNORE INTO jev_policy_feedback VALUES (?,?,?,?)',
                         (request_id,identifier,at.isoformat(),signal))
    if signal=='rejected' and active(store)['id']==identifier and identifier!=BUILTIN:
        cutoff = (at-timedelta(days=7)).isoformat()
        count = store.db.execute("SELECT COUNT(*) FROM jev_policy_feedback WHERE policy_id=? AND signal='rejected' AND at>=? AND at<=?",
                                 (identifier,cutoff,at.isoformat())).fetchone()[0]
        if count>=2:
            return rollback(store,at,expected_id=identifier,reason='two explicit interpretation rejections within seven days')
    return active(store)
