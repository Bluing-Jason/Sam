// Run with: node --test tests/test_prediction.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const html = fs.readFileSync(path.join(__dirname, '..', 'solar_dashboard.html'), 'utf8');
const script = Array.from(html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi))
  .map(m => m[1]).find(s => s.trim());
const HOUR = 3600000;
const NOW = Date.parse('2026-10-21T12:00:00+08:00');

function harness(fetchImpl) {
  let now = NOW;
  const nodes = new Map();
  function element() {
    return {
      dataset: {}, isConnected: true, innerHTML: '', textContent: '',
      addEventListener() {}, querySelectorAll() { return []; },
      classList: { contains() { return false; }, add() {}, remove() {} },
    };
  }
  const document = {
    body: { getAttribute() { return 'device'; } },
    getElementById(id) {
      // Main script only registers UI events; tests do not run boot or equipment polling.
      if (!nodes.has(id)) nodes.set(id, element());
      return nodes.get(id);
    },
    querySelectorAll() { return []; }, addEventListener() {},
  };
  class Clock extends Date { static now() { return now; } }
  const context = vm.createContext({
    document, window: { addEventListener() {} }, Date: Clock,
    console: { warn() {}, error() {}, log() {} },
    fetch: fetchImpl || (async () => { throw Error('Unexpected network call'); }),
    setTimeout() { return 1; }, clearTimeout() {}, setInterval() { return 1; }, clearInterval() {},
  });
  vm.runInContext(script.replace(/\bboot\(\);\s*$/, ''), context);
  const api = vm.runInContext('({State, appendHistoryPoint, calcDailyEnergyBalance, loadPredictionHistory, predictRainyRisk, csvRowToPoint, renderPredictionPanel, refreshVisiblePrediction, _predictionHistoryCache})', context);
  return { ...api, nodes, document, setNow(value) { now = value; } };
}
function point(t, pvP = 100, loadP = 20) { return {t, pvP, loadP}; }
function csvRow(t, pv = '100', load = '20') {
  return {timestamp_ms: String(t), pv_power_w: pv, load_power_w: load};
}
function close(actual, expected) { assert.ok(Math.abs(actual - expected) < 1e-7, actual + ' != ' + expected); }

test('default three-second polling retains enough history for predictions after one hour', () => {
  const h = harness();
  for (let i = 0; i <= 1200; i++) h.appendHistoryPoint('a', point(NOW - HOUR + i * 3000));
  assert.equal(h.State.history.a.length, 1201);
  const energy = h.calcDailyEnergyBalance('a');
  close(energy.sampledHours, 1);
  close(energy.avgPvW, 100);
  close(energy.netDailyWh, 1920);
});

test('history expires by time and keeps a full 24 hours even at one-second polling', () => {
  const h = harness();
  for (let i = 0; i <= 86410; i++) h.appendHistoryPoint('a', point(NOW - 24 * HOUR - 10000 + i * 1000));
  assert.equal(h.State.history.a.length, 86401);
  assert.equal(h.State.history.a[0].t, NOW - 24 * HOUR);
});

test('a fresh browser can immediately use three half-hour CSV samples', async () => {
  let calls = 0;
  const rows = [csvRow(NOW - HOUR), csvRow(NOW - HOUR / 2), csvRow(NOW)];
  const h = harness(async url => {
    calls++;
    assert.ok(url.includes('/api/log_get?deviceId=a&limit=50000&since='));
    assert.equal(Number(new URL('http://localhost' + url).searchParams.get('since')), NOW - 24 * HOUR);
    return {ok: true, json: async () => ({success: true, rows})};
  });
  await Promise.all([h.loadPredictionHistory('a'), h.loadPredictionHistory('a')]);
  assert.equal(calls, 1);
  assert.equal(h.State.history.a, undefined);
  const energy = h.calcDailyEnergyBalance('a');
  close(energy.sampledHours, 1);
  const risk = h.predictRainyRisk({id:'a', batVMin:10, batVMax:13, batCapacityAh:100},
    {batVoltage:12}, {forecast:[{date:'2026-10-22', cloud_cover_mean:50}]});
  assert.equal(risk.insufficient, undefined);
  assert.equal(risk.days.length, 1);
});

test('CSV and dense live samples use elapsed time rather than sample counts', async () => {
  const h = harness(async () => ({ok:true, json:async () => ({success:true,
    rows:[csvRow(NOW - 2 * HOUR, '0'), csvRow(NOW - HOUR, '0'), csvRow(NOW, '100')]})}));
  await h.loadPredictionHistory('a');
  // Last hour ramps linearly from 0 W to 100 W, sampled every three seconds.
  for (let i = 0; i <= 1200; i++) h.appendHistoryPoint('a', point(NOW - HOUR + i * 3000, i / 12));
  const energy = h.calcDailyEnergyBalance('a');
  close(energy.sampledHours, 2);
  close(energy.avgPvW, 25);
  close(energy.avgLoadW, 20);
});

test('sparse, invalid, expired and future data cannot manufacture enough valid history', () => {
  const h = harness();
  h.State.history.a = [point(NOW - 12 * HOUR), point(NOW - 6 * HOUR), point(NOW)];
  assert.equal(h.calcDailyEnergyBalance('a'), null);
  h.State.history.a = [point(NOW - HOUR), point(NOW - HOUR/2, null), point(NOW)];
  assert.equal(h.calcDailyEnergyBalance('a'), null);
  h.State.history.a = [point(NOW - 25 * HOUR), point(NOW), point(NOW + HOUR)];
  assert.equal(h.calcDailyEnergyBalance('a'), null);
  h.State.history.a = [point(NOW - HOUR/2), point(NOW - HOUR/4), point(NOW)];
  assert.equal(h.calcDailyEnergyBalance('a'), null);
});

test('failed CSV load falls back to live history and retries without flooding requests', async () => {
  let calls = 0;
  const h = harness(async () => { calls++; throw Error('offline'); });
  h.State.history.a = [point(NOW - HOUR), point(NOW - HOUR/2), point(NOW)];
  await h.loadPredictionHistory('a');
  await h.loadPredictionHistory('a');
  assert.equal(calls, 1);
  assert.ok(h.calcDailyEnergyBalance('a'));
  h.setNow(NOW + 31000);
  await h.loadPredictionHistory('a');
  assert.equal(calls, 2);
});

test('CSV cache refreshes after five minutes and does not mix station data', async () => {
  let calls = 0;
  const h = harness(async () => ({ok:true, json:async () => {calls++; return {success:true,
    rows:[csvRow(NOW - HOUR), csvRow(NOW - HOUR/2), csvRow(NOW)]};}}));
  await h.loadPredictionHistory('a');
  await h.loadPredictionHistory('a');
  assert.equal(calls, 1);
  assert.equal(h.calcDailyEnergyBalance('b'), null);
  h.setNow(NOW + 300001);
  await h.loadPredictionHistory('a');
  assert.equal(calls, 2);
});

test('prediction panel updates from CSV and ignores a response after navigation', async () => {
  let resolveHistory;
  const h = harness(async url => {
    if (url.startsWith('/api/weather')) return {ok:true, json:async () => ({success:true, forecast:[{date:'2026-10-22',cloud_cover_mean:50,solar_quality:'fair',weather_label:'多雲'}]})};
    return new Promise(resolve => { resolveHistory = () => resolve({ok:true, json:async () => ({success:true,
      rows:[csvRow(NOW - HOUR), csvRow(NOW - HOUR/2), csvRow(NOW)]})}); });
  });
  const mount = h.document.getElementById('dvPredContent');
  const weather = {innerHTML: ''}, energy = {innerHTML: ''};
  mount.querySelector = selector => selector === '#predictionWeatherMount' ? weather : energy;
  const dev = {id:'a', lat:22, lon:114, batVMin:10, batVMax:13};
  const reading = {batVoltage:12, batCurrent:1, loadCurrent:1};
  const promise = h.renderPredictionPanel(dev, reading);
  resolveHistory();
  await promise;
  assert.ok(energy.innerHTML.includes('有效 1.0 小時'));
  assert.ok(weather.innerHTML.includes('斷電風險預測'));
  h.setNow(NOW + 300001);
  const stale = h.renderPredictionPanel(dev, reading);
  mount.isConnected = false;
  energy.innerHTML = 'another station';
  weather.innerHTML = 'another station';
  resolveHistory();
  await stale;
  assert.equal(energy.innerHTML, 'another station');
  assert.equal(weather.innerHTML, 'another station');
});


test('an open panel automatically becomes ready when new live history reaches one hour', async () => {
  let calls = 0;
  const h = harness(async () => {calls++; return {ok:true,json:async()=>({success:true,rows:[]})};});
  const mount = h.document.getElementById('dvPredContent');
  const energy = {innerHTML: ''}, weather = {innerHTML: ''};
  mount.querySelector = selector => selector === '#predictionWeatherMount' ? weather : energy;
  mount.dataset.deviceId = 'a';
  mount.dataset.refreshedAt = String(NOW);
  h.State.openDrawerDevice = 'a';
  const dev = {id:'a',batVMin:10,batVMax:13};
  const reading = {batVoltage:12,batCurrent:1,loadCurrent:1};
  h.refreshVisiblePrediction(dev, reading);
  assert.equal(calls, 0);
  h.setNow(NOW + HOUR);
  for (let i=0;i<=1200;i++) h.appendHistoryPoint('a',point(NOW+i*3000));
  h.refreshVisiblePrediction(dev, reading);
  await new Promise(setImmediate);
  assert.equal(calls, 1);
  assert.ok(energy.innerHTML.includes('有效 1.0 小時'));
});
