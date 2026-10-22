"""Bounded cloud input and persistent spending reservations, separate from memory.

Prices are USD nano-dollars per token, using the published peak rates. A cache
hit is a measured saving, never an assumption made when admitting a request.
"""
import json
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from uuid import uuid4
from zoneinfo import ZoneInfo

from .contexts import dt
from .memory_store import canonical, identity

SCHEMA = '''
CREATE TABLE IF NOT EXISTS cloud_calls(
 id TEXT PRIMARY KEY,site TEXT NOT NULL,created_at TEXT NOT NULL,local_day TEXT NOT NULL,
 local_month TEXT NOT NULL,fingerprint TEXT NOT NULL,status TEXT NOT NULL,
 reserved_nusd INTEGER NOT NULL,charged_nusd INTEGER NOT NULL,body TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS cloud_daily_call ON cloud_calls(site,local_day);
'''
PRICES = dict(model='deepseek-flash',verified_on='2026-10-22',
              input_miss=300,input_hit=6,output=1200,
              source='https://api-docs.deepseek.com/quick_start/pricing/')


class CloudSkipped(RuntimeError):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def limits(config):
    value = config.get('reflection',{})
    try:
        amount = Decimal(str(value.get('monthly_budget_usd',1)))
        if not amount.is_finite() or amount < 0:
            raise ValueError
        budget = int(amount * 1_000_000_000)
    except (InvalidOperation,ValueError,OverflowError):
        raise ValueError('reflection.monthly_budget_usd must be a finite nonnegative amount') from None
    result = dict(monthly_budget_nusd=budget,
                  max_input_tokens=value.get('max_input_tokens',8000),
                  max_output_tokens=value.get('max_output_tokens',2000))
    for name,maximum in [('max_input_tokens',32000),('max_output_tokens',4000)]:
        if type(result[name]) is not int or not 256 <= result[name] <= maximum:
            raise ValueError('reflection.'+name+' is outside its supported range')
    return result


def input_bound(messages):
    """UTF-8 byte upper estimate plus a generous chat-framing allowance.

    This is a conservative admission estimate, not the provider tokenizer's
    measured token count. Actual counts replace it after the response.
    """
    return sum(len(message['content'].encode('utf-8'))+64 for message in messages)+256


def bounded_summary(summary,system,config):
    """Select relevant aggregates; never truncate JSON or an evidence record."""
    cap = limits(config)['max_input_tokens']
    result = {key:deepcopy(value) for key,value in summary.items()
              if key not in {'cells','habits','feedback'}}
    result.update(cells=[],habits=[],feedback=[],selection={
        'available':{key:len(summary[key]) for key in ('cells','habits','feedback')},
        'method':'selected aggregates; omitted rows are not evidence of absence'})
    def fits():
        return input_bound([dict(role='system',content=system),
                            dict(role='user',content=canonical(result))]) <= cap
    if not fits():
        raise CloudSkipped('context_limit')
    # Corrections and failures are useful learning signals; raw history stays local.
    ordered = dict(
        feedback=sorted(summary['feedback'],key=lambda x:(-x['count'],x['id']))[:10],
        cells=sorted(summary['cells'],key=lambda x:(-x['human_actions'],x['id']))[:24],
        habits=sorted(summary['habits'],key=lambda x:(-x['failures'],-x['successes'],x['id']))[:8])
    for key,items in ordered.items():
        for item in items:
            result[key].append(deepcopy(item))
            if not fits():
                result[key].pop()
    return result


def reserve(store,config,at,model,messages,max_output_tokens):
    if model != PRICES['model']:
        raise CloudSkipped('unpriced_model')
    bound = input_bound(messages)
    settings = limits(config)
    if bound > settings['max_input_tokens']:
        raise CloudSkipped('context_limit')
    local = dt(at.isoformat()).astimezone(ZoneInfo(config.get('timezone','Asia/Shanghai')))
    day,month = local.date().isoformat(),local.strftime('%Y-%m')
    fingerprint = identity(dict(model=model,messages=messages,max_output_tokens=max_output_tokens,thinking=False))
    worst = bound*PRICES['input_miss']+max_output_tokens*PRICES['output']
    if store.db.in_transaction:
        raise ValueError('Commit observations before reserving a cloud request')
    store.db.execute('BEGIN IMMEDIATE')
    try:
        if store.db.execute("SELECT 1 FROM cloud_calls WHERE site=? AND fingerprint=? AND status='settled'",
                            (config['site'],fingerprint)).fetchone():
            raise CloudSkipped('unchanged_evidence')
        if store.db.execute('SELECT 1 FROM cloud_calls WHERE site=? AND local_day=?',
                            (config['site'],day)).fetchone():
            raise CloudSkipped('daily_call_limit')
        used = store.db.execute('SELECT COALESCE(SUM(charged_nusd),0) FROM cloud_calls WHERE local_month=?',
                                (month,)).fetchone()[0]
        # The cap is shared by every household using this database.
        if used+worst > settings['monthly_budget_nusd']:
            raise CloudSkipped('monthly_budget')
        identifier = uuid4().hex
        details = dict(model=model,prices=PRICES,input_token_upper_estimate=bound,
                       max_output_tokens=max_output_tokens,usage=None)
        store.db.execute('INSERT INTO cloud_calls VALUES (?,?,?,?,?,?,?,?,?,?)',
            (identifier,config['site'],at.isoformat(),day,month,fingerprint,'reserved',worst,worst,canonical(details)))
        store.db.commit()
        return identifier
    except Exception:
        store.db.rollback()
        raise


def usage_cost(usage):
    if not isinstance(usage,dict):
        return None
    prompt,output = usage.get('prompt_tokens'),usage.get('completion_tokens')
    if any(type(value) is not int or value<0 for value in (prompt,output)):
        return None
    hit,miss = usage.get('prompt_cache_hit_tokens'),usage.get('prompt_cache_miss_tokens')
    reported = (type(hit) is int and type(miss) is int and hit>=0 and miss>=0 and hit+miss==prompt)
    if not reported:
        hit,miss = 0,prompt
    cost = hit*PRICES['input_hit']+miss*PRICES['input_miss']+output*PRICES['output']
    return dict(charged_nusd=cost,prompt_tokens=prompt,completion_tokens=output,
                prompt_cache_hit_tokens=hit,prompt_cache_miss_tokens=miss,
                cache_usage_reported=reported)


def settle(store,identifier,provenance):
    row = store.db.execute('SELECT body,reserved_nusd,status FROM cloud_calls WHERE id=?',(identifier,)).fetchone()
    if row is None or row[2]!='reserved':
        raise ValueError('Cloud reservation is missing or already settled')
    details = json.loads(row[0])
    measured = usage_cost(provenance.get('usage')) if provenance else None
    details['usage'] = measured
    details['response_id'] = provenance.get('response_id') if provenance else None
    charged = measured['charged_nusd'] if measured else row[1]
    status = 'settled' if measured else 'unknown'
    with store.db:
        store.db.execute('UPDATE cloud_calls SET status=?,charged_nusd=?,body=? WHERE id=? AND status=\'reserved\'',
                         (status,charged,canonical(details),identifier))
    return dict(call_id=identifier,status=status,estimated_usd=charged/1_000_000_000,
                reserved_usd=row[1]/1_000_000_000,
                cache_hit_tokens=measured['prompt_cache_hit_tokens'] if measured else None,
                cache_usage_reported=measured['cache_usage_reported'] if measured else False)


def report(store,config,at):
    month = dt(at.isoformat()).astimezone(ZoneInfo(config.get('timezone','Asia/Shanghai'))).strftime('%Y-%m')
    rows = store.db.execute('SELECT status,charged_nusd,body FROM cloud_calls WHERE local_month=?',(month,)).fetchall()
    usages = [json.loads(row[2]).get('usage') for row in rows]
    measured = [usage for usage in usages if usage]
    reported = [u for u in measured if u['cache_usage_reported']]
    hits = sum(u['prompt_cache_hit_tokens'] for u in reported)
    reported_inputs = sum(u['prompt_tokens'] for u in reported)
    inputs = sum(u['prompt_tokens'] for u in measured)
    return dict(month=month,currency='USD',calls=len(rows),
                estimated_spend_including_reservations=sum(row[1] for row in rows)/1_000_000_000,
                monthly_budget_usd=limits(config)['monthly_budget_nusd']/1_000_000_000,
                unsettled_calls=sum(row[0]!='settled' for row in rows),
                prompt_tokens=inputs,completion_tokens=sum(u['completion_tokens'] for u in measured),
                cache_hit_tokens=hits,cache_reported_prompt_tokens=reported_inputs,
                measured_cache_hit_ratio=hits/reported_inputs if reported_inputs else None,
                rates=PRICES)
