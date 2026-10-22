"""Clean container-stop checkpoints; isolated files, clocks and no hardware."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness


def setup():
    C = harness.load()
    clock = [10000.0, 500.0]
    C.time.time = lambda: clock[0]
    C.time.monotonic = lambda: clock[1]
    C._boot_id = lambda: 'original-boot'
    C.PERSIST_STOP_STATE = True
    C.STATE['latch']['co2'] = True
    C.STATE['ran_with'] = {'co2': True}
    for key, age in (('changed_at',10), ('min_run_at',8), ('reading_at',20)):
        C.STATE[key] = clock[0]-age
        C._elapsed_clocks[key] = (clock[0]-age, clock[1]-age)
    C._oob_since = clock[0]-15
    C._oob_monotonic = (C._oob_since, clock[1]-15)
    C._off_draw_since = clock[1]-12
    C._draw_grace = (C.STATE['changed_at'], clock[1]+10)
    return C, clock


def replacement(C, clock, boot='original-boot'):
    D = harness.load()
    D.PERSIST_STOP_STATE = True
    D.DATA_DIR, D.STOP_STATE_PATH = C.DATA_DIR, C.STOP_STATE_PATH
    D.CFG = copy.deepcopy(C.CFG)
    D.STATE['fault'] = copy.deepcopy(C.STATE['fault'])
    D._boot_id = lambda: boot
    return D


def test_clean_stop_restores_latch_and_elapsed_timers_once():
    C, clock = setup()
    assert C._save_stop_state()
    assert os.stat(C.STOP_STATE_PATH).st_mode & 0o777 == 0o600
    clock[0] += 300  # Wall clock step must not affect same-boot downtime.
    clock[1] += 5
    D = replacement(C, clock)
    assert D._load_stop_state()
    assert D.STATE['latch']['co2'] and D.STATE['ran_with'] == {'co2': True}
    assert D._elapsed_age_locked('min_run_at') == 13
    assert D._elapsed_age_locked('reading_at') == 25
    assert D.time.monotonic()-D._off_draw_since == 17
    assert D.time.monotonic()-D._oob_monotonic[1] == 20
    assert D._draw_grace[1]-D.time.monotonic() == 5
    assert D.STATE['fan'] is None and D.STATE['watts'] is None
    assert not os.path.exists(C.STOP_STATE_PATH)
    assert not D._load_stop_state(), 'A crash must not replay an old clean stop'


def test_host_reboot_rebases_clocks_without_renewing_lease_or_draw_timer():
    C, clock = setup()
    assert C._save_stop_state()
    clock[0] += 30
    clock[1] = 2  # A different host/kernel's monotonic epoch.
    D = replacement(C, clock, boot='new-boot')
    assert D._load_stop_state()
    # Step the wall clock BEFORE the first age lookup; a missing restore
    # must not be masked by its legacy wall-clock fallback caching an age.
    clock[0] -= 600
    clock[1] += 5
    assert D._elapsed_age_locked('min_run_at') == 43
    assert D._elapsed_age_locked('reading_at') == 55
    assert D.time.monotonic()-D._off_draw_since == 47
    assert D.time.monotonic()-D._oob_monotonic[1] == 50
    assert D._draw_grace[1] <= D.time.monotonic()


def test_fault_retry_budget_and_spacing_survive_container_replacement():
    C, clock = setup()
    fault = dict(at=clock[0]-30, code='FC-008', msg='Unable to stop', stopped=False,
                 relay_retry=dict(attempts=3, phase='wait'))
    C.STATE['fault'] = fault
    C.save_fault(fault)
    C._relay_retry_clock = (fault['at'], 3, clock[1]+45)
    assert C._save_stop_state()
    clock[0] += 12
    clock[1] = 2
    D = replacement(C, clock, 'new-boot')
    assert D._load_stop_state()
    assert D.STATE['fault']['relay_retry'] == dict(attempts=3, phase='wait')
    assert D._relay_retry_clock[2]-D.time.monotonic() == 33


def test_bad_stale_changed_or_ambiguous_checkpoints_start_cold():
    for damage in ('config', 'fault', 'large_gap', 'backwards', 'bad_latch', 'bad_clock', 'truncated'):
        C, clock = setup()
        assert C._save_stop_state()
        D = replacement(C, clock, 'new-boot')
        if damage == 'config': D.CFG['control']['on_at'] += 1
        if damage == 'fault': D.STATE['fault'] = {'msg': 'New standing fault'}
        if damage == 'large_gap': clock[0] += 86401
        if damage == 'backwards': clock[0] -= 1
        if damage in ('bad_latch', 'bad_clock'):
            p = Path(C.STOP_STATE_PATH)
            record = json.loads(p.read_text())
            if damage == 'bad_latch': record['payload']['latch']['co2'] = 'true'
            else: record['payload']['elapsed_clocks']['reading_at'] = [100, float('inf')]
            p.write_text(json.dumps(record))
        if damage == 'truncated': Path(C.STOP_STATE_PATH).write_text('{')
        assert not D._load_stop_state(), damage
        assert not D.STATE['latch']['co2'], damage
        assert not os.path.exists(C.STOP_STATE_PATH), damage


def test_essential_fault_save_precedes_checkpoint_and_shutdown_freezes_actuation():
    C, clock = setup()
    C.STATE['fault'] = dict(at=clock[0], msg='FC-003 draw fault', code='FC-003', stopped=False)
    C.STATE['fault_unsaved'] = True
    C._stopping.set()
    assert C._stale_client(None) and C._stale_client(C._cfg_generation)
    assert C._save_stop_state()
    assert C.load_fault()['code'] == 'FC-003' and not C.STATE['fault_unsaved']
    C.save_fault = lambda _: (_ for _ in ()).throw(OSError('disk full'))
    C.STATE['fault_unsaved'] = True
    os.unlink(C.STOP_STATE_PATH)
    assert not C._save_stop_state()
    assert not os.path.exists(C.STOP_STATE_PATH)


def test_native_install_default_paths_and_opt_in_data_directory():
    C = harness.load()
    assert C.DATA_DIR == C.DIR and not C.PERSIST_STOP_STATE
    with tempfile.TemporaryDirectory() as folder:
        previous = os.environ.get('FAN_CONTROLLER_DATA_DIR')
        try:
            os.environ['FAN_CONTROLLER_DATA_DIR'] = folder
            D = harness.load()
            for path in (D.CFG_PATH,D.FAULT_PATH,D.HIST_PATH,D.EVENTS_PATH,
                         D.DAILY_PATH,D.MANUAL_PENDING_PATH,D.RESTART_PATH,D.STOP_STATE_PATH):
                assert os.path.dirname(path) == folder
        finally:
            if previous is None: os.environ.pop('FAN_CONTROLLER_DATA_DIR',None)
            else: os.environ['FAN_CONTROLLER_DATA_DIR'] = previous


if __name__ == '__main__':
    sys.exit(harness.run(dict(globals())))
