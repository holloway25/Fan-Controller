"""Exercise the unit generator only: no installer, venv or package installs."""
import getpass
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
import harness

ROOT = pathlib.Path(__file__).resolve().parent.parent


def generate(directory):
    if not shutil.which('awk'):
        harness.skip('awk not available')
    source = (ROOT/'install.sh').read_text()
    program = re.search(r"awk -v who=.*? -v dir=.*? '\n(.*?)\n' \"\$UNIT_SRC\"",source,re.S)
    assert program, 'Unit generator not found'
    return subprocess.check_output(['awk','-v','who='+getpass.getuser(),
        '-v','grp='+getpass.getuser(),'-v','dir='+str(directory),program.group(1),
        str(ROOT/'office-fan-controller.service.example')],text=True)


def test_unit_generator_escapes_percent_specifiers_without_installing():
    for directory in ('/tmp/fan%Z','/tmp/fan%n','/tmp/fan%% & control'):
        unit = generate(directory)
        escaped = directory.replace('%','%%')
        assert 'WorkingDirectory='+escaped+'\n' in unit, unit
        assert 'ExecStart="'+escaped+'/venv/bin/python" controller.py\n' in unit, unit
    ordinary = '/tmp/fan & control'
    assert 'WorkingDirectory='+ordinary+'\n' in generate(ordinary)


def test_percent_path_unit_is_valid_for_systemd_without_running_it():
    if not shutil.which('systemd-analyze'):
        harness.skip('systemd-analyze not available')
    with tempfile.TemporaryDirectory(prefix='fan-unit-') as folder:
        checkout = pathlib.Path(folder)/'fan%Z%n & control'
        executable = checkout/'venv/bin/python'
        executable.parent.mkdir(parents=True)
        executable.write_text('#!/bin/sh\nexit 0\n')
        executable.chmod(0o755)
        unit = pathlib.Path(folder)/'fan-percent-test.service'
        unit.write_text(generate(checkout))
        reply = subprocess.run(['systemd-analyze','verify',str(unit)],
                                capture_output=True,text=True)
        assert reply.returncode == 0, reply.stdout+reply.stderr


def test_apostrophe_path_is_rejected_by_initial_guard_without_installing():
    if not shutil.which('bash'):
        harness.skip('bash not available')
    source = (ROOT/'install.sh').read_text()
    begin = source.index('case "$WHERE" in')
    end = source.index('\nesac',begin)+len('\nesac')
    assert end < source.index('step "Python"'), 'Path guard runs too late'
    snippet = ('WHERE=$1\nNL="$(printf \'\\nx\')"; NL="${NL%x}"\n'
               'die() { printf \'%s\\n\' "$*" >&2; exit 1; }\n'
               +source[begin:end]+'\nprintf \'accepted\\n\'\n')
    for directory, rejected in (("/tmp/fan's controller",True),
                                ("/tmp/fan'controller",True),
                                ('/tmp/fan%Z & controller',False)):
        reply = subprocess.run(['bash','-s','--',directory],input=snippet,
                               text=True,capture_output=True)
        if rejected:
            assert reply.returncode == 1 and 'apostrophe' in reply.stderr, reply
            assert 'accepted' not in reply.stdout
        else:
            assert reply.returncode == 0 and reply.stdout.strip() == 'accepted', reply


if __name__ == '__main__':
    sys.exit(harness.run(dict(globals())))
