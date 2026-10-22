"""HA Gateway ordering with minimal HA interfaces; no HA installation or network.

These tests prove our persistence/revocation logic, not HA API compatibility.
The real integration installation remains a separate target-machine gate.
"""
import copy
from datetime import datetime,timedelta,timezone
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch


class HAError(Exception): pass
class HTTPError(Exception):
    def __init__(self,*args,reason=None): super().__init__(reason or args)


def modules():
    result={}
    for name in ('homeassistant','homeassistant.auth','homeassistant.auth.permissions','homeassistant.helpers','aiohttp'):
        module=types.ModuleType(name); module.__path__=[]; result[name]=module
    def leaf(name,**values):
        module=types.ModuleType(name)
        module.__dict__.update(values)
        result[name]=module
        return module
    leaf('homeassistant.auth.permissions.const',POLICY_CONTROL='control',POLICY_READ='read')
    leaf('homeassistant.const',Platform=types.SimpleNamespace(SWITCH='switch'))
    leaf('homeassistant.exceptions',HomeAssistantError=HAError)
    leaf('homeassistant.helpers.http',HomeAssistantView=type('View',(),{}),KEY_HASS='fixture_hass')
    leaf('homeassistant.helpers.storage',Store=object)
    for name in ('entity_registry','device_registry','label_registry'):
        leaf('homeassistant.helpers.'+name)
    result['aiohttp'].web=types.SimpleNamespace(HTTPForbidden=HTTPError,HTTPBadGateway=HTTPError,
                                               HTTPServiceUnavailable=HTTPError,HTTPNotFound=HTTPError)
    return result


class BoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.patcher=patch.dict(sys.modules,modules())
        self.patcher.start()
        name='_moqi_ha_boundary_fixture'
        folder=Path(__file__).parents[1]/'custom_components/moqi'
        spec=importlib.util.spec_from_file_location(name,folder/'__init__.py',submodule_search_locations=[str(folder)])
        self.module=importlib.util.module_from_spec(spec)
        sys.modules[name]=self.module
        spec.loader.exec_module(self.module)
        self.at=datetime.now(timezone.utc)
        self.calls=[]
        self.history=[]
        self.hass=types.SimpleNamespace(states=types.SimpleNamespace(get=lambda entity:self.state),
            services=types.SimpleNamespace(async_call=self.service),config=types.SimpleNamespace(units=types.SimpleNamespace(temperature_unit='°C')))
        self.state=types.SimpleNamespace(state='off',attributes={},last_updated=self.at)
        self.data=dict(enabled=True,commands={})
        self.persisted=None
        self.on_save=None
        self.fail_service=False
        owner=self
        class Storage:
            async def async_save(self,value):
                owner.persisted=copy.deepcopy(value)
                owner.history.append(('saved',next(iter(value['commands'].values()))['status']))
                if owner.on_save:
                    function=owner.on_save; owner.on_save=None; function()
        self.storage=Storage()
        self.can_control=True
        self.user=types.SimpleNamespace(permissions=types.SimpleNamespace(check_entity=lambda entity,policy:self.can_control))
        self.context=types.SimpleNamespace(id='fixture-context')
        self.gateway=self.new_gateway(self.data)
        self.payload=dict(id='fixture-command',revision=self.gateway.current_scope()['revision'],entity_id='light.lamp',
                          action='turn_on',value=None,issued_at=self.at.isoformat())

    def tearDown(self):
        for name in list(sys.modules):
            if name.startswith('_moqi_ha_boundary_fixture'): del sys.modules[name]
        self.patcher.stop()

    def new_gateway(self,data):
        gateway=self.module.Gateway(self.hass,self.storage,data,'observe','control')
        gateway.current_scope=lambda:self.module.scope({'light.lamp':['control']},'observe','control',gateway.data['enabled'])
        return gateway

    async def service(self,domain,action,data,**kwargs):
        self.calls.append((domain,action,data,kwargs))
        self.history.append(('service',action))
        self.assertEqual(self.persisted['commands']['fixture-command']['status'],'outcome_unknown')
        if self.fail_service: raise HAError('SYNTHETIC device may already have received the command')

    async def test_receipt_persisted_before_dispatch_and_restart_cannot_duplicate_action(self):
        value=await self.gateway.execute(self.payload,self.context,self.user)
        self.assertEqual(self.history,[('saved','outcome_unknown'),('service','turn_on'),('saved','service_completed')])
        self.assertEqual(value['context_id'],'fixture-context')
        restarted=self.new_gateway(copy.deepcopy(self.persisted))
        repeated=await restarted.execute(self.payload,self.context,self.user)
        self.assertEqual(repeated['status'],'service_completed')
        self.assertEqual(len(self.calls),1)

    async def test_revocation_during_persistence_prevents_the_service_call(self):
        self.on_save=lambda:self.data.update(enabled=False)
        value=await self.gateway.execute(self.payload,self.context,self.user)
        self.assertEqual(value['status'],'not_dispatched_scope_changed')
        self.assertEqual(self.calls,[])
        self.assertEqual(self.persisted['commands']['fixture-command']['status'],'not_dispatched_scope_changed')

    async def test_observed_device_change_while_saving_is_not_overwritten(self):
        def replace_state():
            self.state=types.SimpleNamespace(state='on',attributes={},last_updated=self.at+timedelta(microseconds=1))
        self.on_save=replace_state
        value=await self.gateway.execute(self.payload,self.context,self.user)
        self.assertEqual(value['status'],'not_dispatched_device_changed')
        self.assertEqual(self.calls,[])

    async def test_user_permission_revoked_while_saving_prevents_dispatch(self):
        def revoke(): self.can_control=False
        self.on_save=revoke
        value=await self.gateway.execute(self.payload,self.context,self.user)
        self.assertEqual(value['status'],'not_dispatched_permission_changed')
        self.assertEqual(self.calls,[])

    async def test_service_error_preserves_unknown_outcome_and_never_repeats(self):
        self.fail_service=True
        with self.assertRaises(HTTPError): await self.gateway.execute(self.payload,self.context,self.user)
        self.assertEqual(self.persisted['commands']['fixture-command']['status'],'outcome_unknown')
        restarted=self.new_gateway(copy.deepcopy(self.persisted))
        value=await restarted.execute(self.payload,self.context,self.user)
        self.assertEqual(value['status'],'outcome_unknown')
        self.assertEqual(len(self.calls),1)


if __name__=='__main__': unittest.main()
