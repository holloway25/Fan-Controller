"""Partial Protect payloads cannot hide an enabled trigger's outage."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness
import test_alert_delivery as E


def setup():
    C = harness.load()
    sent = E.wire(C)
    C.CFG['protect']['sensor_id'] = 'selected'
    C.CFG['control'].update(mode='auto', co2_enabled=True,
        temp_enabled=True, humidity_enabled=True, stale_minutes=1)
    vals = dict(co2=500, temperature=20, humidity=40)
    class Protect:
        def readings(self, _):
            return dict(vals), 'co2'
    class Plug:
        on = False
        def __init__(self):
            self.commands = []
        def state(self):
            return self.on
        def set(self, on):
            self.commands.append(on)
            self.on = on
    plug = Plug()
    C._clients = lambda: (Protect(), plug)
    C.STATE.update(reading_at=C.time.time()-120, fan=False)
    return C, vals, plug, sent


def reject(C, metric):
    try:
        C.tick()
    except C.ProtectError as exc:
        assert metric in str(exc), str(exc)
    else:
        raise AssertionError('Partial sensor poll was accepted')


def test_each_missing_enabled_metric_preserves_safety_state():
    for metric, rule in [('co2','co2'), ('temperature','temp'), ('humidity','humidity')]:
        C, vals, plug, sent = setup()
        vals[metric] = None
        C.STATE['latch'][rule] = True
        before = C.STATE['reading_at']
        reject(C, rule)
        assert C.STATE['reading_at'] == before and not C.STATE['history']
        assert C.STATE['latch'][rule] is True and not plug.commands
        C._failsafe()
        assert plug.commands == [True] and len(sent) == 1
        assert sent[0][0].startswith('FC-001')
        reject(C, rule)
        assert C._stale_delivered and len(sent) == 1
        vals[metric] = {'co2':500,'temperature':20,'humidity':40}[metric]
        C.tick()
        assert C.STATE['reading_at'] > before
        assert not C._stale_delivered and C._stale_episode is None
        assert len(sent) == 2 and sent[-1][0].startswith('FC-100')


def test_disabled_missing_metrics_are_not_required():
    C, vals, plug, sent = setup()
    C.CFG['control'].update(co2_enabled=False, temp_enabled=False)
    vals.update(co2=None, temperature=None)
    C.tick()
    assert C.STATE['humidity'] == 40 and len(C.STATE['history']) == 1
    C._failsafe()
    assert not plug.commands and not sent


def test_partial_outage_waits_for_configured_elapsed_deadline():
    C, vals, plug, sent = setup()
    clock = [1000.0]
    C.time.monotonic = lambda: clock[0]
    C._stamp_elapsed_locked('reading_at')
    vals['co2'] = None
    for age in (0, 30, 59):
        clock[0] = 1000 + age
        reject(C, 'co2')
        C._failsafe()
        assert not plug.commands and not sent
    clock[0] = 1060
    reject(C, 'co2')
    C._failsafe()
    assert plug.commands == [True] and len(sent) == 1


def test_partial_outage_respects_disabled_failsafe_and_non_auto_modes():
    for mode, mins in [('auto',0), ('manual',1), ('on',1)]:
        C, vals, plug, sent = setup()
        C.CFG['control'].update(mode=mode, stale_minutes=mins)
        vals['co2'] = None
        reject(C, 'co2')
        commands = list(plug.commands)
        C._failsafe()
        assert plug.commands == commands and not sent


def test_poll_loop_reports_partial_sensor_failure_and_ventilates():
    C, vals, plug, sent = setup()
    vals['co2'] = None
    class Finished(Exception):
        pass
    class Wake:
        def wait(self, _):
            raise Finished()
        def clear(self):
            pass
        def set(self):
            pass
    C._wait_for_dns = lambda: True
    C._wake = Wake()
    before = C.STATE['reading_at']
    try:
        C.loop()
    except Finished:
        pass
    assert C.STATE['reading_at'] == before
    assert 'co2' in C.STATE['sensor_error']
    assert plug.commands == [True] and len(sent) == 1


if __name__ == '__main__':
    sys.exit(harness.run(dict(globals())))
