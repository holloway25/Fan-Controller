const assert = require("assert");
const H = require("./harness.cjs");

async function saveResult(result, ok = true) {
  let saves = 0;
  const page = await H.open({
    beforeParse(w) {
      const original = w.fetch;
      w.fetch = (url, opts) => {
        if (url === "/api/config" && opts) {
          saves++;
          return Promise.resolve({ ok, status: ok ? 200 : 500,
            json: () => Promise.resolve(result) });
        }
        return original(url, opts);
      };
    },
  });
  page.document.getElementById("save4").click();
  await H.settle(page.window);
  assert.equal(saves, 1, "email Save did not submit exactly once");
  return page.document.getElementById("emsg");
}

module.exports = {
  async test_email_save_preserves_cleared_password_warning() {
    const notice = "Mail server changed, so the SMTP password was cleared.";
    const message = await saveResult({ ok: true, notice });
    assert.equal(message.textContent, notice);
    assert(message.classList.contains("err"), "warning styled as success");
  },
  async test_email_save_preserves_durability_warning() {
    const notice = "Saved and in use, but the disk did not confirm the write will survive a power cut.";
    const message = await saveResult({ ok: true, notice });
    assert.equal(message.textContent, notice);
    assert(message.classList.contains("err"), "durability warning styled as success");
  },
  async test_email_save_still_reports_normal_success() {
    const message = await saveResult({ ok: true });
    assert.equal(message.textContent, "Saved.");
    assert(message.classList.contains("ok"));
  },
  async test_email_save_still_reports_write_failure() {
    const error = "Could not save settings. Nothing was changed.";
    const message = await saveResult({ error }, false);
    assert.equal(message.textContent, error);
    assert(message.classList.contains("err"));
  },
};
