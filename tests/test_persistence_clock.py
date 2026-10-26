"""History and daily batch cadence follows elapsed time, including at boot."""
import json
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness


def test_history_and_daily_flush_on_elapsed_cadence_after_clock_rollback():
    C = harness.load()
    wall, mono = [10000.0], [0.0]
    class Sensor:
        def readings(self, _): return {'co2': 500}, 'co2'
    class Plug:
        def state(self): return False
    C.CFG['protect']['sensor_id'] = 'test'
    C.CFG['control'].update(mode='auto', stale_minutes=0)
    C._client_snapshot = lambda: (Sensor(), Plug(), C._cfg_generation)
    with patch.object(C.time, 'time', lambda: wall[0]), \
            patch.object(C.time, 'monotonic', lambda: mono[0]):
        C.tick()
        assert len(json.load(open(C.HIST_PATH))) == 1, 'First flush skipped at early uptime'
        assert json.load(open(C.DAILY_PATH))[0]['n'] == 1
        wall[0] += 299 - 600
        mono[0] += 299
        C.tick()
        assert len(json.load(open(C.HIST_PATH))) == 1
        assert json.load(open(C.DAILY_PATH))[0]['n'] == 1
        wall[0] += 1
        mono[0] += 1
        C.tick()
        hist = json.load(open(C.HIST_PATH))
        assert len(hist) == 3, 'Clock rollback postponed history flush'
        assert json.load(open(C.DAILY_PATH))[0]['n'] == 3, 'Daily flush postponed'
        assert [p['t'] for p in hist] == [9699, 9700, 10000], 'Sample wall times changed'


def test_forced_flush_and_failed_write_cadence_remain_bounded():
    C = harness.load()
    wall, mono, writes = [10000.0], [0.0], []
    def fail(hist):
        writes.append(list(hist))
        return False
    with patch.object(C.time, 'time', lambda: wall[0]), \
            patch.object(C.time, 'monotonic', lambda: mono[0]), \
            patch.object(C, 'save_hist', fail):
        assert C.maybe_save_hist([]) is False
        assert len(writes) == 1
        wall[0] += 600
        mono[0] += 10
        assert C.maybe_save_hist([]) is False
        assert len(writes) == 1, 'Failed disk retried before elapsed interval'
        assert C.maybe_save_hist([], force=True) is False
        assert len(writes) == 2, 'Shutdown force did not bypass cadence'
        mono[0] += 300
        assert C.maybe_save_hist([]) is False
        assert len(writes) == 3


if __name__ == '__main__':
    sys.exit(harness.run(dict(globals())))
