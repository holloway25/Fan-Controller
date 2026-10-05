"""Clock corrections must not postpone protection or lose chart points."""
import json
import os
import sys
from unittest.mock import patch
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness


def test_draw_fault_uses_elapsed_time_through_rollback_and_restart():
    for restart in (False, True):
        C = harness.load()
        wall, mono = [10000.0], [1000.0]
        C.CFG['control']['mode'] = 'auto'
        C.CFG['plug'].update(watts_enabled=True, watts_min=10,
                            watts_max=25, watts_fault_seconds=60)
        C.STATE.update(fan=True, changed_at=8000, watts=100, watts_at=wall[0])
        C._notify = lambda *a, **k: None
        class Plug:
            commands = []
            def state(self): return True
            def set(self, on): self.commands.append(on)
        plug = Plug()
        with patch.object(C.time, 'time', lambda: wall[0]), \
             patch.object(C.time, 'monotonic', lambda: mono[0]):
            C._draw_check(plug)
            wall[0] -= 600
            mono[0] += 30
            if restart:
                payload = C._resume_payload()
                C._oob_since = C._oob_monotonic = C._draw_grace = None
                with patch.dict(os.environ, {C.RESUME_ENV: json.dumps(payload)}):
                    assert C._apply_resume()
            C.STATE['watts_at'] = wall[0]
            C._draw_check(plug)
            assert C.STATE['fault'] is None
            mono[0] += 30
            wall[0] += 30
            C.STATE['watts_at'] = wall[0]
            C._draw_check(plug)
            assert C.STATE['fault'] and plug.commands == [False]
            assert 'for 60s' in C.STATE['fault']['msg']
            assert C.STATE['fault']['at'] == wall[0]


def test_actual_poll_rollback_forces_full_sorted_history_reconciliation():
    C = harness.load()
    wall = [10000.0]
    reading = [700]
    class Sensor:
        def readings(self, sid): return {'co2': reading[0]}, 'path'
    class Plug:
        def state(self): return False
        def set(self, on): return on
    C._client_snapshot = lambda: (Sensor(), Plug(), C._cfg_generation)
    C.CFG['protect']['sensor_id'] = 'test'
    C.CFG['control'].update(mode='auto', cold_start_minutes=0)
    client = C.app.test_client()
    with patch.object(C.time, 'time', lambda: wall[0]):
        C.tick()
        wall[0] += 30
        C.tick()
        old = client.get('/api/history').get_json()
        wall[0] -= 600
        reading[0] = 900
        C.tick()
        body = client.get('/api/history?since=%s&gen=%s' %
                          (old['history_rev'], old['history_generation'])).get_json()
        assert body['full'] and body['history_generation'] != old['history_generation']
        assert [p['t'] for p in body['history']] == [9430, 10000, 10030]
        assert body['history'][0]['v'] == 900 and body['first_t'] == 9430
        assert client.get('/api/state').get_json()['history_generation'] == body['history_generation']
        assert client.get('/api/history').get_json()['history'] == body['history']
        assert C.load_hist() == sorted(C.load_hist(), key=lambda p: p['t'])


if __name__ == '__main__':
    sys.exit(harness.run(dict(globals())))
