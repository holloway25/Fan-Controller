const assert = require("assert");
const H = require("./harness.cjs");

module.exports = {
  async test_dangerous_actions_require_confirmation() {
    const writes = [];
    const { window, document } = await H.open({
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
