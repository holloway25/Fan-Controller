"""Mid-run trigger changes explain continued running without extra actuation."""
import os
import sys
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
import harness


def setup():
    C = harness.load()
    C.CFG['control'].update(mode='auto',co2_enabled=True,temp_enabled=True,
                            humidity_enabled=True,on_at=1000,off_at=750,
                            temp_on=26,temp_off=24,humidity_on=70,humidity_off=60)
    C.CFG['protect']['sensor_id'] = 'sensor'
    lease = C.time.time()-100
    C.STATE.update(fan=True,changed_at=lease,latch={'co2':True,'temp':False,'humidity':False},
                   ran_with={'co2':True})
    class Sensor:
        values = {'co2':900,'temperature':20,'humidity':65}
        def readings(self,sid):
            return dict(self.values),'airQuality.co2'
    class Plug:
        on = True
        def __init__(self):
            self.commands = []
        def state(self):
            return self.on
        def set(self,on):
            self.commands.append(on)
            self.on = on
    sensor,plug = Sensor(),Plug()
    C._client_snapshot = lambda:(sensor,plug,C._cfg_generation)
    return C,sensor,plug,lease


def notes(C):
    return [e['msg'] for e in reversed(C.STATE['events']) if e['msg'].startswith('Fan on — ')]


def test_humidity_joining_is_logged_once_without_command_or_lease_reset():
    C,s,p,lease = setup()
    C.tick()
    assert not notes(C)
    s.values['humidity'] = 70.05
    C.tick()
    assert notes(C) == ['Fan on — 70% RH']
    s.values['humidity'] = 74
    C.tick()
    assert len(notes(C)) == 1
    assert not p.commands
    assert C.STATE['changed_at'] == lease
    assert C.STATE['ran_with'] == {'co2':True,'humidity':True}


def test_reactivated_trigger_is_logged_again_not_lost_in_run_accumulator():
    C,s,p,lease = setup()
    for rh in (70,60,71):
        s.values['humidity'] = rh
        C.tick()
    assert notes(C) == ['Fan on — 70% RH',
                        'Fan on — 71% RH']
    assert not p.commands and C.STATE['changed_at'] == lease


def test_simultaneous_new_triggers_share_one_activity_entry():
    C,s,p,lease = setup()
    s.values.update(temperature=27.2,humidity=71)
    C.tick()
    assert notes(C) == ['Fan on — 27.2 C and 71% RH']
    assert not p.commands


def test_start_entry_already_names_triggers_without_extra_staying_on_entry():
    C,s,p,lease = setup()
    C.STATE.update(fan=False,latch={'co2':False,'temp':False,'humidity':False},ran_with={})
    p.on = False
    s.values.update(co2=1002,humidity=70)
    C.tick()
    assert notes(C) == ['Fan on — 1002 ppm and 70% RH']
    assert p.commands == [True]
    assert any(e['msg'] == 'Fan on — 1002 ppm and 70% RH' for e in C.STATE['events'])
    C.tick()
    assert notes(C) == ['Fan on — 1002 ppm and 70% RH'] and p.commands == [True]


def test_disabled_or_missing_metrics_do_not_invent_activation_events():
    C,s,p,lease = setup()
    C.CFG['control']['humidity_enabled'] = False
    s.values.update(humidity=90,temperature=None)
    try:
        C.tick()
    except C.ProtectError as exc:
        assert 'temp' in str(exc)
    else:
        raise AssertionError('Missing enabled temperature accepted')
    assert not notes(C) and not p.commands


def test_manual_and_force_on_do_not_claim_auto_trigger_activations():
    for mode in ('manual','on'):
        C,s,p,lease = setup()
        C.CFG['control']['mode'] = mode
        s.values.update(temperature=30,humidity=80)
        C.tick()
        assert not notes(C) and not p.commands


if __name__ == '__main__':
    sys.exit(harness.run(dict(globals())))
