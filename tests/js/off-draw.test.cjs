const assert = require('assert');
const H = require('./harness.cjs');
module.exports = {
  async test_failed_manual_off_shows_retry_wait_and_red_normal_running_watts() {
    const state=H.baseState({fan:true,watts:23,switch_age:100,
      fault:{code:'FC-009',msg:'FC-009 Manual OFF unconfirmed',stopped:false},
      fault_retry:{attempts:0,phase:'first',remaining:29.5},manual_off_pending:true});
    state.config.control.mode='manual';
    const page=await H.open({state});
    assert(page.document.getElementById('topmsg').textContent.includes('first OFF retry in 30s'));
    assert.strictEqual(page.document.getElementById('fan').textContent,'Fan on - Fault');
    assert(page.document.getElementById('w').classList.contains('bad'));
  },
  async test_fault_stop_colours_continued_watts_red_even_with_on_or_unknown_relay() {
    for(const fan of [false,true,null]) {
      const state=H.baseState({fan,watts:23,switch_age:100,
        fault:{code:'FC-008',msg:'FC-008 Unable to stop',stopped:false},
        fault_retry:{attempts:6,phase:'exhausted',remaining:0}});
      const page=await H.open({state});
      assert(page.document.getElementById('w').classList.contains('bad'),`relay ${fan}`);
    }
    const overridden=H.baseState({fan:true,watts:23,switch_age:100,
      fault:{code:'FC-008',msg:'FC-008 Unable to stop',stopped:false}});
    overridden.config.control.mode='on';
    const page=await H.open({state:overridden});
    assert(page.document.getElementById('w').classList.contains('on'));
    assert(!page.document.getElementById('w').classList.contains('bad'));
  },
  async test_off_draw_marks_existing_fan_and_watts_pills_red_without_rewording() {
    const page=await H.open({state:H.baseState({fan:false,watts:23,switch_age:100})});
    assert.strictEqual(page.document.getElementById('fan').textContent,'Fan off');
    assert(page.document.getElementById('fan').classList.contains('bad'));
    assert(page.document.getElementById('w').classList.contains('bad'));
    const healthy=await H.open({state:H.baseState({fan:false,watts:0,switch_age:100})});
    assert(!healthy.document.getElementById('fan').classList.contains('bad'));
    assert(!healthy.document.getElementById('w').classList.contains('bad'));
    const settling=await H.open({state:H.baseState({fan:false,watts:23,switch_age:2})});
    assert(!settling.document.getElementById('w').classList.contains('bad'));
  },
  async test_bounded_retry_banner_reports_wait_exhaustion_recovery_and_override() {
    for(const [phase,stopped,mode,expected] of [
      ['first',false,'auto','retry 1 of 3'],
      ['wait',false,'auto','final retries in 42s'],
      ['final',false,'auto','final retry 1 of 3'],
      ['exhausted',false,'auto','automatic retries stopped'],
      ['recovered',true,'auto','stop confirmed; acknowledge fault'],
      ['exhausted',false,'on','Force on overrides fault stop'],
    ]) {
      const state=H.baseState({fan:false,watts:stopped?0:23,switch_age:100,
        fault:{code:'FC-008',msg:'FC-008 Unable to stop',stopped},
        fault_retry:{attempts:phase==='first'?1:phase==='final'?4:6,phase,remaining:41.2}});
      state.config.control.mode=mode;
      const page=await H.open({state});
      const msg=page.document.getElementById('topmsg').textContent;
      assert(msg.includes(expected),msg);
      if(phase==='exhausted'||stopped||mode==='on')assert(!msg.includes('retrying'),msg);
      assert.strictEqual(page.document.getElementById('fan').textContent,'Fan off - Fault');
    }
  },
  async test_power_help_covers_off_draw_and_does_not_promise_a_successful_stop() {
    const page=await H.open();
    const monitor=page.document.querySelector('label[for="watts_enabled"]').textContent;
    const delay=page.document.querySelector('label[for="watts_fault_seconds"]').textContent;
    assert(monitor.includes('ON and OFF')&&monitor.includes('Existing faults remain active'));
    assert(delay.includes('OFF command is attempted')&&!delay.includes('fan is stopped'));
  },
  async test_off_draw_limit_loads_saves_and_preserves_pending_edits() {
    const state = H.baseState();
    state.config.plug.watts_off_max = 1;
    let saved;
    const page = await H.open({state,beforeParse(w) {
      const original=w.fetch;
      w.fetch=(url,opts)=>{
        if(url==='/api/config'&&opts){saved=JSON.parse(opts.body);return Promise.resolve({ok:true,json:async()=>({ok:true})});}
        return original(url,opts);
      };
    }});
    const field=page.document.getElementById('watts_off_max');
    assert.strictEqual(field.value,'1');
    field.value='2';
    field.dispatchEvent(new page.window.Event('input',{bubbles:true}));
    await H.poll(page.window);
    assert.strictEqual(field.value,'2');
    page.document.getElementById('save3').click();
    await H.settle(page.window);
    assert.strictEqual(saved.plug.watts_off_max,2);
  },
  async test_older_state_defaults_off_limit_and_welded_fault_is_not_reported_stopped() {
    const state=H.baseState({fan:false,fault:{code:'FC-007',msg:'FC-007 OFF but drawing 23 W',stopped:false}});
    const page=await H.open({state});
    assert.strictEqual(page.document.getElementById('watts_off_max').value,'1');
    assert(page.document.getElementById('topmsg').textContent.includes('stop unconfirmed'));
  },
};
