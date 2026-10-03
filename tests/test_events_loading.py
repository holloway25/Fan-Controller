"""Damaged activity records cannot corrupt the browser's state JSON."""
import json
import os
import sys
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
import harness


def write(C, events):
    with open(C.EVENTS_PATH,'w') as stream:
        json.dump(events,stream)


def test_invalid_event_timestamps_and_messages_are_discarded():
    C = harness.load()
    good = {'at':C.time.time(), 'msg':'Fan on — 1002 ppm'}
    invalid = [{'at':v,'msg':'bad timestamp'} for v in
               (None,True,False,'bad','123',[],{},float('nan'),float('inf'),
                -float('inf'),10**400,1e300)]
    invalid += [{'at':good['at'],'msg':v} for v in
                (None,True,False,42,[],{},float('inf'))]
    invalid += [None,[],{}, {'at':good['at']}, {'msg':'missing timestamp'}]
    write(C, invalid+[good])
    assert C.load_events() == [good]


def test_only_supported_event_fields_survive_and_json_remains_valid():
    C = harness.load()
    good = {'at':C.time.time(), 'msg':'restored event'}
    write(C,[dict(good,unknown=float('inf'),cls={'bad':float('nan')})])
    loaded = C.load_events()
    assert loaded == [good]
    C.STATE['events'] = loaded
    reply = C.app.test_client().get('/api/state')
    assert reply.status_code == 200 and reply.is_json
    # Python's default JSON parser accepts non-finite constants; browsers do
    # not. Reject them here rather than trusting Flask's .json convenience.
    def invalid_constant(value):
        raise AssertionError('Invalid browser JSON: '+value)
    data = json.loads(reply.get_data(as_text=True),parse_constant=invalid_constant)
    assert data['events'][0]['at'] == good['at']
    assert data['events'][0]['msg'] == good['msg']
    assert set(data['events'][0]) == {'at','msg','cls'}


def test_exponent_overflow_row_is_dropped_and_valid_tail_keeps_its_order():
    C = harness.load()
    good = [{'at':C.time.time()-i,'msg':'event '+str(i)}
            for i in range(C.EVENTS_MAX+2)]
    with open(C.EVENTS_PATH,'w') as stream:
        stream.write('[{"at":1e309,"msg":"restored event"},'+
                     json.dumps(good)[1:])
    assert C.load_events() == good[:C.EVENTS_MAX]


if __name__ == '__main__':
    sys.exit(harness.run(dict(globals())))
