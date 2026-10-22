"""Explicit comfort statements; no empirical observations or earned trust added."""
from .constraint_scopes import applies
from .contexts import dt
from .memory_store import objects

ACTIVITIES = frozenset({'working', 'sleeping'})


def effective_preferences(store, activity, at):
    if activity is not None and activity not in ACTIVITIES:
        raise ValueError('Use working, sleeping or no explicit activity')
    return [p for p in objects(store,'memory_preferences')
            if p.get('active',True) and p['activity']==activity and dt(p['created_at'])<=at]


def apply_to_shadow(results,store,config,at,activity):
    """Show the user-stated target beside its baseline; it earns no proposal grant."""
    for preference in effective_preferences(store,activity,at):
        matching=[r for r in results if r.get('entity_id')==preference['entity_id'] and
                  r.get('property')=='temperature']
        baseline=[{'habit_id':r['habit_id'],'target':r['target'],'probability':r['probability']} for r in matching]
        results[:]=[r for r in results if r not in matching]
        denied=any(applies(c,preference['entity_id'],'set_temperature',at,config)
                   for c in objects(store,'memory_constraints'))
        results.append({
            'habit_id':None,'entity_id':preference['entity_id'],'property':'temperature',
            'target':preference['value'],'unit':preference['unit'],'probability':None,
            'status':'shadow','reason':'constraint' if denied else 'explicit_preference',
            'proposal':None,'learned_baseline':baseline,'preference':preference,
            'explanation':f"User-stated {activity} preference: {preference['value']} {preference['unit']}. "
                          'Observed behaviour and earned trust remain separate.',
        })
    return results
