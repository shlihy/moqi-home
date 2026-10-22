"""Pure command contract used by the HA boundary; no HA/model dependencies."""
import hashlib
import json
import math
from datetime import datetime

CONTROL_DOMAINS = {'light','switch','fan','climate'}
OBSERVE_DOMAINS = CONTROL_DOMAINS | {'sensor','binary_sensor','person','device_tracker'}


class CommandRejected(ValueError):
    pass


def fingerprint(value):
    encoded = json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def scope(entities, observe_label, control_label, enabled):
    """Only entity labels count; an area/device label never widens the selection."""
    observed,controlled = [],[]
    for entity,labels in entities.items():
        domain = entity.split('.',1)[0]
        if control_label in labels and domain in CONTROL_DOMAINS:
            controlled.append(entity)
        if (observe_label in labels or entity in controlled) and domain in OBSERVE_DOMAINS:
            observed.append(entity)
    result = dict(schema_version=1,execution_enabled=bool(enabled),
                  observed_entities=sorted(observed),control_entities=sorted(controlled))
    result['revision'] = fingerprint(result)
    return result


def validate_command(value, current, state, now):
    """Return one fixed HA service call; never accept a domain/service from a model."""
    if not isinstance(value,dict) or set(value) != {'id','revision','entity_id','action','value','issued_at'}:
        raise CommandRejected('invalid_command_fields')
    if (not isinstance(value['id'],str) or not 1 <= len(value['id']) <= 100 or
            any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in value['id'])):
        raise CommandRejected('invalid_command_id')
    if not current['execution_enabled']:
        raise CommandRejected('execution_disabled_in_ha')
    if value['revision'] != current['revision']:
        raise CommandRejected('ha_scope_changed')
    entity = value['entity_id']
    if not isinstance(entity,str) or entity not in current['control_entities']:
        raise CommandRejected('entity_not_selected_in_ha')
    try:
        issued = datetime.fromisoformat(value['issued_at'].replace('Z','+00:00'))
    except (AttributeError,ValueError):
        raise CommandRejected('invalid_command_time') from None
    if issued.tzinfo is None or not 0 <= (now-issued).total_seconds() <= 30:
        raise CommandRejected('stale_or_future_command')
    if not state or state['state'] in {'unknown','unavailable'}:
        raise CommandRejected('device_unavailable')
    domain = entity.split('.',1)[0]
    action = value['action']
    if not isinstance(action,str):
        raise CommandRejected('invalid_action')
    data = {'entity_id':entity}
    if action in {'turn_on','turn_off'} and value['value'] is None:
        return domain,action,data
    if domain=='climate' and action=='set_temperature':
        temperature = value['value']
        attrs = state['attributes']
        if attrs.get('temperature_unit') != '°C':
            raise CommandRejected('temperature_unit_not_celsius')
        low,high = attrs.get('min_temp'),attrs.get('max_temp')
        if (isinstance(temperature,bool) or not isinstance(temperature,(float,int)) or
                not math.isfinite(temperature) or low is None or high is None or
                not low <= temperature <= high):
            raise CommandRejected('outside_device_temperature_bounds')
        data['temperature'] = temperature
        return domain,action,data
    if domain=='climate' and action=='set_hvac_mode' and value['value'] in state['attributes'].get('hvac_modes',[]):
        data['hvac_mode'] = value['value']
        return domain,action,data
    raise CommandRejected('unsupported_action')
