"""Local, persistent browser playground for reviewing real JEV memory changes."""
import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from .api import HomeMemory, Observation
from .constraint_scopes import applies
from .interpretation import MemoryInterpreter
from .memory_store import meta, objects, set_meta

SITE = 'SYNTHETIC-JEV-PLAYGROUND'
CONFIG = {'site':SITE,'timezone':'Asia/Shanghai','entities':{
    'switch.dining_light':{'role':'actuator','room':'dining','category':'light','risk':'low','aliases':['餐厅灯','dining light']},
    'switch.fan':{'role':'actuator','room':'dining','category':'ventilation','risk':'low','aliases':['风扇','fan']},
    'climate.study':{'role':'actuator','room':'study','category':'hvac','risk':'medium','aliases':['书房空调','书房的空调']},
    'climate.bedroom':{'role':'actuator','room':'bedroom','category':'hvac','risk':'medium','aliases':['卧室空调','卧室的空调']},
    'binary_sensor.dining_presence':{'role':'presence','room':'dining'},
    'binary_sensor.study_presence':{'role':'presence','room':'study'},
}}
START = datetime(2026,10,22,15,tzinfo=timezone.utc)


def _check_owned_database(filename):
    path = Path(filename).resolve()
    if not path.exists():
        return
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        owner = connection.execute("SELECT value FROM memory_meta WHERE key='local_playground_site'").fetchone() if 'memory_meta' in tables else None
        if tables and (not owner or json.loads(owner[0]) != SITE):
            raise ValueError('Choose a new database path. This playground opens only its own synthetic data.')


class LocalPlayground:
    def __init__(self,database,model_path,mode='hybrid'):
        _check_owned_database(database)
        self.engine = HomeMemory(CONFIG,database)
        self.mode = mode
        self.model_error = None
        self.interpreter = None
        try:
            self.interpreter = MemoryInterpreter(CONFIG,model_path=model_path,mode=mode)
        except (RuntimeError,OSError,ValueError) as exc:
            self.model_error = str(exc)
        self.at = datetime.fromisoformat(meta(self.engine.store,'local_playground_clock',START.isoformat()))
        self.activity = meta(self.engine.store,'local_playground_activity')
        with self.engine.store.db:
            set_meta(self.engine.store,'local_playground_site',SITE)
        if meta(self.engine.store,'local_playground_clock') is None:
            self._seed()

    def _seed(self):
        for offset in range(10,0,-1):
            start = START-timedelta(days=offset)
            self._baselines(start-timedelta(minutes=1))
            self.engine.observe_interval(start-timedelta(minutes=1),start+timedelta(minutes=30))
            self.engine.ingest(Observation('switch.dining_light',start+timedelta(minutes=5),'off','on',
                                          source='human',evidence='Synthetic verified physical control'))
            self.engine.ingest(Observation('climate.study',start+timedelta(minutes=10),'heat','heat',
                                          attributes={'temperature':22},previous_attributes={'temperature':24},
                                          source='human',evidence='Synthetic thermostat adjustment'))
        self._baselines(START-timedelta(minutes=1))
        self.engine.observe_interval(START-timedelta(minutes=1),START+timedelta(minutes=30))
        self.engine.learn(START)
        with self.engine.store.db:
            set_meta(self.engine.store,'local_playground_clock',START.isoformat())

    def _baselines(self,at):
        self.engine.ingest(Observation('switch.dining_light',at,'on'))
        self.engine.ingest(Observation('climate.study',at,'heat',attributes={'temperature':24}))
        for entity in ('binary_sensor.dining_presence','binary_sensor.study_presence'):
            self.engine.ingest(Observation(entity,at,'on'))

    def view(self):
        data = self.engine.inspect(self.at)
        for value in data['constraints']:
            value['effective_now'] = applies(value,value['entity_id'],value['deny_actions'][0],self.at,CONFIG)
        excluded = {row['event_key'] for row in data['learning_exceptions'] if row.get('active',True)}
        events = [{**row,'learning_excluded':row['event_key'] in excluded}
                  for row in data['recent_events'] if row['attributed_source'] in {'human','device'}]
        return {
            'synthetic':True,'physical_execution':False,'mode':self.mode,'model_error':self.model_error,
            'model_ready':self.interpreter is not None and self.interpreter.agent is not None,
            'asof':self.at.isoformat(),'local_time':self.at.astimezone(ZoneInfo(CONFIG['timezone'])).isoformat(),
            'timezone':CONFIG['timezone'],'activity':self.activity,
            'entities':{key:value.get('aliases',[key])[0] for key,value in CONFIG['entities'].items()},
            'memory':data,'events':events,'changes':self.engine.change_history(),
            'shadow':self.engine.shadow(self.at,activity=self.activity),
        }

    def command(self,action,data):
        if action == 'interpret':
            if self.interpreter is None:
                raise RuntimeError(self.model_error)
            result = self.engine.propose_change(data['text'],self.at,self.interpreter,
                                                selected_event=data.get('selected_event'))
        elif action in {'apply','cancel','undo'}:
            operation = getattr(self.engine,action+'_change')
            result = operation(data['id'],self.at)
        elif action == 'activity':
            value = data.get('activity') or None
            if value not in {None,'working','sleeping'}:
                raise ValueError('Choose working, sleeping or no explicit activity')
            self.activity = value
            with self.engine.store.db:
                set_meta(self.engine.store,'local_playground_activity',value)
            result = {'activity':value}
        elif action == 'clock':
            value = datetime.fromisoformat(data['local_time'])
            if value.tzinfo is None:
                value = value.replace(tzinfo=ZoneInfo(CONFIG['timezone']))
            value = value.astimezone(timezone.utc)
            if value < self.at:
                raise ValueError('Move the synthetic clock forward; use a fresh database to start over')
            self.engine.learn(value)
            self.at = value
            with self.engine.store.db:
                set_meta(self.engine.store,'local_playground_clock',value.isoformat())
            result = {'asof':value.isoformat()}
        else:
            raise ValueError('Unknown playground action')
        return {'result':result,'view':self.view()}

    def close(self):
        self.engine.close()


class Handler(BaseHTTPRequestHandler):
    def _json(self,status,value):
        payload = json.dumps(value,ensure_ascii=False,allow_nan=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Content-Length',str(len(payload)))
        self.send_header('Cache-Control','no-store')
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        route = urlparse(self.path).path
        if route == '/':
            body = (Path(__file__).parent/'web'/'local.html').read_bytes()
            self.send_response(200)
            self.send_header('Content-Type','text/html; charset=utf-8')
            self.send_header('Content-Length',str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif route == '/api/view':
            self._json(200,self.server.app.view())
        else:
            self._json(404,{'error':'Page not found'})

    def do_POST(self):
        origin = self.headers.get('Origin')
        if origin and origin != 'http://'+self.headers.get('Host',''):
            self._json(403,{'error':'Use the page served by this local playground'})
            return
        if not self.headers.get('Content-Type','').startswith('application/json'):
            self._json(415,{'error':'Expected a JSON request'})
            return
        try:
            length = int(self.headers.get('Content-Length','0'))
            if not 0 < length <= 65536:
                raise ValueError('Request size must be between 1 and 65536 bytes')
            data = json.loads(self.rfile.read(length))
            if not isinstance(data,dict):
                raise ValueError('Expected a JSON object')
            route = urlparse(self.path).path
            if not route.startswith('/api/'):
                self._json(404,{'error':'Action not found'})
                return
            self._json(200,self.server.app.command(route.removeprefix('/api/'),data))
        except (ValueError,KeyError) as exc:
            self._json(400,{'error':str(exc)})
        except RuntimeError as exc:
            self._json(503,{'error':str(exc)})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',default='data/jev-playground.sqlite3')
    parser.add_argument('--model',default='models/jev-int8')
    parser.add_argument('--mode',choices=['hybrid','model','rules'],default='hybrid')
    parser.add_argument('--port',type=int,default=8879)
    options = parser.parse_args()
    app = LocalPlayground(options.database,options.model,options.mode)
    server = HTTPServer(('127.0.0.1',options.port),Handler)
    server.app = app
    print(f'Moqi local playground: http://127.0.0.1:{server.server_port} ({options.mode})',flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.close()


if __name__ == '__main__':
    main()
