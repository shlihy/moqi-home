"""Independent daily reflection process; online decisions never await DeepSeek."""
import argparse
from contextlib import closing
import json
import logging
import os
import time
from datetime import datetime,timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .core import Store
from .contexts import dt
from .memory_store import canonical,identity
from .reflection import DeepSeek,DeepSeekError,reflect

log = logging.getLogger(__name__)


def run_local_due(store,config,at):
    """Nightly L3/thermal work runs in this process, never in the online loop."""
    from .memory import due,reflect as local_reflect
    at=dt(at.isoformat())
    local=at.astimezone(ZoneInfo(config.get('timezone','Asia/Shanghai')))
    if (local.hour,local.minute)<(3,0) or not due(store,config,at):
        return dict(status='not_due')
    job=identity(['local_consolidation',config['site'],local.date().isoformat()])
    if store.db.in_transaction:
        raise ValueError('Commit before claiming local consolidation')
    with store.db:
        inserted=store.db.execute('INSERT OR IGNORE INTO background_jobs VALUES (?,?,?,?)',
            (job,at.isoformat(),'running',canonical(dict(kind='local_consolidation')))).rowcount
    if not inserted:
        return dict(status='already_claimed',job_id=job)
    try:
        value=local_reflect(store,config,at,config.get('memory_directory','data/memory'))
        result=dict(status='complete',revision=value['revision'],habits=len(value['habits']))
    except (RuntimeError,ValueError,OSError) as exc:
        result=dict(status='failed',error=str(exc))
    with store.db:
        store.db.execute('UPDATE background_jobs SET status=?,body=? WHERE id=?',(result['status'],canonical(result),job))
    return dict(result,job_id=job)


def run_due(store,config,at,*,client=None):
    at = dt(at.isoformat())
    settings = config.get('reflection',{})
    if not settings.get('enabled',False):
        return {'status':'disabled'}
    local = at.astimezone(ZoneInfo(config.get('timezone','Asia/Shanghai')))
    if (local.hour,local.minute)<(3,5):
        return {'status':'not_due'}
    if client is None and not os.environ.get('DEEPSEEK_API_KEY'):
        return {'status':'unconfigured'}
    if store.db.in_transaction:
        raise ValueError('Commit observations before claiming a background job')
    job = identity(['deepseek',config['site'],local.date().isoformat()])
    with store.db:
        inserted = store.db.execute('INSERT OR IGNORE INTO background_jobs VALUES (?,?,?,?)',
            (job,at.isoformat(),'running',canonical({'site':config['site'],'local_day':local.date().isoformat()}))).rowcount
    if not inserted:
        previous = store.db.execute('SELECT status FROM background_jobs WHERE id=?',(job,)).fetchone()[0]
        return dict(status='already_claimed',job_id=job,previous_status=previous)
    try:
        result = reflect(store,config,at,client or DeepSeek(model=os.environ.get('DEEPSEEK_MODEL','deepseek-flash')))
    except (DeepSeekError,ValueError) as exc:
        result = dict(status='failed',error=str(exc))
    with store.db:
        store.db.execute('UPDATE background_jobs SET status=?,body=? WHERE id=?',
                         (result['status'],canonical(result),job))
    return dict(result,job_id=job)


def main():
    parser = argparse.ArgumentParser(description='Moqi local consolidation and bounded DeepSeek worker; no device access')
    parser.add_argument('--config',default='config.local.json')
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--wait-for-config',action='store_true',help='Wait for the HA runtime to create its generated configuration')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    path=Path(args.config)
    if args.wait_for_config:
        while not path.is_file():
            log.info('Waiting for HA-selected runtime configuration')
            time.sleep(30)
    config = json.loads(path.read_text(encoding='utf-8-sig'))
    database=Path(config['database']).resolve()
    previous = None
    with closing(Store(config['database'])) as store:
        while True:
            config=json.loads(path.read_text(encoding='utf-8-sig'))
            if Path(config['database']).resolve()!=database:
                raise ValueError('Restart the worker after changing its database')
            at=datetime.now(timezone.utc)
            local=run_local_due(store,config,at)
            cloud=run_due(store,config,at)
            from .policy_worker import run_due as maintain_policy
            policy=maintain_policy(store,config,at)
            result=dict(local=local,reflection=cloud,policy=policy)
            if args.once:
                print(json.dumps(result,ensure_ascii=False))
                return
            marker = (local['status'],cloud['status'],cloud.get('job_id'),cloud.get('previous_status'),policy['status'])
            if marker!=previous:
                log.info('Background worker: local=%s, reflection=%s, policy=%s',local['status'],cloud['status'],policy['status'])
                previous = marker
            time.sleep(30)


if __name__=='__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
