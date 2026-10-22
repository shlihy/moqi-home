"""Durable preview/apply/cancel/undo for explicit household memory changes."""
import json
from contextlib import contextmanager
from uuid import uuid4

from .constraint_scopes import resolve_scope
from .contexts import dt, number
from .memory_store import canonical, change, identity, objects, put
from .preferences import ACTIVITIES
from .understanding import constraint_value


@contextmanager
def _transaction(store):
    if store.db.in_transaction:
        raise RuntimeError('Finish the current observation transaction before changing memory')
    store.db.execute('BEGIN IMMEDIATE')
    try:
        yield
        store.db.commit()
    except Exception:
        store.db.rollback()
        raise


def _event(store, key, at):
    row = next((r for r in store.events() if r['event_key'] == key), None)
    if not row or row['role'] != 'actuator' or row['kind'] not in {'transition','attributes'}:
        raise ValueError('Select an existing actuator change')
    if dt(row['occurred_at']) >= at or not row['raw_json'] or row['raw_json'] == '{}':
        raise ValueError('The selected event is future-dated or its raw evidence is no longer retained')
    return row


def _target(store, config, parsed, text, at, input_source='app'):
    kind = parsed['kind']
    if kind in {'exception','attribution'}:
        event = _event(store, parsed['event_key'], at)
        if kind == 'attribution':
            if parsed['source'] not in {'human','device','unknown'}:
                raise ValueError('Invalid attribution source')
            return 'annotations', event['event_key'], {'source': parsed['source'], 'reason': text}
        value = {'id':event['event_key'],'event_key':event['event_key'],'entity_id':event['entity_id'],
                 'reason':text,'source':'voice' if input_source=='voice' else 'text','created_at':at.isoformat(),'active':True}
        return 'memory_exclusions', value['id'], value
    entity = parsed['entity_id']
    spec = config['entities'].get(entity)
    if not spec or spec['role'] != 'actuator':
        raise ValueError('Select an allowlisted actuator')
    if kind == 'constraint':
        scope = resolve_scope(parsed['scope'], at, config)
        value = constraint_value(config,entity,[parsed['action']],scope.pop('scope'),text,input_source,at,**scope)
        return 'memory_constraints', value['id'], value
    if kind == 'preference':
        value = number(parsed['value'])
        if (value is None or parsed['unit'] != '°C' or parsed['activity'] not in ACTIVITIES or
                not (spec.get('category') == 'hvac' or entity.startswith('climate.'))):
            raise ValueError('A comfort preference needs an HVAC entity, exact Celsius value and supported activity')
        body = {'id':identity([entity,'temperature',parsed['activity']]),'entity_id':entity,
                'activity':parsed['activity'],'value':value,'unit':'°C','source':'voice' if input_source=='voice' else 'text',
                'reason':text,'created_at':at.isoformat(),'active':True}
        return 'memory_preferences', body['id'], body
    raise ValueError('Unsupported memory change kind')


def _read(store, table, key):
    if table == 'annotations':
        row = store.db.execute('SELECT source,reason FROM annotations WHERE event_key=?',(key,)).fetchone()
        return {'source':row[0],'reason':row[1]} if row else None
    row = store.db.execute(f'SELECT body FROM {table} WHERE id=?',(key,)).fetchone()
    return json.loads(row[0]) if row else None


def _write(store, table, key, value, at, reason):
    if table == 'annotations':
        if value is None:
            source = store.db.execute('SELECT source FROM events WHERE event_key=?',(key,)).fetchone()[0]
            store._annotate(key,source,reason,at.isoformat())
            store.db.execute('DELETE FROM annotations WHERE event_key=?',(key,))
        else:
            store._annotate(key,value['source'],value['reason'],at.isoformat())
    elif value is None:
        store.db.execute(f'DELETE FROM {table} WHERE id=?',(key,))
    else:
        put(store,table,value)


def _rebuild(store,config,at,entity):
    """Rebuild understanding without running raw-data retention or operational feedback."""
    from .memory import ingest_contexts, mine, opportunities, evaluate, reconcile_attribution, learn
    from .understanding import world_models
    from .memory_store import meta, set_meta
    from .statistics import beta_tail

    previous = meta(store,'consolidated_at')
    if previous and at < dt(previous):
        raise ValueError('A memory correction cannot move the learning clock backwards')
    ingest_contexts(store,config,at)
    mine(store,config,at)
    opportunities(store,config,at)
    evaluate(store,at)
    reconcile_attribution(store,at)
    learn(store,config,at)
    # Withdraw autonomy if its supporting scored evidence was corrected away.
    for habit in objects(store,'memory_habits'):
        if habit['entity_id'] != entity:
            continue
        if habit['trust_level'] >= 3 and beta_tail(habit['accept_alpha'],habit['accept_beta'],.9) <= .8:
            habit['trust_level'] = 2
        if habit['trust_level'] >= 2 and beta_tail(habit['shadow_alpha'],habit['shadow_beta'],.85) <= .8:
            habit['trust_level'] = 1
        put(store,'memory_habits',habit)
    world_models(store,config,at)
    set_meta(store,'consolidated_at',at.isoformat())


def request(store, identifier):
    row = store.db.execute('SELECT created_at,status,body FROM correction_requests WHERE id=?',(identifier,)).fetchone()
    if not row:
        raise ValueError('Unknown memory change request')
    return {'id':identifier,'created_at':row[0],'status':row[1],**json.loads(row[2])}


def _save(store, value, status):
    body = {k:v for k,v in value.items() if k not in {'id','created_at','status'}}
    store.db.execute('UPDATE correction_requests SET status=?,body=? WHERE id=?',
                     (status,canonical(body),value['id']))


def preview(store,config,parsed,text,at,*,input_source='app'):
    if input_source not in {'app','voice'}:
        raise ValueError('Memory change source must be app or voice')
    if not isinstance(text,str) or not 1 <= len(text.strip()) <= 500:
        raise ValueError('Enter a statement of 1–500 characters')
    if parsed.get('status') != 'interpreted':
        return parsed
    if parsed.get('kind') in {'explain','memory_changes'}:
        return {**parsed,'status':'read_only'}
    table,key,after = _target(store,config,parsed,text,at,input_source)
    value = {'id':uuid4().hex,'created_at':at.isoformat(),'status':'pending','text':text,
             'input_source':input_source,'interpretation':parsed,'table':table,'key':key,
             'before':_read(store,table,key),'preview':after}
    with _transaction(store):
        store.db.execute('INSERT INTO correction_requests VALUES (?,?,?,?)',
                         (value['id'],value['created_at'],'pending',canonical({k:v for k,v in value.items()
                             if k not in {'id','created_at','status'}})))
    return value


def apply(store,config,identifier,at):
    with _transaction(store):
        value = request(store,identifier)
        if value['status'] == 'applied':
            return value
        if value['status'] != 'pending':
            raise ValueError('Only a pending memory change can be applied')
        if at < dt(value['created_at']):
            raise ValueError('Apply cannot precede the preview time')
        table,key,after = _target(store,config,value['interpretation'],value['text'],at,
                                value.get('input_source','app'))
        if value['interpretation']['kind'] == 'constraint' and after['scope'] == 'temporary':
            if after['expires_at'] != value['preview']['expires_at']:
                raise ValueError('The temporary preview expired; interpret the statement again')
        else:
            if _read(store,table,key) != value['before']:
                raise ValueError('Memory changed after this preview; interpret the statement again')
        before = _read(store,table,key)
        _write(store,table,key,after,at,value['text'])
        if table in {'annotations','memory_exclusions'}:
            event = _event(store,key,at)
            _rebuild(store,config,at,event['entity_id'])
        value.update(table=table,key=key,before=before,applied_after=after,applied_at=at.isoformat())
        _save(store,value,'applied')
        change(store,at.isoformat(),'memory_change_applied',
               {'request_id':identifier,'kind':value['interpretation']['kind'],'before':before,'after':after})
    return request(store,identifier)


def cancel(store,identifier,at):
    with _transaction(store):
        value = request(store,identifier)
        if value['status'] == 'cancelled':
            return value
        if value['status'] != 'pending':
            raise ValueError('Only a pending memory change can be cancelled')
        value['cancelled_at'] = at.isoformat()
        _save(store,value,'cancelled')
    return request(store,identifier)


def undo(store,config,identifier,at):
    with _transaction(store):
        value = request(store,identifier)
        if value['status'] == 'undone':
            return value
        if value['status'] != 'applied':
            raise ValueError('Only an applied memory change can be undone')
        if at < dt(value['applied_at']):
            raise ValueError('Undo cannot precede Apply')
        table,key = value['table'],value['key']
        if _read(store,table,key) != value['applied_after']:
            raise ValueError('A newer change affects this memory; undo that change first')
        _write(store,table,key,value['before'],at,'Undo memory correction '+identifier)
        if table in {'annotations','memory_exclusions'}:
            event = _event(store,key,at)
            _rebuild(store,config,at,event['entity_id'])
        value['undone_at'] = at.isoformat()
        _save(store,value,'undone')
        change(store,at.isoformat(),'memory_change_undone',{'request_id':identifier,'restored':value['before']})
    return request(store,identifier)


def history(store):
    identifiers = store.db.execute('SELECT id FROM correction_requests ORDER BY rowid DESC LIMIT 50').fetchall()
    return [request(store,row[0]) for row in identifiers]
