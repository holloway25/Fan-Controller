const assert = require('assert');
const H = require('./harness.cjs');
module.exports = {
  async test_cold_start_duration_loads_and_is_saved_from_plug_settings() {
    const state = H.baseState();
    state.config.control.cold_start_minutes = 15;
    let saved;
    const page = await H.open({state,beforeParse(w) {
      const original = w.fetch;
      w.fetch = (url, opts) => {
        if (url === '/api/config' && opts) {
          saved = JSON.parse(opts.body);
          return Promise.resolve({ok:true,json:async()=>({ok:true})});
        }
        return original(url,opts);
      };
    }});
    const field = page.document.getElementById('cold_start_minutes');
    assert.strictEqual(field.value,'15');
    field.value = '0';
    field.dispatchEvent(new page.window.Event('input',{bubbles:true}));
    await H.poll(page.window);
    assert.strictEqual(field.value,'0','refresh overwrote unsaved edit');
    page.document.getElementById('save3').click();
    await H.settle(page.window);
    assert.strictEqual(saved.control.cold_start_minutes,0);
  },
  async test_older_state_payload_shows_default_cold_start_duration() {
    const page = await H.open();
    assert.strictEqual(page.document.getElementById('cold_start_minutes').value,'15');
  },
  async test_cold_start_countdown_is_distinguished_from_normal_minimum_run() {
    const state = H.baseState({fan:true,cold_start_remaining:600});
    const page = await H.open({state});
    assert(page.document.getElementById('minrun').textContent.startsWith('Cold start 10m'));
    state.cold_start_remaining = 0;
    await H.poll(page.window);
    assert(page.document.getElementById('minrun').textContent.startsWith('Min run'));
  },
  async test_minimum_run_countdown_uses_demand_lease_not_physical_switch_time() {
    const state = H.baseState({fan:true,changed_at:H.now()-3600,min_run_at:H.now()-60});
    const page = await H.open({state});
    const pill = page.document.getElementById('minrun');
    assert(pill.classList.contains('on'),'renewed lease not shown');
    assert(pill.textContent.startsWith('Min run 9m'),'countdown used physical switch time');
  },
};
