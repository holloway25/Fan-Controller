"""Maintenance cannot silently introduce a minor/major dependency update."""
import importlib.util
import os
from pathlib import Path
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('dependencies',ROOT/'tools/container_dependencies.py')
D=importlib.util.module_from_spec(spec)
spec.loader.exec_module(D)


def test_only_patch_releases_and_existing_packages_are_accepted():
    source=D.pins('Flask==3.1.3\nrequests==2.34.2\n')
    def report(flask='3.1.4', requests='2.34.3'):
        return {'install':[{'metadata':{'name':'flask','version':flask}},
                           {'metadata':{'name':'requests','version':requests}}]}
    assert D.resolved(source,report())=='Flask==3.1.4\nrequests==2.34.3\n'
    for bad in ('3.2.0','4.0.0','3.1.2','3.1.4rc1','3.1.4.post1'):
        try: D.resolved(source,report(flask=bad))
        except ValueError: pass
        else: raise AssertionError('Unapproved version accepted: '+bad)
    for bad in ({'install':[]},{'install':[{'metadata':{'name':'unknown','version':'1.0.0'}}]}):
        try: D.resolved(source,bad)
        except ValueError: pass
        else: raise AssertionError('Unexpected dependency graph accepted')


def test_invalid_or_duplicate_pins_cannot_relax_update_policy():
    for bad in ('Flask>=3','Flask==3.1.3\nflask==3.1.4','Flask==3.1',''):
        try: D.pins(bad)
        except ValueError: pass
        else: raise AssertionError('Invalid pin accepted')


if __name__=='__main__':
    sys.exit(harness.run(dict(globals())))
