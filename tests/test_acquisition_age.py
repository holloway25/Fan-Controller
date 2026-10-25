"""Read freshness includes relay I/O and time waiting to commit the result."""
import os
import sys
import threading
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness


def test_sensor_age_includes_relay_delay_even_when_wall_clock_steps():
    for step in (-600, 600):
        C = harness.load()
        wall, mono = [10000.0], [1000.0]
        class Sensor:
            def readings(self, _):
                return {'co2': 500}, 'co2'
        class Plug:
            reads = 0
            commands = []
            def state(self):
                self.reads += 1
                if self.reads == 1:
                    wall[0] += 12 + step
                    mono[0] += 12
                return False
            def set(self, on): self.commands.append(on)
        plug = Plug()
        C.CFG['control'].update(mode='auto', stale_minutes=1)
        C.CFG['protect']['sensor_id'] = 'test'
        C.STATE.update(fan=False)
        C._client_snapshot = lambda: (Sensor(), plug, C._cfg_generation)
        C._notify = lambda *args, **kwargs: None
        with patch.object(C.time, 'time', lambda: wall[0]), \
                patch.object(C.time, 'monotonic', lambda: mono[0]):
            C.tick()
            assert C.STATE['reading_at'] == 10000
            assert C._elapsed_clocks['reading_at'] == (10000, 1000)
            assert C.app.test_client().get('/api/state').get_json()['sensor_age'] == 12
            assert C._failsafe_cap(100) == 48, 'Relay delay postponed fail-safe eligibility'
            wall[0] += 48
            mono[0] += 48
            C._failsafe()
            assert plug.commands == [True], 'Fail-safe missed original acquisition deadline'


def test_power_age_includes_actuation_lock_wait_and_expires_in_api():
    C = harness.load()
    wall, mono = [10000.0], [1000.0]
    waiting = threading.Event()
    errors = []
    class ActLock:
        def __init__(self): self.lock = threading.Lock()
        def __enter__(self):
            waiting.set()
            self.lock.acquire()
        def __exit__(self, *args): self.lock.release()
    gate = ActLock()
    class Plug:
        def power_w(self): return 20
    C.CFG['control']['mode'] = 'auto'
    C.CFG['plug']['watts_enabled'] = False
    C._client_snapshot = lambda: (None, Plug(), C._cfg_generation)
    C._relay_fallback_needed = lambda *args: False
    C._retry_fault_persist = lambda: None
    C._retry_relay_notice = lambda: None
    def run():
        try:
            C.watts_loop()
        except BaseException as exc:
            errors.append(exc)
    gate.lock.acquire()
    worker = threading.Thread(target=run)
    try:
        with patch.object(C, '_act_lock', gate), \
                patch.object(C.time, 'time', lambda: wall[0]), \
                patch.object(C.time, 'monotonic', lambda: mono[0]), \
                patch.object(C.time, 'sleep', lambda _: C._stopping.set()):
            worker.start()
            assert waiting.wait(1), 'Meter never reached the commit lock'
            wall[0] -= 600
            mono[0] += 20
            gate.lock.release()
            worker.join(2)
            assert not worker.is_alive() and not errors, errors
            assert C.STATE['watts'] == 20
            assert C.STATE['watts_at'] == 10000
            assert C._elapsed_clocks['watts_at'] == (10000, 1000)
            assert C._elapsed_age_locked('watts_at') == 20
            state = C.app.test_client().get('/api/state').get_json()
            assert state['watts'] is None, 'Queued sample was shown beyond its stale limit'
    finally:
        C._stopping.set()
        if gate.lock.locked(): gate.lock.release()
        if worker.ident is not None: worker.join(2)


if __name__ == '__main__':
    sys.exit(harness.run(dict(globals())))
