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

async function testEmailResult(notice, mailOk = true, saveOk = true) {
  const requests = [];
  const page = await H.open({
    beforeParse(w) {
      const original = w.fetch;
      w.fetch = (url, opts) => {
        if (opts && (url === "/api/config" || url === "/api/email/test")) {
          requests.push(url);
          const saving = url === "/api/config";
          const ok = saving ? saveOk : mailOk;
          const result = saving
            ? (ok ? { ok: true, notice } : { error: "Settings save failed." })
            : (ok ? { ok: true, sent_to: ["a@example.com"] }
              : { error: "Email send failed." });
          return Promise.resolve({ ok, status: ok ? 200 : 400,
            json: () => Promise.resolve(result) });
        }
        return original(url, opts);
      };
    },
  });
  page.document.getElementById("emailtest").click();
  await H.settle(page.window);
  return { message: page.document.getElementById("emsg"), requests };
}

module.exports = {
  async test_email_button_blocks_repeated_clicks_and_recovers_after_completion() {
    for(const mailOk of [true,false]){
      const requests=[];
      let finish;
      const page=await H.open({beforeParse(w){
        const original=w.fetch;
        w.fetch=(url,opts)=>{
          if(opts&&url==="/api/config")requests.push(url);
          if(opts&&url==="/api/email/test"){
            requests.push(url);
            return new Promise(resolve=>{finish=()=>resolve({ok:mailOk,status:mailOk?200:504,
              json:async()=>mailOk?{ok:true,sent_to:["a@example.com"]}:{error:"Email test timed out."}})});
          }
          return original(url,opts);
        };
      }});
      const button=page.document.getElementById("emailtest");
      button.click();
      button.click();
      await H.settle(page.window);
      assert(button.disabled,"button remains enabled while sending");
      button.click();
      assert.deepStrictEqual(requests,["/api/config","/api/email/test"]);
      finish();
      await H.settle(page.window);
      assert(!button.disabled,"button did not recover after completion");
      assert(page.document.getElementById("emsg").textContent.includes(mailOk?"Sent to":"timed out"));
    }
  },
  async test_email_test_keeps_save_warnings_with_success() {
    for (const notice of [
      "Saved and in use, but the disk did not confirm the write will survive a power cut.",
      "Mail server changed, so the SMTP password was cleared.",
    ]) {
      const { message, requests } = await testEmailResult(notice);
      assert(message.textContent.includes(notice), "save warning was lost");
      assert(message.textContent.includes("Sent to a@example.com."), "email result was lost");
      assert(message.classList.contains("err"), "warning styled as success");
      assert.deepStrictEqual(requests, ["/api/config", "/api/email/test"]);
    }
  },
  async test_email_test_keeps_save_warning_when_email_fails() {
    const notice = "Settings may not survive a power cut.";
    const { message } = await testEmailResult(notice, false);
    assert(message.textContent.includes(notice), "save warning was lost on email failure");
    assert(message.textContent.includes("Email send failed."));
    assert(message.classList.contains("err"));
  },
  async test_email_test_without_warning_reports_success() {
    const { message } = await testEmailResult(null);
    assert.equal(message.textContent, "Sent to a@example.com.");
    assert(message.classList.contains("ok"));
  },
  async test_email_test_does_not_send_after_save_failure() {
    const { message, requests } = await testEmailResult(null, true, false);
    assert.equal(message.textContent, "Settings save failed.");
    assert(message.classList.contains("err"));
    assert.deepStrictEqual(requests, ["/api/config"]);
  },
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
