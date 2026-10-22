"""Client for Moqi's HA-owned scope and dispatch boundary, not generic services.

Post timeouts have uncertain outcomes. Query a receipt; never resend a command
automatically. Actual device state must still be confirmed by the event adapter.
"""
import json
from queue import Empty,Full,Queue
from threading import Event,Thread
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .ha import websocket_url
from .memory_store import canonical


class GatewayError(RuntimeError):
    def __init__(self,message,*,http_status=None):
        super().__init__(message)
        self.http_status = http_status


class HAGateway:
    def __init__(self,base_url,token,*,opener=None,timeout=10):
        websocket_url(base_url)
        if not isinstance(token,str) or not token.strip():
            raise ValueError('Configure HA_TOKEN for the gateway client')
        self.base_url,self.token = base_url.rstrip('/'),token
        self.opener,self.timeout = opener or urlopen,timeout

    def _request(self,path,payload=None):
        request = Request(self.base_url+path,
            data=canonical(payload).encode() if payload is not None else None,
            headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json'},
            method='POST' if payload is not None else 'GET')
        try:
            with self.opener(request,timeout=self.timeout) as response:
                body = response.read(1_048_577)
        except HTTPError as exc:
            raise GatewayError('HA gateway HTTP '+str(exc.code),http_status=exc.code) from None
        except (URLError,TimeoutError,OSError):
            suffix = '; command outcome unknown, query receipt' if payload is not None else ''
            raise GatewayError('HA gateway unavailable'+suffix) from None
        if len(body)>1_048_576:
            raise GatewayError('HA gateway response exceeds the 1 MiB limit')
        try:
            result = json.loads(body)
        except ValueError:
            raise GatewayError('HA gateway returned invalid JSON') from None
        if not isinstance(result,dict):
            raise GatewayError('HA gateway returned an invalid response')
        return result

    def scope(self):
        result = self._request('/api/moqi/scope')
        if (type(result.get('schema_version')) is not int or result['schema_version']!=1 or
                not isinstance(result.get('revision'),str) or
                not isinstance(result.get('execution_enabled'),bool) or
                any(not isinstance(result.get(key),list) or
                    any(not isinstance(entity,str) for entity in result[key])
                    for key in ('observed_entities','control_entities'))):
            raise GatewayError('HA gateway scope contract is invalid')
        return result

    def submit(self,proposal,revision):
        payload = {key:proposal[key] for key in ('id','entity_id','action','value','issued_at')}
        payload['revision'] = revision
        return self._request('/api/moqi/execute',payload)

    def profile(self):
        result = self._request('/api/moqi/profile')
        if (type(result.get('schema_version')) is not int or result['schema_version']!=1 or not isinstance(result.get('entities'),dict) or
                not isinstance(result.get('policy'),dict) or not isinstance(result.get('revision'),str)):
            raise GatewayError('HA gateway profile contract is invalid')
        return result

    def receipt(self,command_id):
        return self._request('/api/moqi/commands/'+quote(command_id,safe=''))


class ReceiptFeed:
    """Receipt GETs run off the event loop, with bounded queues and no POSTs."""
    def __init__(self,gateway):
        self.gateway=gateway
        self.requests=Queue(maxsize=16)
        self.responses=Queue(maxsize=16)
        self.pending=set()
        self.ready={}
        self.next_check={}
        self.stop=Event()
        self.thread=Thread(target=self._run,name='moqi-ha-receipts',daemon=True)

    def start(self): self.thread.start()

    def _run(self):
        while not self.stop.is_set():
            try: identifier=self.requests.get(timeout=.5)
            except Empty: continue
            try: result=self.gateway.receipt(identifier)
            except GatewayError: result=None
            self.responses.put((identifier,result))

    def poll(self,identifier,at):
        while True:
            try: key,value=self.responses.get_nowait()
            except Empty: break
            self.pending.discard(key)
            self.ready[key]=value
            self.next_check[key]=at.timestamp()+(5 if value else 15)
        if identifier in self.ready:
            value=self.ready.pop(identifier)
            if value is None: raise GatewayError('HA receipt is not available yet')
            return value
        if identifier not in self.pending and at.timestamp()>=self.next_check.get(identifier,0):
            try: self.requests.put_nowait(identifier)
            except Full: return None
            self.pending.add(identifier)
        return None

    def close(self):
        self.stop.set()
        self.thread.join(timeout=self.gateway.timeout+1)
