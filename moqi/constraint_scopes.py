"""Household-local scope resolution shared by decisions and safety review."""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from .contexts import context, dt

SCOPES = frozenset({'always', 'night', 'temporary', 'weekday_night'})


def resolve_scope(kind, at, config):
    """Resolve an interpreted scope into timestamps without asking the model."""
    if kind not in SCOPES:
        raise ValueError('Unsupported constraint scope')
    if at.tzinfo is None:
        raise ValueError('Scope resolution needs a timezone-aware timestamp')
    result = {'scope': kind, 'valid_from': dt(at.isoformat()).isoformat()}
    if kind == 'temporary':
        zone = ZoneInfo(config.get('timezone', 'Asia/Shanghai'))
        local = at.astimezone(zone)
        end_date = local.date() + timedelta(days=local.hour >= 7)
        end = datetime.combine(end_date, time(7), tzinfo=zone)
        result['expires_at'] = dt(end.isoformat()).isoformat()
    return result


def applies(constraint, entity, action, at, config):
    """Half-open temporary intervals; overnight recurrence belongs to its start date."""
    if (not constraint.get('active', True) or constraint['entity_id'] != entity or
            action not in constraint['deny_actions']):
        return False
    if constraint.get('valid_from') and at < dt(constraint['valid_from']):
        return False
    if constraint.get('expires_at') and at >= dt(constraint['expires_at']):
        return False
    kind = constraint['scope']
    if kind in {'always', 'temporary'}:
        return True
    local = at.astimezone(ZoneInfo(config.get('timezone', 'Asia/Shanghai')))
    night = local.hour >= 23 or local.hour < 7
    if kind == 'night':
        return night
    if kind == 'weekday_night':
        if not night:
            return False
        anchor = local - timedelta(days=local.hour < 7)
        return context(anchor, config)[0] == 'weekday'
    raise ValueError('Unsupported constraint scope')
