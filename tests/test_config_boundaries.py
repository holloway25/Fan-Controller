"""Configuration requests reject unknown sections and overflowing numbers."""
import copy
import json
import os
import sys
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
import harness


def test_legacy_enable_requires_boolean_before_migration():
    C = harness.load()
    for value in ('false', 'true', 0, 1, None, [], {}):
        with open(C.CFG_PATH, 'w') as stream:
            json.dump({'control': {'mode': 'auto', 'enabled': value}}, stream)
        try:
            C.load_cfg()
        except C.ConfigurationError:
            pass
        else:
            raise AssertionError('Invalid legacy enable accepted: %r' % value)
    for value, mode in ((False, 'manual'), (True, 'auto')):
        with open(C.CFG_PATH, 'w') as stream:
            json.dump({'control': {'mode': 'auto', 'enabled': value}}, stream)
        cfg = C.load_cfg()
        assert cfg['control']['mode'] == mode
        assert 'enabled' not in cfg['control']


def test_unknown_sections_reject_the_whole_request_without_side_effects():
    C = harness.load()
    C.CFG['ui']['password_hash'] = ''
    client = C.app.test_client()
    before = copy.deepcopy(C.CFG)
    C.save_cfg = lambda *a: (_ for _ in ()).throw(AssertionError('Rejected request saved'))
    C.promise_manual_off = lambda: (_ for _ in ()).throw(AssertionError('Rejected request promised stop'))
    C._wake.set = lambda: (_ for _ in ()).throw(AssertionError('Rejected request woke controller'))
    for body in ({'controls':{'mode':'manual'}},
                 {'control':{'mode':'manual'},'controls':{'on_at':1500}},
                 {'control':{'mode':'on'},'unknown':{}},
                 {'ui':{}}, {'email':{'enabled':False},'emails':{}}):
        generation = C._cfg_generation
        reply = client.post('/api/config',json=body)
        assert reply.status_code == 400, (body,reply.status_code)
        assert reply.is_json and 'Unknown configuration section' in reply.json['error']
        assert C.CFG == before and C._cfg_generation == generation


def test_oversized_numbers_return_json_validation_errors_without_saving():
    C = harness.load()
    C.CFG['ui']['password_hash'] = ''
    client = C.app.test_client()
    before = copy.deepcopy(C.CFG)
    C.save_cfg = lambda *a: (_ for _ in ()).throw(AssertionError('Invalid number saved'))
    for section,key in (('control','on_at'),('control','poll_seconds'),('plug','watts_min')):
        for value in (10**400, -(10**400)):
            reply = client.post('/api/config',json={section:{key:value}})
            assert reply.status_code == 400 and reply.is_json, reply.status_code
            assert 'finite number' in reply.json['error']
            assert C.CFG == before


if __name__ == '__main__':
    sys.exit(harness.run(dict(globals())))
