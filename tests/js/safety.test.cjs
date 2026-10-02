const assert = require("assert");
const H = require("./harness.cjs");

module.exports = {
  async test_dangerous_actions_require_confirmation() {
    const writes = [];
    const { window, document } = await H.open({
      state: H.baseState({ fault: { msg: "Draw fault", stopped: true } }),
      beforeParse(w) {
        const original = w.fetch;
        w.fetch = (url, opts) => {
          if (opts && (url === "/api/config" || url === "/api/fault/clear"))
            writes.push(String(url));
          return original(url, opts);
        };
        w.confirm = () => false;
      },
    });
    document.querySelector('[data-mode="on"]').click();
    document.getElementById("faultclear").click();
    await H.settle(window);
    assert.deepStrictEqual(writes, [], "cancelled safety actions sent writes");
  },

  async test_force_on_without_fault_does_not_prompt() {
    const writes = [];
    let prompts = 0;
    const { window, document } = await H.open({
      beforeParse(w) {
        const original = w.fetch;
        w.fetch = (url, opts) => {
          if (opts && url === "/api/config") writes.push(JSON.parse(opts.body));
          return original(url, opts);
        };
        w.confirm = () => { prompts++; return false; };
      },
    });
    document.querySelector('[data-mode="on"]').click();
    await H.settle(window);
    assert.equal(prompts, 0, "prompt shown without a fault");
    assert.deepStrictEqual(writes, [{ control: { mode: "on" } }]);
  },

  async test_force_on_with_fault_can_be_confirmed() {
    const writes = [];
    let prompts = 0;
    const { window, document } = await H.open({
      state: H.baseState({ fault: { msg: "Draw fault", stopped: true } }),
      beforeParse(w) {
        const original = w.fetch;
        w.fetch = (url, opts) => {
          if (opts && url === "/api/config") writes.push(JSON.parse(opts.body));
          return original(url, opts);
        };
        w.confirm = () => { prompts++; return true; };
      },
    });
    document.querySelector('[data-mode="on"]').click();
    await H.settle(window);
    assert.equal(prompts, 1);
    assert.deepStrictEqual(writes, [{ control: { mode: "on" } }]);
  },

  async test_dashboard_labels_and_ranges_match_inputs() {
    const { document } = await H.open();
    for (const label of document.querySelectorAll(".row label")) {
      assert(label.htmlFor, "unassociated label: " + label.textContent);
      assert(document.getElementById(label.htmlFor), label.htmlFor);
    }
    const expected = {
      poll_seconds: [5, 3600], min_run_minutes: [0, 240],
      on_at: [300, 10000], off_at: [300, 10000],
      temp_on: [-20, 60], temp_off: [-20, 60],
    };
    for (const [id, [lo, hi]] of Object.entries(expected)) {
      const input = document.getElementById(id);
      assert.equal(Number(input.min), lo, id + " minimum");
      assert.equal(Number(input.max), hi, id + " maximum");
    }
  },
};
