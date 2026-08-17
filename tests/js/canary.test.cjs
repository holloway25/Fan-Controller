// The render canary, run rather than read.
//
// It only fires when something is already broken, which makes it
// exactly the kind of code that rots unnoticed: nobody would spot a
// canary that had stopped working. Until now it was asserted by
// grepping the template for "function canary".
const assert = require("assert");
const H = require("./harness.cjs");

const tests = {};

// Break a render the way a real bug does: the code runs and the nodes
// never arrive. Synchronous, so the canary sees the same DOM the user
// would - an earlier version removed the rows from a MutationObserver,
// which runs as a microtask and therefore fired AFTER the canary had
// already looked. The test was measuring its own timing.
function swallowAppends(id) {
  return w => {
    w.document.addEventListener("DOMContentLoaded", () => {
      const el = w.document.getElementById(id);
      if (el) el.appendChild = () => {};
    });
  };
}

tests["a healthy page reports nothing"] = async () => {
  const t = H.now();
  const { posted, errors } = await H.open({
    state: H.baseState({ events: [
      { at: t, msg: "Fan on - 1000 ppm", cls: "ok" },
      { at: t - 60, msg: "Changed: on_at", cls: "" },
    ] }),
  });
  assert.deepStrictEqual(errors, [], "page errors: " + errors.join("; "));
  assert.deepStrictEqual(posted, [],
    "a working page reported itself: " + JSON.stringify(posted));
};

tests["an empty log with events to show is reported"] = async () => {
  const t = H.now();
  const { posted } = await H.open({
    state: H.baseState({ events: [
      { at: t, msg: "Fan on - 1000 ppm", cls: "ok" },
    ] }),
    // Break the render the way a real bug would: the rows are built
    // and then something removes them. The canary must not care HOW.
    beforeParse: swallowAppends("log"),
  });
  const render = posted.filter(p => p.kind === "render");
  assert.ok(render.length >= 1,
    "nothing reported: " + JSON.stringify(posted));
  assert.ok(/activity log rendered no rows/.test(render[0].msg), render[0].msg);
  assert.ok(/for 1 events/.test(render[0].msg), render[0].msg);
};

tests["the empty state is not reported"] = async () => {
  // No events at all is a correct render of nothing. Reporting it
  // would train everyone to ignore the code.
  const { posted, document } = await H.open({
    state: H.baseState({ events: [] }),
  });
  assert.strictEqual(document.getElementById("log").textContent,
    "Nothing yet.");
  assert.deepStrictEqual(posted.filter(p => p.kind === "render"), []);
};

tests["an empty trace with points to draw is reported"] = async () => {
  const { posted, window } = await H.open({
    beforeParse: swallowAppends("trace"),
  });
  // THE SECOND POLL IS THE FIRST ONE THAT CAN SEE THIS. State and
  // history are separate fetches, so the first render has no HIST and
  // the canary correctly says nothing about a trace it cannot judge.
  await H.poll(window);
  const render = posted.filter(p => p.kind === "render");
  assert.ok(render.some(r => /CO2 trace rendered nothing/.test(r.msg)),
    "the trace canary did not fire: " + JSON.stringify(posted));
};

tests["a short history is not reported as a broken trace"] = async () => {
  // drawTrace returns early below two usable points. That is correct
  // behaviour, not a failure, and the canary uses the same threshold.
  const t = H.now();
  const { posted } = await H.open({
    history: H.baseHistory([{ t: t - 60, v: 800, tc: 21, rh: 50 }]),
  });
  assert.deepStrictEqual(posted.filter(p => p.kind === "render"), [],
    "one point was treated as a broken render");
};

tests["a condition reports once, not every poll"] = async () => {
  const t = H.now();
  const { posted } = await H.open({
    state: H.baseState({ events: [{ at: t, msg: "Fan on", cls: "ok" }] }),
    beforeParse: swallowAppends("log"),
    // Several polls' worth: a wall display must not turn a broken
    // render into a request every five seconds.
    ticks: 4,
  });
  const render = posted.filter(p => /activity log/.test(p.msg));
  assert.strictEqual(render.length, 1,
    "reported " + render.length + " times");
};

module.exports = tests;
