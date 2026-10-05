const assert = require('assert');
const H = require('./harness.cjs');

async function selectionSave(explicit) {
  let body;
  const page = await H.open({beforeParse(w) {
    const original = w.fetch;
    w.fetch = (url, opts) => {
      if (url === '/api/config' && opts) {
        body = JSON.parse(opts.body);
        return Promise.resolve({ok: true, json: async () => ({ok: true})});
      }
      return original(url, opts);
    };
  }});
  const select = page.document.getElementById('p_sensor');
  select.value = '';
  if (explicit) select.dispatchEvent(new page.window.Event('change', {bubbles: true}));
  page.document.getElementById('save2').click();
  await H.settle(page.window);
  return body;
}

module.exports = {
  async test_explicit_not_selected_clears_sensor() {
    const body = await selectionSave(true);
    assert.strictEqual(body.protect.sensor_id, '');
  },
  async test_unpopulated_picker_preserves_saved_sensor() {
    const body = await selectionSave(false);
    assert(!Object.hasOwn(body.protect, 'sensor_id'));
  },
};
