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
    assert.equal(doc.getElementById("p_sensor").options.length,4);
  },
};
