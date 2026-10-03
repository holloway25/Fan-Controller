const assert = require('assert');
const H = require('./harness.cjs');
const tests = {};

tests['bad historical metrics do not freeze newer metrics or activity'] = async () => {
  const t = H.now();
  const state = H.baseState({temperature:21,humidity:50,events:[]});
  const history = H.baseHistory([
    {t:t-120,v:800,tc:20,rh:48},
    {t:t-60,v:810,tc:21,rh:49},
    {t:t-30,v:820,tc:'bad',rh:'bad'},
    {t:t-20,v:830,tc:NaN,rh:Infinity},
    {t:t-10,v:840,tc:true,rh:[]},
  ]);
  const page = await H.open({state,history});
  await H.poll(page.window);
  state.temperature = 23.5;
  state.humidity = 55;
  state.events = [{at:t,msg:'Fan on — 70% RH',cls:'ok'}];
  await H.poll(page.window);
  assert.strictEqual(page.document.getElementById('tc').textContent,'23.5°C');
  assert.strictEqual(page.document.getElementById('rh').textContent,'55.0%');
  assert.ok(page.document.getElementById('log').textContent.includes('Fan on — 70% RH'));
  const lines = [...page.document.querySelectorAll('#trace2 polyline')];
  assert.strictEqual(lines.length,2);
  assert.ok(lines.every(el => !/NaN|Infinity/.test(el.getAttribute('points'))));
  assert.deepStrictEqual(page.errors,[]);
};

tests['historical metrics with no finite numbers show no data without throwing'] = async () => {
  const t = H.now();
  const page = await H.open({history:H.baseHistory([
    {t:t-60,v:800,tc:'bad',rh:null},
    {t:t-30,v:810,tc:Infinity,rh:false},
  ])});
  await H.poll(page.window);
  assert.strictEqual(page.document.querySelectorAll('#trace2 polyline').length,0);
  assert.ok(page.document.getElementById('trace2').textContent.includes('no data yet'));
  assert.deepStrictEqual(page.errors,[]);
};

module.exports = tests;
