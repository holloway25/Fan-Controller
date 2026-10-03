"""Polling cadence, no-hardware development interlock and safe diagnostics."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness


def run_loop(C, seconds=65, advance=None):
    clock = [1000.0]
    old_time, old_sleep = C.time.time, C.time.sleep
    def sleep(n):
        clock[0] += n
        if advance:
            advance(clock[0])
        if clock[0] >= 1000+seconds:
            raise StopIteration()
    C.time.time, C.time.sleep = lambda: clock[0], sleep
    try:
        C.watts_loop()
    except StopIteration:
        pass
    finally:
        C.time.time, C.time.sleep = old_time, old_sleep


class Meter:
    def __init__(self, C, fail_first=False):
        self.C, self.fail_first = C, fail_first
        self.relays, self.powers = [], []
    def state(self):
        self.relays.append(self.C.time.time())
        if self.fail_first and len(self.relays) == 1:
            raise self.C.PlugError('unsafe device message', reason='ReadTimeout', elapsed=12)
        return False
    def power_w(self):
        self.powers.append(self.C.time.time())
        return 0


def test_meter_five_seconds_relay_thirty_seconds_and_immediate_start():
    C = harness.load()
    p = Meter(C)
    C._client_snapshot = lambda: (None,p,C._cfg_generation)
    run_loop(C)
    assert p.relays == [1000,1030,1060]
    assert p.powers == list(range(1000,1065,5))


def test_changed_generation_refreshes_relay_immediately():
    C = harness.load()
    p = Meter(C)
    C._client_snapshot = lambda: (None,p,C._cfg_generation)
    def advance(now):
        if now == 1010:
            C._cfg_generation += 1
    run_loop(C,seconds=20,advance=advance)
    assert p.relays == [1000,1010]


def test_relay_failure_recovers_only_after_a_successful_relay_read():
    C = harness.load()
    p = Meter(C, fail_first=True)
    C._client_snapshot = lambda: (None,p,C._cfg_generation)
    run_loop(C,seconds=35)
    errors = [e for e in C.STATE['events'] if 'Draw loop error' in e['msg']]
    recovered = [e for e in C.STATE['events'] if 'Draw loop recovered' in e['msg']]
    assert len(errors) == len(recovered) == 1
    assert 'ReadTimeout, 12.00s' in errors[0]['msg']
    assert 'unsafe device message' not in errors[0]['msg']
    assert recovered[0]['at'] == 1030


def test_disabled_installation_has_no_background_plug_calls():
    C = harness.load(plug_access_disabled=True)
    assert C.PLUG_ACCESS_DISABLED
    assert C.STATE['plug_polling_enabled'] is False
    def forbidden(*args):
        raise AssertionError('Disabled installation contacted the plug')
    C._client_snapshot = forbidden
    run_loop(C,seconds=10)
    assert C.STATE['watts'] is None and C.STATE['fan'] is None
    class Plug:
        state = forbidden
    assert C._fan_status(Plug()) is None


def test_interlock_blocks_real_client_reads_tests_and_switches_before_http():
    C = harness.load(plug_access_disabled=True)
    old = C.requests.get
    def forbidden(*args,**kwargs):
        raise AssertionError('HTTP reached despite interlock')
    C.requests.get = forbidden
    try:
        p = C.Tasmota('192.0.2.10')
        for action in (p.state,p.power_w,lambda:p.set(True),lambda:p.set(False)):
            try:
                action()
            except C.PlugError as error:
                assert error.reason == 'local interlock'
            else:
                raise AssertionError('Interlock did not reject request')
    finally:
        C.requests.get = old


def test_transport_diagnostics_never_include_request_urls_or_credentials():
    C = harness.load()
    old = C.requests.get
    def fail(*args,**kwargs):
        raise C.requests.ReadTimeout('http://device/cm?password=CHANGEME')
    C.requests.get = fail
    try:
        try:
            C.Tasmota('192.0.2.10',password='CHANGEME').state()
        except C.PlugError as error:
            detail = C._plug_issue(error)
            assert 'ReadTimeout' in detail and 's)' in detail
            assert 'CHANGEME' not in detail and 'http' not in detail
        else:
            raise AssertionError('Timeout did not raise')
    finally:
        C.requests.get = old


def test_response_diagnostics_are_classified_not_copied_from_device():
    C = harness.load()
    old = C.requests.get
    class Reply:
        status_code = 200
        content = b'PRIVATE'
        value = {}
        def json(self):
            if isinstance(self.value,Exception):
                raise self.value
            return self.value
    reply = Reply()
    C.requests.get = lambda *args,**kwargs:reply
    try:
        cases = [(503,b'PRIVATE',{},'HTTP 503'),
                 (200,b'',{},'empty reply'),
                 (200,b'PRIVATE',ValueError('PRIVATE'),'non-JSON reply'),
                 (200,b'PRIVATE',[],'non-object JSON reply'),
                 (200,b'PRIVATE',{'WARNING':'PRIVATE'},'credentials rejected'),
                 (200,b'PRIVATE',{},'missing relay state')]
        for status,content,value,reason in cases:
            reply.status_code,reply.content,reply.value = status,content,value
            try:
                C.Tasmota('192.0.2.10').state()
            except C.PlugError as error:
                assert error.reason == reason
                assert 'PRIVATE' not in C._plug_issue(error)
            else:
                raise AssertionError('Invalid reply accepted')
    finally:
        C.requests.get = old


def test_external_stop_before_next_relay_poll_does_not_latch_draw_fault():
    C = harness.load()
    C.START_TIME = 0
    C.CFG['plug'].update(watts_enabled=True, watts_min=15,watts_max=25,
                         watts_fault_seconds=10)
    class ExternalStop(Meter):
        commands = []
        def state(self):
            self.relays.append(self.C.time.time())
            return self.C.time.time() < 1005
        def power_w(self):
            return 20 if self.C.time.time() < 1005 else 0
        def set(self,on):
            self.commands.append(on)
    p = ExternalStop(C)
    C._client_snapshot = lambda:(None,p,C._cfg_generation)
    C._notify = lambda *args,**kwargs:None
    run_loop(C,seconds=20)
    assert C.STATE['fault'] is None, 'External stop became a persistent draw fault'
    assert C.STATE['fan'] is False
    assert not p.commands
    assert p.relays == [1000,1015]
    assert any('changed outside' in e['msg'] for e in C.STATE['events'])


def test_interlocked_modes_still_read_sensor_and_preserve_stop_promise():
    for mode in ('manual','on','auto'):
        C = harness.load(plug_access_disabled=True)
        C.CFG['control']['mode'] = mode
        C.CFG['protect']['sensor_id'] = 'sensor'
        C.STATE['manual_off_pending'] = True
        C.promise_manual_off()
        class Sensor:
            calls = 0
            def readings(self,sid):
                self.calls += 1
                return {'co2':1500,'temperature':24,'humidity':50},'airQuality.co2'
        sensor = Sensor()
        C._client_snapshot = lambda:(sensor,C.Tasmota('192.0.2.10'),C._cfg_generation)
        C.tick()
        assert sensor.calls == 1
        assert C.STATE['co2'] == 1500 and C.STATE['reading_at'] is not None
        assert C.STATE['manual_off_pending'] is True
        assert C.manual_off_promised()
        assert C.STATE['fan'] is None


def test_running_or_unreachable_relay_still_latches_real_draw_fault():
    for reachable in (True,False):
        C = harness.load()
        C.CFG['control']['mode'] = 'auto'
        C.CFG['plug'].update(watts_enabled=True,watts_min=15,watts_max=25,
                             watts_fault_seconds=10)
        now = C.time.time()
        C.STATE.update(fan=True,watts=0,watts_at=now,changed_at=now-100)
        C._oob_since,C._oob_generation = now-20,C._cfg_generation
        C._notify = lambda *args,**kwargs:None
        class Plug:
            commands = []
            def state(self):
                free = C._act_lock.acquire(blocking=False)
                if free:
                    C._act_lock.release()
                assert not free,'Relay confirmation lost actuation locking'
                if not reachable:
                    raise C.PlugError('Unreachable')
                return True
            def set(self,on):
                self.commands.append(on)
        plug = Plug()
        C._draw_check(plug,C._cfg_generation)
        assert C.STATE['fault']['code'] == C.FC_UNDER_POWER
        assert plug.commands == ([False] if reachable else [])
        assert C.STATE['fault']['stopped'] is reachable


if __name__ == '__main__':
    sys.exit(harness.run(dict(globals())))
