const assert = require('assert');
const H = require('./harness.cjs');

module.exports = {
  async test_elapsed_heartbeat_and_refresh_ignore_server_clock_offsets() {
    for (const offset of [-600,600]) {
      const state=H.baseState({reading_at:H.now()+offset,draw_loop_at:H.now()+offset,
        process_started_at:H.now()+offset-100, sensor_age:1,draw_loop_age:5});
      const page=await H.open({state});
      assert(!page.document.getElementById('drawloopmsg').textContent.includes('not responding'));
      assert(page.document.getElementById('sub').textContent.startsWith('Refresh in 29s'));
      assert(page.document.getElementById('dot').classList.contains('live'));
      state.draw_loop_age=31;state.sensor_age=31;
      await H.poll(page.window);
      assert(page.document.getElementById('drawloopmsg').textContent.includes('not responding'));
      assert(page.document.getElementById('sub').textContent.startsWith('Refresh due'));
      assert(page.document.getElementById('dot').classList.contains('bad'));
    }
  },
};
