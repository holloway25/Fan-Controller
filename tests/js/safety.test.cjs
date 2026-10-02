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
          if (opts && url === "/api/config")
            return Promise.resolve({ ok: false, status: 409, json: async () => ({
              error: "Fault override requires confirmation", fault_override_required: true,
              fault_override_token: "example-episode-1" }) });
          return original(url, opts);
        };
        w.confirm = () => false;
      },
    });
    document.querySelector('[data-mode="on"]').click();
    document.getElementById("faultclear").click();
    await H.settle(window);
    assert.deepStrictEqual(writes, ["/api/config"], "cancelled override was retried or fault cleared");
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
    const tokens = [];
    let prompts = 0;
    const { window, document } = await H.open({
      state: H.baseState({ fault: { msg: "Draw fault", stopped: true } }),
      beforeParse(w) {
        const original = w.fetch;
        w.fetch = (url, opts) => {
          if (opts && url === "/api/config") {
            writes.push(JSON.parse(opts.body));
            tokens.push(opts.headers["X-Fault-Override"]);
            if (!opts.headers["X-Fault-Override"])
              return Promise.resolve({ ok: false, status: 409, json: async () => ({
                error: "Fault override requires confirmation", fault_override_required: true,
                fault_override_token: "example-episode-1" }) });
          }
          return original(url, opts);
        };
        w.confirm = () => { prompts++; return true; };
      },
    });
    document.querySelector('[data-mode="on"]').click();
    await H.settle(window);
    assert.equal(prompts, 1);
    assert.deepStrictEqual(writes, [{ control: { mode: "on" } }, { control: { mode: "on" } }]);
    assert.deepStrictEqual(tokens, [undefined, "example-episode-1"]);
  },

  async test_server_fault_prompts_even_with_stale_healthy_dashboard() {
    let prompts = 0;
    const tokens = [];
    const { window, document } = await H.open({
      state: H.baseState({ fault: null }),
      beforeParse(w) {
        const original = w.fetch;
        w.fetch = (url, opts) => {
          if (opts && url === "/api/config") {
            tokens.push(opts.headers["X-Fault-Override"]);
            return Promise.resolve({ ok: false, status: 409, json: async () => ({
              error: "New fault", fault_override_required: true,
              fault_override_token: "example-new-episode" }) });
          }
          return original(url, opts);
        };
        w.confirm = () => { prompts++; return false; };
      },
    });
    document.querySelector('[data-mode="on"]').click();
    await H.settle(window);
    assert.equal(prompts, 1, "stale healthy state bypassed confirmation");
    assert.deepStrictEqual(tokens, [undefined], "cancelled override was sent");
  },

  async test_new_fault_during_confirmation_is_not_blindly_overridden() {
    const tokens = [];
    let prompts = 0;
    const { window, document } = await H.open({
      beforeParse(w) {
        const original = w.fetch;
        w.fetch = (url, opts) => {
          if (opts && url === "/api/config") {
            tokens.push(opts.headers["X-Fault-Override"]);
            const challenge = tokens.length === 1 ? "example-old-episode" : "example-new-episode";
            return Promise.resolve({ ok: false, status: 409, json: async () => ({
              error: "Confirm the current fault", fault_override_required: true,
              fault_override_token: challenge }) });
          }
          return original(url, opts);
        };
        w.confirm = () => { prompts++; return true; };
      },
    });
    document.querySelector('[data-mode="on"]').click();
    await H.settle(window);
    assert.equal(prompts, 1);
    assert.deepStrictEqual(tokens, [undefined, "example-old-episode"], "new fault was retried without new confirmation");
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
