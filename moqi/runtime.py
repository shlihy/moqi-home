"""Integrated local learning and shadow/live decision loop. DeepSeek runs elsewhere.

Runtime consumes verified adapter observations or HA's unknown-attribution
events. It never infers a human operation from an HA user_id.
"""
import argparse
from copy import deepcopy
from datetime import datetime,timezone
import json
import logging
import os
from pathlib import Path
from queue import Empty,Queue
from threading import Event,Thread
import time

from .api import HomeMemory,Observation,validate_config
from .attribution import match_echoes
from .contexts import dt,latest
from .decision import decide
from .execution import Executor
from .ha import collect
from .ha_gateway import GatewayError,HAGateway,ReceiptFeed
from .memory import evaluate,feedback,ingest_contexts,learn,mine,opportunities,reconcile_attribution
from .memory_store import canonical,change,identity,meta,objects,set_meta

log=logging.getLogger(__name__)


class Runtime:
    def __init__(self,memory,*,executor=None):
        self.memory=memory
        self.executor=executor or Executor(memory)
        self.profile=None
        self.profile_at=None
        self._inputs=None
        self._attribution=None

    def observe(self,value):
        """Hardware adapters must provide their own verified attribution evidence."""
        return self.memory.ingest(value if isinstance(value,Observation) else Observation.from_dict(value))

    def apply_profile(self,profile,at):
        config=self.memory.config
        owned=dict(config,entities=deepcopy(profile['entities']),timezone=profile['timezone'],
                   thermal_rooms=deepcopy(profile.get('thermal_rooms',{})))
        if owned['entities']:
            validate_config(owned)
        if not isinstance(profile.get('policy'),dict) or not isinstance(profile.get('execution_enabled'),bool):
            raise ValueError('Invalid HA runtime profile')
        changed=self.profile is None or profile['revision']!=self.profile['revision']
        if changed:
            removed=[entity for entity,spec in config['entities'].items()
                     if owned['entities'].get(entity,{}).get('room')!=spec['room']]
            for entity in removed:
                row=latest(self.memory.store.events(),entity,at)
                # Explicitly terminate observation eligibility on a scope/area
                # change. This is an observation boundary, not a device action.
                self.memory.ingest(Observation(entity,at,None,row['new_state'] if row else None))
            config.update(owned)
            with self.memory.store.db:
                change(self.memory.store,at.isoformat(),'ha_profile_changed',dict(
                    revision=profile['revision'],removed_or_moved=removed,issues=profile.get('issues',[])))
            self._inputs=None
        self.profile=deepcopy(profile)
        self.profile_at=at
        return changed

    def tick(self,at):
        at=dt(at.isoformat())
        store,config=self.memory.store,self.memory.config
        previous=meta(store,'runtime_at')
        if previous and at<dt(previous):
            return dict(status='clock_moved_backwards',decisions=[])
        acknowledged=self.executor.reconcile(at)
        signature=(store.db.execute('SELECT COUNT(*),MAX(occurred_at) FROM events WHERE occurred_at<?',(at.isoformat(),)).fetchone(),
                   store.db.execute('SELECT COALESCE(MAX(id),0) FROM annotation_history').fetchone()[0],
                   store.db.execute('SELECT COUNT(*) FROM forecasts WHERE window_end<=? AND outcome IS NULL',(at.isoformat(),)).fetchone()[0],
                   tuple((key,status,(at-dt(issued)).total_seconds()>=600) for key,status,issued in
                         store.db.execute("SELECT id,status,issued_at FROM execution_ledger WHERE feedback IS NULL")),
                   int(at.timestamp())//900)
        if signature!=self._inputs:
            with store.db:
                match_echoes(store,config,at)
                ingest_contexts(store,config,at)
                # Sensor samples do not repeatedly remine every household habit.
                if self._attribution!=signature[1]:
                    mine(store,config,at)
                opportunities(store,config,at)
                evaluate(store,at)
                feedback(store,config,at)
                reconcile_attribution(store,at)
                learn(store,config,at)
            self._attribution=signature[1]
            self._inputs=signature
        decisions=decide(store,config,at)
        profile=self.profile
        profile_fresh=profile is not None and self.profile_at is not None and 0<=(at-self.profile_at).total_seconds()<=60
        for item in decisions:
            proposal=item['proposal']
            if proposal is None: continue
            entity=proposal['entity_id']
            lease=store.db.execute('SELECT until_at FROM human_leases WHERE entity_id=?',(entity,)).fetchone()
            row=latest(store.events(),entity,at)
            key=identity([proposal['habit_id'],proposal['action'],proposal['value'],proposal['probability'],
                int(at.timestamp())//900,row['occurred_at'] if row else None,
                bool(lease and dt(lease[0])>at),profile['revision'] if profile_fresh else None,
                identity(objects(store,'memory_constraints')),identity(objects(store,'memory_preferences'))])
            slot='runtime_attempt:'+identity([entity,proposal['action']])
            if meta(store,slot)==key:
                item['execution']=dict(status='shadow',reason='unchanged_decision_already_checked')
                continue
            if not profile_fresh:
                item['execution']=dict(status='shadow',reason='ha_profile_unavailable_or_stale')
            else:
                item['execution']=self.executor.dispatch(proposal,profile,at)
            with store.db: set_meta(store,slot,key)
        holds=self.executor.announcement_holds()
        with store.db:
            set_meta(store,'runtime_at',at.isoformat())
            if holds!=meta(store,'runtime_announcement_holds',[]):
                set_meta(store,'runtime_announcement_holds',holds)
                change(store,at.isoformat(),'required_announcement_holds_changed',dict(holds=holds))
                if holds: log.warning('Automatic habits are held pending announcement recovery: %s',len(holds))
        return dict(status='running',decisions=decisions,acknowledged=acknowledged,
                    announcement_holds=holds)


class ProfileFeed:
    """HA metadata I/O does not hold the event loop or a SQLite connection."""
    def __init__(self,gateway):
        self.gateway=gateway
        self.queue=Queue(maxsize=1)
        self.stop=Event()
        self.thread=Thread(target=self._run,name='moqi-ha-profile',daemon=True)

    def _run(self):
        while not self.stop.is_set():
            try: value=dict(profile=self.gateway.profile(),at=datetime.now(timezone.utc))
            except GatewayError: value=dict(error='ha_profile_unavailable')
            try: self.queue.get_nowait()
            except Empty: pass
            self.queue.put(value)
            self.stop.wait(15)

    def start(self): self.thread.start()
    def close(self): self.stop.set(); self.thread.join(timeout=self.gateway.timeout+1)


def save_runtime_config(config,path):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix('.tmp')
    temporary.write_text(canonical(config)+'\n',encoding='utf-8')
    temporary.replace(path)


def main():
    parser=argparse.ArgumentParser(description='Moqi HA-selected local learning runtime')
    parser.add_argument('--config',default='config.local.json')
    parser.add_argument('--execute',action='store_true',help='Permit earned low-risk light actions if HA also enables them')
    parser.add_argument('--audio-config',help='Explicit target-only local microphone/model configuration')
    parser.add_argument('--jev-model',help='Transferred ONNX artifact directory, loaded only if code cannot interpret')
    args=parser.parse_args()
    config=json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
    logging.basicConfig(level=logging.INFO)
    gateway=HAGateway(config['ha_url'],os.environ.get('HA_TOKEN'))
    # The sample entity list is never used as an actual HA selection.
    while True:
        try: initial=gateway.profile()
        except GatewayError:
            log.warning('HA profile is unavailable; waiting before collecting or deciding')
            time.sleep(15)
            continue
        if initial['entities']: break
        log.info('Select observation entities and their areas in HA; none are ready yet')
        time.sleep(15)
    config.update(entities=initial['entities'],timezone=initial['timezone'])
    with HomeMemory(config,config['database']) as memory:
        receipts=ReceiptFeed(gateway)
        executor=Executor(memory,gateway,enabled=args.execute,receipt_probe=receipts)
        runtime=Runtime(memory,executor=executor)
        runtime.apply_profile(initial,datetime.now(timezone.utc))
        audio=None
        if args.audio_config:
            from .audio import create
            from .conversation import VoiceConversation
            from .interpretation import MemoryInterpreter
            from .speech import SpeechBroker
            audio_config,tts,player,microphone=create(args.audio_config)
            if audio_config['room'] not in {s['room'] for s in memory.config['entities'].values()}:
                raise ValueError('The microphone room must be an HA selected area')
            if audio_config['timezone']!=memory.config['timezone']:
                raise ValueError('Audio and HA must use the same household timezone')
            broker=SpeechBroker(memory,tts,player)
            executor.speech=broker
            conversation=VoiceConversation(memory,MemoryInterpreter(memory.config,model_path=args.jev_model))
            microphone.start()
            audio=(player,microphone,broker,conversation)
        generated=Path(config['database']).parent/'runtime-config.json'
        save_runtime_config(memory.config,generated)
        feed=ProfileFeed(gateway)
        feed.start()
        receipts.start()
        reported_audio_error=None
        def tick():
            nonlocal reported_audio_error
            changed=False
            try:
                update=feed.queue.get_nowait()
                if 'profile' in update:
                    changed=runtime.apply_profile(update['profile'],update['at'])
                    if changed: save_runtime_config(memory.config,generated)
            except Empty: pass
            now=datetime.now(timezone.utc)
            if audio:
                from zoneinfo import ZoneInfo
                player,microphone,broker,conversation=audio
                if microphone.error and microphone.error!=reported_audio_error:
                    log.error('Voice input stopped: %s',microphone.error)
                    reported_audio_error=microphone.error
                player.zone=ZoneInfo(memory.config['timezone'])
                try:
                    utterance=microphone.queue.get_nowait()
                    if (0<=(now-utterance.at).total_seconds()<=15 and
                            utterance.room in {s['room'] for s in memory.config['entities'].values()}):
                        result=conversation.handle(utterance.text,session_id=utterance.session_id,room=utterance.room,at=now)
                        played=broker.reply(result,room=utterance.room,request_id=utterance.request_id,user_engaged=True,at=now)
                        if not played.get('playback_attempted'):
                            conversation.discard_pending(utterance.room,now)
                except Empty: pass
            runtime.tick(datetime.now(timezone.utc))
            return changed
        try:
            collect(memory.config,os.environ['HA_TOKEN'],memory.store,tick,poll_interval=1)
        finally:
            feed.close()
            receipts.close()
            if audio:
                audio[1].close()
                audio[0].close()


if __name__=='__main__':
    try: main()
    except KeyboardInterrupt: pass
