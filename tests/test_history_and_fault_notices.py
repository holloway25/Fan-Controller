"""Damaged disk data and failed stops must not mislead the operator."""
import json
import os
import sys
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
import harness
import test_alert_delivery as E


def test_saved_history_validates_every_known_metric_and_preserves_good_rows():
    C = harness.load()
    now = C.time.time()
    good = {'t':now,'v':800,'tc':21.5,'rh':50,'pm':None}
    legacy = {'t':now-30,'v':790}
    bad = [dict(good,**{key:value}) for key in ('v','tc','rh','pm')
           for value in ('bad',True,False,[],{},float('nan'),float('inf'),10**400)]
    bad.append(dict(good,t=10**400))
    with open(C.HIST_PATH,'w') as stream:
        json.dump(bad+[legacy,good],stream)
    assert C.load_hist() == [legacy,good]
    assert json.dumps(C.load_hist(),allow_nan=False)


def test_unsaved_fault_with_failed_stop_does_not_claim_success():
    C = harness.load()
    sent = E.wire(C)
    C.CFG['control']['mode'] = 'auto'
    C.CFG['plug'].update(watts_enabled=True,watts_min=15,watts_max=25,watts_fault_seconds=10)
    now = C.time.time()
    C.STATE.update(fan=True,watts=0,watts_at=now,changed_at=now-100)
    C._oob_since,C._oob_generation = now-20,C._cfg_generation
    def full_disk(value):
        raise OSError('simulated full disk')
    C.save_fault = full_disk
    class Plug:
        def state(self):
            return True
        def set(self,on):
            raise C.PlugError('simulated stop failure')
    C._draw_check(Plug(),C._cfg_generation)
    assert C.STATE['fan'] is True and C.STATE['fault']['stopped'] is False
    body = next(body for subject,body in sent if subject.startswith(C.FC_FAULT_UNSAVED))
    assert 'not currently confirmed' in body and 'may still be running' in body
    assert 'Fan stopped' not in body and 'fan is stopped' not in body


def test_restart_notice_explains_force_on_instead_of_claiming_stopped():
    C = harness.load()
    sent = E.wire(C)
    C.CFG['control']['mode'] = 'on'
    C.STATE.update(fan=True,fault={'at':C.time.time()-7200,'msg':'FC-003 Draw fault',
                                 'code':C.FC_UNDER_POWER,'stopped':True,'delivered_at':None})
    C._notify_restart_fault()
    body = next(body for subject,body in sent if subject.startswith(C.FC_RESTART_FAULT))
    assert 'Force on overrides' in body and 'fan may run' in body
    assert 'Fan kept stopped' not in body and 'fan stays stopped' not in body


def test_notice_only_confirms_a_currently_observed_stop():
    C = harness.load()
    f = {'stopped':True}
    assert 'confirmed off at the last check' in C._fault_stop_status(f,'auto',False)[0]
    for fan in (True,None):
        assert 'not currently confirmed' in C._fault_stop_status(f,'auto',fan)[0]
    C.PLUG_ACCESS_DISABLED = True
    assert 'no stop command can be sent' in C._fault_stop_status(f,'manual',False)[0]


if __name__ == '__main__':
    sys.exit(harness.run(dict(globals())))
