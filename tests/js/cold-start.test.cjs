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
    assert.strictEqual(page.document.getElementById('cold_start_minutes').value,'10');
  },
  async test_cold_start_countdown_is_distinguished_from_normal_minimum_run() {
    const state = H.baseState({fan:true,cold_start_remaining:600});
    const page = await H.open({state});
    assert(page.document.getElementById('minrun').textContent.startsWith('Cold start 10m'));
    state.cold_start_remaining = 0;
    await H.poll(page.window);
    assert(page.document.getElementById('minrun').textContent.startsWith('Min run'));
  },
};
