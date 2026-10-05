"""Draw protection and relay adoption do not depend on Protect succeeding."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness


class Plug:
    def __init__(self, on=True, watts=100):
        self.on, self.watts = on, watts
        self.commands, self.reads = [], 0

    def state(self):
        self.reads += 1
        return self.on

    def power_w(self):
        return self.watts

    def set(self, on):
        self.commands.append(on)
        self.on = on


def setup():
    C = harness.load()
    C.CFG["control"].update(mode="auto", stale_minutes=0)
    C.CFG["plug"].update(watts_enabled=True, watts_min=15,
                         watts_max=25, watts_fault_seconds=30)
    C.STATE.update(fan=None, reading_at=None, changed_at=None, fault=None)
    C._notify = lambda *args, **kwargs: None
    return C


def test_failsafe_restart_renews_physical_spin_up_grace():
    C = setup()
    clock = [1000.0]
    real_time, real_mono = C.time.time, C.time.monotonic
    C.time.time = lambda: clock[0]
    C.time.monotonic = lambda: clock[0]
    C.CFG['control'].update(stale_minutes=1)
    C.CFG['plug']['watts_fault_seconds'] = 5
    C.STATE.update(fan=True, changed_at=1, reading_at=1)
    plug = Plug(on=False, watts=100)
    C._client_snapshot = lambda: (None, plug, C._cfg_generation)
    try:
        C._failsafe()
        assert plug.commands == [True] and C.STATE['changed_at'] == 1000
        for t in (1000, 1005, 1010, 1020):
            clock[0] = t
            C.STATE.update(watts=100, watts_at=t)
            C._draw_check(plug)
            assert C.STATE['fault'] is None
        clock[0] = 1021
        C.STATE.update(watts=100, watts_at=clock[0])
        C._draw_check(plug)
        clock[0] = 1026
        C.STATE.update(watts=100, watts_at=clock[0])
        C._draw_check(plug)
        assert C.STATE['fault'] and plug.commands[-1] is False
    finally:
        C.time.time = real_time
        C.time.monotonic = real_mono


def test_running_fan_is_protected_during_disconnected_sensor_startup():
    C = setup()
    clock = [1000.0]
    real_time, real_sleep, real_mono = C.time.time, C.time.sleep, C.time.monotonic
    C.START_TIME = clock[0] - 100
    C.CFG["protect"]["sensor_id"] = "sensor"
    class Sensor:
        calls = 0
        def readings(self, sid):
            self.calls += 1
            raise C.ProtectError("Selected sensor is disconnected")
    sensor, plug = Sensor(), Plug()
    C._client_snapshot = lambda: (sensor, plug, C._cfg_generation)
    def advance(seconds):
        clock[0] += seconds
        if clock[0] >= 1100:
            raise StopIteration()
    C.time.time, C.time.sleep = lambda: clock[0], advance
    C.time.monotonic = lambda: clock[0]
    try:
        try:
            C.tick()
        except C.ProtectError:
            pass
        C.watts_loop()
    except StopIteration:
        pass
    finally:
        C.time.time, C.time.sleep = real_time, real_sleep
        C.time.monotonic = real_mono
    assert plug.commands == [False], "100 W running fan escaped draw protection"
    assert C.STATE["fault"]["code"] == C.FC_OVER_POWER
    assert C.STATE["fault"]["stopped"] is True
    assert C.STATE["fan"] is False
    assert C.STATE["reading_at"] is None
    assert sensor.calls == 1, "meter loop should not acquire sensor readings"


def test_relay_refresh_rejects_stale_clients_before_reading():
    C = setup()
    plug = Plug()
    old = C._cfg_generation
    C._cfg_generation += 1
    C._refresh_relay(plug, old)
    assert plug.reads == 0
    assert C.STATE["fan"] is None


def test_startup_adoption_does_not_invent_a_new_run_lease():
    C = setup()
    C._refresh_relay(Plug(), C._cfg_generation)
    assert C.STATE["fan"] is True
    assert C.STATE["changed_at"] is None
    assert not any("changed outside" in e["msg"] for e in C.STATE["events"])


def test_external_switch_refresh_preserves_run_tracking():
    C = setup()
    C.STATE.update(fan=True, ran_with={"co2": True})
    C._refresh_relay(Plug(on=False), C._cfg_generation)
    assert C.STATE["fan"] is False
    assert C.STATE["ran_with"] == {}
    assert C.STATE["changed_at"] is not None
    assert sum("changed outside" in e["msg"] for e in C.STATE["events"]) == 1
    C._refresh_relay(Plug(on=False), C._cfg_generation)
    assert sum("changed outside" in e["msg"] for e in C.STATE["events"]) == 1


def test_relay_read_and_adoption_hold_actuation_lock():
    C = setup()
    class Watching(Plug):
        def state(self):
            free = C._act_lock.acquire(blocking=False)
            if free:
                C._act_lock.release()
            assert not free, "settings can change during relay read"
            return True
    C._refresh_relay(Watching(), C._cfg_generation)
    assert C.STATE["fan"] is True


def test_power_failure_still_refreshes_relay_and_reaches_fault_detector():
    C = setup()
    C.START_TIME = C.time.time() - 1000
    class MissingMeter(Plug):
        def power_w(self):
            raise C.PlugError("Meter unreachable")
    plug = MissingMeter()
    C._client_snapshot = lambda: (None, plug, C._cfg_generation)
    C._oob_since = C.time.time() - 100
    C._oob_generation = C._cfg_generation
    real_sleep = C.time.sleep
    def stop_loop(_):
        raise StopIteration()
    C.time.sleep = stop_loop
    try:
        C.watts_loop()
    except StopIteration:
        pass
    finally:
        C.time.sleep = real_sleep
    assert plug.commands == [False]
    assert C.STATE["fault"] is not None


def test_reconfigured_meter_read_is_discarded_before_state_commit():
    C = setup()
    fresh = Plug(on=False, watts=0)
    class Old(Plug):
        def power_w(self):
            C._cfg_generation += 1
            return 100
    old = Old()
    C._client_snapshot = lambda: (None, old if C._cfg_generation == 0 else fresh,
                                  C._cfg_generation)
    real_sleep = C.time.sleep
    def stop_loop(_):
        raise StopIteration()
    C.time.sleep = stop_loop
    try:
        C.watts_loop()
    except StopIteration:
        pass
    finally:
        C.time.sleep = real_sleep
    assert C.STATE["watts"] == 0, "superseded meter sample was committed"
    assert C.STATE["fan"] is False
    assert C.STATE["fault"] is None
    assert not old.commands and not fresh.commands


def _auto_during_relay_change(change):
    C = setup()
    clock = [10000.0]
    real_time = C.time.time
    C.time.time = lambda: clock[0]
    C.CFG["control"].update(co2_enabled=True, temp_enabled=False,
                             humidity_enabled=False,
                             min_run_minutes=0 if change == "off" else 10,
                             on_at=1000, off_at=740)
    C.CFG["protect"]["sensor_id"] = "selected"
    C.STATE.update(fan=True, changed_at=clock[0]-3600)
    class Sensor:
        def readings(self, sid):
            return {"co2": 500}, "airQuality.co2"
    plug = Plug()
    C._client_snapshot = lambda: (Sensor(), plug, C._cfg_generation)
    def interleave(_):
        # tick has released its first actuation hold and is doing history I/O.
        if change:
            clock[0] += 5
            plug.on = False
            C._refresh_relay(plug, C._cfg_generation)
            if change == "off-on":
                clock[0] += 5
                plug.on = True
                C._refresh_relay(plug, C._cfg_generation)
        return False
    C.maybe_save_hist = interleave
    try:
        C.tick()
    finally:
        C.time.time = real_time
    return C, plug, clock[0]


def test_auto_honours_new_external_off_on_minimum_run_lease():
    C, plug, now = _auto_during_relay_change("off-on")
    assert C.STATE["changed_at"] == now, "fresh lease timestamp was overwritten"
    assert C.STATE["fan"] is True, "Auto stopped a fresh external run"
    assert not plug.commands, "Auto used the previous run's expired lease"


def test_auto_uses_current_off_state_without_a_redundant_stop():
    C, plug, now = _auto_during_relay_change("off")
    assert C.STATE["fan"] is False
    assert C.STATE["changed_at"] == now, "external stop timestamp was overwritten"
    assert not plug.commands, "Auto acted on stale cached fan state"


def test_auto_still_stops_when_the_current_lease_really_expired():
    C, plug, now = _auto_during_relay_change(None)
    assert C.STATE["fan"] is False
    assert plug.commands == [False]


if __name__ == "__main__":
    sys.exit(harness.run(dict(globals())))
