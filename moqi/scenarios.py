"""Synthetic chronological software acceptance. No model, HA or audio calls.

Observations are declared synthetic adapter evidence. Command receipts in the
undo/temperature cases simulate independently verified device feedback; they
grant no real device permission. Trust and counts are never manually seeded.
"""
import argparse
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from .api import HomeMemory,Observation
from .contexts import dt
from .core import normalize
from .memory import consolidate,register_execution
from .memory_store import objects
from .runtime import Runtime
from .safety import review

START=datetime(2026,1,5,10,tzinfo=timezone.utc)  # 18:00 household time.
CONFIG=dict(site='SYNTHETIC-ACCEPTANCE',timezone='Asia/Shanghai',entities={
    'light.lamp':dict(role='actuator',room='dining',category='light',risk='low'),
    'climate.ac':dict(role='actuator',room='dining',category='hvac',risk='medium',unit='°C')})


def metrics(memory):
    return dict(observations=memory.store.db.execute('SELECT COUNT(*) FROM events').fetchone()[0],
                teaching_inputs=memory.store.db.execute('SELECT COUNT(*) FROM correction_requests').fetchone()[0],
                cloud_calls=memory.store.db.execute('SELECT COUNT(*) FROM cloud_calls').fetchone()[0])


def physical(memory,entity,at,state,before,**kwargs):
    memory.ingest(Observation(entity,at,state,before,source='human',
        evidence='SYNTHETIC independently labelled physical operation',**kwargs))


def echo(memory,entity,at,state,before,context,attributes=None):
    memory.store.add(normalize(dict(event_type='state_changed',time_fired=at.isoformat(),data=dict(
        entity_id=entity,old_state=dict(state=before),new_state=dict(state=state,attributes=attributes or {},
        context={'id':context}))),memory.config))


def formation(memory,days=25):
    runtime=Runtime(memory)
    for day in range(days):
        start=START+timedelta(days=day)
        runtime.observe(Observation('light.lamp',start,'off'))
        runtime.tick(start+timedelta(minutes=1))
        physical(memory,'light.lamp',start+timedelta(minutes=5),'on','off')
        memory.observe_interval(start,start+timedelta(minutes=18))
        runtime.tick(start+timedelta(minutes=19))
    habits=objects(memory.store,'memory_habits')
    hits=memory.store.db.execute("SELECT COUNT(*) FROM forecasts WHERE outcome='hit'").fetchone()[0]
    candidates=[h for h in habits if h['entity_id']=='light.lamp' and h['target']=='on' and
                h['context'].get('day_type')=='weekday']
    chosen=max(candidates,key=lambda h:(h['trust_level'],-h['specificity']))
    return runtime,chosen,dict(habits=len(habits),shadow_hits=hits,earned_trust=chosen['trust_level'],
        checks=dict(habit_formed=bool(habits),shadow_hits_observed=hits>0,autonomy_earned=chosen['trust_level']>=2))


def undo_scenario(root):
    with HomeMemory(CONFIG,str(root/'undo.sqlite3')) as memory:
        runtime,habit,formed=formation(memory)
        trust=[habit['trust_level']]
        leases=[]
        review_reasons=[]
        cursor=START+timedelta(days=25)
        for index in range(2):
            while cursor.astimezone(ZoneInfo('Asia/Shanghai')).weekday()>=5:
                cursor+=timedelta(days=1)
            identifier='SYNTHETIC-undo-'+str(index)
            memory.ingest(Observation('light.lamp',cursor,'off'))
            register_execution(memory.store,identifier,'light.lamp','on','off',cursor+timedelta(seconds=1),habit['id'],context_id=identifier)
            echo(memory,'light.lamp',cursor+timedelta(seconds=2),'on','off',identifier)
            corrected=cursor+timedelta(minutes=2)
            physical(memory,'light.lamp',corrected,'off','on')
            memory.observe_interval(cursor,cursor+timedelta(minutes=16))
            runtime.tick(cursor+timedelta(minutes=17))
            current=next(h for h in objects(memory.store,'memory_habits') if h['id']==habit['id'])
            trust.append(current['trust_level'])
            until=dt(memory.store.db.execute('SELECT until_at FROM human_leases WHERE entity_id=?',('light.lamp',)).fetchone()[0])
            leases.append((until-corrected).total_seconds()/60)
            with memory.store.db:
                memory.store.db.execute('INSERT INTO sessions(started_at,last_seen_at,status) VALUES (?,?,?)',
                    (cursor.isoformat(),(cursor+timedelta(minutes=17)).isoformat(),'connected'))
            proposal=dict(id='SYNTHETIC-retry-'+str(index),entity_id='light.lamp',action='turn_on',value=None,
                habit_id=habit['id'],issued_at=(cursor+timedelta(minutes=17)).isoformat(),
                probability=current['alpha']/(current['alpha']+current['beta']),risk='low',announce=False)
            result=review(memory.store,CONFIG,dict(devices={'light.lamp':dict(actions=['turn_on','turn_off'])}),proposal,
                          cursor+timedelta(minutes=17))
            review_reasons.append(result['reason'])
            cursor+=timedelta(days=1)
        negatives=[e['beta'] for e in objects(memory.store,'memory_evidence') if e.get('signal')=='undo' and e['habit_id']==habit['id']]
        return dict(formation=formed,trust_sequence=trust,undo_beta=negatives,lease_minutes=leases,review_reasons=review_reasons,
            metrics=metrics(memory),checks=dict(**formed['checks'],beta_three_each_time=negatives==[3,3],lease_90_minutes=leases==[90,90],
                        two_undos_return_to_shadow=trust[-1]==1,
                        retry_denied=all(value!='review_passed_execution_disabled' for value in review_reasons)))


def temperature_scenario(root):
    with HomeMemory(CONFIG,str(root/'temperature.sqlite3')) as memory:
        for day in range(4):
            at=START+timedelta(days=day)
            memory.ingest(Observation('climate.ac',at,'heat',attributes={'temperature':21}))
            physical(memory,'climate.ac',at+timedelta(minutes=1),'heat','heat',
                     previous_attributes={'temperature':21},attributes={'temperature':22})
            memory.observe_interval(at,at+timedelta(minutes=16))
        at=START+timedelta(days=4)
        consolidate(memory.store,CONFIG,at)
        habit=next(h for h in objects(memory.store,'memory_habits') if h['entity_id']=='climate.ac' and h['property']=='temperature')
        before=next(m for m in objects(memory.store,'memory_models') if m['id'].startswith('preference:'))
        register_execution(memory.store,'SYNTHETIC-temperature','climate.ac','22','21',at,habit['id'],prop='temperature')
        physical(memory,'climate.ac',at+timedelta(minutes=2),'heat','heat',
                 previous_attributes={'temperature':22},attributes={'temperature':23})
        memory.observe_interval(at,at+timedelta(minutes=16))
        consolidate(memory.store,CONFIG,at+timedelta(minutes=17))
        after=next(m for m in objects(memory.store,'memory_models') if m['id']==before['id'])
        signal=memory.store.db.execute('SELECT feedback FROM execution_ledger WHERE id=?',('SYNTHETIC-temperature',)).fetchone()[0]
        return dict(before_values=before['values'],after_values=after['values'],signal=signal,metrics=metrics(memory),
            receipt_source='synthetic_verified_command_receipt_fixture; no actual climate control',
            energy_target=after['energy_target'],accepted_range=after['accepted_range'],
            checks=dict(distribution_updated=after['samples']==before['samples']+1 and 23 in after['values'],
                        micro_adjustment_is_not_undo=signal=='correction',
                        no_undo_penalty=not any(e.get('signal')=='undo' for e in objects(memory.store,'memory_evidence'))))


def drift_scenario(root):
    with HomeMemory(CONFIG,str(root/'drift.sqlite3')) as memory:
        runtime,_,_=formation(memory)
        original={h['id'] for h in objects(memory.store,'memory_habits')}
        for day in range(25,35):
            at=START+timedelta(days=day)
            runtime.observe(Observation('light.lamp',at,'off'))
            runtime.tick(at+timedelta(minutes=1))
            runtime.tick(at+timedelta(minutes=17))
            runtime.tick(at+timedelta(minutes=34))
            physical(memory,'light.lamp',at+timedelta(minutes=35),'on','off')
            memory.observe_interval(at,at+timedelta(minutes=55))
            runtime.tick(at+timedelta(minutes=56))
        habits=objects(memory.store,'memory_habits')
        dormant=[h['id'] for h in habits if h['id'] in original and h['status']=='drift_dormant']
        shifted=[h for h in habits if h['slot']==37 and h['target']=='on']
        versions=[h['id'] for h in habits if h.get('previous_version') in original]
        return dict(dormant_originals=dormant,new_shifted_habits=len(shifted),split_versions=versions,metrics=metrics(memory),
            checks=dict(old_versions_preserved=original<={h['id'] for h in habits},
                        observed_later_time_formed=bool(shifted),drift_splits_created=bool(dormant and versions)))


def unknown_scenario(root):
    with HomeMemory(CONFIG,str(root/'unknown.sqlite3')) as memory:
        runtime=Runtime(memory)
        for day in range(8):
            at=START+timedelta(days=day)
            runtime.observe(Observation('light.lamp',at,'off'))
            runtime.observe(Observation('light.lamp',at+timedelta(minutes=5),'on','off'))
            memory.observe_interval(at,at+timedelta(minutes=16))
            runtime.tick(at+timedelta(minutes=17))
        return dict(metrics=metrics(memory),checks=dict(unknown_never_taught_a_habit=not objects(memory.store,'memory_habits')))


def run(directory):
    directory=Path(directory).resolve()
    directory.mkdir(parents=True,exist_ok=False)
    cases={}
    for name,function in [('formation_and_undo',undo_scenario),('temperature_micro_adjustment',temperature_scenario),
                           ('schedule_drift',drift_scenario),('unknown_attribution',unknown_scenario)]:
        cases[name]=function(directory)
        print(json.dumps(dict(scenario=name,checks=cases[name]['checks']),ensure_ascii=False),flush=True)
    cloud_calls=sum(value['metrics']['cloud_calls'] for value in cases.values())
    teaching=sum(value['metrics']['teaching_inputs'] for value in cases.values())
    report=dict(schema='moqi.software-replay.v1',synthetic=True,model_inference=False,cloud_calls=cloud_calls,
        live_ha=False,audio_playback=False,teaching_inputs=teaching,web_actions=0,scenarios=cases,
        passed=cloud_calls==0 and teaching==0 and all(all(value['checks'].values()) for value in cases.values()))
    (directory/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',required=True,help='New isolated output directory; never use a household database')
    args=parser.parse_args()
    result=run(args.directory)
    print(json.dumps(dict(passed=result['passed'],synthetic=True,report=str(Path(args.directory)/'report.json'))))
    if not result['passed']: raise SystemExit(1)


if __name__=='__main__': main()
