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
  async test_startup_recheck_does_not_say_restart_will_clear_the_fault() {
    const {document}=await H.open({state:H.baseState({
      fault:{msg:"Draw fault",stopped:false,stop_recheck:true},fault_unsaved:true})});
    const message=document.getElementById("unsavedmsg").textContent;
    assert(message.includes("Latest stop status pending disk save"));
    assert(message.includes("fault remains latched"));
  },

  async test_relay_fault_banner_does_not_repeat_an_old_fan_off_message() {
    const state = H.baseState({fan:true, fault:{msg:"FC-008 Fan off",stopped:false},
      fault_retry:{phase:"recovered", attempts:6, remaining:0}});
    const {document} = await H.open({state});
    const text = document.getElementById("topmsg").textContent;
    assert(text.includes("FC-008") && text.includes("stop unconfirmed"));
    assert(!text.includes("Fan off"));
  },

  async test_email_save_does_not_resubmit_other_tabs_safety_settings() {
    const state = H.baseState();
    state.config.plug.watts_off_max = 5;
    let body;
    const {window,document} = await H.open({state,beforeParse(w){
      const fetch = w.fetch;
      w.fetch = (url,opts) => {
        if(url!=="/api/config")return fetch(url,opts);
        body=JSON.parse(opts.body);
        return Promise.resolve({ok:true,json:async()=>({ok:true,config:state.config})});
      };
    }});
    state.config.protect.sensor_id = "replacement";
    await H.poll(window);
    assert.equal(document.getElementById("p_sensor").value,"replacement");
    const input = document.getElementById("e_sender");
    input.value = "fan@example.com";
    input.dispatchEvent(new window.Event("input"));
    state.config.plug.watts_off_max = 1;
    await H.poll(window);
    document.getElementById("save").click();
    await H.settle(window);
    assert.deepStrictEqual(body.email,{sender:"fan@example.com"});
    assert.deepStrictEqual(body.plug,{});
    assert.deepStrictEqual(body.protect,{});
    assert.deepStrictEqual(body.expected,{email:{sender:""}});
  },

  async test_calibration_save_expects_original_plug_address() {
    let body;
    const {window,document} = await H.open({beforeParse(w){
      const fetch = w.fetch;
      w.fetch = (url,opts) => {
        if(url!=="/api/config")return fetch(url,opts);
        body=JSON.parse(opts.body);
        return Promise.resolve({ok:true,json:async()=>({ok:true})});
      };
    }});
    const limit = document.getElementById("watts_off_max");
    limit.value = "0";
    limit.dispatchEvent(new window.Event("input"));
    document.getElementById("save").click();
    await H.settle(window);
    assert.deepStrictEqual(body.plug,{watts_off_max:0});
    assert.deepStrictEqual(body.expected.plug,{watts_off_max:1,host:"192.0.2.10"});
  },

  async test_fault_banner_distinguishes_force_on_override_from_stop_retry() {
    for (const mode of ["auto","manual","on"]) {
      for (const stopped of [false,true]) {
        const state = H.baseState({fault:{msg:"Draw fault",stopped}});
        state.config.control.mode = mode;
        const {document} = await H.open({state});
        const text = document.getElementById("topmsg").textContent;
        assert(text.includes("Draw fault"));
        assert.strictEqual(text.includes("Force on overrides fault stop"),mode === "on");
        assert.strictEqual(text.includes("stop unconfirmed, retrying"),mode !== "on" && !stopped);
      }
    }
    const state = H.baseState({fault:null});
    state.config.control.mode = "on";
    const {document} = await H.open({state});
    assert(!document.getElementById("topmsg").textContent.includes("overrides"));
  },
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
    for (const over of [{reading_at:H.now()-600,error:"Selected sensor is disconnected",sensor_error:"disconnected"},
                       {reading_at:H.now()-600,error:null},
                       {reading_at:H.now(),error:"Selected sensor is disconnected",sensor_error:"disconnected"}]) {
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
    const state = H.baseState({reading_at:H.now()-600,error:"disconnected",sensor_error:"disconnected"});
    state.config.control.temp_enabled = true;
    state.config.control.humidity_enabled = true;
    const page = await H.open({state});
    state.reading_at = H.now(); state.error = null; state.sensor_error = null;
    await H.poll(page.window);
    for (const id of ["ppm","tc","rh"]) {
      const el = page.document.getElementById(id);
      assert(el.classList.contains("on"),id);
      assert(!el.textContent.includes("stale"),id);
    }
  },
  async test_plug_failure_does_not_mark_fresh_sensor_values_stale() {
    const state = H.baseState({co2:1500,reading_at:H.now(),
      error:"Plug rejected switching",sensor_error:null});
    state.config.control.temp_enabled = true;
    state.config.control.humidity_enabled = true;
    const {document} = await H.open({state});
    for (const id of ["ppm","tc","rh"]) {
      const el = document.getElementById(id);
      assert(!el.textContent.includes("stale"),id+" mislabelled stale");
      assert(el.classList.contains("on")||el.classList.contains("trig"),id+" lost live colour");
    }
    assert(document.getElementById("dot").classList.contains("bad"));
    assert(document.getElementById("topmsg").textContent.includes("Plug rejected"));
  },
};
