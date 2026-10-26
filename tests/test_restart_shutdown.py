"""Real process replacement must not discard a shutdown signal."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness


CHILD = r'''
import importlib.util, json, os, pathlib, sys, threading, time
root = pathlib.Path(sys.argv[1])
spec = importlib.util.spec_from_file_location('fan', root/'controller.py')
C = importlib.util.module_from_spec(spec)
spec.loader.exec_module(C)
C.loop = lambda: C._stopping.wait(10)
C.watts_loop = lambda: C._stopping.wait(10)
C.RESTART_POLL_SECONDS = 0.01
C._notify = lambda *a, **k: None
C.STATE['fan'] = True
C.STATE['latch']['co2'] = True
with C._state_lock:
    C._stamp_elapsed_locked('changed_at')
real_make, real_flush, real_exec = C._make_http_server, C.flush_hist, C.os.execv
real_mark = C._mark_fds_close_on_exec
def make():
    result = real_make('127.0.0.1', 0)
    return result
def flush(signum=None, frame=None):
    if signum is None and threading.current_thread().name == 'fan-restart':
        (root/'flush-entered').touch()
        if sys.argv[2] == 'shutdown':
            deadline = time.monotonic()+8
            while not C._stopping.is_set() and time.monotonic() < deadline:
                time.sleep(0.005)
    return real_flush(signum, frame)
def execute(*args):
    (root/'exec-observed.json').write_text(json.dumps({
        'main': threading.current_thread() is threading.main_thread(),
        'stopping': C._stopping.is_set()}))
    script = "from pathlib import Path; import time; Path('replacement').touch(); time.sleep(30)"
    real_exec(sys.executable, [sys.executable, '-c', script])
C._make_http_server, C.flush_hist, C.os.execv = make, flush, execute
def mark():
    if sys.argv[2] == 'commit':
        (root/'commit-entered').touch()
        deadline = time.monotonic()+8
        while not C._stopping.is_set() and time.monotonic() < deadline:
            time.sleep(0.005)
    real_mark()
C._mark_fds_close_on_exec = mark
# Start the request only after _start_controller installed signals and dispatch.
def request():
    while C.signal.getsignal(C.signal.SIGTERM) == C.signal.SIG_DFL:
        time.sleep(0.005)
    (root/'ready').touch()
    (root/'restart.request').touch()
threading.Thread(target=request, daemon=True).start()
C._run_controller()
'''


def exercise(kind):
    if os.name != 'posix':
        harness.skip('POSIX process signals and exec')
    with tempfile.TemporaryDirectory(prefix='fan-restart-shutdown-') as folder:
        root = Path(folder)
        shutil.copy2(Path(harness.SRC)/'controller.py', root/'controller.py')
        env = dict(os.environ, FAN_CONTROLLER_DATA_DIR=str(root),
                   FAN_CONTROLLER_PERSIST_STATE='1', FAN_CONTROLLER_DISABLE_PLUG='1')
        with (root/'process.log').open('w+') as log:
            process = subprocess.Popen([sys.executable, '-c', CHILD, str(root), kind],
                                       env=env, stdout=log, stderr=log)
            def wait_for(name):
                deadline = time.monotonic()+5
                while not (root/name).exists():
                    assert process.poll() is None, (root/'process.log').read_text()
                    assert time.monotonic() < deadline, 'Missing '+name
                    time.sleep(0.01)
            try:
                wait_for('ready')
                wait_for('flush-entered')
                if kind in ('shutdown', 'commit'):
                    if kind == 'commit':
                        wait_for('commit-entered')
                    process.send_signal(signal.SIGTERM)
                    try:
                        code = process.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        raise AssertionError('Replacement survived SIGTERM during restart flush')
                    assert code == 0, (root/'process.log').read_text()
                    assert not (root/'exec-observed.json').exists(), 'Restart overrode shutdown'
                    assert not (root/'replacement').exists()
                    checkpoint = json.loads((root/'resume.json').read_text())
                    assert checkpoint['payload']['latch']['co2'] is True
                else:
                    wait_for('replacement')
                    observed = json.loads((root/'exec-observed.json').read_text())
                    assert observed == {'main': True, 'stopping': False}, observed
            finally:
                if process.poll() is None:
                    process.terminate()
                    try: process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=3)


def test_sigterm_during_restart_flush_prevents_exec_and_saves_checkpoint():
    exercise('shutdown')


def test_requested_restart_commits_on_signal_handling_main_thread():
    exercise('restart')


def test_sigterm_during_final_restart_preparation_also_wins():
    exercise('commit')


def test_shutdown_cancels_queued_commit_and_releases_handover_locks():
    C = harness.load()
    queued, callbacks, results, execs = threading.Event(), [], [], []
    def dispatch(callback):
        callbacks.append(callback)
        queued.set()
    C._restart_dispatch = dispatch
    worker = threading.Thread(target=lambda: results.append(C._restart_now()))
    with patch.object(C.os, 'execv', lambda *args: execs.append(args)):
        try:
            worker.start()
            assert queued.wait(2)
            C._stopping.set()
            worker.join(2)
            assert not worker.is_alive() and results == [False]
            # A previously queued web-loop callback must also refuse the exec.
            callbacks[0]()
            assert not execs
            for lock in (C._cfg_lock, C._act_lock, C._plug_request_lock, C._fault_lock):
                assert lock.acquire(blocking=False), 'Cancelled restart stranded a lock'
                lock.release()
            assert not any('active fault is not saved' in e['msg'] for e in C.STATE['events'])
        finally:
            C._stopping.set()
            worker.join(2)


if __name__ == '__main__':
    sys.exit(harness.run(dict(globals())))
