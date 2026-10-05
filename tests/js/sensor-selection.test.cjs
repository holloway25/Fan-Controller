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
  async test_find_keeps_saved_replacement_and_explicit_empty_selection() {
    for (const replacement of ['replacement', '']) {
      const saves = [];
      const page = await H.open({beforeParse(w) {
        const original = w.fetch;
        w.fetch = (url, opts) => {
          if (url === '/api/config' && opts) {
            saves.push(JSON.parse(opts.body));
            return Promise.resolve({ok: true, json: async () => ({ok: true})});
          }
          if (url === '/api/protect/sensors')
            return Promise.resolve({ok: true, json: async () => ({sensors:
              [{id: 's', name: 'Original'}, {id: 'replacement', name: 'New'}]})});
          return original(url, opts);
        };
      }});
      const select = page.document.getElementById('p_sensor');
      const option = page.document.createElement('option');
      option.value = 'replacement'; select.appendChild(option);
      select.value = replacement;
      select.dispatchEvent(new page.window.Event('change', {bubbles: true}));
      page.document.getElementById('find').click();
      await H.settle(page.window);
      assert.strictEqual(saves[0].protect.sensor_id, replacement);
      assert.strictEqual(select.value, replacement);
      page.document.getElementById('save2').click();
      await H.settle(page.window);
      assert.strictEqual(saves[1].protect.sensor_id, replacement);
    }
  },
  async test_explicit_not_selected_clears_sensor() {
    const body = await selectionSave(true);
    assert.strictEqual(body.protect.sensor_id, '');
  },
  async test_unpopulated_picker_preserves_saved_sensor() {
    const body = await selectionSave(false);
    assert(!Object.hasOwn(body.protect, 'sensor_id'));
  },
};
