"""Polling cadence, no-hardware development interlock and safe diagnostics."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness


def run_loop(C, seconds=65, advance=None):
    clock = [1000.0]
    old_time, old_sleep, old_mono = C.time.time, C.time.sleep, C.time.monotonic
    def sleep(n):
        clock[0] += n
        if advance:
            advance(clock[0])
        if clock[0] >= 1000+seconds:
            raise StopIteration()
    C.time.time, C.time.sleep = lambda: clock[0], sleep
    C.time.monotonic = lambda: clock[0]
    try:
        C.watts_loop()
    except StopIteration:
        pass
    finally:
        C.time.time, C.time.sleep = old_time, old_sleep
        C.time.monotonic = old_mono


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


def test_normal_checks_are_fresh_and_force_on_uses_original_five_second_cadence():
    for mode in ('auto', 'manual', 'on'):
        C = harness.load()
        C.CFG['control']['mode'] = mode
        C.CFG['protect']['sensor_id'] = 'sensor'
        class Sensor:
            def readings(self, sid):
                return {'co2': 1200, 'temperature': 20, 'humidity': 50}, 'co2'
        class Running(Meter):
            def state(self):
                self.relays.append(self.C.time.time())
                return True
            def power_w(self):
                self.powers.append(self.C.time.time())
                return 23
            def set(self, on):
                raise AssertionError('Stable running fan was commanded')
        p = Running(C)
        C.STATE.update(fan=True, changed_at=990, manual_off_pending=False)
        C._client_snapshot = lambda: (Sensor(), p, C._cfg_generation)
        def sensor_poll(now):
            if now in (1010,1040):
                C.tick()
        run_loop(C, advance=sensor_poll)
        expected = (sorted(list(range(1000,1065,5))+[1010,1010,1040,1040])
                    if mode == 'on' else [1000,1010,1040])
        assert p.relays == expected, (mode,p.relays)
        assert len(p.powers) == 13, (mode,p.powers)


def test_force_on_checks_every_five_seconds_without_duplicate_start():
    C = harness.load()
    C.CFG['control']['mode'] = 'on'
    class Switching(Meter):
        on = False
        commands = 0
        def state(self):
            self.relays.append(self.C.time.time())
            return self.on
        def set(self,on):
            self.commands += 1
            self.on = on
    p = Switching(C)
    C._client_snapshot = lambda: (None,p,C._cfg_generation)
    run_loop(C, seconds=20)
    assert p.relays == [1000,1005,1010,1015]
    assert p.commands == 1
    assert C.STATE['changed_at'] == 1000


def test_force_on_confirmation_is_available_to_fallback_cache():
    C = harness.load()
    C.CFG['control']['mode'] = 'on'
    class Switching(Meter):
        on = False
        def state(self):
            self.relays.append(self.C.time.time())
            return self.on
        def set(self,on): self.on = on
    p = Switching(C)
    C._force_on(p,C._cfg_generation)
    C._refresh_relay(p,C._cfg_generation,poll=True)
    assert len(p.relays) == 1 and C.STATE['fan'] is True


def test_cached_failed_attempt_does_not_retry_from_second_loop():
    C = harness.load()
    p = Meter(C, fail_first=True)
    old = C.time.time
    C.time.time = lambda: 1000
    try:
        with C._act_lock:
            for _ in range(2):
                assert C._fan_status(p, poll=True) is None
    finally:
        C.time.time = old
    assert p.relays == [1000]


def test_force_on_failed_read_is_visible_without_inventing_new_run():
    C = harness.load()
    C.CFG['control']['mode'] = 'on'
    C.STATE.update(fan=True, changed_at=900)
    class Failing(Meter):
        commands = 0
        def set(self, on):
            assert on is True
            self.commands += 1
    p = Failing(C,fail_first=True)
    C._force_on(p, C._cfg_generation, poll=True)
    C._force_on(p, C._cfg_generation, poll=True)
    assert len(p.relays) == p.commands == 1
    assert C.STATE['changed_at'] == 900
    msgs = [e['msg'] for e in C.STATE['events']]
    assert any('Force on reasserted' in m and 'ReadTimeout' in m for m in msgs)
    assert not any('Fan on — forced on' in m for m in msgs)


def test_explicit_confirmation_bypasses_background_relay_cache():
    C = harness.load()
    p = Meter(C)
    old = C.time.time
    C.time.time = lambda: 1000
    try:
        with C._act_lock:
            assert C._fan_status(p,poll=True) is False
            assert C._fan_status(p) is False
    finally:
        C.time.time = old
    assert p.relays == [1000,1000]


def test_fault_stop_confirmation_replaces_cached_running_relay():
    C = harness.load()
    C.CFG['control']['mode'] = 'auto'
    C.STATE['fault'] = {'at':1, 'msg':'fault', 'stopped':False}
    class Switch(Meter):
        on = True
        def state(self):
            self.relays.append(self.C.time.time())
            return self.on
        def set(self,on):
            self.on = on
    p = Switch(C)
    with C._act_lock:
        assert C._fan_status(p,poll=True) is True
    assert C._enforce_stop(p,C._cfg_generation) is True
    with C._act_lock:
        assert C._fan_status(p,poll=True) is False
    assert len(p.relays) == 2


def test_background_cache_is_not_shared_with_another_plug():
    C = harness.load()
    p,q = Meter(C),Meter(C)
    with C._act_lock:
        assert C._fan_status(p,poll=True) is False
        assert C._fan_status(q,poll=True) is False
    assert len(p.relays) == len(q.relays) == 1


def test_meter_checks_cache_expiry_not_a_second_independent_schedule():
    C = harness.load()
    p = Meter(C)
    C._client_snapshot = lambda: (None,p,C._cfg_generation)
    old = C.time.time
    C.time.time = lambda: 990
    try:
        C._refresh_relay(p,C._cfg_generation,poll=True)
    finally:
        C.time.time = old
    run_loop(C,seconds=65)
    assert p.relays == [990,1020,1050], p.relays


def test_healthy_normal_checks_do_not_trigger_background_relay_requests():
    for mode in ('auto','manual'):
        C = harness.load()
        C.CFG['control']['mode'] = mode
        p = Meter(C)
        C._client_snapshot = lambda: (None,p,C._cfg_generation)
        old = C.time.time
        C.time.time = lambda: 1000
        try:
            C._refresh_relay(p,C._cfg_generation,poll=True)
        finally:
            C.time.time = old
        C._normal_relay_at = (p,C._cfg_generation,1000)
        refresh = C._refresh_relay
        background_checks = []
        def background_refresh(*args, **kwargs):
            background_checks.append(C.time.time())
            return refresh(*args, **kwargs)
        C._refresh_relay = background_refresh
        def normal_check(now):
            if now in (1030,1060):
                refresh(p,C._cfg_generation,poll=True)
                C._normal_relay_at = (p,C._cfg_generation,now)
        # Auto and Manual need no extra relay reads during healthy polling.
        extra_force_checks = []
        C._force_on = lambda *args,**kw: extra_force_checks.append(C.time.time())
        run_loop(C,advance=normal_check)
        assert p.relays == [1000,1030,1060], (mode,p.relays)
        assert not background_checks, (mode,background_checks)
        assert not extra_force_checks, (mode,extra_force_checks)


def test_fallback_resumes_when_normal_checks_stop():
    C = harness.load()
    p = Meter(C)
    C._client_snapshot = lambda: (None,p,C._cfg_generation)
    old = C.time.time
    C.time.time = lambda: 1000
    try:
        C._refresh_relay(p,C._cfg_generation,poll=True)
    finally:
        C.time.time = old
    C._normal_relay_at = (p,C._cfg_generation,1000)
    run_loop(C,seconds=75)
    assert p.relays == [1000,1040,1070], p.relays


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
    old = C._plug_http_get
    def forbidden(*args,**kwargs):
        raise AssertionError('HTTP reached despite interlock')
    C._plug_http_get = forbidden
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
        C._plug_http_get = old


def test_transport_diagnostics_never_include_request_urls_or_credentials():
    C = harness.load()
    old = C._plug_http_get
    def fail(*args,**kwargs):
        raise C.requests.ReadTimeout('http://device/cm?password=CHANGEME')
    C._plug_http_get = fail
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
        C._plug_http_get = old


def test_response_diagnostics_are_classified_not_copied_from_device():
    C = harness.load()
    old = C._plug_http_get
    class Reply:
        status_code = 200
        content = b'PRIVATE'
        value = {}
        def json(self):
            if isinstance(self.value,Exception):
                raise self.value
            return self.value
    reply = Reply()
    C._plug_http_get = lambda *args,**kwargs:reply
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
        C._plug_http_get = old


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
            def power_w(self):
                return 0
        plug = Plug()
        C._draw_check(plug,C._cfg_generation)
        assert C.STATE['fault']['code'] == C.FC_UNDER_POWER
        assert plug.commands == ([False] if reachable else [])
        assert C.STATE['fault']['stopped'] is reachable


if __name__ == '__main__':
    sys.exit(harness.run(dict(globals())))
