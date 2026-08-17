// Load the real dashboard under jsdom, with the server stubbed.
//
// THE POINT IS THAT THE PAGE'S OWN CODE RUNS. Everything else in this
// repository asserts the template's SOURCE - that a line is present -
// which catches a deletion and not a mistake. Three regressions got
// past that: log colours that stopped matching when messages gained a
// fault-code prefix, a version footer whose assignment could be made
// unreachable while the string stayed, and a range read off the drawn
// scale rather than the readings.
//
// fetch is installed in beforeParse, NOT after construction. The page
// calls tickUI() as soon as it parses, so a stub attached afterwards
// misses the first render and the tests see an empty page for reasons
// that have nothing to do with the code under test. That cost twenty
// minutes the first time.
const fs = require("fs");
const path = require("path");
const { JSDOM, VirtualConsole } = require("jsdom");

const ROOT = path.resolve(__dirname, "..", "..");

// Every window opened, so the runner can close them between tests.
// The page sets a 5s interval; an unclosed window keeps that timer
// alive and Node never exits. The suite hung on exactly this.
const OPEN = [];
const PAGE = path.join(ROOT, "templates", "index.html");

function now() {
  return Math.floor(Date.now() / 1000);
}

// A state payload shaped like /api/state. Deliberately complete: a
// missing key makes render() throw, and a test that fails because its
// fixture was thin teaches nothing.
function baseState(over) {
  const t = now();
  return Object.assign({
    fan: false,
    co2: 900,
    watts: null,
    watts_at: null,
    events: [],
    config: {
      control: {
        mode: "auto", on_at: 1000, off_at: 740, co2_enabled: true,
        temp_enabled: false, humidity_enabled: false, poll_seconds: 30,
        min_run_minutes: 10, stale_minutes: 0, temp_on: 26, temp_off: 24,
        humidity_on: 70, humidity_off: 60,
      },
      protect: { host: "192.0.2.1", username: "u", sensor_id: "s",
        verify_tls: false },
      plug: { host: "192.0.2.10", user: "u", watts_enabled: false,
        watts_min: 15, watts_max: 25, watts_fault_seconds: 60 },
      email: { enabled: false, host: "", port: 25, username: "",
        sender: "", recipients: "" },
      ui: {},
    },
    history_rev: 1,
    history_generation: "g1",
    fault: null,
    fault_unsaved: false,
    latch: {},
    error: null,
    reading_at: t,
    changed_at: t,
    temperature: 21,
    humidity: 50,
    pm25: null,
  }, over || {});
}

function baseHistory(points, ranges) {
  const t = now();
  const hist = points || Array.from({ length: 40 }, (_, i) => ({
    t: t - (40 - i) * 60,
    v: 700 + i * 5,
    tc: 20 + i * 0.05,
    rh: 45 + i * 0.1,
  }));
  return {
    history: hist,
    history_rev: 1,
    full: true,
    history_generation: "g1",
    first_t: hist.length ? hist[0].t : null,
    ranges: ranges || { v: { lo: 700, hi: 895 }, tc: { lo: 20, hi: 22 },
      rh: { lo: 45, hi: 49 } },
  };
}

// Open the page. Returns the window plus what the page sent back, so a
// test can assert on the DOM and on the reports it produced.
async function open(opts) {
  opts = opts || {};
  const state = opts.state || baseState();
  const history = opts.history || baseHistory();
  const posted = [];
  const errors = [];

  const vc = new VirtualConsole();
  vc.on("jsdomError", e => errors.push(e.message));

  const html = fs.readFileSync(PAGE, "utf8");
  const dom = new JSDOM(html, {
    runScripts: "dangerously",
    virtualConsole: vc,
    url: "http://localhost:8096/",
    pretendToBeVisual: true,
    beforeParse(w) {
      w.fetch = (url, o) => {
        const u = String(url);
        if (u.indexOf("/api/clienterror") === 0) {
          posted.push(JSON.parse(o.body));
          return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
        }
        if (u.indexOf("/api/history") === 0) {
          return Promise.resolve({ ok: true,
            json: () => Promise.resolve(history) });
        }
        return Promise.resolve({ ok: true, json: () => Promise.resolve(state) });
      };
      // Narrow or wide, so the responsive viewBox can be exercised.
      w.matchMedia = q => ({
        matches: !!opts.narrow && /max-width/.test(q),
        media: q, addEventListener() {}, removeEventListener() {},
      });
      if (opts.beforeParse) opts.beforeParse(w);
    },
  });

  OPEN.push(dom);
  await settle(dom.window, opts.ticks || 1);
  return { dom, window: dom.window, document: dom.window.document,
    posted, errors };
}

// The page renders from promises, so the DOM is not ready when the
// constructor returns. Yielding to the microtask queue a few times is
// enough; a fixed sleep would make the suite slow AND flaky.
async function settle(w, ticks) {
  for (let i = 0; i < (ticks || 1) * 12; i++) {
    await new Promise(r => w.setTimeout(r, 5));
  }
}

// The next refresh, without waiting five seconds for it.
//
// THE PAGE FETCHES STATE AND HISTORY SEPARATELY, so the first poll
// renders with no HIST at all - anything depending on the trace can
// only be true from the SECOND poll onwards. Sleeping through the real
// interval would put five seconds into the suite per test and make it
// timing-dependent; calling the page's own tick is what the interval
// does anyway.
async function poll(w, times) {
  for (let i = 0; i < (times || 1); i++) {
    w.eval("tickUI()");
    await settle(w, 1);
  }
}

function closeAll() {
  while (OPEN.length) {
    try { OPEN.pop().window.close(); } catch (e) { /* already gone */ }
  }
}

module.exports = { open, settle, poll, closeAll, baseState,
  baseHistory, now, ROOT };
