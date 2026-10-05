const assert = require('assert');
const H = require('./harness.cjs');
const tests = {};

tests['rollback epoch replaces cached chart with all sorted samples'] = async () => {
  const t = H.now();
  const state = H.baseState({history_rev:2,history_generation:'g1'});
  const history = H.baseHistory([{t:t-60,v:700},{t:t-30,v:800}]);
  history.history_rev=2;
  const page = await H.open({state,history});
  await H.poll(page.window);
  state.co2=900; state.history_rev=3; state.history_generation='g1:rollback';
  Object.assign(history,{full:true,history_rev:3,history_generation:'g1:rollback',
    first_t:t-600,history:[{t:t-600,v:900},{t:t-60,v:700},{t:t-30,v:800}]});
  await H.poll(page.window);
  const line=page.document.querySelector('#trace polyline');
  assert(line, 'CO2 chart not drawn');
  const points=line.getAttribute('points').trim().split(/\s+/);
  assert.strictEqual(points.length,3,'rollback reading missing from chart');
  const xs=points.map(p=>Number(p.split(',')[0]));
  assert(xs[0]<xs[1]&&xs[1]<xs[2],'chart does not progress chronologically');
  assert.deepStrictEqual(page.errors,[]);
};

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
