"""A rejected fail-safe ON must not suppress or misdescribe the sensor alert."""
import os
import sys
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
import harness
import test_alert_delivery as E


class Plug:
    def __init__(self,C,unreachable=False):
        self.C=C
        self.unreachable=unreachable
        self.fail=True
        self.on=False
        self.commands=[]
    def state(self):
        if self.unreachable:
            raise self.C.PlugError('Simulated read timeout')
        return self.on
    def set(self,on):
        self.commands.append(on)
        if self.fail:
            raise self.C.PlugError('Simulated rejected ON')
        self.on=on
        return on
    def power_w(self):
        return 23 if self.on else 0


def setup(unreachable=False,mail_failure=False):
    C=harness.load()
    sent=(E.wire_failing if mail_failure else E.wire)(C)
    C.CFG['control'].update(mode='auto',stale_minutes=1)
    C.STATE.update(fan=True,reading_at=C.time.time()-3600,changed_at=C.time.time()-120)
    p=Plug(C,unreachable)
    C._clients=lambda:(None,p)
    return C,p,sent


def failed(C):
    try:
        C._failsafe()
    except C.PlugError as exc:
        assert 'rejected ON' in str(exc)
    else:
        raise AssertionError('Failed ON did not retain its diagnostic exception')


def test_failed_on_still_alerts_truthfully_without_renewing_start_timing():
    for unreachable in (False,True):
        C,p,sent=setup(unreachable)
        old=C.STATE['changed_at']
        remember=C._remember_relay
        C._remember_relay=lambda *a: (_ for _ in ()).throw(AssertionError('Failed ON cached as successful'))
        failed(C)
        C._remember_relay=remember
        assert len(sent)==1,'Rejected ON suppressed sensor-outage email'
        subject,body=sent[0]
        assert subject.startswith('FC-001') and 'ON unconfirmed' in subject
        assert 'Ventilation is NOT confirmed' in body
        assert 'check plug and fan' in body and 'ON attempted, NOT confirmed' in body
        assert 'switched on as a precaution' not in body and 'already running' not in body
        assert C.STATE['changed_at']==old,'Failed ON renewed physical start timing'
        if not unreachable:
            assert C.STATE['fan'] is False,'Confirmed OFF observation was lost'
        assert p.commands==[True]
        assert not any('Fan on — no reading' in e['msg'] for e in C.STATE['events'])
        assert C._stale_delivered and not C._stale_inflight


def test_failed_on_alert_still_runs_outside_actuation_lock_and_only_once_per_outage():
    C,p,sent=setup()
    original=C._notify
    def check_lock(*args,**kwargs):
        assert not C._act_lock.locked(),'Email queued under actuation lock'
        return original(*args,**kwargs)
    C._notify=check_lock
    for _ in range(3):
        failed(C)
    assert len(sent)==1 and p.commands==[True,True,True]
    p.fail=False
    C._failsafe()
    assert C.STATE['fan'] is True and len(sent)==1
    assert p.commands==[True,True,True,True]


def test_failed_mail_retries_even_when_failsafe_on_also_fails():
    C,p,attempts=setup(mail_failure=True)
    failed(C)
    failed(C)
    assert len(attempts)==2
    assert not C._stale_delivered and not C._stale_inflight


def test_sensor_recovery_closes_failed_on_outage_without_claiming_fan_recovery():
    C,p,sent=setup()
    failed(C)
    class Protect:
        def readings(self,sid):
            return {'co2':500,'temperature':20,'humidity':40},'co2'
    C.CFG['protect']['sensor_id']='test-sensor'
    C._clients=lambda:(Protect(),p)
    C.tick()
    assert len(sent)==2 and sent[-1][0].startswith('FC-100')
    assert 'Readings are arriving again' in sent[-1][1]
    assert 'fan has been switched on' not in sent[-1][1]
    assert not C._stale_delivered and C._stale_episode is None
    assert C.STATE['fan'] is False
    C.STATE['reading_at']=C.time.time()-3600
    failed(C)
    assert len(sent)==3 and sent[-1][0].startswith('FC-001')


def test_no_failed_on_alert_when_failsafe_is_disabled_or_blocked():
    for reason in ('manual','force','disabled','fault','fresh'):
        C,p,sent=setup()
        if reason=='manual':C.CFG['control']['mode']='manual'
        if reason=='force':C.CFG['control']['mode']='on'
        if reason=='disabled':C.CFG['control']['stale_minutes']=0
        if reason=='fault':C.STATE['fault']={'at':C.time.time(),'msg':'Existing fault','stopped':False}
        if reason=='fresh':C.STATE['reading_at']=C.time.time()
        C._failsafe()
        assert not sent and not p.commands,reason


if __name__=='__main__':
    sys.exit(harness.run(dict(globals())))
