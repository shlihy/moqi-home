"""HA owns device selection and the final dispatch boundary.

No connection is made to Moqi or a cloud service during installation. The worker
uses authenticated local HTTP; its token is never given to either language model.
"""
import asyncio
from datetime import datetime, timezone

from aiohttp import web
from homeassistant.auth.permissions.const import POLICY_CONTROL, POLICY_READ
from homeassistant.const import Platform
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry, label_registry,device_registry
from homeassistant.helpers.http import HomeAssistantView, KEY_HASS
from homeassistant.helpers.storage import Store

from .control import CommandRejected, fingerprint, scope, validate_command
from .profile import build_profile

DOMAIN = 'moqi'


class Gateway:
    def __init__(self,hass,store,data,observe_label,control_label):
        self.hass,self.store,self.data = hass,store,data
        self.observe_label,self.control_label = observe_label,control_label
        self.lock = asyncio.Lock()
        self.active = True

    def current_scope(self):
        registry = entity_registry.async_get(self.hass)
        entities = {entry.entity_id:entry.labels for entry in registry.entities.values()
                    if entry.disabled_by is None}
        result = scope(entities,self.observe_label,self.control_label,self.data['enabled'] and self.active)
        # Area/purpose changes invalidate pending commands as well as permission
        # changes. Live temperatures do not invalidate the selection digest.
        result['revision'] = fingerprint([result['revision'],self.metadata(result)])
        return result

    def metadata(self,current):
        registry = entity_registry.async_get(self.hass)
        devices = device_registry.async_get(self.hass)
        labels = label_registry.async_get(self.hass)
        result = {}
        for entity in current['observed_entities']:
            entry = registry.async_get(entity)
            state = self.hass.states.get(entity)
            if entry is None:
                continue
            device = devices.async_get(entry.device_id) if entry.device_id else None
            area_id = entry.area_id or (device.area_id if device else None)
            attrs = state.attributes if state else {}
            names = list(entry.aliases or [])
            if attrs.get('friendly_name'): names.append(attrs['friendly_name'])
            unit = self.hass.config.units.temperature_unit if entity.startswith('climate.') else attrs.get('unit_of_measurement')
            result[entity] = dict(room=area_id,aliases=sorted(set(names)) or [entity],
                device_class=attrs.get('device_class'),unit=unit,min_temp=attrs.get('min_temp'),max_temp=attrs.get('max_temp'),
                hvac_modes=list(attrs.get('hvac_modes',[])),labels=sorted(
                    labels.labels[key].name for key in entry.labels if key in labels.labels))
        return result

    async def set_enabled(self,value):
        async with self.lock:
            self.data['enabled'] = value
            await self.store.async_save(self.data)

    async def execute(self,payload,context,user):
        entity = payload.get('entity_id') if isinstance(payload,dict) else None
        if not isinstance(entity,str) or not user.permissions.check_entity(entity,POLICY_CONTROL):
            raise web.HTTPForbidden(reason='entity_permission_denied')
        async with self.lock:
            raw_state = self.hass.states.get(entity)
            state = dict(state=raw_state.state,attributes=dict(raw_state.attributes)) if raw_state else None
            if state and entity.startswith('climate.'):
                state['attributes']['temperature_unit'] = self.hass.config.units.temperature_unit
            current = self.current_scope()
            domain,action,data = validate_command(payload,current,state,datetime.now(timezone.utc))
            digest = fingerprint(payload)
            previous = self.data['commands'].get(payload['id'])
            if previous:
                if previous['digest'] != digest:
                    raise CommandRejected('command_id_reused_with_different_payload')
                return previous
            # Persist before issuing. A crash leaves outcome_unknown, never a
            # second physical action. A receipt is not proof of a device state.
            receipt = dict(id=payload['id'],entity_id=entity,digest=digest,status='outcome_unknown',
                           context_id=context.id,issued_at=payload['issued_at'])
            self.data['commands'][payload['id']] = receipt
            await self.store.async_save(self.data)
            if self.current_scope()['revision'] != current['revision']:
                receipt['status'] = 'not_dispatched_scope_changed'
                await self.store.async_save(self.data)
                return receipt
            if not user.permissions.check_entity(entity,POLICY_CONTROL):
                receipt['status']='not_dispatched_permission_changed'
                await self.store.async_save(self.data)
                return receipt
            latest = self.hass.states.get(entity)
            if latest is None or latest.last_updated != raw_state.last_updated:
                receipt['status'] = 'not_dispatched_device_changed'
                await self.store.async_save(self.data)
                return receipt
            try:
                await self.hass.services.async_call(domain,action,data,blocking=True,context=context)
            except HomeAssistantError:
                # HA may have sent a physical command before reporting failure.
                # Preserve uncertainty, return a queryable receipt, never retry.
                raise web.HTTPBadGateway(reason='device_outcome_unknown') from None
            receipt['status'] = 'service_completed'
            await self.store.async_save(self.data)
            return receipt


class ScopeView(HomeAssistantView):
    url = '/api/moqi/scope'
    name = 'api:moqi:scope'

    async def get(self,request):
        gateway = request.app[KEY_HASS].data.get(DOMAIN)
        if gateway is None:
            raise web.HTTPServiceUnavailable(reason='moqi_not_loaded')
        user = request['hass_user']
        result = gateway.current_scope()
        # The digest covers global HA configuration; per-user API permission is
        # additionally checked here and again immediately before dispatch.
        result['observed_entities'] = [e for e in result['observed_entities']
                                      if user.permissions.check_entity(e,POLICY_READ)]
        result['control_entities'] = [e for e in result['control_entities']
                                     if user.permissions.check_entity(e,POLICY_CONTROL)]
        return self.json(result)


class ProfileView(HomeAssistantView):
    url = '/api/moqi/profile'
    name = 'api:moqi:profile'

    async def get(self,request):
        gateway = request.app[KEY_HASS].data.get(DOMAIN)
        if gateway is None:
            raise web.HTTPServiceUnavailable(reason='moqi_not_loaded')
        user = request['hass_user']
        current = gateway.current_scope()
        current['observed_entities'] = [e for e in current['observed_entities'] if user.permissions.check_entity(e,POLICY_READ)]
        current['control_entities'] = [e for e in current['control_entities'] if user.permissions.check_entity(e,POLICY_CONTROL)]
        return self.json(build_profile(current,gateway.metadata(current),gateway.hass.config.time_zone))


class ExecuteView(HomeAssistantView):
    url = '/api/moqi/execute'
    name = 'api:moqi:execute'

    async def post(self,request):
        gateway = request.app[KEY_HASS].data.get(DOMAIN)
        if gateway is None:
            raise web.HTTPServiceUnavailable(reason='moqi_not_loaded')
        try:
            payload = await request.json()
        except (ValueError,TypeError):
            return self.json({'error':'invalid_json_command'},status_code=400)
        try:
            result = await gateway.execute(payload,self.context(request),request['hass_user'])
        except CommandRejected as exc:
            # Contract errors are bounded reason codes, not arbitrary HA data.
            reason = str(exc)
            return self.json({'error':reason},status_code=409 if reason=='ha_scope_changed' else 400)
        return self.json(result)


class ReceiptView(HomeAssistantView):
    url = '/api/moqi/commands/{command_id}'
    name = 'api:moqi:receipt'

    async def get(self,request,command_id):
        gateway = request.app[KEY_HASS].data.get(DOMAIN)
        if gateway is None:
            raise web.HTTPServiceUnavailable(reason='moqi_not_loaded')
        receipt = gateway.data['commands'].get(command_id)
        if not receipt:
            raise web.HTTPNotFound()
        if not request['hass_user'].permissions.check_entity(receipt['entity_id'],POLICY_CONTROL):
            raise web.HTTPForbidden()
        return self.json(receipt)


async def async_setup(hass,config):
    hass.http.register_view(ScopeView())
    hass.http.register_view(ExecuteView())
    hass.http.register_view(ReceiptView())
    hass.http.register_view(ProfileView())
    return True


async def async_setup_entry(hass,entry):
    registry = label_registry.async_get(hass)
    def label(name):
        existing = next((v for v in registry.labels.values() if v.name==name),None)
        return (existing or registry.async_create(name,icon='mdi:home-automation')).label_id
    store = Store(hass,1,'moqi.gateway')
    data = await store.async_load() or {'enabled':False,'commands':{}}
    hass.data[DOMAIN] = Gateway(hass,store,data,label('默契：允许观察'),label('默契：允许控制'))
    for name in ('默契：灯具','默契：可靠在场','默契：室外温度','默契：无窗'):
        label(name)
    await hass.config_entries.async_forward_entry_setups(entry,[Platform.SWITCH])
    return True


async def async_unload_entry(hass,entry):
    gateway = hass.data[DOMAIN]
    gateway.active = False
    if await hass.config_entries.async_unload_platforms(entry,[Platform.SWITCH]):
        hass.data.pop(DOMAIN,None)
        return True
    gateway.active = True
    return False
