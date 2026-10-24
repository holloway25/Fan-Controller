const assert = require("assert");
const H = require("./harness.cjs");

module.exports = {
  async test_blank_and_nonfinite_timers_never_post_but_explicit_zero_does() {
    for(const id of ["min_run_minutes","cold_start_minutes","stale_minutes"]){
      const writes=[];
      const {window,document}=await H.open({beforeParse(w){
        const original=w.fetch;
        w.fetch=(url,opts)=>{
          if(url==="/api/config"&&opts)writes.push(JSON.parse(opts.body));
          return original(url,opts);
        };
      }});
      for(const bad of ["","Infinity","NaN"]){
        document.getElementById(id).value=bad;
        document.getElementById("save").click();
        await H.settle(window);
        assert.strictEqual(writes.length,0,id+" accepted "+bad);
        assert.ok(document.getElementById("savemsg").textContent.includes("must be a number"));
      }
      document.getElementById(id).value="0";
      document.getElementById("save").click();
      await H.settle(window);
      assert.strictEqual(writes.length,1,id+" rejected explicit zero");
      assert.strictEqual(writes[0].control[id],0);
    }
  },
};
