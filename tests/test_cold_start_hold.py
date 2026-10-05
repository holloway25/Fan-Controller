"""Cold-start relay recovery uses simulated devices, never live hardware."""
import json
import os
import sys
from contextlib import contextmanager
from unittest.mock import patch
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness


class Plug:
    def __init__(self, on=True):
        self.on, self.commands = on, []
    def state(self):
        return self.on
    def set(self, on):
        self.on = on
        self.commands.append(on)


@contextmanager
def boot(minutes=10, mode='auto', fault=None, interlocked=False):
    C = harness.load(plug_access_disabled=interlocked)
    C.CFG['control'].update(mode=mode,cold_start_minutes=minutes,
        min_run_minutes=30,co2_enabled=True,temp_enabled=False,
        humidity_enabled=False,on_at=1000,off_at=750)
    C.CFG['protect']['sensor_id'] = 'selected'
    C.STATE['fault'] = fault
    C._notify = lambda *a, **k: None
    clock = [10000.0]
    reading = [900.0]
    plug = Plug()
    class Sensor:
        def readings(self, sid):
            return {'co2':reading[0]}, 'airQuality.co2'
    C._client_snapshot = lambda:(Sensor(),plug,C._cfg_generation)
    with patch.object(C.time,'time',lambda:clock[0]), \
         patch.object(C.time,'monotonic',lambda:clock[0]):
        C._announce_start(False)
        yield C, plug, clock, reading


def test_already_on_cold_boot_holds_ten_minutes_then_stops_without_demand():
    with boot() as (C,p,clock,reading):
        C.tick()
        assert p.commands == [] and C.STATE['changed_at'] is None
        assert C.STATE['cold_start_hold_until'] == 10600
        clock[0] = 10599
        C.tick()
        assert p.commands == []
        clock[0] = 10600
        C.tick()
        assert p.commands == [False] and C.STATE['cold_start_hold_until'] is None
        assert sum('Latched on' in e['msg'] for e in C.STATE['events']) == 1
        assert any(e['msg']=='FC-302 Latched on — Fan on (10 min)' for e in C.STATE['events'])
        assert C.event_class('FC-302 Latched on — Fan on (10 min)') == ''


def test_off_unknown_disabled_manual_forced_and_faulted_starts_do_not_get_hold():
    for options in ({'minutes':0},{'mode':'manual'},{'mode':'on'},
                    {'fault':{'at':1,'msg':'fault','code':'FC-003'}},
                    {'interlocked':True}):
        with boot(**options) as (C,p,clock,reading):
            C._refresh_relay(p,C._cfg_generation)
            assert C.STATE['cold_start_hold_until'] is None, options
    with boot() as (C,p,clock,reading):
        with C._state_lock:
            C._adopt_relay_locked(None,clock[0])
        assert C.STATE['cold_start_pending'] is True
        p.on = False
        C._refresh_relay(p,C._cfg_generation)
        p.on = True
        C._refresh_relay(p,C._cfg_generation)
        assert C.STATE['cold_start_hold_until'] is None


def test_fresh_demand_takes_over_with_normal_minimum_run():
    with boot() as (C,p,clock,reading):
        C.tick()
        clock[0] += 60
        reading[0] = 1100
        C.tick()
        assert C.STATE['latch']['co2'] is True
        assert C.STATE['cold_start_hold_until'] is None
        assert C.STATE['changed_at'] == clock[0]
        reading[0] = 700
        clock[0] = 10601
        C.tick()
        assert not p.commands
        clock[0] = 11861
        C.tick()
        assert p.commands == [False]


def test_fault_stop_is_not_delayed_by_startup_hold():
    with boot() as (C,p,clock,reading):
        C.tick()
        C._store_fault({'at':clock[0],'msg':'draw fault','code':'FC-003',
                        'stopped':False,'attempted_at':None,'delivered_at':None})
        assert C.STATE['cold_start_hold_until'] is None
        assert C._enforce_stop(p,C._cfg_generation) is True
        assert p.commands == [False]


def test_external_stop_and_relevant_config_change_cancel_hold():
    with boot() as (C,p,clock,reading):
        C.tick()
        p.on = False
        C._refresh_relay(p,C._cfg_generation)
        assert C.STATE['cold_start_hold_until'] is None
    with boot() as (C,p,clock,reading):
        C.tick()
        client = C.app.test_client()
        assert client.post('/api/config',json={'email':{'enabled':False}}).status_code == 200
        assert C.STATE['cold_start_hold_until'] == 10600
        assert client.post('/api/config',json={'control':{'cold_start_minutes':15}}).status_code == 200
        assert C.STATE['cold_start_hold_until'] is None


def test_preserving_restart_keeps_existing_deadline_without_new_cold_hold():
    with boot() as (C,p,clock,reading):
        C.tick()
        data = C._resume_payload()
        clock[0] += 120
        C.STATE['cold_start_hold_until'] = None
        with patch.dict(os.environ,{C.RESUME_ENV:json.dumps(data)}):
            assert C._apply_resume() is True
        assert C.STATE['cold_start_hold_until'] == 10600
        C._announce_start(True)
        assert sum('Latched on' in e['msg'] for e in C.STATE['events']) == 1


def test_ordinary_preserving_restart_does_not_create_startup_hold():
    C = harness.load()
    C.CFG['control']['mode'] = 'auto'
    C._announce_start(True)
    C._refresh_relay(Plug(),C._cfg_generation)
    assert C.STATE['cold_start_hold_until'] is None
    assert not any('Latched on' in e['msg'] for e in C.STATE['events'])


def test_missing_enabled_reading_does_not_end_expired_hold():
    with boot() as (C,p,clock,reading):
        C.tick()
        clock[0] += 601
        C.CFG['control']['temp_enabled'] = True
        class PartialSensor:
            def readings(self,sid):
                return {'co2':None,'temperature':22},'temperature'
        C._client_snapshot = lambda:(PartialSensor(),p,C._cfg_generation)
        C.tick()
        assert p.commands == [] and C.STATE['cold_start_hold_until'] == 10600


def test_configurable_duration_and_invalid_settings_are_checked():
    with boot(minutes=15) as (C,p,clock,reading):
        C.tick()
        assert C.STATE['cold_start_hold_until'] == 10900
        client = C.app.test_client()
        for value in (-1,241,1.5,True,10**400):
            reply = client.post('/api/config',json={'control':{'cold_start_minutes':value}})
            assert reply.status_code == 400 and reply.is_json, value


def test_monotonic_deadline_does_not_change_when_wall_clock_is_corrected():
    with boot() as (C,p,clock,reading):
        C.tick()
        with patch.object(C.time,'time',lambda:500000):
            C.tick()
        assert not p.commands and C.STATE['cold_start_hold_until'] == 10600
        clock[0] = 10601
        C.tick()
        assert p.commands == [False]


def test_power_protection_remains_active_during_startup_hold():
    with boot() as (C,p,clock,reading):
        C.START_TIME = clock[0]-100
        C.CFG['plug'].update(watts_enabled=True,watts_min=19,watts_max=27,
                             watts_fault_seconds=30)
        C.tick()
        C.STATE.update(watts=100,watts_at=clock[0])
        C._draw_check(p,C._cfg_generation)
        clock[0] += 31
        C.STATE['watts_at'] = clock[0]
        C._draw_check(p,C._cfg_generation)
        assert C.STATE['fault'] is not None and p.commands == [False]
        assert C.STATE['cold_start_hold_until'] is None


if __name__ == '__main__':
    sys.exit(harness.run(dict(globals())))
