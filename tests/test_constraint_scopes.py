"""Scope regressions that could keep a temporary instruction active too long."""
import unittest
import tempfile
from datetime import datetime
from pathlib import Path

from moqi.constraint_scopes import applies, resolve_scope
from moqi.core import Store
from moqi.memory_store import objects
from moqi.understanding import constraint, write_understanding

CONFIG = {'timezone': 'Asia/Shanghai'}
ENTITY = 'switch.dining_light'


def local(value):
    return datetime.fromisoformat(value + '+08:00')


def restriction(kind, **fields):
    return {'entity_id': ENTITY, 'deny_actions': ['turn_off'], 'scope': kind, **fields}


class ConstraintScopeTests(unittest.TestCase):
    def test_tonight_expires_at_next_local_seven_and_never_before_apply(self):
        start = local('2026-10-22T21:00:00')
        value = restriction('temporary', **resolve_scope('temporary', start, CONFIG))
        self.assertFalse(applies(value, ENTITY, 'turn_off', local('2026-10-22T20:59:59'), CONFIG))
        self.assertTrue(applies(value, ENTITY, 'turn_off', local('2026-10-23T06:59:59'), CONFIG))
        self.assertFalse(applies(value, ENTITY, 'turn_off', local('2026-10-23T07:00:00'), CONFIG))
        self.assertFalse(applies(value, ENTITY, 'turn_off', local('2026-10-23T23:00:00'), CONFIG))

    def test_before_and_at_seven_resolve_to_different_mornings(self):
        before = resolve_scope('temporary', local('2026-10-23T06:59:59'), CONFIG)
        boundary = resolve_scope('temporary', local('2026-10-23T07:00:00'), CONFIG)
        self.assertEqual(before['expires_at'], '2026-10-22T23:00:00+00:00')
        self.assertEqual(boundary['expires_at'], '2026-10-23T23:00:00+00:00')

    def test_friday_night_stays_workday_after_midnight(self):
        value = restriction('weekday_night')
        self.assertTrue(applies(value, ENTITY, 'turn_off', local('2026-10-24T01:00:00'), CONFIG))
        self.assertFalse(applies(value, ENTITY, 'turn_off', local('2026-10-24T23:00:00'), CONFIG))
        self.assertFalse(applies(value, ENTITY, 'turn_off', local('2026-10-23T22:59:59'), CONFIG))

    def test_holiday_and_explicit_workday_use_night_start_date(self):
        value = restriction('weekday_night')
        config = {**CONFIG, 'holidays': ['2026-10-23'], 'workdays': ['2026-10-24']}
        self.assertFalse(applies(value, ENTITY, 'turn_off', local('2026-10-24T01:00:00'), config))
        self.assertTrue(applies(value, ENTITY, 'turn_off', local('2026-10-25T01:00:00'), config))

    def test_legacy_scopes_and_other_devices_keep_their_meaning(self):
        at = local('2026-10-23T12:00:00')
        self.assertTrue(applies(restriction('always'), ENTITY, 'turn_off', at, CONFIG))
        self.assertFalse(applies(restriction('night'), ENTITY, 'turn_off', at, CONFIG))
        self.assertFalse(applies(restriction('always'), 'switch.fan', 'turn_off', at, CONFIG))
        self.assertFalse(applies(restriction('always'), ENTITY, 'turn_on', at, CONFIG))
        self.assertFalse(applies(restriction('always', active=False), ENTITY, 'turn_off', at, CONFIG))

    def test_temporary_scope_survives_editable_export_import_and_restart(self):
        config = {**CONFIG, 'site': 'synthetic', 'entities': {
            ENTITY: {'role': 'actuator', 'room': 'dining', 'category': 'light'},
        }}
        at = local('2026-10-22T21:00:00')
        scope = resolve_scope('temporary', at, config)
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / 'memory.sqlite3')
            memory = Path(directory) / 'understanding'
            store = Store(database)
            try:
                original = constraint(store, config, ENTITY, ['turn_off'], 'temporary',
                                      'Only tonight', 'app', at,
                                      valid_from=scope['valid_from'], expires_at=scope['expires_at'])
                write_understanding(store, config, at, memory)
                write_understanding(store, config, at, memory)
            finally:
                store.close()
            store = Store(database)
            try:
                rows = objects(store, 'memory_constraints')
                self.assertEqual(rows, [original])
                self.assertFalse(applies(rows[0], ENTITY, 'turn_off', local('2026-10-23T07:00:00'), config))
            finally:
                store.close()


if __name__ == '__main__':
    unittest.main()
