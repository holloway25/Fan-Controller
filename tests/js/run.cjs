// Run every *.test.cjs beside this file.
//
// Deliberately tiny: a test runner is a dependency that has to be
// installed, pinned, upgraded and understood, and this needs to
// discover files and report failures. jsdom is the only dependency
// worth the cost here.
//
// Output matches the Python harness - PASS/FAIL lines and a non-zero
// exit - so tests/test_js.py can read it and so a person reading CI
// output does not have to learn a second format.
const fs = require("fs");
const path = require("path");
const H = require("./harness.cjs");

async function main() {
  // An optional directory, so the "no tests found" path can be tested
  // without copying this file somewhere node cannot resolve jsdom from.
  const here = process.argv[2] ? path.resolve(process.argv[2]) : __dirname;
  const files = fs.readdirSync(here)
    .filter(f => f.endsWith(".test.cjs")).sort();

  let failed = 0;
  let ran = 0;

  for (const file of files) {
    const tests = require(path.join(here, file));
    for (const name of Object.keys(tests).sort()) {
      ran++;
      try {
        // A HANG MUST REPORT, NOT STALL. Without this a test that never
        // settles takes the whole CI job down with a timeout and no
        // indication of which one it was.
        await Promise.race([
          tests[name](),
          new Promise((_, rej) => setTimeout(
            () => rej(new Error("timed out after 20s")), 20000)),
        ]);
        console.log("PASS  " + name);
      } catch (e) {
        failed++;
        const msg = (e && e.message ? e.message : String(e))
          .split("\n").slice(0, 4).join(" ");
        console.log("FAIL  " + name + ": " + msg);
      } finally {
        // Close the windows this test opened. The page installs a
        // five-second interval and an unclosed window keeps it - and
        // the process - alive.
        H.closeAll();
      }
    }
  }

  console.log("");
  if (failed) {
    console.log(failed + " of " + ran + " failed");
    process.exit(1);
  }
  if (!ran) {
    // An empty run is not a pass. The same rule the mutation harness
    // learned: a step that does no work and goes green is worse than
    // one that fails.
    console.log("no tests found in " + here);
    process.exit(2);
  }
  console.log("all " + ran + " passed");
}

main().catch(e => {
  console.log("RUNNER ERROR: " + (e && e.stack ? e.stack : e));
  process.exit(1);
});
