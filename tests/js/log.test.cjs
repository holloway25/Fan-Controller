// The activity log, rendered.
//
// THE REGRESSION THIS WOULD HAVE CAUGHT: on 2026-08-16 the colouring
// rules were anchored on "^Draw fault" and "^Fan ", and both stopped
// matching the moment messages gained an FC- prefix. Faults rendered
// as ordinary text for a day, every test green, found by someone
// looking at a screen.
//
// The rules themselves now live in controller.event_class and are
// tested in Python. What is tested HERE is the other half: that the
// page puts the server's answer on the row, and that the row says what
// the event said.
const assert = require("assert");
const H = require("./harness.cjs");

const tests = {};

function rows(document) {
  return [...document.getElementById("log").children];
}

tests["every event becomes a row, newest first"] = async () => {
  const t = H.now();
  const { document } = await H.open({
    state: H.baseState({ events: [
      { at: t, msg: "FC-003 Draw fault - 8.2 W", cls: "fault" },
      { at: t - 60, msg: "Fan on - 1000 ppm", cls: "ok" },
      { at: t - 120, msg: "Changed: on_at", cls: "" },
    ] }),
  });
  const r = rows(document);
  assert.strictEqual(r.length, 3, "rows: " + r.length);
  assert.ok(/Draw fault/.test(r[0].textContent), r[0].textContent);
  assert.ok(/Changed: on_at/.test(r[2].textContent), r[2].textContent);
};

tests["the class comes from the server, not from the words"] = async () => {
  const t = H.now();
  // Deliberately contradictory: a message that the OLD in-page rules
  // would have coloured green, marked "fault" by the server. The page
  // must obey the field, because the field is the tested decision.
  const { document } = await H.open({
    state: H.baseState({ events: [
      { at: t, msg: "Fan on - 1000 ppm", cls: "fault" },
      { at: t - 60, msg: "FC-003 Draw fault", cls: "ok" },
      { at: t - 120, msg: "Mode: Manual", cls: "m-manual" },
    ] }),
  });
  const r = rows(document);
  assert.strictEqual(r[0].className, "fault", r[0].className);
  assert.strictEqual(r[1].className, "ok", r[1].className);
  assert.strictEqual(r[2].className, "m-manual", r[2].className);
};

tests["a missing class leaves the row uncoloured"] = async () => {
  const t = H.now();
  const { document } = await H.open({
    state: H.baseState({ events: [
      { at: t, msg: "something new", cls: null },
      { at: t - 1, msg: "something else" },
    ] }),
  });
  const r = rows(document);
  assert.strictEqual(r[0].className, "", "null cls: " + r[0].className);
  assert.strictEqual(r[1].className, "", "absent cls: " + r[1].className);
};

tests["event text is never treated as markup"] = async () => {
  // /api/clienterror is open even once a UI password is set, so a LAN
  // device can put text into the activity log. Interpolating that into
  // innerHTML was a stored XSS; textContent is the fix and this is the
  // test that says so.
  const t = H.now();
  const { document } = await H.open({
    state: H.baseState({ events: [
      { at: t, msg: "<img src=x onerror=alert(1)>", cls: "" },
    ] }),
  });
  const r = rows(document);
  assert.strictEqual(r[0].getElementsByTagName("img").length, 0,
    "markup was parsed: " + r[0].innerHTML);
  assert.ok(/<img/.test(r[0].textContent), r[0].textContent);
};

tests["today shows a time, older entries show a date"] = async () => {
  const t = H.now();
  const { document } = await H.open({
    state: H.baseState({ events: [
      { at: t, msg: "today", cls: "" },
      { at: t - 3 * 86400, msg: "three days ago", cls: "" },
    ] }),
  });
  const r = rows(document);
  const todayStamp = r[0].querySelector("time").textContent;
  const oldStamp = r[1].querySelector("time").textContent;
  assert.ok(/^\d{2}:\d{2}$/.test(todayStamp),
    "today should be a time only: " + todayStamp);
  assert.ok(/^\d{2}\/\d{2} \d{2}:\d{2}$/.test(oldStamp),
    "older should carry a day and month: " + oldStamp);
};

tests["the timestamp expands on a tap and collapses again"] = async () => {
  const t = H.now();
  const { document, window } = await H.open({
    state: H.baseState({ events: [{ at: t, msg: "an event", cls: "" }] }),
  });
  const el = rows(document)[0].querySelector("time");
  const short = el.textContent;

  assert.strictEqual(el.getAttribute("aria-expanded"), "false");
  el.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  const long = el.textContent;
  assert.notStrictEqual(long, short, "a tap did not expand it");
  assert.strictEqual(el.getAttribute("aria-expanded"), "true");
  assert.ok(long.length > short.length, long);

  el.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  assert.strictEqual(el.textContent, short, "a second tap did not collapse");
  assert.strictEqual(el.getAttribute("aria-expanded"), "false");
};

tests["the timestamp expands from the keyboard too"] = async () => {
  const t = H.now();
  const { document, window } = await H.open({
    state: H.baseState({ events: [{ at: t, msg: "an event", cls: "" }] }),
  });
  const el = rows(document)[0].querySelector("time");
  const short = el.textContent;
  assert.strictEqual(el.tabIndex, 0, "not reachable by keyboard");

  el.dispatchEvent(new window.KeyboardEvent("keydown",
    { key: "Enter", bubbles: true }));
  assert.notStrictEqual(el.textContent, short, "Enter did nothing");

  el.dispatchEvent(new window.KeyboardEvent("keydown",
    { key: " ", bubbles: true }));
  assert.strictEqual(el.textContent, short, "Space did not collapse it");
};

tests["the expanded form is longer than the column"] = async () => {
  // The point of expanding: the full local value with its offset,
  // which is why it must be allowed to wrap rather than be clipped.
  const t = H.now();
  const { document, window } = await H.open({
    state: H.baseState({ events: [{ at: t, msg: "an event", cls: "" }] }),
  });
  const el = rows(document)[0].querySelector("time");
  el.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  assert.ok(/\d{4}-\d{2}-\d{2}/.test(el.textContent),
    "expanded form has no date: " + el.textContent);
  assert.ok(/[+-]\d{2}:\d{2}$/.test(el.textContent),
    "expanded form has no offset: " + el.textContent);
};

tests["a machine-readable timestamp is on every row"] = async () => {
  const t = H.now();
  const { document } = await H.open({
    state: H.baseState({ events: [{ at: t, msg: "an event", cls: "" }] }),
  });
  const el = rows(document)[0].querySelector("time");
  assert.ok(el.getAttribute("datetime"), "no datetime attribute");
  assert.ok(el.getAttribute("aria-label"),
    "no accessible label - a screen reader gets the short form only");
};

module.exports = tests;
