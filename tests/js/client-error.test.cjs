const assert = require('assert');
const H = require('./harness.cjs');

module.exports = {
  'failed client error reports do not generate unhandled rejection reports': async () => {
    const { window: w } = await H.open();
    const offline = new Error('simulated reporting endpoint offline');
    let calls = 0, unhandled = 0;
    const original = w.fetch;
    function reportRejection() {
      const event = new w.Event('unhandledrejection');
      event.reason = offline;
      w.dispatchEvent(event);
    }
    const onUnhandled = reason => {
      if (reason !== offline) return;
      unhandled++;
      // Model the browser's unhandledrejection event, but cap a broken loop.
      if (calls < 4) reportRejection();
    };
    process.on('unhandledRejection', onUnhandled);
    w.fetch = (url, opts) => {
      if (String(url) !== '/api/clienterror') return original(url, opts);
      calls++;
      return Promise.reject(offline);
    };
    try {
      reportRejection();
      for (let i = 0; i < 8; i++) await new Promise(resolve => setImmediate(resolve));
      assert.strictEqual(unhandled, 0, 'Reporting failure became an unhandled rejection');
      assert.strictEqual(calls, 1, 'Reporting request recursively reported its own failure');
      w.fetch = () => { throw offline; };
      assert.doesNotThrow(() => w._report('synchronous fetch failure'));
    } finally {
      w.fetch = original;
      process.removeListener('unhandledRejection', onUnhandled);
    }
  },
};
