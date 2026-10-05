const assert = require('assert');
const H = require('./harness.cjs');
module.exports = {
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
