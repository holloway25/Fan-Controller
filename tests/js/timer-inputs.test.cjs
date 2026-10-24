const assert = require("assert");
const H = require("./harness.cjs");

module.exports = {
  async test_all_numeric_settings_reject_blanks_and_nonfinite_values() {
    const ids=["on_at","off_at","min_run_minutes","cold_start_minutes","poll_seconds",
      "temp_on","temp_off","humidity_on","humidity_off","stale_minutes",
      "watts_min","watts_max","watts_off_max","watts_fault_seconds","e_port"];
    const zeroAllowed=new Set(["min_run_minutes","cold_start_minutes","stale_minutes","watts_off_max"]);
    for(const id of ids){
      const writes=[];
      const {window,document}=await H.open({beforeParse(w){
        const original=w.fetch;
        w.fetch=(url,opts)=>{
          if(url==="/api/config"&&opts)writes.push(JSON.parse(opts.body));
          return original(url,opts);
        };
      }});
      const originalValue=document.getElementById(id).value;
      for(const bad of ["","Infinity","NaN"]){
        document.getElementById(id).value=bad;
        document.getElementById("save").click();
        await H.settle(window);
        assert.strictEqual(writes.length,0,id+" accepted "+bad);
        assert.ok(document.getElementById("savemsg").textContent.includes("must be a number"));
      }
      const accepted=zeroAllowed.has(id)?"0":originalValue;
      document.getElementById(id).value=accepted;
      document.getElementById("save").click();
      await H.settle(window);
      assert.strictEqual(writes.length,1,id+" rejected explicit value");
      const section=id==="e_port"?"email":id.startsWith("watts_")?"plug":"control";
      assert.strictEqual(writes[0][section][id==="e_port"?"port":id],Number(accepted));
    }
  },
};
