const assert = require("assert");
const H = require("./harness.cjs");
const notice = "Saved, but the disk did not confirm durability.";

async function action(id, messageId, resultFails = false, confirmFault = false, warning = notice) {
  let saves = 0;
  const page = await H.open({ beforeParse(w) {
    const original = w.fetch;
    w.confirm = () => true;
    w.fetch = (url, opts) => {
      if (url === "/api/config" && opts) {
        saves++;
        if (confirmFault && saves === 1)
          return Promise.resolve({ok:false, status:409, json:async()=>({
            error:"Confirm fault", fault_override_required:true,
            fault_override_token:"example-fault-episode"})});
        return Promise.resolve({ok:true, json:async()=>({ok:true, notice:warning})});
      }
      if (url === "/api/protect/sensors" || url === "/api/plug/test")
        return Promise.resolve({ok:!resultFails, status:resultFails?400:200,
          json:async()=>resultFails?{error:"Device request failed"}:
            url === "/api/plug/test"?{on:false, watts:0}:
              {sensors:[{id:"s", name:"Test sensor", co2:500}]}});
      return original(url, opts);
    };
  }});
  if (id === "mode") page.document.querySelector('[data-mode="on"]').click();
  else page.document.getElementById(id).click();
  await H.settle(page.window);
  await H.poll(page.window);
  return page.document.getElementById(messageId);
}

module.exports = {
  async test_mode_save_and_confirmed_override_keep_notices_after_refresh() {
    for (const confirmed of [false,true]) {
      const msg = await action("mode", "modemsg", false, confirmed);
      assert(msg, "mode notice has no separate display");
      assert(msg.textContent.includes(notice));
      assert(msg.classList.contains("err"));
    }
  },
  async test_find_and_plug_keep_notices_with_success() {
    for (const [id,msgId,text] of [["find","pmsg","sensor(s) found"],
                                  ["plugtest","hmsg","Plug responded"]]) {
      const msg = await action(id,msgId);
      assert(msg.textContent.includes(notice),id+" lost notice");
      assert(msg.textContent.includes(text),id+" lost result");
      assert(msg.classList.contains("err"));
    }
  },
  async test_find_and_plug_keep_notices_when_device_request_fails() {
    for (const [id,msgId] of [["find","pmsg"],["plugtest","hmsg"]]) {
      const msg = await action(id,msgId,true);
      assert(msg.textContent.includes(notice),id+" lost notice on failure");
      assert(msg.textContent.includes("Device request failed"));
      assert(msg.classList.contains("err"));
    }
  },
  async test_find_and_plug_without_notices_still_report_success() {
    for (const [id,msgId] of [["find","pmsg"],["plugtest","hmsg"]]) {
      const msg = await action(id,msgId,false,false,"");
      assert(msg.classList.contains("ok"));
      assert(!msg.textContent.includes(notice));
    }
  },
  async test_stale_or_failed_sensor_values_are_not_healthy() {
    for (const over of [{reading_at:H.now()-600,error:"Selected sensor is disconnected"},
                       {reading_at:H.now()-600,error:null},
                       {reading_at:H.now(),error:"Selected sensor is disconnected"}]) {
      const state = H.baseState(over);
      state.config.control.temp_enabled = true;
      state.config.control.humidity_enabled = true;
      const {document} = await H.open({state});
      for (const id of ["ppm","tc","rh"]) {
        const el = document.getElementById(id);
        assert(el.textContent.includes("stale"),id+" is not labelled stale");
        assert(!el.classList.contains("on")&&!el.classList.contains("trig"),id+" looks live");
      }
      assert(document.getElementById("ppm").textContent.includes("900"),"cached value lost");
    }
  },
  async test_fresh_sensor_values_remain_healthy_after_recovery() {
    const state = H.baseState({reading_at:H.now()-600,error:"disconnected"});
    state.config.control.temp_enabled = true;
    state.config.control.humidity_enabled = true;
    const page = await H.open({state});
    state.reading_at = H.now(); state.error = null;
    await H.poll(page.window);
    for (const id of ["ppm","tc","rh"]) {
      const el = page.document.getElementById(id);
      assert(el.classList.contains("on"),id);
      assert(!el.textContent.includes("stale"),id);
    }
  },
};
