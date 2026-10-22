"""HA label/dispatch contract and HTTP fixtures; no HA installation is claimed."""
import importlib.util
import io
import json
import unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path

from moqi.ha_gateway import HAGateway, GatewayError

MODULE = Path(__file__).resolve().parents[1]/'custom_components/moqi/control.py'
spec = importlib.util.spec_from_file_location('moqi_ha_control_contract',MODULE)
control = importlib.util.module_from_spec(spec)
spec.loader.exec_module(control)
AT = datetime(2026,10,22,12,tzinfo=timezone.utc)


class GatewayContractTests(unittest.TestCase):
    def current(self,**kwargs):
        return control.scope(kwargs.get('entities',{'light.dining':{'control'}}),'observe','control',kwargs.get('enabled',True))

    def command(self,current,**kwargs):
        return dict(id='command-1',revision=current['revision'],entity_id='light.dining',
                    action=kwargs.get('action','turn_on'),value=kwargs.get('value'),
                    issued_at=kwargs.get('issued_at',AT.isoformat()))

    def test_ha_selection_is_the_source_and_protected_domains_never_expand_it(self):
        current = self.current(entities={'light.dining':{'control'},'sensor.temp':{'observe'},
            'light.not_selected':set(),'lock.front':{'control'},'camera.private':{'observe'}})
        self.assertEqual(current['control_entities'],['light.dining'])
        self.assertEqual(current['observed_entities'],['light.dining','sensor.temp'])
        self.assertNotEqual(current['revision'],self.current(enabled=False)['revision'])

    def test_device_revocation_rejects_previously_prepared_command(self):
        previous = self.current()
        command = self.command(previous)
        current = self.current(entities={})
        with self.assertRaisesRegex(control.CommandRejected,'ha_scope_changed'):
            control.validate_command(command,current,{'state':'off','attributes':{}},AT)
        command['revision'] = current['revision']
        with self.assertRaisesRegex(control.CommandRejected,'entity_not_selected'):
            control.validate_command(command,current,{'state':'off','attributes':{}},AT)

    def test_fixed_action_domain_disabled_stale_and_unavailable_are_enforced(self):
        current = self.current()
        state = dict(state='off',attributes={})
        self.assertEqual(control.validate_command(self.command(current),current,state,AT),
                         ('light','turn_on',{'entity_id':'light.dining'}))
        for action in ['reload','turn_on;restart',[],None]:
            with self.subTest(action=action),self.assertRaises(control.CommandRejected):
                control.validate_command(self.command(current,action=action),current,state,AT)
        with self.assertRaisesRegex(control.CommandRejected,'disabled'):
            control.validate_command(self.command(current),self.current(enabled=False),state,AT)
        for seconds in [-1,31]:
            command = self.command(current,issued_at=(AT-timedelta(seconds=seconds)).isoformat())
            with self.assertRaisesRegex(control.CommandRejected,'stale_or_future'):
                control.validate_command(command,current,state,AT)
        with self.assertRaisesRegex(control.CommandRejected,'unavailable'):
            control.validate_command(self.command(current),current,{'state':'unavailable','attributes':{}},AT)

    def test_climate_values_use_real_units_bounds_and_supported_modes(self):
        current = self.current(entities={'climate.ac':{'control'}})
        state = dict(state='heat',attributes={'temperature_unit':'°C','min_temp':16,'max_temp':30,'hvac_modes':['heat','off']})
        command = self.command(current,action='set_temperature',value=23)
        command['entity_id'] = 'climate.ac'
        self.assertEqual(control.validate_command(command,current,state,AT)[2]['temperature'],23)
        for value in [True,float('nan'),31,'23']:
            command['value'] = value
            with self.subTest(value=value),self.assertRaises(control.CommandRejected):
                control.validate_command(command,current,state,AT)
        command.update(value=23)
        state['attributes']['temperature_unit'] = '°F'
        with self.assertRaisesRegex(control.CommandRejected,'unit'):
            control.validate_command(command,current,state,AT)

    def test_post_timeout_never_retries_and_receipt_is_separate(self):
        paths = []
        def opener(request,timeout):
            paths.append((request.method,request.full_url))
            if request.method=='POST':
                raise TimeoutError()
            return io.BytesIO(b'{"id":"command-1","status":"outcome_unknown"}')
        gateway = HAGateway('http://ha.example:8123','synthetic-token',opener=opener)
        command = self.command(self.current())
        with self.assertRaisesRegex(GatewayError,'outcome unknown'):
            gateway.submit(command,command['revision'])
        self.assertEqual(len(paths),1)
        self.assertEqual(gateway.receipt('command-1')['status'],'outcome_unknown')
        self.assertEqual([method for method,path in paths],['POST','GET'])
        self.assertTrue(all('/api/services/' not in path for method,path in paths))


if __name__=='__main__':
    unittest.main()
