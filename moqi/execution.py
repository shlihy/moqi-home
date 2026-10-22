"""Reserved dispatch, independent review and observed HA command receipts.

The initial execution scope is a low-risk light. Other learned devices remain
shadow decisions. No model has access to this module's gateway credentials.
"""
import json
import subprocess
import sys
from datetime import timedelta

from .contexts import attributes,dt,latest,number
from .ha_gateway import GatewayError
from .memory_store import canonical,identity,objects
from .speech_policy import abort_prepared


class ProcessReviewer:
    def review(self,memory,policy,proposal,at):
        payload=dict(config=dict(memory.config,database=memory.store.db.execute('PRAGMA database_list').fetchone()[2]),
                     policy=policy,proposal=proposal,at=at.isoformat())
        try:
            result=subprocess.run([sys.executable,'-X','utf8','-m','moqi.safety','--request'],
                input=canonical(payload),text=True,encoding='utf-8',capture_output=True,timeout=5,check=False)
        except subprocess.TimeoutExpired:
            return dict(allowed=False,reason='independent_review_timeout')
        if result.returncode:
            return dict(allowed=False,reason='independent_review_failed')
        try:
            value=json.loads(result.stdout)
            if not isinstance(value,dict) or type(value.get('allowed')) is not bool:
                raise ValueError
        except ValueError:
            return dict(allowed=False,reason='invalid_independent_review')
        return value


class Executor:
    def __init__(self,memory,gateway=None,*,enabled=False,speech=None,reviewer=None,receipt_probe=None):
        self.memory,self.gateway,self.enabled,self.speech=memory,gateway,enabled,speech
        self.reviewer=reviewer or ProcessReviewer()
        self.receipt_probe=receipt_probe

    def _record(self,proposal,at,result):
        habit=next((h for h in objects(self.memory.store,'memory_habits') if h['id']==proposal['habit_id']),None)
        with self.memory.store.db:
            self.memory.store.db.execute('INSERT OR IGNORE INTO execution_decisions VALUES (?,?,?)',
                (identity([proposal['id'],at.isoformat(),result]),at.isoformat(),
                 canonical(dict(proposal=proposal,habit=habit,result=result))))
        return result

    def dispatch(self,proposal,profile,at):
        store,config=self.memory.store,self.memory.config
        def deny(reason):
            return self._record(proposal,at,dict(id=proposal['id'],status='shadow',reason=reason))
        if not self.enabled or self.gateway is None:
            return deny('runtime_shadow_mode')
        if not profile.get('execution_enabled') or proposal['entity_id'] not in profile['control_entities']:
            return deny('ha_execution_not_selected')
        spec=config['entities'].get(proposal['entity_id'],{})
        if spec.get('category')!='light' or spec.get('risk')!='low':
            return deny('initial_execution_scope_low_risk_light')
        if proposal['habit_id'] in {item['habit_id'] for item in self.announcement_holds()}:
            return deny('required_announcement_unresolved')
        verdict=self.reviewer.review(self.memory,profile['policy'],proposal,at)
        if not verdict['allowed']:
            return deny(verdict['reason'])
        # Recompute announcement requirements from earned memory in the reviewer.
        proposal=dict(proposal,announce=verdict['announce'])
        row=latest(store.events(),proposal['entity_id'],at)
        if not row:
            return deny('no_observed_state')
        prop='temperature' if proposal['action']=='set_temperature' else 'state'
        before=attributes(row).get('temperature') if prop=='temperature' else row['new_state']
        target=(str(proposal['value']) if proposal['action'] in {'set_temperature','set_hvac_mode'}
                else 'on' if proposal['action']=='turn_on' else 'off')
        if store.db.in_transaction:
            raise ValueError('Commit before dispatching a decision')
        store.db.execute('BEGIN IMMEDIATE')
        try:
            if store.db.execute('SELECT 1 FROM execution_ledger WHERE id=?',(proposal['id'],)).fetchone():
                store.db.rollback()
                return deny('command_already_reserved')
            if store.db.execute("SELECT 1 FROM execution_ledger WHERE entity_id=? AND status IN ('reserved','outcome_unknown','service_completed')",
                                (proposal['entity_id'],)).fetchone():
                store.db.rollback()
                return deny('previous_command_unresolved')
            store.db.execute('INSERT INTO execution_ledger(id,entity_id,target,before_state,issued_at,habit_id,status,property) VALUES (?,?,?,?,?,?,?,?)',
                (proposal['id'],proposal['entity_id'],target,str(before),proposal['issued_at'],proposal['habit_id'],'reserved',prop))
            store.db.execute('INSERT INTO execution_dispatch VALUES (?,?)',
                (proposal['id'],canonical(dict(proposal=proposal,revision=profile['revision']))))
            store.db.commit()
        except Exception:
            store.db.rollback()
            raise
        if proposal['announce']:
            if self.speech is None:
                self._not_sent(proposal['id'],'not_dispatched_audio_unavailable')
                return deny('announcement_unavailable')
            try:
                prepared=self.speech.prepare_action(proposal,at)
            except Exception:
                self._not_sent(proposal['id'],'not_dispatched_audio_failed')
                raise
            if not prepared['allowed']:
                self._not_sent(proposal['id'],'not_dispatched_'+prepared['reason'])
                return deny(prepared['reason'])
            # Synthesis can take time. Recheck constraints/leases and freshness
            # before the first POST, after reserving the actual audio budget.
            now=self.speech.clock()
            verdict=self.reviewer.review(self.memory,profile['policy'],proposal,now)
            if not verdict['allowed']:
                self._not_sent(proposal['id'],'not_dispatched_'+verdict['reason'])
                abort_prepared(store,'execution-'+proposal['id'])
                return deny(verdict['reason'])
        try:
            receipt=self.gateway.submit(proposal,profile['revision'])
        except GatewayError as exc:
            if exc.http_status in {400,401,403,404,409,422}:
                self._not_sent(proposal['id'],'not_dispatched_ha_'+str(exc.http_status))
                if proposal['announce']: abort_prepared(store,'execution-'+proposal['id'])
            else:
                with store.db:
                    store.db.execute("UPDATE execution_ledger SET status='outcome_unknown' WHERE id=?",(proposal['id'],))
            status=store.db.execute('SELECT status FROM execution_ledger WHERE id=?',(proposal['id'],)).fetchone()[0]
            return self._record(proposal,at,dict(id=proposal['id'],status='pending' if status=='outcome_unknown' else status,
                                               reason='ha_request_failed_query_receipt' if status=='outcome_unknown' else 'ha_rejected_before_dispatch'))
        self._receipt(proposal['id'],receipt)
        return self._record(proposal,at,dict(id=proposal['id'],status=receipt['status'],reason='await_observed_state'))

    def _not_sent(self,identifier,status):
        with self.memory.store.db:
            self.memory.store.db.execute('UPDATE execution_ledger SET status=? WHERE id=?',(status,identifier))

    def _receipt(self,identifier,value):
        store=self.memory.store
        row=store.db.execute('SELECT entity_id,issued_at FROM execution_ledger WHERE id=?',(identifier,)).fetchone()
        status=value.get('status')
        if (not row or value.get('id')!=identifier or value.get('entity_id')!=row[0] or
                value.get('issued_at')!=row[1] or
                status not in {'service_completed','outcome_unknown','not_dispatched_scope_changed','not_dispatched_device_changed',
                               'not_dispatched_permission_changed'} or
                not isinstance(value.get('context_id'),str) or not value['context_id']):
            raise GatewayError('HA command receipt contract is invalid')
        with store.db:
            store.db.execute('UPDATE execution_ledger SET status=?,context_id=? WHERE id=?',
                             (status,value['context_id'],identifier))
        if status.startswith('not_dispatched'):
            prepared=store.db.execute('SELECT id FROM prepared_speech WHERE execution_id=?',(identifier,)).fetchone()
            if prepared: abort_prepared(store,prepared[0])

    def reconcile(self,at):
        store=self.memory.store
        pending=store.db.execute("SELECT id,entity_id,target,issued_at,context_id,property FROM execution_ledger WHERE status IN ('reserved','outcome_unknown','service_completed')").fetchall()
        acknowledged=[]
        for identifier,entity,target,issued,context_id,prop in pending:
            # Recover a persisted receipt after a POST timeout or process restart.
            if self.gateway is not None and not context_id:
                try:
                    value=(self.receipt_probe.poll(identifier,at) if self.receipt_probe is not None
                           else self.gateway.receipt(identifier))
                    if value is None: continue
                    self._receipt(identifier,value)
                except GatewayError:
                    continue
                status,context_id=store.db.execute('SELECT status,context_id FROM execution_ledger WHERE id=?',(identifier,)).fetchone()
                if status.startswith('not_dispatched'): continue
            if not context_id:
                continue
            delay=30 if self.memory.config['entities'].get(entity,{}).get('category')=='hvac' else 5
            matches=[]
            for event in store.events():
                if (event['entity_id']!=entity or event['kind'] not in {'transition','attributes'} or
                        not dt(issued)<=dt(event['occurred_at'])<=min(at,dt(issued)+timedelta(seconds=delay)) or
                        json.loads(event['context_json']).get('id')!=context_id):
                    continue
                reached=(number(attributes(event).get('temperature'))==number(target) if prop=='temperature'
                         else event['new_state']==target)
                if reached: matches.append(event)
            if not matches:
                continue
            with store.db:
                store.db.execute("UPDATE execution_ledger SET status='acknowledged' WHERE id=?",(identifier,))
            acknowledged.append(identifier)
        if self.speech is not None:
            # Includes acknowledgments recovered by a previous process, whose
            # PCM buffer was never claimed or played before the crash.
            ready=store.db.execute("SELECT p.execution_id FROM prepared_speech p JOIN execution_ledger e ON e.id=p.execution_id WHERE p.status='prepared' AND e.status='acknowledged'").fetchall()
            for (identifier,) in ready: self.speech.announce(identifier,at)
        return acknowledged

    def announcement_holds(self):
        rows=self.memory.store.db.execute('''SELECT e.id,e.habit_id,p.status,d.body
            FROM execution_ledger e JOIN execution_dispatch d ON d.id=e.id
            LEFT JOIN prepared_speech p ON p.execution_id=e.id
            WHERE e.status='acknowledged' AND (p.status IS NULL OR p.status!='playback_attempted')''').fetchall()
        return [dict(execution_id=key,habit_id=habit,reason=status or 'missing_announcement_reservation')
                for key,habit,status,body in rows if json.loads(body)['proposal']['announce']]
