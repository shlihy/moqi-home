"""Automatic evidence-gated JEV releases; no cloud client or device capability.

Labelled manifests come from independent review or a verified adapter. A model's
own answer, abstention or a fabricated synthetic score is never a label source.
"""
import argparse
from contextlib import closing
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path

from . import jev_policy
from .contexts import dt
from .core import Store
from .jev_runtime import model_files
from .memory_store import canonical,change,identity


def read_cases(path,at):
    path=Path(path).resolve()
    manifest=json.loads(path.read_text(encoding='utf-8-sig'))
    if (type(manifest.get('schema_version')) is not int or manifest['schema_version']!=1 or
            manifest.get('labels_from') not in {'human_review','verified_adapter'} or
            not isinstance(manifest.get('dataset_id'),str) or not manifest['dataset_id']):
        raise ValueError('Policy data needs version, dataset identity and independent label provenance')
    frozen=dt(manifest['frozen_at'])
    if frozen>at:
        raise ValueError('A dataset cannot be frozen in the future')
    source=(path.parent/manifest['cases_file']).resolve()
    if not source.is_relative_to(path.parent) or source.stat().st_size>8_388_608:
        raise ValueError('Unsafe or overlarge policy case file')
    raw=source.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=manifest['sha256']:
        raise ValueError('Frozen policy case hash changed')
    cases=[json.loads(line) for line in raw.decode('utf-8-sig').splitlines() if line.strip()]
    if len(cases)>4000: raise ValueError('Too many policy cases')
    for case in cases:
        if (not isinstance(case,dict) or not isinstance(case.get('id'),str) or not case['id'] or
                not isinstance(case.get('text'),str) or not 1<=len(case['text'])<=500 or dt(case['at'])>frozen):
            raise ValueError('Case identity, text or time contradicts the frozen dataset')
        jev_policy.validate_expected(case['expected'])
    return manifest,cases


def _enough(cases,phase):
    supported=sum(case['expected'].get('status')!='needs_clarification' for case in cases)
    return (len(cases)>= (200 if phase=='validation' else 50) and
            supported>= (100 if phase=='validation' else 20) and
            len(cases)-supported>= (20 if phase=='validation' else 5))


def run_due(store,config,at,*,judge=None):
    at=dt(at.isoformat())
    settings=config.get('jev_policy',{})
    if not settings.get('enabled',False): return dict(status='disabled')
    current=jev_policy.active(store)['id']
    pending=store.db.execute("SELECT id,status FROM jev_policies WHERE parent_id=? AND status IN ('proposed','shadow') ORDER BY created_at,id",
                             (current,)).fetchall()
    if not pending: return dict(status='no_candidate')
    interpreter=None
    for identifier,status in pending:
        policy=jev_policy.get(store,identifier)
        phase='validation' if status=='proposed' else 'shadow'
        path=settings.get(phase+'_manifest')
        if not path or not Path(path).is_file():
            return dict(status='awaiting_independent_'+phase+'_data',policy_id=identifier)
        try:
            manifest,cases=read_cases(path,at)
        except (OSError,ValueError,KeyError,TypeError) as exc:
            return dict(status='invalid_policy_data',policy_id=identifier,error=str(exc))
        if phase=='validation':
            if dt(manifest['frozen_at'])>dt(policy['created_at']):
                return dict(status='invalid_validation_cutoff',policy_id=identifier)
        else:
            previous=jev_policy._latest_evaluation(store,identifier,'validation')
            if not previous or not previous['passed']:
                raise ValueError('A shadow candidate has no passing validation')
            cases=[case for case in cases if dt(case['at'])>dt(previous['at']) and case['id'] not in previous['case_ids']]
        if not _enough(cases,phase):
            return dict(status='awaiting_enough_'+phase+'_data',policy_id=identifier,cases=len(cases))
        if judge is None:
            directory=settings.get('model_directory')
            if not directory: return dict(status='model_not_configured')
            try: model_files(directory)
            except (RuntimeError,ValueError): return dict(status='model_artifact_not_ready')
            from .interpretation import MemoryInterpreter
            interpreter=MemoryInterpreter(config,model_path=directory)
            def evaluate_case(version,case):
                interpreter.policy=version
                result=interpreter.interpret(case['text'],selected_event=case.get('selected_event'))
                if result['status'] in {'failed','unavailable'}:
                    raise RuntimeError('JEV evaluation did not complete; this is not a measured quality result')
                return result
            active_judge=evaluate_case
        else:
            active_judge=judge
        job=identity(['jev_evaluation',identifier,phase,identity(cases)])
        if store.db.in_transaction:
            raise ValueError('Commit before claiming policy evaluation')
        with store.db:
            claimed=store.db.execute('INSERT OR IGNORE INTO background_jobs VALUES (?,?,?,?)',
                (job,at.isoformat(),'running',canonical(dict(policy_id=identifier,phase=phase,dataset_id=manifest['dataset_id'])))).rowcount
        if not claimed: continue
        try:
            report=jev_policy.evaluate(store,identifier,cases,active_judge,at,phase=phase)
            if not report['passed']:
                with store.db:
                    store.db.execute("UPDATE jev_policies SET status='rejected' WHERE id=? AND status IN ('proposed','shadow')",(identifier,))
                    change(store,at.isoformat(),'jev_policy_rejected',dict(id=identifier,phase=phase,report=report['id']))
                outcome='rejected'
            elif phase=='shadow':
                jev_policy.activate(store,identifier,at)
                outcome='activated'
            else:
                outcome='awaiting_prospective_shadow'
            result=dict(status=outcome,policy_id=identifier,phase=phase,report_id=report['id'],dataset_id=manifest['dataset_id'])
        except (RuntimeError,ValueError) as exc:
            result=dict(status='evaluation_failed',policy_id=identifier,phase=phase,error=str(exc))
        with store.db:
            store.db.execute('UPDATE background_jobs SET status=?,body=? WHERE id=?',(result['status'],canonical(result),job))
        return result
    return dict(status='no_new_evaluation_data')


def status(store):
    return dict(active=jev_policy.active(store)['id'],versions=[dict(id=key,created_at=at,parent_id=parent,status=state)
        for key,at,parent,state in store.db.execute('SELECT id,created_at,parent_id,status FROM jev_policies ORDER BY created_at')])


def main():
    parser=argparse.ArgumentParser(description='Target/server policy maintenance, status and developer rollback')
    parser.add_argument('--config',default='config.local.json')
    actions=parser.add_mutually_exclusive_group()
    actions.add_argument('--status',action='store_true')
    actions.add_argument('--rollback-current',action='store_true')
    parser.add_argument('--reason')
    args=parser.parse_args()
    if args.rollback_current and not args.reason: parser.error('--rollback-current needs --reason')
    config=json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
    with closing(Store(config['database'])) as store:
        at=datetime.now(timezone.utc)
        if args.status: result=status(store)
        elif args.rollback_current: result=jev_policy.rollback(store,at,reason=args.reason)
        else: result=run_due(store,config,at)
        print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__': main()
