"""OFF-fault email retries are independent of hardware retries."""
import copy
import json
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness
import test_alert_delivery as E
from test_off_draw import setup, check


def exhausted():
    C, clock, _, plug = setup()
    attempts = E.wire_failing(C)
    check(C, clock, plug)
    check(C, clock, plug, 30)
    for delay in (30, 30, 60, 30, 30):
        check(C, clock, plug, delay)
    assert len(attempts) == 3 and len(plug.commands) == 6
    assert C.STATE['fault']['relay_notice'] == dict(stage='exhausted', delivered=False)
    return C, clock, plug


def test_failed_terminal_email_retries_once_after_delay_without_hardware():
    C, clock, plug = exhausted()
    sent = E.wire(C)
    budget = copy.deepcopy(C.STATE['fault']['relay_retry'])
    clock[0] += 59
    C._retry_relay_notice()
    assert not sent
    clock[0] += 1
    C._retry_relay_notice()
    assert len(sent) == 1 and sent[0][0].startswith('FC-008')
    assert 'All six' in sent[0][1]
    assert C.load_fault()['relay_notice']['delivered']
    for _ in range(10):
        clock[0] += 60
        C._retry_relay_notice()
    assert len(sent) == 1 and len(plug.commands) == 6
    assert C.STATE['fault']['relay_retry'] == budget


def test_failed_recovery_email_retries_but_not_while_stop_is_superseded():
    C, clock, plug = exhausted()
    E.wire_failing(C)
    plug.watts = 0
    check(C, clock, plug, 5, watts=0)
    assert C.STATE['fault']['relay_notice']['stage'] == 'recovered'
    sent = E.wire(C)
    C._record_relay_observation(True)
    clock[0] += 60
    C._retry_relay_notice()
    assert not sent, 'Unconfirmed stop generated a recovery email'
    C._retry_fault_persist()
    check(C, clock, plug, 5, watts=0)
    C._retry_relay_notice()
    assert len(sent) == 1 and sent[0][0].startswith('FC-103')
    assert len(plug.commands) == 6 and C.STATE['fault']


def test_pending_notice_survives_cold_restart_without_replenishing_budget():
    C, clock, plug = exhausted()
    D = harness.load()
    D.CFG = copy.deepcopy(C.CFG)
    D.save_fault(C.load_fault())
    D.STATE['fault'] = D.load_fault()
    sent = E.wire(D)
    D._retry_relay_notice()
    assert len(sent) == 1 and sent[0][0].startswith('FC-008')
    assert 'All six' in sent[0][1]
    assert D.STATE['fault']['relay_notice']['stage'] == 'exhausted'
    assert D.STATE['fault']['relay_retry'] == dict(attempts=6, phase='exhausted')
    assert D.load_fault()['relay_notice']['delivered']
    fresh = harness.load()
    fresh.CFG = copy.deepcopy(D.CFG)
    fresh.save_fault(D.load_fault())
    fresh.STATE['fault'] = fresh.load_fault()
    sent_again = E.wire(fresh)
    fresh._retry_relay_notice()
    assert not sent_again and fresh.STATE['fault']['relay_notice']['delivered']


def test_inflight_and_old_callbacks_cannot_deliver_a_newer_stage_or_episode():
    C, clock, _, plug = setup()
    callbacks = []
    C._notify = lambda subject, body, on_complete=None: callbacks.append(on_complete)
    check(C, clock, plug)
    check(C, clock, plug, 30)
    C._retry_relay_notice()
    assert len(callbacks) == 1, 'Same notice queued twice while sending'
    check(C, clock, plug, 30)
    check(C, clock, plug, 30)
    assert len(callbacks) == 2
    callbacks[0](True)
    assert C.STATE['fault']['relay_notice'] == dict(stage='hard', delivered=False)
    old_at = C.STATE['fault']['at']
    replacement = copy.deepcopy(C.STATE['fault'])
    replacement['at'] = old_at + 1
    C._store_fault(replacement)
    callbacks[1](True)
    assert not C.STATE['fault']['relay_notice']['delivered']
    assert C.STATE['fault']['at'] == old_at + 1
    C._retry_relay_notice()
    assert len(callbacks) == 3
    C._clear_fault()
    callbacks[2](True)
    assert C.STATE['fault'] is None and not os.path.exists(C.FAULT_PATH)
    C._retry_relay_notice()
    assert len(callbacks) == 3


def test_disabled_email_retains_latest_notice_and_force_on_can_still_notify():
    C, clock, plug = exhausted()
    C.CFG['email']['enabled'] = False
    C.CFG['control']['mode'] = 'on'
    clock[0] += 60
    C._retry_relay_notice()
    assert not C.STATE['fault']['relay_notice']['delivered']
    sent = E.wire(C)
    C._retry_relay_notice()
    assert len(sent) == 1 and 'exhausted' in sent[0][1]
    assert len(plug.commands) == 6


def test_notification_marker_damage_fails_closed_and_write_failure_is_not_safety_failure():
    C, clock, plug = exhausted()
    original = C.load_fault()
    for invalid in (None, {}, {'stage':'bad','delivered':False},
                    {'stage':'hard','delivered':1}, {'stage':'hard','delivered':False,'extra':0}):
        damaged = dict(original, relay_notice=invalid)
        with open(C.FAULT_PATH, 'w') as stream:
            json.dump(damaged, stream)
        assert C.load_fault()['code'] == 'FC-206'
    C.save_fault(original)
    clock[0] += 60
    C.save_fault = lambda _: (_ for _ in ()).throw(OSError('simulated notification write failure'))
    sent = E.wire(C)
    C._retry_relay_notice()
    assert len(sent) == 1 and C.STATE['fault']['relay_notice']['delivered']
    assert not C.STATE['fault_unsaved']
    assert len(plug.commands) == 6


def test_real_meter_loop_retries_pending_mail_after_hardware_exhaustion():
    for interlocked in (False, True):
        C, clock, plug = exhausted()
        sent = E.wire(C)
        clock[0] += 60
        C.PLUG_ACCESS_DISABLED = interlocked
        C._clients = lambda: (None, plug)
        class Finished(Exception):
            pass
        def stop_loop(_):
            raise Finished()
        C.time.sleep = stop_loop
        try:
            C.watts_loop()
        except Finished:
            pass
        assert len(sent) == 1 and sent[0][0].startswith('FC-008')
        assert len(plug.commands) == 6


if __name__ == '__main__':
    sys.exit(harness.run(dict(globals())))
