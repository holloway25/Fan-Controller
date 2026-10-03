"""Oversized device readings and restart numbers are rejected, not raised."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness


def test_numeric_helpers_reject_overflow_and_keep_valid_numbers():
    C = harness.load()
    for parse in (C._num, C._n):
        for value in (10**400, -(10**400), float('inf'), float('nan'),
                      True, False, None, '28'):
            assert parse(value) is None
        for value in (0, -5, 28, 28.5):
            assert parse(value) == float(value)
    assert C._num({'value': 10**400}) is None


def test_bad_disabled_co2_does_not_abort_temperature_control():
    C = harness.load()
    C.CFG['control'].update(mode='auto', co2_enabled=False,
                            temp_enabled=True, humidity_enabled=False,
                            temp_on=26, temp_off=24, stale_minutes=0)
    C.CFG['protect']['sensor_id'] = 'selected'
    protect = C.Protect('console.invalid', '', '', False)
    protect._get = lambda _: {'sensors': [
        {'id': 'selected', 'isConnected': True,
         'airQuality': {'co2': 10**400, 'temperature': 28}}]}
    class Plug:
        commands = None
        def __init__(self):
            self.commands = []
        def state(self):
            return False
        def set(self, on):
            self.commands.append(on)
    plug = Plug()
    C.STATE.update(fan=False, fault=None)
    C._client_snapshot = lambda: (protect, plug, C._cfg_generation)
    C._notify = lambda *args, **kwargs: None
    C.tick()
    assert C.STATE['co2'] is None
    assert C.STATE['temperature'] == 28
    assert C.STATE['reading_at'] is not None
    assert plug.commands == [True]


def test_oversized_handover_numbers_do_not_abort_resume():
    C = harness.load()
    original_start = C.START_TIME
    payload = {'v': 1, 'changed_at': 10**400, 'reading_at': -(10**400),
               'oob_since': 10**400, 'start_time': 10**400,
               'latch': {'temp': True}, 'manual_off_pending': False}
    old = os.environ.get(C.RESUME_ENV)
    os.environ[C.RESUME_ENV] = json.dumps(payload)
    try:
        assert C._apply_resume() is True
    finally:
        if old is None:
            os.environ.pop(C.RESUME_ENV, None)
        else:
            os.environ[C.RESUME_ENV] = old
    assert C.STATE['changed_at'] is None
    assert C.STATE['reading_at'] is None
    assert C._oob_since is None
    assert C.START_TIME == original_start
    assert C.STATE['latch']['temp'] is True


if __name__ == '__main__':
    sys.exit(harness.run(dict(globals())))
