const assert=require('assert');
const H=require('./harness.cjs');
const fields=[['p_pass','p_host','protect'],['g_pass','g_host','plug'],['e_pass','e_host','email']];

async function pageWithSave(handler) {
  const state=H.baseState();
  return H.open({state,beforeParse(w){
    const original=w.fetch;
    w.fetch=(url,opts)=>url==='/api/config'&&opts
      ?handler(JSON.parse(opts.body),state):original(url,opts);
  }});
}
function reply(config,notice='') {
  return {ok:true,json:async()=>({ok:true,config,notice})};
}
function edit(page,id,value) {
  const input=page.document.getElementById(id);
  input.value=value;
  input.dispatchEvent(new page.window.Event('input',{bubbles:true}));
}
async function save(page) {
  page.document.getElementById('save').click();
  await H.settle(page.window);
}

module.exports={
  async test_saved_password_is_not_resent_to_replacement_host() {
    for(const [id,host,section] of fields) {
      const requests=[];
      const page=await pageWithSave((body,state)=>{
        requests.push(body);
        const cleared=requests.length===2&&!Object.hasOwn(body[section],'password');
        if(cleared)state.config[section].password='';
        else state.config[section].password='SET';
        state.config[section].host=body[section].host;
        return Promise.resolve(reply(state.config,cleared?'Host changed; credentials cleared.':''));
      });
      edit(page,id,'example-submitted-value');
      await save(page);
      const input=page.document.getElementById(id);
      assert.strictEqual(input.value,'');
      assert.strictEqual(input.placeholder,'unchanged');
      edit(page,host,'replacement.example.com');
      await save(page);
      assert.strictEqual(requests[0][section].password,'example-submitted-value');
      assert(!Object.hasOwn(requests[1][section],'password'),'old credential resubmitted');
      assert.strictEqual(input.placeholder,'');
      assert(page.document.getElementById('savemsg').textContent.includes('credentials cleared'));
    }
  },
  async test_failed_password_save_preserves_all_inputs() {
    const page=await pageWithSave(()=>Promise.resolve({ok:false,status:500,
      json:async()=>({error:'Settings could not be saved.'})}));
    for(const [id] of fields)edit(page,id,'example-unsaved-value');
    await save(page);
    for(const [id] of fields)assert.strictEqual(page.document.getElementById(id).value,'example-unsaved-value');
  },
  async test_password_edits_during_save_are_retained() {
    let finish;
    const page=await pageWithSave((body,state)=>new Promise(resolve=>{
      for(const [, , section] of fields)state.config[section].password='SET';
      finish=()=>resolve(reply(state.config));
    }));
    for(const [id] of fields)edit(page,id,'example-original-value');
    await save(page);
    assert(finish,'Save not submitted');
    for(const [id,host] of fields){
      edit(page,id,'example-new-edit');
      edit(page,host,'pending-edit.example.com');
    }
    finish();
    await H.settle(page.window);
    await H.poll(page.window);
    for(const [id,host] of fields){
      assert.strictEqual(page.document.getElementById(id).value,'example-new-edit');
      assert.strictEqual(page.document.getElementById(host).value,'pending-edit.example.com');
    }
  },
};
