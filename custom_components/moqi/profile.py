"""HA metadata to Moqi roles. Unknown capabilities stay visible, not inferred."""

SENSOR_ROLES = {'temperature':'temperature','humidity':'humidity','power':'power','energy':'energy',
                'illuminance':'light','carbon_dioxide':'co2'}


def build_profile(current,metadata,timezone):
    entities,devices,issues = {},{},[]
    for entity in current['observed_entities']:
        details = metadata.get(entity,{})
        domain = entity.split('.',1)[0]
        room = details.get('room')
        labels = details.get('labels',[])
        if not room:
            issues.append(dict(entity_id=entity,reason='assign_area_in_ha'))
            continue
        spec = dict(room=room,aliases=details.get('aliases') or [entity])
        device_class = details.get('device_class')
        if domain in {'light','switch','fan','climate'}:
            category = ('light' if domain=='light' or '默契：灯具' in labels else
                        'hvac' if domain=='climate' else 'ventilation' if domain=='fan' else 'unknown')
            spec.update(role='actuator',category=category,
                        risk='unclassified' if category=='unknown' else 'medium' if category=='hvac' else 'low')
            if category=='unknown':
                issues.append(dict(entity_id=entity,reason='verify_switched_load_before_autonomy'))
        elif domain=='sensor':
            role = SENSOR_ROLES.get(device_class)
            if role is None:
                issues.append(dict(entity_id=entity,reason='unsupported_sensor_class'))
                continue
            spec.update(role=role,unit=details.get('unit'))
            if role=='temperature':
                spec['feature'] = 'outdoor' if '默契：室外温度' in labels else 'indoor'
            elif role=='light':
                spec['feature']='light'
        elif domain=='binary_sensor':
            if '默契：可靠在场' in labels:
                spec.update(role='presence')
            elif device_class in {'motion','occupancy','presence'}:
                # An HA device_class does not prove continuous room occupancy.
                spec.update(role='motion')
            elif device_class in {'door','window','opening'}:
                spec.update(role='contact',contact_type=device_class)
            elif device_class=='moisture':
                spec.update(role='leak')
            else:
                issues.append(dict(entity_id=entity,reason='unsupported_binary_sensor_class'))
                continue
        elif domain in {'person','device_tracker'}:
            spec.update(role='presence',boundary='home',feature='mobile_home')
        else:
            issues.append(dict(entity_id=entity,reason='unsupported_entity_domain'))
            continue
        entities[entity] = spec
        if entity in current['control_entities'] and spec['role']=='actuator':
            actions = ['turn_on','turn_off']
            rule = dict(actions=actions,max_actions_per_hour=4,min_cycle_minutes=10)
            if domain=='climate':
                rule['actions'] = actions+['set_temperature','set_hvac_mode']
                low,high = details.get('min_temp'),details.get('max_temp')
                rule['temperature'] = {mode:[low,high] for mode in details.get('hvac_modes',[])
                    if mode in {'heat','cool'} and low is not None and high is not None and details.get('unit')=='°C'}
            devices[entity] = rule
    for entity,rule in devices.items():
        spec = entities[entity]
        room = spec['room']
        room_entities = {key:value for key,value in entities.items() if value['room']==room}
        if spec['category']=='hvac':
            rule['windows'] = [key for key,value in room_entities.items() if value.get('contact_type')=='window']
            rule['window_context_verified'] = bool(rule['windows']) or '默契：无窗' in metadata[entity].get('labels',[])
            if not rule['window_context_verified']:
                issues.append(dict(entity_id=entity,reason='verify_window_context_in_ha'))
        if spec['category']=='ventilation':
            presence = [key for key,value in room_entities.items() if value['role']=='presence']
            co2 = [key for key,value in room_entities.items() if value['role']=='co2']
            if len(presence)==1: rule['presence'] = presence[0]
            if len(co2)==1: rule['co2'] = co2[0]
    thermal={}
    outdoors=[key for key,value in entities.items() if value.get('feature')=='outdoor' and value.get('unit') in {'°C','C'}]
    for room in {value['room'] for value in entities.values()}:
        hvac=[key for key,value in entities.items() if value['room']==room and value.get('category')=='hvac']
        indoor=[key for key,value in entities.items() if value['room']==room and value.get('feature')=='indoor'
                and value.get('unit') in {'°C','C'}]
        if len(hvac)==1 and len(indoor)==1 and len(outdoors)==1:
            thermal[room]=dict(indoor=indoor[0],outdoor=outdoors[0],hvac=hvac[0],input_mode='hvac_action')
        elif hvac:
            issues.append(dict(room=room,reason='thermal_context_requires_unambiguous_indoor_outdoor_and_hvac'))
    return dict(current,timezone=timezone,entities=entities,policy={'devices':devices},issues=issues,thermal_rooms=thermal)
