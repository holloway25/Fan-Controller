const assert = require("assert");
const H = require("./harness.cjs");

async function find(sensors) {
  const page = await H.open({beforeParse(w) {
    const original = w.fetch;
    w.fetch = (url, opts) => url === "/api/protect/sensors"
      ? Promise.resolve({ok:true,json:async()=>({sensors})}) : original(url,opts);
  }});
  page.document.getElementById("find").click();
  await H.settle(page.window);
  return page.document;
}

module.exports = {
  async test_console_change_discards_old_sensor_options_and_blocks_their_save() {
    const state = H.baseState();
    let saves=0;
    const {window,document} = await H.open({state,beforeParse(w) {
      const fetch=w.fetch;
      w.fetch=(url,opts)=>{
        if(url==="/api/config")saves++;
        if(url==="/api/protect/sensors")
          return Promise.resolve({ok:true,json:async()=>({protect_host:state.config.protect.host,
            sensors:[{id:"old-only",name:"Old console sensor"}]})});
        return fetch(url,opts);
      };
    }});
    const picker=document.getElementById("p_sensor");
    assert(Array.from(picker.options).some(option=>option.value==="old-only"));
    picker.value="old-only";
    picker.dispatchEvent(new window.Event("change"));
    document.getElementById("p_host").value="replacement.invalid";
    document.getElementById("save2").click();
    await H.settle(window);
    assert.equal(saves,0);
    assert(document.getElementById("pmsg").textContent.includes("find sensors on that console"));

    // Return the form to untouched state as another tab replaces the console.
    window.eval("touched=false");
    state.config.protect.host="replacement.invalid";
    state.config.protect.sensor_id="new-sensor";
    await H.poll(window);
    assert(!Array.from(picker.options).some(option=>option.value==="old-only"));
    assert.equal(picker.value,"new-sensor");
  },

  async test_disconnected_discovery_labels_retained_co2_and_does_not_invite_selection() {
    const doc = await find([{id:"s",name:"Offline sensor",co2:500,isConnected:false}]);
    const options = doc.getElementById("p_sensor").textContent;
    assert(options.includes("disconnected")&&options.includes("retained 500 ppm"));
    const msg = doc.getElementById("pmsg");
    assert(msg.textContent.includes("0 reporting CO2"),msg.textContent);
    assert(msg.textContent.includes("1 disconnected"));
    assert(!msg.textContent.includes("Pick one and save"));
    assert(msg.classList.contains("err"));
  },
  async test_discovery_counts_only_connected_or_legacy_live_candidates() {
    const doc = await find([{id:"a",name:"Offline",co2:500,isConnected:false},
                           {id:"b",name:"Online",co2:600,isConnected:true},
                           {id:"c",name:"Legacy",co2:700}]);
    assert(doc.getElementById("pmsg").textContent.includes("2 reporting CO2"));
    assert(doc.getElementById("pmsg").textContent.includes("1 disconnected"));
    const picker = doc.getElementById("p_sensor");
    assert.equal(picker.options.length,5);
    assert.equal(picker.value,"s");
    assert(picker.selectedOptions[0].textContent.includes("not in list"));
  },
};
