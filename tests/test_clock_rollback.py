"""Clock corrections must not postpone protection or lose chart points."""
import json
import os
import sys
from unittest.mock import patch
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness


def test_failsafe_and_lease_use_elapsed_time_across_clock_steps():
    C = harness.load()
    wall, mono = [10000.0], [1000.0]
    reading = [1100]
    class Sensor:
        def readings(self, sid): return {'co2': reading[0]}, 'path'
    class Plug:
        on = False
        commands = []
        def state(self): return self.on
        def set(self, value): self.on = value; self.commands.append(value)
    plug = Plug()
    C._client_snapshot = lambda: (Sensor(), plug, C._cfg_generation)
    C.CFG['protect']['sensor_id'] = 'test'
    C.CFG['control'].update(mode='auto', stale_minutes=1,min_run_minutes=10)
    C._notify = lambda *a, **k: None
    with patch.object(C.time, 'time', lambda: wall[0]), patch.object(C.time, 'monotonic',lambda:mono[0]):
        C.tick()
        assert plug.commands == [True]
        wall[0] += 600; mono[0] += 5; reading[0] = 500
        C.tick()
        assert plug.commands == [True], 'Forward step bypassed minimum run'
        assert C.app.test_client().get('/api/state').get_json()['min_run_remaining'] == 595
        # Last good reading at mono1005; rollback must not move its deadline.
        wall[0] -= 600
        mono[0] += 30
        assert C._failsafe_cap(100) == 30
        payload = C._resume_payload()
        with patch.dict(os.environ,{C.RESUME_ENV:json.dumps(payload)}):
            assert C._apply_resume()
        plug.on = False
        mono[0] += 30
        C._failsafe()
        assert plug.commands == [True,True], 'Sensor outage deadline postponed'


def test_expired_power_is_unknown_after_clock_rollback():
    C = harness.load()
    wall, mono = [10000.0], [1000.0]
    C.CFG['plug'].update(watts_enabled=True, watts_min=10,watts_max=25,watts_fault_seconds=30)
    C.CFG['control']['mode'] = 'auto'
    C.STATE.update(fan=True, changed_at=8000, watts=20)
    C._notify = lambda *a, **k: None
    class Plug:
        commands = []
        def state(self): return True
        def set(self,on): self.commands.append(on)
    p = Plug()
    with patch.object(C.time,'time',lambda:wall[0]), patch.object(C.time,'monotonic',lambda:mono[0]):
        C._stamp_elapsed_locked('watts_at')
        C._draw_check(p)
        wall[0] -= 600; mono[0] += 20
        assert C.app.test_client().get('/api/state').get_json()['watts'] is None
        C._draw_check(p)
        mono[0] += 30
        C._draw_check(p)
        assert C.STATE['fault'] and p.commands == [False]


def test_meter_failures_after_rollback_expire_last_successful_acquisition():
    C = harness.load()
    wall, mono = [10000.0], [1000.0]
    C.CFG['control']['mode'] = 'auto'
    C.CFG['plug'].update(watts_enabled=True,watts_min=10,watts_max=25,watts_fault_seconds=30)
    C.STATE.update(fan=True,changed_at=8000)
    C._notify = lambda *a, **k: None
    class Plug:
        commands = []
        reads = 0
        def state(self): return not self.commands
        def set(self,on): self.commands.append(on)
        def power_w(self):
            self.reads += 1
            if self.reads == 1: return 20
            raise C.PlugError('simulated meter outage')
    p = Plug()
    C._client_snapshot = lambda:(None,p,C._cfg_generation)
    def sleep(seconds):
        mono[0] += seconds
        wall[0] = 9400 + mono[0] - 1000
        if mono[0] >= 1090: raise StopIteration()
    with patch.object(C.time,'time',lambda:wall[0]), \
         patch.object(C.time,'monotonic',lambda:mono[0]), patch.object(C.time,'sleep',sleep):
        try: C.watts_loop()
        except StopIteration: pass
        assert C.STATE['fault'] and p.commands == [False]
        assert C.app.test_client().get('/api/state').get_json()['watts'] is None


def test_never_read_startup_age_survives_rollback_and_restart():
    C = harness.load()
    wall, mono = [10000.0], [1000.0]
    C.START_TIME = wall[0]
    C._elapsed_clocks['startup'] = (wall[0],mono[0])
    C.CFG['control'].update(mode='auto',stale_minutes=1)
    C._notify = lambda *a, **k: None
    class Plug:
        commands = []
        def state(self): return False
        def set(self,on): self.commands.append(on)
    p = Plug()
    C._client_snapshot = lambda:(None,p,C._cfg_generation)
    with patch.object(C.time,'time',lambda:wall[0]), patch.object(C.time,'monotonic',lambda:mono[0]):
        wall[0] -= 600; mono[0] += 30
        payload = C._resume_payload()
        with patch.dict(os.environ,{C.RESUME_ENV:json.dumps(payload)}):
            assert C._apply_resume()
        assert C._failsafe_cap(100) == 30
        mono[0] += 30
        C._failsafe()
        assert p.commands == [True]


def test_force_to_auto_preserves_physical_spin_up_grace():
    C = harness.load()
    wall, mono = [10000.0], [1000.0]
    C.START_TIME = 1
    C.CFG['control']['mode'] = 'on'
    C.CFG['plug'].update(watts_enabled=True,watts_min=10,watts_max=25,watts_fault_seconds=5)
    C._notify = lambda *a, **k: None
    class Plug:
        on = False
        commands = []
        def state(self): return self.on
        def set(self,value): self.on = value; self.commands.append(value)
    p = Plug()
    with patch.object(C.time,'time',lambda:wall[0]), patch.object(C.time,'monotonic',lambda:mono[0]):
        C._force_on(p)
        switch = C.STATE['changed_at']
        response = C.app.test_client().post('/api/config',json={'control':{'mode':'auto'}})
        assert response.status_code == 200
        assert C.STATE['changed_at'] == switch
        for elapsed in (1,6,19):
            mono[0] = 1000 + elapsed; wall[0] = 10000 + elapsed
            C.STATE['watts'] = 0
            C._stamp_elapsed_locked('watts_at')
            C._draw_check(p)
            assert C.STATE['fault'] is None and p.commands == [True]


def test_new_restart_request_with_pre_start_timestamp_is_consumed():
    C = harness.load()
    with open(C.RESTART_PATH,'w') as stream: stream.write('restart')
    os.utime(C.RESTART_PATH,(C._PROC_START-600,C._PROC_START-600))
    assert C._consume_restart_request()
    assert not os.path.exists(C.RESTART_PATH)


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
