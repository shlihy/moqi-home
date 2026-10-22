"""Background DeepSeek hypotheses. Never called from the online decision path."""
import json
import os
from collections import Counter
from datetime import timedelta
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .contexts import dt
from .memory import human_actions, event_context, ingest_contexts
from .memory_store import canonical, change, identity, objects, put
from .understanding import proposal_value
from . import jev_policy
from .jev_questions import QUESTIONS
from . import llm_cost


class DeepSeekError(RuntimeError):
    """A failed external request, without response bodies or credentials in logs."""
    def __init__(self,message,*,provenance=None):
        super().__init__(message)
        self.provenance = provenance


SYSTEM = """你是 Moqi 的后台习惯分析器。只分析给出的去标识化统计，不控制设备。
提出最多三项可用后续行为验证的 context_split 假设；没有依据就返回空 hypotheses。
feature 只能是 weekday、outdoor、slot，value 必须是统计中实际出现的整数值。
每项必须引用支持该 feature/value 的 evidence_ids，不捏造证据，不提高信任。
不修改安全、设备权限、语音静默、用户约束或任何代码。
可以根据习惯统计和语音纠正统计，建议改进一个既有 JEV 问题的措辞。
jev_policy 为 null 表示没有这种建议；有建议时必须引用现有证据，保持问题名及全部答案 ID 不变。
question_updates 只含 instructions 和完整 criteria 描述；habit_ids 只能引用提供的习惯 ID。
问题说明和答案描述要简短；本地会核对问题头 256 token、每项答案 48 token 的实际分词限制，不能靠截断生效。
parent_id 必须是 current_policy.id；所有建议都会先验证，不会直接上线。
输出 json 对象，且只能使用如下字段：
{"schema_version":1,"hypotheses":[{"kind":"context_split","feature":"weekday",
"value":1,"rationale":"与其他工作日比较的待验证假设","evidence_ids":["示例ID"]}],"jev_policy":null}
jev_policy 非 null 时格式为：{"schema_version":1,"parent_id":"当前版本ID",
"question_updates":{"问题名":{"instructions":"新说明","criteria":{"既有答案ID":"说明"}}},
"habit_ids":[],"evidence_ids":["示例ID"],"rationale":"为什么值得验证"}
""" + '\n固定的 JEV 问题及答案域（当前措辞变化在用户消息中）：\n' + canonical(QUESTIONS)


def _evidence_snapshot(store, config, at):
    """Only past, eligible human behavior; no names, exact times or raw payloads."""
    # A standalone reflection command can run before the nightly consolidation.
    # Reuse its event-time context builder, without fitting habits or feedback.
    ingest_contexts(store,config,at)
    counts = Counter()
    for row in human_actions(store, at):
        if dt(row['occurred_at']) < at - timedelta(days=7):
            continue
        context = event_context(store, row)['features']
        for feature in ('weekday', 'outdoor', 'slot'):
            value = context.get(feature)
            if type(value) is int:
                # The entity/room mapping stays local; only opaque group IDs leave.
                group = identity([row['entity_id'], context['room']])
                counts[(group, feature, value)] += 1
    cells = [dict(id=identity([group,feature,value]), group=group,
                  feature=feature,value=value,human_actions=count)
             for (group,feature,value),count in sorted(counts.items())]
    feedback = []
    for version,signal,count in store.db.execute(
            'SELECT policy_id,signal,COUNT(*) FROM jev_policy_feedback WHERE at>=? AND at<? GROUP BY policy_id,signal',
            ((at-timedelta(days=7)).isoformat(),at.isoformat())):
        feedback.append(dict(id=identity(['jev_feedback',version,signal]),policy_id=version,signal=signal,count=count))
    current = jev_policy.active(store)
    habits = []
    for h in objects(store,'memory_habits'):
        if h['status']=='active' and dt(h['created_at'])<at:
            context = {key:value for key,value in h['context'].items() if key!='room' and key!='devices'}
            habits.append(dict(id=h['id'],group=identity([h['entity_id'],h['context']['room']]),
                property=h['property'],target=h['target'],context=context,
                successes=h['successes'],failures=h['failures'],trust_level=h['trust_level']))
    return dict(schema='moqi.reflection-evidence.v1', window_days=7,
                interpretation='association hypotheses only, not learned truth',cells=cells,feedback=feedback,
                habits=habits,current_policy=dict(id=current['id'],question_updates=current['question_updates']))


def evidence_summary(store,config,at):
    """One consistent snapshot while the event collector may be writing."""
    if store.db.in_transaction:
        raise ValueError('Commit observations before building a reflection snapshot')
    store.db.execute('BEGIN IMMEDIATE')
    try:
        result = _evidence_snapshot(store,config,at)
        store.db.commit()
        return result
    except Exception:
        store.db.rollback()
        raise


class DeepSeek:
    def __init__(self, *, api_key=None, model='deepseek-flash', timeout=30, opener=None):
        self.api_key = api_key if api_key is not None else os.environ.get('DEEPSEEK_API_KEY')
        self.model = model
        self.timeout = timeout
        self.opener = opener or urlopen

    @staticmethod
    def messages(summary):
        # Keep static instructions first. Never append growing chat histories.
        return [dict(role='system',content=SYSTEM),dict(role='user',content=canonical(summary))]

    def hypotheses(self,summary,*,store,config,at):
        """The application entry point always reserves its worst-case spend."""
        if not isinstance(self.api_key,str) or not self.api_key.strip():
            raise DeepSeekError('DeepSeek is not configured: set DEEPSEEK_API_KEY')
        output = llm_cost.limits(config)['max_output_tokens']
        identifier = llm_cost.reserve(store,config,at,self.model,self.messages(summary),output)
        try:
            result,provenance = self._request(summary,max_output_tokens=output)
        except DeepSeekError as exc:
            llm_cost.settle(store,identifier,exc.provenance)
            raise
        provenance['billing'] = llm_cost.settle(store,identifier,provenance)
        return result,provenance

    def _request(self, summary, *, max_output_tokens=2000):
        """HTTP transport, exercised with captured responses by protocol tests."""
        if not isinstance(self.api_key, str) or not self.api_key.strip():
            raise DeepSeekError('DeepSeek is not configured: set DEEPSEEK_API_KEY')
        payload = dict(model=self.model, stream=False, max_tokens=max_output_tokens,
                       thinking={'type':'disabled'},
                       response_format={'type':'json_object'},
                       messages=self.messages(summary))
        request = Request('https://api.deepseek.com/chat/completions',
                          data=canonical(payload).encode('utf-8'),
                          headers={'Content-Type':'application/json',
                                   'Authorization':'Bearer '+self.api_key},method='POST')
        try:
            with self.opener(request,timeout=self.timeout) as response:
                raw = response.read(1_048_577)
        except HTTPError as exc:
            raise DeepSeekError('DeepSeek HTTP '+str(exc.code)) from None
        except (URLError,TimeoutError,OSError):
            raise DeepSeekError('DeepSeek network request failed') from None
        if len(raw) > 1_048_576:
            raise DeepSeekError('DeepSeek response exceeds the 1 MiB limit')
        provenance = None
        try:
            body = json.loads(raw)
            provenance = dict(provider='deepseek',model=body.get('model',self.model),
                              response_id=body.get('id'),usage=body.get('usage'))
            choice = body['choices'][0]
            if choice['finish_reason'] != 'stop':
                raise DeepSeekError('DeepSeek response was not completed',provenance=provenance)
            result = json.loads(choice['message']['content'])
            if not isinstance(result,dict):
                raise DeepSeekError('DeepSeek did not return a JSON object',provenance=provenance)
        except (ValueError,KeyError,TypeError,IndexError,AttributeError):
            raise DeepSeekError('DeepSeek returned invalid or empty JSON',provenance=provenance) from None
        return result,provenance


def reflect(store, config, at, client):
    """Validate the entire batch, then atomically enqueue shadow-only hypotheses.

    The external call runs outside a DB transaction. Existing hypotheses retain
    their original start time and accumulated evidence when proposed again.
    """
    if at.tzinfo is None:
        raise ValueError('Reflection needs a timezone-aware cutoff')
    if store.db.in_transaction:
        raise ValueError('Commit observations before calling DeepSeek')
    summary = evidence_summary(store,config,at)
    if not summary['cells'] and not summary['feedback']:
        return dict(status='no_evidence',created=[],reused=[],cloud_called=False)
    if isinstance(client,DeepSeek):
        try:
            summary = llm_cost.bounded_summary(summary,SYSTEM,config)
        except llm_cost.CloudSkipped as exc:
            return dict(status=exc.reason,cloud_called=False)
        if not summary['cells'] and not summary['feedback']:
            return dict(status='context_limit',cloud_called=False)
    with store.db:
        store.db.execute('INSERT OR IGNORE INTO reflection_summaries VALUES (?,?,?)',
                         (identity(summary),at.isoformat(),canonical(summary)))
    try:
        if isinstance(client,DeepSeek):
            response,provenance = client.hypotheses(summary,store=store,config=config,at=at)
        else:
            response,provenance = client.hypotheses(summary)
    except llm_cost.CloudSkipped as exc:
        return dict(status=exc.reason,cloud_called=False)
    if (not isinstance(response,dict) or not {'schema_version','hypotheses'}<=set(response) or
            set(response)-{'schema_version','hypotheses','jev_policy'} or
            type(response['schema_version']) is not int or response['schema_version'] != 1 or
            not isinstance(response['hypotheses'],list) or len(response['hypotheses']) > 3):
        raise ValueError('Invalid reflection response contract')
    references = {cell['id']:cell for cell in summary['cells']}
    policy = None
    if response.get('jev_policy') is not None:
        policy = jev_policy.validate_candidate(store,response['jev_policy'],
            set(references)|{cell['id'] for cell in summary['feedback']},at)
    candidates = []
    for value in response['hypotheses']:
        if not isinstance(value,dict) or set(value) != {'kind','feature','value','rationale','evidence_ids'}:
            raise ValueError('Unknown hypothesis fields')
        cited = value['evidence_ids']
        if (not isinstance(cited,list) or not cited or
                any(not isinstance(key,str) or key not in references for key in cited)):
            raise ValueError('Hypothesis cites missing evidence')
        p = proposal_value({k:v for k,v in value.items() if k != 'evidence_ids'},at)
        if any(references[key]['feature'] != p['feature'] or references[key]['value'] != p['value'] for key in cited):
            raise ValueError('Cited evidence does not support the proposed split')
        p.update(source='deepseek',evidence_ids=sorted(set(cited)),
                 summary_id=identity(summary),provenance=provenance)
        candidates.append(p)
    created,reused = [],[]
    with store.db:
        existing = {(p['kind'],p['feature'],p['value']):p for p in objects(store,'memory_proposals')}
        for p in candidates:
            key = (p['kind'],p['feature'],p['value'])
            if key in existing:
                reused.append(existing[key]['id'])
                continue
            put(store,'memory_proposals',p)
            existing[key] = p
            created.append(p['id'])
        change(store,at.isoformat(),'deepseek_reflection',dict(
            created=created,reused=reused,summary_id=identity(summary),provenance=provenance))
        if policy:
            jev_policy.insert_candidate(store,policy,provenance)
    return dict(status='shadow_only',created=created,reused=reused,cloud_called=True,
                policy_candidate=policy['id'] if policy else None)
