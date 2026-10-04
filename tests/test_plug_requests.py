"""Real Tasmota client, mocked HTTP: plug requests cannot overlap."""
import os
import sys
import threading
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
import harness


class Reply:
    status_code = 200
    content = b'{}'
    def json(self):
        return {'POWER':'ON', 'StatusSNS':{'ENERGY':{'Power':23}}}


def test_power_relay_and_switch_requests_are_serial_across_client_instances():
    C = harness.load()
    first_entered, release_first = threading.Event(), threading.Event()
    second_started, overlapped = threading.Event(), threading.Event()
    gate = threading.Lock()
    calls, failures = [], []
    active = [0]
    def get(url,params,timeout):
        with gate:
            active[0] += 1
            if active[0] > 1:
                overlapped.set()
            calls.append(params['cmnd'])
            first = len(calls) == 1
        if first:
            first_entered.set()
            assert release_first.wait(2), 'Test did not release first request'
        with gate:
            active[0] -= 1
        return Reply()
    old = C._plug_http_get
    C._plug_http_get = get
    # Different instances matter: config saves rebuild the client, and the
    # old meter call can still be in flight when the replacement is used.
    a,b = C.Tasmota('192.0.2.10'),C.Tasmota('192.0.2.10')
    def worker(action,started=None):
        try:
            if started:
                started.set()
            action()
        except BaseException as error:
            failures.append(error)
    threads = [threading.Thread(target=worker,args=(a.power_w,)),
               threading.Thread(target=worker,args=(b.state,second_started)),
               threading.Thread(target=worker,args=(lambda:b.set(True),))]
    try:
        threads[0].start()
        assert first_entered.wait(1)
        threads[1].start()
        threads[2].start()
        assert second_started.wait(1)
        assert not overlapped.wait(.15), 'HTTP requests overlapped'
        release_first.set()
        for thread in threads:
            thread.join(2)
            assert not thread.is_alive(), 'Plug request deadlocked'
        assert not failures, failures
        assert sorted(calls) == ['Power','Power ON','Status 8'], calls
    finally:
        release_first.set()
        for thread in threads:
            if thread.ident is not None:
                thread.join(2)
        C._plug_http_get = old


def test_transport_and_reply_errors_release_the_request_lock():
    C = harness.load()
    p = C.Tasmota('192.0.2.10')
    old = C._plug_http_get
    class BadReply(Reply):
        def json(self):
            raise ValueError('bad response')
    def timeout(*args,**kwargs):
        raise C.requests.ConnectTimeout('unsafe request details')
    try:
        for get in (timeout,lambda *a,**kw:BadReply()):
            C._plug_http_get = get
            try:
                p.state()
                raise AssertionError('Expected a plug error')
            except C.PlugError:
                pass
            assert C._plug_request_lock.acquire(blocking=False), 'Lock leaked after error'
            C._plug_request_lock.release()
            C._plug_http_get = lambda *a,**kw:Reply()
            assert p.state() is True
    finally:
        C._plug_http_get = old


def test_request_path_does_not_require_actuation_lock():
    C = harness.load()
    old = C._plug_http_get
    done = threading.Event()
    failures = []
    C._plug_http_get = lambda *a,**kw:Reply()
    def read():
        try:
            assert C.Tasmota('192.0.2.10').state() is True
        except BaseException as error:
            failures.append(error)
        finally:
            done.set()
    thread = threading.Thread(target=read,daemon=True)
    try:
        with C._act_lock:
            thread.start()
            assert done.wait(1), 'HTTP path waits for actuation lock (inverted lock order)'
        thread.join(1)
        assert not failures, failures
    finally:
        C._plug_http_get = old


if __name__ == '__main__':
    sys.exit(harness.run(dict(globals())))
