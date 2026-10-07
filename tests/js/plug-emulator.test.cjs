const assert = require('assert');
const fs = require('fs');
const path = require('path');
const {JSDOM} = require('jsdom');

async function open(post) {
  const source = fs.readFileSync(path.join(__dirname,'../../tools/plug_emulator.py'),'utf8');
  const html = source.match(/PAGE = b'''([\s\S]*?)'''/)[1];
  const state = {relay:false,fan_profile:true,startup_watts:27,startup_seconds:3,
    on_watts:23,warm_watts:22,warm_after_minutes:30,off_watts:0,failure:'none',
    target:'all',delay_seconds:0,timeout_seconds:15,outage_seconds:30,fail_next:0};
  const writes=[];
  const dom = new JSDOM(html,{url:'http://127.0.0.1:8981/',runScripts:'dangerously',
    beforeParse(w) {
      w.setInterval=()=>1;
      w.fetch=async (url,opts)=>{
        if(opts && opts.method==='POST') {
          const body=JSON.parse(opts.body);writes.push(body);
          if(post) return post(body,state);
          Object.assign(state,body);
        }
        return {ok:true,json:async()=>({...state})};
      };
    }});
  await new Promise(resolve=>setImmediate(resolve));
  return {dom,state,writes,w:dom.window};
}
function edit(page,id,value) {
  const field=page.w.document.getElementById(id);
  field.value=String(value);
  field.dispatchEvent(new page.w.Event('change',{bubbles:true}));
}
function apply(page) {
  return page.w.document.getElementById('form').onsubmit({preventDefault(){}});
}

module.exports={
  async test_emulator_failure_apply_does_not_submit_stale_relay() {
    const page=await open();
    try {
      page.state.relay=true; // Controller switches ON after page loads OFF.
      edit(page,'failure','drop_connection');edit(page,'target','power');edit(page,'fail_next',3);
      await apply(page);
      assert.deepStrictEqual(page.writes,[{failure:'drop_connection',target:'power',fail_next:3}]);
      assert.strictEqual(page.state.relay,true,'Failure settings switched relay OFF');
      assert.strictEqual(page.w.document.getElementById('relay').value,'true');
      await apply(page);
      assert.strictEqual(page.writes.length,1,'No-op Apply sent stale settings');
      edit(page,'relay',false);await apply(page);
      assert.deepStrictEqual(page.writes[1],{relay:false});
    } finally {page.dom.window.close();}
  },
  async test_emulator_pending_edits_survive_successful_apply() {
    let finish;
    const page=await open((body,state)=>new Promise(resolve=>{
      finish=()=>{Object.assign(state,body);resolve({ok:true,json:async()=>({...state})});};
    }));
    try {
      edit(page,'fail_next',3);
      const saving=apply(page);
      edit(page,'fail_next',4);
      finish();await saving;
      assert.strictEqual(page.w.document.getElementById('fail_next').value,'4');
      const second=apply(page);finish();await second;
      assert.deepStrictEqual(page.writes,[{fail_next:3},{fail_next:4}]);
    } finally {page.dom.window.close();}
  },
  async test_emulator_failed_apply_retains_edits_for_retry() {
    let reject=true;
    const page=await open((body,state)=>{
      if(reject)return {ok:false};
      Object.assign(state,body);return {ok:true,json:async()=>({...state})};
    });
    try {
      edit(page,'target','power');await apply(page);
      assert.strictEqual(page.w.document.getElementById('target').value,'power');
      reject=false;await apply(page);
      assert.deepStrictEqual(page.writes,[{target:'power'},{target:'power'}]);
    } finally {page.dom.window.close();}
  }
};
