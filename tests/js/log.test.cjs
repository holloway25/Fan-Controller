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

tests["every row carries a day and month, today included"] = async () => {
  // Today's rows used to show the time alone, to save width in a
  // fixed-width column. It read badly: a bare "18:29" sitting below a
  // dated row leaves the reader working out which day it belongs to,
  // and entries either side of midnight are minutes apart and a day
  // apart with only one of them saying so.
  //
  // Asserted on BOTH a fresh event and an old one, because the bug
  // this replaces was conditional on the date matching today - a test
  // using only an old timestamp passed throughout.
  const t = H.now();
  const { document } = await H.open({
    state: H.baseState({ events: [
      { at: t, msg: "from today", cls: "" },
      { at: t - 86400 * 3, msg: "from three days ago", cls: "" },
    ] }),
  });
  for (const row of rows(document)) {
    const shown = row.querySelector("time").textContent;
    assert.ok(/^\d{2}\/\d{2} \d{2}:\d{2}$/.test(shown),
      "row is not dated DD/MM HH:MM: " + JSON.stringify(shown));
  }
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

tests["an expanded timestamp survives the next poll"] = async () => {
  // THE BUG THIS EXISTS FOR: render() deletes and rebuilds every row
  // on each poll, so state held on the element lasted five seconds -
  // tap a timestamp, wait, and it collapsed under you. Reported by
  // review of 3ea51b3, and missed here because the original test
  // tapped and asserted without ever polling. A lifecycle bug needs
  // the lifecycle.
  const t = H.now();
  const { document, window } = await H.open({
    state: H.baseState({ events: [
      { at: t, msg: "an event", cls: "" },
      { at: t - 60, msg: "another", cls: "" },
    ] }),
  });
  const el = () => rows(document)[0].querySelector("time");
  const short = el().textContent;

  el().dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  const long = el().textContent;
  assert.notStrictEqual(long, short);

  await H.poll(window, 2);

  assert.strictEqual(el().textContent, long,
    "the timestamp collapsed on the next poll");
  assert.strictEqual(el().getAttribute("aria-expanded"), "true");
  // And the row beside it is untouched.
  assert.strictEqual(rows(document)[1].querySelector("time")
    .getAttribute("aria-expanded"), "false");
};

tests["expansion follows the event, not the row position"] = async () => {
  // A newer event pushes everything down a row. The expanded one is
  // still the same event and must still be expanded.
  const t = H.now();
  const state = H.baseState({ events: [
    { at: t - 60, msg: "the interesting one", cls: "" },
  ] });
  const { document, window } = await H.open({ state });
  const first = rows(document)[0].querySelector("time");
  first.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  const long = first.textContent;

  state.events.unshift({ at: t, msg: "something newer", cls: "" });
  await H.poll(window, 2);

  const r = rows(document);
  assert.strictEqual(r.length, 2, "rows: " + r.length);
  assert.strictEqual(r[1].querySelector("time").textContent, long,
    "expansion did not follow the event down");
  assert.strictEqual(r[0].querySelector("time")
    .getAttribute("aria-expanded"), "false",
    "the new row arrived expanded");
};

tests["collapsing survives a poll too"] = async () => {
  const t = H.now();
  const { document, window } = await H.open({
    state: H.baseState({ events: [{ at: t, msg: "an event", cls: "" }] }),
  });
  const el = () => rows(document)[0].querySelector("time");
  const short = el().textContent;
  el().dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  el().dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  await H.poll(window, 2);
  assert.strictEqual(el().textContent, short, "it re-expanded itself");
  assert.strictEqual(el().getAttribute("aria-expanded"), "false");
};

tests["a key is dropped once its event leaves the log"] = async () => {
  // THE BUG THIS EXISTS FOR: nothing ever removed from the expanded
  // set. Keys were added on a tap and kept for the life of the page,
  // while the comment beside the set claimed it was bounded by the
  // log. Only MATCHING was bounded. Reported by review of a6779ed.
  //
  // Asserted on the set itself rather than on the screen, because the
  // symptom is precisely that nothing shows: a key for an event that
  // is no longer sent has no row to be wrong about.
  const t = H.now();
  const state = H.baseState({ events: [
    { at: t, msg: "the one that goes away", cls: "" },
    { at: t - 60, msg: "the one that stays", cls: "" },
  ] });
  const { document, window } = await H.open({ state });

  rows(document).forEach(r =>
    r.querySelector("time").dispatchEvent(
      new window.MouseEvent("click", { bubbles: true })));
  assert.strictEqual(window.eval("EXPANDED.size"), 2,
    "both taps should have been recorded");

  // The older event ages out of the window the server sends.
  state.events = [{ at: t, msg: "the one that goes away", cls: "" }];
  await H.poll(window, 2);

  assert.strictEqual(window.eval("EXPANDED.size"), 1,
    "the key for the departed event was kept");
  // The one still on screen is untouched by the prune.
  assert.strictEqual(rows(document)[0].querySelector("time")
    .getAttribute("aria-expanded"), "true",
    "the prune took a visible event with it");
};

tests["an event that comes back is not still expanded"] = async () => {
  // The user-visible half of the same property, and the one that would
  // survive the set being reimplemented. Nothing in this application
  // re-sends a departed event, so this is the behaviour the prune
  // implies rather than a case anyone will hit.
  const t = H.now();
  const gone = { at: t - 60, msg: "here, then not, then here", cls: "" };
  const state = H.baseState({ events: [{ at: t, msg: "steady", cls: "" }, gone] });
  const { document, window } = await H.open({ state });

  const target = () => rows(document)[1].querySelector("time");
  target().dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  assert.strictEqual(target().getAttribute("aria-expanded"), "true");

  state.events = [{ at: t, msg: "steady", cls: "" }];
  await H.poll(window, 2);
  assert.strictEqual(rows(document).length, 1, "it did not leave");

  state.events = [{ at: t, msg: "steady", cls: "" }, gone];
  await H.poll(window, 2);
  assert.strictEqual(target().getAttribute("aria-expanded"), "false",
    "it came back already expanded, so the key was never dropped");
};

tests["the timestamp is reachable by hover and by screen reader"] = async () => {
  // ADDED BEFORE the source assertions were deleted, not after. The two
  // dashboard-source tests being removed asserted `when.title=` and
  // setAttribute("aria-label") by reading the file. jsdom covered the
  // rest of what they claimed - a <time> element, the short form, the
  // machine-readable value - but not these two, so deleting them first
  // would have lost real coverage while the count said otherwise.
  const t = H.now();
  const { document } = await H.open({ state: H.baseState({ events: [
    { at: t, msg: "something happened", cls: "" },
  ] }) });

  const when = rows(document)[0].querySelector("time");
  const title = when.getAttribute("title") || "";
  const label = when.getAttribute("aria-label") || "";

  assert.ok(title.length > when.textContent.length,
    "the full timestamp is not reachable on hover");
  assert.ok(label.length > 0, "no accessible label on the timestamp");
  // A tooltip is nothing on a touch screen, which is why both exist.
  assert.ok(label.length >= title.length - 1, "the label is not the full value");
};

module.exports = tests;
