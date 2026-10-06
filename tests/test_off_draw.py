"""OFF-power faults, stop confirmation and elapsed timers; no hardware."""
import copy
import json
import os
import sys
import threading
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness


class Plug:
    def __init__(self, C, relay=False, watts=23, reject=False):
        self.C, self.relay, self.watts, self.reject = C, relay, watts, reject
        self.commands = []
    def state(self):
        return self.relay
    def set(self, on):
        self.commands.append(on)
        if self.reject:
            raise self.C.PlugError('Rejected switch')
        self.relay = on
        return on
    def power_w(self):
        return self.watts


def setup(mode='auto'):
    C = harness.load()
    clock = [10000.0]
    C.time.time = lambda: clock[0]
    C.time.monotonic = lambda: clock[0]
    C.CFG['control']['mode'] = mode
    C.CFG['plug'].update(watts_enabled=True, watts_off_max=1, watts_fault_seconds=30)
    C.STATE.update(fan=False, watts=23, watts_at=clock[0], changed_at=clock[0]-100,
                   fault=None, fault_unsaved=False)
    notices = []
    C.CFG['email'].update(enabled=True, host='relay.invalid', sender='fan@invalid', recipients='test@invalid')
    def notify(*args, **kwargs):
        notices.append(args)
        if kwargs.get('on_complete'):
            kwargs['on_complete'](True)
    C._notify = notify
    return C, clock, notices, Plug(C)


def check(C, clock, plug, advance=0, watts=23):
    clock[0] += advance
    C.STATE['watts'] = watts
    C._stamp_elapsed_locked('watts_at', C.time.time())
    C._draw_check(plug, C._cfg_generation)


def test_off_draw_latches_and_never_confirms_welded_stop():
    for mode in ('auto', 'manual'):
        C, clock, notices, plug = setup(mode)
        check(C, clock, plug)
        check(C, clock, plug, 29)
        assert C.STATE['fault'] is None
        check(C, clock, plug, 1)
        assert C.STATE['fault'] is not None, 'Sustained OFF draw was not faulted'
        assert C.STATE['fault']['code'] == C.FC_OFF_DRAW
        assert C.STATE['fault']['stopped'] is False
        assert plug.commands == [False] and len(notices) == 1
        assert 'NOT confirmed' in notices[0][1]
        check(C, clock, plug, 5)
        assert plug.commands == [False] and len(notices) == 1
        assert json.load(open(C.FAULT_PATH))['stopped'] is False
        plug.watts = 0
        check(C, clock, plug, 5, watts=0)
        assert C.STATE['fault']['stopped'] is True
        assert C.STATE['fault'] is not None  # Recovery requires acknowledgement.


def test_standby_settling_stale_readings_and_force_on_do_not_fault():
    for condition in ('standby','settling','stale','override','disabled','external_on'):
        C, clock, notices, plug = setup()
        if condition == 'override': C.CFG['control']['mode'] = 'on'
        if condition == 'disabled': C.CFG['plug']['watts_enabled'] = False
        if condition == 'external_on': plug.relay = True
        for advance in (0, 35):
            clock[0] += advance
            C.STATE['watts_at'] = clock[0] - (20 if condition == 'stale' else 0)
            if condition == 'standby': C.STATE['watts'] = 1
            if condition == 'settling': C._stamp_elapsed_locked('changed_at')
            C._draw_check(plug, C._cfg_generation)
        assert C.STATE['fault'] is None, condition
        assert not notices and not plug.commands


def test_fault_stop_requires_fresh_low_draw_and_invalidates_old_confirmation():
    for reading in (23, None):
        C, clock, notices, plug = setup()
        C.STATE['fault'] = dict(at=clock[0], msg='Existing fault', code=C.FC_OVER_POWER, stopped=True)
        C.STATE['watts'] = 0  # Old cached zero must not certify the stop.
        plug.watts = reading
        assert C._enforce_stop(plug, C._cfg_generation) is False
        assert C.STATE['fault']['stopped'] is False
        plug.watts = 0
        assert C._enforce_stop(plug, C._cfg_generation) is True
        assert C.STATE['fault']['stopped'] is True


def test_rejected_off_is_retried_without_claiming_stopped():
    C, clock, notices, plug = setup()
    plug.relay, plug.reject = True, True
    C.STATE['fault'] = dict(at=clock[0], msg='Fault', code=C.FC_OVER_POWER, stopped=False)
    assert C._enforce_stop(plug, C._cfg_generation) is False
    assert C.STATE['fan'] is True and C.STATE['fault']['stopped'] is False
    assert C._enforce_stop(plug, C._cfg_generation) is False
    assert plug.commands == [False,False]


def test_unrelated_saves_and_fresh_restart_keep_off_timer():
    C, clock, notices, plug = setup()
    check(C, clock, plug)
    started = C._off_draw_since
    clock[0] += 20
    response = C.app.test_client().post('/api/config',json={})
    assert response.status_code == 200
    assert C._off_draw_since == started
    payload = C._resume_payload()
    D = harness.load()
    D.CFG = copy.deepcopy(C.CFG)
    D._notify = lambda *a, **kw: None
    os.environ[D.RESUME_ENV] = json.dumps(payload)
    try:
        assert D._apply_resume()
    finally:
        os.environ.pop(D.RESUME_ENV,None)
    assert D._off_draw_since == started
    D.STATE.update(fan=False, watts=23, watts_at=clock[0])
    check(D, clock, Plug(D), 10)
    assert D.STATE['fault']['code'] == D.FC_OFF_DRAW
    response = C.app.test_client().post('/api/config',json={'plug':{'watts_off_max':2}})
    assert response.status_code == 200 and C._off_draw_since is None


def test_real_emulator_off_reply_with_load_raises_safety_fault():
    from test_plug_emulator import E
    C, clock, notices, _ = setup()
    server = E.EmulatorServer(('127.0.0.1',0))
    server.state.update(dict(off_watts=23))
    thread = threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    try:
        plug = C.Tasmota('127.0.0.1:%d' % server.server_port)
        assert plug.state() is False and plug.power_w() == 23
        check(C, clock, plug)
        check(C, clock, plug, 30)
        assert C.STATE['fault']['code'] == 'FC-007'
        assert C.STATE['fault']['stopped'] is False
        assert len(notices) == 1
        server.state.update(dict(off_watts=0))
        check(C, clock, plug, 5, watts=0)
        assert C.STATE['fault']['stopped'] is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_off_timer_uses_elapsed_time_and_healthy_reading_resets_it():
    C, clock, notices, plug = setup()
    check(C, clock, plug)
    check(C, clock, plug, 20, watts=0)
    assert C._off_draw_since is None
    check(C, clock, plug)
    C.time.time = lambda: clock[0]-600  # Wall-clock correction must not delay protection.
    check(C, clock, plug, 30)
    assert C.STATE['fault'] is not None
    assert C.STATE['fault']['code'] == C.FC_OFF_DRAW


if __name__ == '__main__':
    sys.exit(harness.run(dict(globals())))
