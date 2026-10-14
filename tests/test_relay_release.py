"""Bounded physical OFF-draw retries; isolated clocks/devices/mail sinks."""
import copy
import json
import os
import sys
import threading
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_off_draw import setup, check, Plug
import harness


def raised():
    C, clock, mail, plug = setup()
    check(C, clock, plug)
    check(C, clock, plug, 30)
    assert plug.commands == [False]
    return C, clock, mail, plug


def test_auto_polls_never_command_around_fault_scheduler():
    for phase, count, unsaved in [('first',1,False), ('wait',3,False),
            ('exhausted',6,False), ('first',1,True)]:
        C, clock, mail, plug = raised()
        C.STATE['fault']['relay_retry'] = dict(attempts=count, phase=phase)
        C.STATE['fault_unsaved'] = unsaved
        plug.relay = True
        class Protect:
            def readings(self, _):
                return dict(co2=500, temperature=20, humidity=40), 'co2'
        C.CFG['protect']['sensor_id'] = 'test'
        C._clients = lambda: (Protect(), plug)
        before = list(plug.commands)
        for _ in range(3):
            C.tick()
        assert plug.commands == before, 'Auto bypassed fault scheduler'
        assert C.STATE['fault']['relay_retry'] == dict(attempts=count, phase=phase)
        assert C.STATE['fault_unsaved'] == unsaved


def test_external_on_revokes_recovered_stop_without_new_budget():
    C, clock, mail, plug = raised()
    plug.watts = 0
    check(C, clock, plug, 5, watts=0)
    assert C.STATE['fault']['stopped']
    retry = copy.deepcopy(C.STATE['fault']['relay_retry'])
    plug.relay = True
    C._refresh_relay(plug, C._cfg_generation)
    assert C.STATE['fan'] is True
    assert not C.STATE['fault']['stopped'] and C.STATE['fault_unsaved']
    assert C.STATE['fault']['relay_retry'] == retry
    before = list(plug.commands)
    assert C._enforce_stop(plug, C._cfg_generation) is False
    assert C._retry_fault_persist()
    assert C._enforce_stop(plug, C._cfg_generation) is False
    assert not C.load_fault()['stopped']
    assert C.STATE['fault']['relay_retry'] == retry and plug.commands == before


def test_two_groups_exact_spacing_and_terminal_exhaustion():
    C, clock, mail, plug = raised()
    for interval, count, phase in ((29,1,'first'),(1,2,'first'),
            (29,2,'first'),(1,3,'wait'),(59,3,'wait'),(1,4,'final'),
            (29,4,'final'),(1,5,'final'),(29,5,'final'),(1,6,'exhausted')):
        check(C, clock, plug, interval)
        assert len(plug.commands) == count, (interval, count, plug.commands)
        assert C.STATE['fault']['relay_retry'] == {'attempts':count,'phase':phase}
        assert C._valid_fault_record(json.load(open(C.FAULT_PATH)))
    assert C.STATE['fault']['code'] == 'FC-008'
    assert C.STATE['fault']['stopped'] is False
    assert len(mail) == 3
    assert [m[0].split()[0] for m in mail] == ['FC-007','FC-008','FC-008']
    assert 'first three' in mail[1][1] and 'All six' in mail[2][1]
    for _ in range(10):
        check(C, clock, plug, 30)
    assert len(plug.commands) == 6 and len(mail) == 3
    state = C.app.test_client().get('/api/state').get_json()
    assert state['fault_retry']['phase'] == 'exhausted'
    assert C.STATE['fault'] is not None  # Auto cannot resume by itself.


def test_recovery_mail_once_without_clearing_and_no_false_meter_confirmation():
    C, clock, mail, plug = raised()
    for watts in (None,23):
        plug.watts = watts
        check(C, clock, plug, 5, watts=watts)
        assert C.STATE['fault']['stopped'] is False
    plug.watts = 0
    check(C, clock, plug, 5, watts=0)
    assert C.STATE['fault']['stopped'] is True
    assert C.STATE['fault']['relay_retry']['phase'] == 'recovered'
    assert C.STATE['fault'] is not None and len(mail) == 2
    assert mail[-1][0].startswith('FC-103') and 'until acknowledged' in mail[-1][1]
    for _ in range(5):
        check(C, clock, plug, 30, watts=0)
    assert len(mail) == 2 and len(plug.commands) == 1
    # A fresh high reading invalidates confirmation, but cannot replenish tries.
    plug.watts = 23
    check(C, clock, plug, 30)
    assert not C.STATE['fault']['stopped']
    assert len(plug.commands) == 1
    C._retry_fault_persist()
    plug.watts = 0
    check(C, clock, plug, 30, watts=0)
    assert C.STATE['fault']['stopped']
    assert len(plug.commands) == 1 and len(mail) == 2


def test_force_on_override_preserves_fault_budget_and_blocks_recovery_notice():
    C, clock, mail, plug = raised()
    C.CFG['control']['mode'] = 'on'
    for _ in range(4):
        check(C, clock, plug, 30, watts=0)
        C._enforce_stop(plug, C._cfg_generation)
    assert len(plug.commands) == 1 and len(mail) == 1
    assert C.STATE['fault']['relay_retry']['attempts'] == 1
    assert not C.STATE['fault']['stopped']
    C.CFG['control']['mode'] = 'auto'
    check(C, clock, plug)
    assert len(plug.commands) == 2
    assert C.STATE['fault']['relay_retry']['attempts'] == 2
    assert C.STATE['fault'] is not None


def test_preserving_and_cold_restarts_never_replenish_budget():
    C, clock, mail, plug = raised()
    check(C, clock, plug, 30)
    check(C, clock, plug, 30)
    check(C, clock, plug, 20)
    snapshot = copy.deepcopy(C.STATE['fault'])
    payload = C._resume_payload()
    D = harness.load()
    D.CFG = copy.deepcopy(C.CFG)
    D.save_fault(snapshot)
    D.STATE['fault'] = D.load_fault()
    D.STATE.update(fan=False, watts=23)
    D._notify = lambda *a, **kw: None
    os.environ[D.RESUME_ENV] = json.dumps(payload)
    assert D._apply_resume()
    p = Plug(D)
    check(D, clock, p, 39)
    assert not p.commands
    check(D, clock, p, 1)
    assert p.commands == [False]
    assert D.STATE['fault']['relay_retry']['attempts'] == 4
    # A cold process grants a conservative delay, not new attempts.
    E = harness.load()
    E.CFG = copy.deepcopy(D.CFG)
    E.save_fault(copy.deepcopy(D.STATE['fault']))
    E.STATE['fault'] = E.load_fault()
    E._notify = lambda *a, **kw: None
    p = Plug(E)
    check(E, clock, p)
    check(E, clock, p, 29)
    assert not p.commands
    check(E, clock, p, 1)
    assert p.commands == [False] and E.STATE['fault']['relay_retry']['attempts'] == 5
    check(E, clock, p, 30)
    assert len(p.commands) == 2 and E.STATE['fault']['relay_retry']['phase'] == 'exhausted'
    check(E, clock, p, 300)
    assert len(p.commands) == 2


def test_attempt_is_saved_before_command_and_storage_failure_blocks_actuation():
    C, clock, mail, plug = raised()
    original = plug.set
    def verified(on):
        persisted = json.load(open(C.FAULT_PATH))
        assert persisted['relay_retry']['attempts'] == 2
        return original(on)
    plug.set = verified
    check(C, clock, plug, 30)
    assert len(plug.commands) == 2
    save = C.save_fault
    def failed(_):
        raise OSError('simulated storage failure')
    C.save_fault = failed
    check(C, clock, plug, 30)
    assert len(plug.commands) == 2 and C.STATE['fault_unsaved']
    check(C, clock, plug, 120)
    assert len(plug.commands) == 2
    C.save_fault = save
    C._retry_fault_persist()
    assert json.load(open(C.FAULT_PATH))['relay_retry']['attempts'] == 3


def test_retry_metadata_is_fail_closed_and_low_draw_after_exhaustion_recovers():
    C, clock, mail, plug = raised()
    for interval in (30,30,60,30,30):
        check(C, clock, plug, interval)
    valid = copy.deepcopy(C.STATE['fault'])
    for invalid in ({'attempts':7,'phase':'first'}, {'attempts':True,'phase':'first'},
                    {'attempts':3,'phase':'first'}, {'attempts':6,'phase':'wait'},
                    {'attempts':1,'phase':'first','unknown':1}):
        bad = copy.deepcopy(valid)
        bad['relay_retry'] = invalid
        C.save_fault(bad)
        assert C.load_fault()['code'] == 'FC-206'
    C.save_fault(valid)
    plug.watts = 0
    check(C, clock, plug, 30, watts=0)
    assert len(plug.commands) == 6 and len(mail) == 4
    assert C.STATE['fault']['stopped'] and C.STATE['fault']['code'] == 'FC-008'
    check(C, clock, plug, 30, watts=0)
    assert len(mail) == 4


def test_settings_saves_and_clock_steps_do_not_shorten_spacing():
    C, clock, mail, plug = raised()
    response = C.app.test_client().post('/api/config',json={'email':{'enabled':False}})
    assert response.status_code == 200
    C.time.time = lambda: clock[0] + 100000
    check(C, clock, plug, 29)
    assert len(plug.commands) == 1
    C.time.time = lambda: clock[0] - 600
    check(C, clock, plug, 1)
    assert len(plug.commands) == 2


def test_cached_low_draw_cannot_falsely_certify_current_load_or_extra_poll_after_recovery():
    C, clock, mail, plug = raised()
    check(C, clock, plug, 5, watts=0)  # Current meter reply is still 23 W.
    assert not C.STATE['fault']['stopped'] and len(mail) == 1
    plug.watts = 0
    check(C, clock, plug, 5, watts=0)
    assert C.STATE['fault']['stopped']
    def unnecessary():
        raise AssertionError('Recovered fault added an unnecessary relay read')
    plug.state = unnecessary
    check(C, clock, plug, 30, watts=0)
    assert len(plug.commands) == 1 and len(mail) == 2


def test_compact_activity_order_and_detailed_emails():
    C, clock, mail, plug = raised()
    events = [e['msg'] for e in reversed(C.STATE['events'])]
    assert events[-2:] == ['FC-007 OFF retry started','FC-007 OFF retry 1 of 6']
    assert 'Three attempts are allowed, 30 seconds apart.' in mail[0][1]
    for interval in (30,30,60,30,30):
        check(C, clock, plug, interval)
    events = [e['msg'] for e in reversed(C.STATE['events'])]
    assert 'FC-008 First 3 OFF attempts failed — wait 60s for next 3' in events
    assert events[-2:] == ['FC-008 OFF retry 6 of 6',
                           'FC-008 All 6 OFF retries failed — hard fault']
    assert 'Automatic OFF retries have stopped.' in mail[-1][1]
    plug.watts = 0
    check(C, clock, plug, 5, watts=0)
    assert C.STATE['events'][0]['msg'] == 'FC-103 Relay reports OFF — hard fault remains'
    assert 'fresh measured draw' in mail[-1][1]
    assert C.STATE['fault'] and C.STATE['fault']['stopped']


def test_old_activity_is_compact_in_api_without_rewriting_saved_rows():
    C, clock, mail, plug = setup()
    old = [
        'FC-007 OFF retry started. Three attempts are allowed, 30 seconds apart.',
        'FC-008 The first three OFF attempts did not confirm power removal. A hard fault is latched.',
        'FC-008 All six OFF retries are exhausted. Automatic OFF retries have stopped.',
        'FC-103 The relay reports OFF and fresh measured draw is within the OFF limit. Stop confirmed.',
        'FC-007 OFF retry 2 of 6', 'Fan on — 1002 ppm',
    ]
    C.STATE['events'] = [dict(at=clock[0],msg=m) for m in old]
    original = copy.deepcopy(C.STATE['events'])
    result = C.app.test_client().get('/api/state').get_json()['events']
    assert [e['msg'] for e in result] == [
        'FC-007 OFF retry started',
        'FC-008 First 3 OFF attempts failed — wait 60s for next 3',
        'FC-008 All 6 OFF retries failed — hard fault',
        'FC-103 Relay reports OFF — hard fault remains',
        *old[4:],
    ]
    assert C.STATE['events'] == original


def manual_failed(monitoring=True):
    C, clock, mail, plug = setup('manual')
    C.CFG['plug']['watts_enabled'] = monitoring
    C.STATE['manual_off_pending'] = True
    C.promise_manual_off()
    plug.relay, plug.reject = True, True
    try:
        C._manual_off(plug,C._cfg_generation)
    except C.PlugError:
        pass
    else:
        raise AssertionError('Initial failed OFF must retain its diagnostic error')
    assert C.STATE['fan'] is True
    assert C.STATE['fault'] is not None, 'Failed Manual OFF did not latch a fault'
    assert C.STATE['fault']['code'] == 'FC-009'
    assert C.STATE['fault']['relay_retry'] == {'attempts':0,'phase':'first'}
    assert C.manual_off_promised() and C.STATE['manual_off_pending']
    assert C._valid_fault_record(C.load_fault())
    assert plug.commands == [False]
    return C, clock, mail, plug


def test_failed_manual_stop_latches_even_with_healthy_watts_and_monitor_disabled():
    for monitoring in (True,False):
        C, clock, mail, plug = manual_failed(monitoring)
        for delay in (0,5,5,5,5,9):
            try:
                C._manual_off(plug,C._cfg_generation)
            except C.PlugError:
                raise AssertionError('Manual promise bypassed fault retry scheduling')
            check(C,clock,plug,delay)
            assert plug.commands == [False]
        check(C,clock,plug,1)
        assert plug.commands == [False,False]
        assert len(mail)==1 and mail[0][0].startswith('FC-009')
        assert C.STATE['fault']['relay_retry']['attempts']==1
        for delay in (30,30,60,30,30):
            C._manual_off(plug,C._cfg_generation)
            check(C,clock,plug,delay)
        assert len(plug.commands)==7  # Original requested stop plus six retries.
        assert C.STATE['fault']['code']=='FC-008'
        assert C.STATE['fault']['relay_retry']['phase']=='exhausted'
        assert len(mail)==3 and all(m[0].startswith('FC-008') for m in mail[1:])
        for _ in range(5):
            C._manual_off(plug,C._cfg_generation)
            check(C,clock,plug,30)
        assert len(plug.commands)==7 and len(mail)==3
        assert C.STATE['manual_off_pending'] and C.manual_off_promised()


def test_manual_promise_budget_survives_cold_restart_and_force_override():
    C, clock, mail, plug = manual_failed()
    C.CFG['control']['mode']='on'
    C._manual_off(plug,C._cfg_generation)
    check(C,clock,plug,30)
    assert len(plug.commands)==1 and not mail
    assert C.STATE['fault']['code']=='FC-009'
    C.CFG['control']['mode']='manual'
    check(C,clock,plug)
    assert len(plug.commands)==2 and len(mail)==1
    snapshot=copy.deepcopy(C.STATE['fault'])
    D=harness.load()
    D.CFG=copy.deepcopy(C.CFG)
    D.save_fault(snapshot)
    D.STATE['fault']=D.load_fault()
    D.STATE['manual_off_pending']=True
    D.promise_manual_off()
    D._notify=lambda *a,**kw: None
    p=Plug(D,relay=True,watts=23,reject=True)
    D._manual_off(p,D._cfg_generation)
    check(D,clock,p)
    assert not p.commands
    check(D,clock,p,30)
    assert p.commands==[False]
    assert D.STATE['fault']['relay_retry']['attempts']==2


def test_manual_recovery_requires_low_draw_and_discharges_promise_without_clearing_fault():
    C, clock, mail, plug = manual_failed()
    check(C,clock,plug,30)
    plug.reject,plug.relay=False,False
    check(C,clock,plug,5)
    assert not C.STATE['fault']['stopped']  # OFF with 23 W is not recovery.
    assert C.manual_off_promised()
    plug.watts=0
    check(C,clock,plug,5,watts=0)
    assert C.STATE['fault']['stopped'] and C.STATE['fault']['code']=='FC-009'
    assert not C.STATE['manual_off_pending'] and not C.manual_off_promised()
    assert len(mail)==2 and mail[-1][0].startswith('FC-103')
    C._manual_off(plug,C._cfg_generation)
    check(C,clock,plug,60,watts=0)
    assert len(plug.commands)==2 and len(mail)==2
    # Auto is still blocked by the standing fault, despite a healthy recovery.
    c=copy.deepcopy(C.CFG['control'])
    c['mode']='auto'
    wanted,_=C.decide(2000,20,False,c,100,{},fault=C.STATE['fault'])
    assert wanted is False


def pending_manual_recovery():
    C, clock, mail, plug = manual_failed()
    C.STATE['ran_with'] = {'co2'}
    C.STATE['min_run_at'] = clock[0]
    plug.reject, plug.relay, plug.watts = False, False, 0
    save = C.save_fault
    def disk_full(_):
        raise OSError('Simulated full disk')
    C.save_fault = disk_full
    check(C, clock, plug, 5, watts=0)
    assert C.STATE['fault']['stopped'] and C.STATE['fault_unsaved']
    assert C.STATE['fault']['relay_retry']['phase'] == 'recovered'
    assert C.STATE['manual_off_pending'] and C.manual_off_promised()
    assert not C._retry_fault_persist()
    assert C.STATE['manual_off_pending'] and C.manual_off_promised()
    C.save_fault = save
    return C, clock, mail, plug


def test_manual_recovery_persistence_retry_finishes_stop_before_acknowledgement():
    C, clock, mail, plug = pending_manual_recovery()
    assert C._retry_fault_persist()
    assert not C.STATE['fault_unsaved']
    assert C.load_fault()['stopped'] and C.STATE['fault']['code'] == 'FC-009'
    assert not C.STATE['manual_off_pending'] and not C.manual_off_promised()
    assert not C.STATE['ran_with']
    assert C.STATE['min_run_at'] is None
    # The recovered fast path and subsequent acknowledgement owe no new OFF.
    before = list(plug.commands)
    assert C._enforce_stop(plug, C._cfg_generation)
    response = C.app.test_client().post('/api/fault/clear', json={})
    assert response.status_code == 200 and response.get_json()['cleared']
    assert C.STATE['fault'] is None
    plug.relay = True
    C._record_relay_observation(True)
    C._manual_off(plug, C._cfg_generation)
    assert plug.relay is True and plug.commands == before


def test_shutdown_saved_manual_recovery_is_not_owed_after_cold_start():
    C, clock, mail, plug = pending_manual_recovery()
    assert C._persist_fault_on_exit()
    assert not C.STATE['manual_off_pending'] and not C.manual_off_promised()
    assert not C.STATE['ran_with'] and C.STATE['min_run_at'] is None
    assert not C._retry_fault_persist()
    D = harness.load()
    D.CFG = copy.deepcopy(C.CFG)
    D.MANUAL_PENDING_PATH = C.MANUAL_PENDING_PATH
    D.save_fault(C.load_fault())
    D.STATE['fault'] = D.load_fault()
    D._notify = lambda *args, **kwargs: None
    D._restore_manual_stop_promise()
    assert not D.STATE['manual_off_pending']
    response = D.app.test_client().post('/api/fault/clear', json={})
    assert response.status_code == 200 and response.get_json()['cleared']
    p = Plug(D, relay=True, watts=23)
    D._record_relay_observation(True)
    D._manual_off(p, D._cfg_generation)
    assert p.relay is True and not p.commands


def test_recovery_cleanup_cannot_remove_a_concurrent_new_manual_marker():
    for shutdown in (False, True):
        C, clock, mail, plug = pending_manual_recovery()
        C.CFG['control']['mode'] = 'auto'
        cleaning, release, saving, promised = (threading.Event() for _ in range(4))
        clear, promise = C.clear_manual_off_promise, C.promise_manual_off
        results = []
        def paused_cleanup():
            cleaning.set()
            assert release.wait(3)
            clear()
        def watched_promise():
            promised.set()
            promise()
        def recover():
            results.append(C._persist_fault_on_exit() if shutdown else C._retry_fault_persist())
        def save_manual():
            saving.set()
            results.append(C.app.test_client().post('/api/config',
                json={'control': {'mode': 'manual'}}).status_code)
        C.clear_manual_off_promise = paused_cleanup
        C.promise_manual_off = watched_promise
        recovery = threading.Thread(target=recover)
        saver = threading.Thread(target=save_manual)
        recovery.start()
        try:
            assert cleaning.wait(3)
            saver.start()
            assert saving.wait(3)
            assert not promised.wait(0.1), 'New marker created during old recovery cleanup'
        finally:
            release.set()
            recovery.join(4)
            if saver.ident is not None:
                saver.join(4)
        assert not recovery.is_alive() and not saver.is_alive()
        assert results == [True, 200]
        assert C.CFG['control']['mode'] == 'manual'
        assert C.STATE['manual_off_pending'] and C.manual_off_promised()
        # A cold start must retain this NEW request, not the completed one.
        D = harness.load()
        D.CFG = copy.deepcopy(C.CFG)
        D.MANUAL_PENDING_PATH = C.MANUAL_PENDING_PATH
        D._restore_manual_stop_promise()
        assert D.STATE['manual_off_pending']


def test_new_manual_save_finishes_older_recovery_before_creating_promise():
    C, clock, mail, plug = pending_manual_recovery()
    C.CFG['control']['mode'] = 'auto'
    save = C.save_fault
    def disk_full(_):
        raise OSError('Simulated full disk')
    C.save_fault = disk_full
    response = C.app.test_client().post('/api/config', json={'control': {'mode': 'manual'}})
    assert response.status_code == 503 and C.CFG['control']['mode'] == 'auto'
    assert C.STATE['fault_unsaved'] and C.manual_off_promised()
    C.save_fault = save
    response = C.app.test_client().post('/api/config', json={'control': {'mode': 'manual'}})
    assert response.status_code == 200
    assert not C.STATE['fault_unsaved'] and C.STATE['fault']['stopped']
    assert C.STATE['manual_off_pending'] and C.manual_off_promised()
    assert not C._retry_fault_persist()
    assert C.STATE['manual_off_pending'] and C.manual_off_promised()


def test_manual_marker_is_created_inside_actuation_transaction():
    C, clock, mail, plug = setup('auto')
    promise, save = C.promise_manual_off, C.save_cfg
    seen = []
    def locked():
        free = C._act_lock.acquire(blocking=False)
        if free:
            C._act_lock.release()
        assert not free, 'Manual promise/config persisted without actuation lock'
        seen.append(True)
    def watched_promise():
        locked()
        promise()
    def watched_save(config):
        locked()
        save(config)
    C.promise_manual_off, C.save_cfg = watched_promise, watched_save
    result = C.app.test_client().post('/api/config', json={'control': {'mode': 'manual'}})
    assert result.status_code == 200 and seen == [True, True]
    assert C.STATE['manual_off_pending'] and C.manual_off_promised()


def test_busy_shutdown_saves_recovery_then_reconciles_exact_promise_on_cold_start():
    for newer in (False, True):
        C, clock, mail, plug = pending_manual_recovery()
        original_id = C._manual_stop_id()
        C._act_lock.acquire()
        try:
            assert C._persist_fault_on_exit()
            assert C.load_fault()['manual_stop_id'] == original_id
            assert not C.STATE['fault_unsaved'], 'The essential safety record was saved'
            assert C.STATE['manual_off_pending'] and C.manual_off_promised()
        finally:
            C._act_lock.release()
        if newer:
            C.promise_manual_off()  # New identity even with identical wall time.
            assert C._manual_stop_id() != original_id
        D = harness.load()
        D.CFG = copy.deepcopy(C.CFG)
        D.MANUAL_PENDING_PATH = C.MANUAL_PENDING_PATH
        D.save_fault(C.load_fault())
        D.STATE['fault'] = D.load_fault()
        D._restore_manual_stop_promise()
        assert D.STATE['fault'] is not None and D.STATE['fault']['stopped']
        assert D.STATE['manual_off_pending'] is newer
        assert D.manual_off_promised() is newer


def test_busy_shutdown_deferred_recovery_cleanup_retries_without_clearing_fault():
    C, clock, mail, plug = pending_manual_recovery()
    C._act_lock.acquire()
    try:
        assert C._persist_fault_on_exit()
    finally:
        C._act_lock.release()
    assert C.STATE['manual_off_pending'] and not C.STATE['fault_unsaved']
    def no_rewrite(_):
        raise AssertionError('Deferred cleanup rewrote an already durable safety fault')
    C.save_fault = no_rewrite
    assert C._retry_fault_persist()
    assert not C.STATE['manual_off_pending'] and not C.manual_off_promised()
    assert C.STATE['fault']['stopped'] and not C.STATE['fault_unsaved']


def test_recovery_marker_identity_validation_is_fail_closed():
    C, clock, mail, plug = pending_manual_recovery()
    record = copy.deepcopy(C.STATE['fault'])
    assert C._valid_fault_record(record)
    for invalid in (None, True, 123, '', 'a'*63, 'a'*65, 'z'*64):
        record['manual_stop_id'] = invalid
        assert not C._valid_fault_record(record), invalid


def test_unreachable_manual_stop_does_not_claim_a_mechanical_fault():
    C, clock, mail, plug = setup('manual')
    C.STATE['manual_off_pending']=True
    C.promise_manual_off()
    def unreachable():
        raise C.PlugError('Simulated timeout')
    plug.state=unreachable
    plug.reject=True
    try:
        C._manual_off(plug,C._cfg_generation)
    except C.PlugError:
        pass
    fault=C.STATE['fault']
    assert fault['code']=='FC-009' and 'unknown' in fault['msg']
    assert not fault['stopped'] and 'welded' not in fault['msg']
    assert C.STATE['manual_off_pending']
    check(C,clock,plug,30)
    assert plug.commands==[False,False], 'Unknown read prevented bounded OFF command'
    assert len(mail)==1 and mail[0][0].startswith('FC-009')


def test_initial_manual_retry_wait_survives_preserving_and_cold_restarts():
    C, clock, mail, plug = manual_failed()
    snapshot=copy.deepcopy(C.STATE['fault'])
    payload=C._resume_payload()
    clock[0]+=10
    D=harness.load()
    D.CFG=copy.deepcopy(C.CFG)
    D.save_fault(snapshot)
    D.STATE['fault']=D.load_fault()
    D.STATE['manual_off_pending']=True
    D.promise_manual_off()
    D._notify=lambda *a,**kw: None
    os.environ[D.RESUME_ENV]=json.dumps(payload)
    assert D._apply_resume()
    p=Plug(D,relay=True,watts=23,reject=True)
    D._manual_off(p,D._cfg_generation)
    check(D,clock,p,19)
    assert not p.commands
    check(D,clock,p,1)
    assert p.commands==[False]
    E=harness.load()
    E.CFG=copy.deepcopy(C.CFG)
    E.save_fault(snapshot)
    E.STATE['fault']=E.load_fault()
    E._notify=lambda *a,**kw: None
    p=Plug(E,relay=True,watts=23,reject=True)
    check(E,clock,p)
    check(E,clock,p,29)
    assert not p.commands
    check(E,clock,p,1)
    assert p.commands==[False] and E.STATE['fault']['relay_retry']['attempts']==1


if __name__ == '__main__':
    sys.exit(harness.run(dict(globals())))
