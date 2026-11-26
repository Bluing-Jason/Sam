// Run with: node --test tests/test_daily_energy.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const html = fs.readFileSync(path.join(__dirname, '..', 'solar_dashboard.html'), 'utf8');
const script = Array.from(html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi))
  .map(m => m[1]).find(s => s.trim());
const HOUR = 3600000;
const midnight = Date.parse('2026-10-22T00:00:00+08:00');
const NOW = midnight + 12 * HOUR;
function row(t, pv = 50, load = 25) {
  // Controller counters intentionally carry 580 Wh from the preceding cycle.
  return { timestamp_ms: t, pv_power_w: pv, load_power_w: load,
    today_chg_kwh: 0.58, today_load_kwh: 0.58 };
}
function halfHourly(start, end, pv = 50, load = 25) {
  const result = [];
  for (let t = start; t <= end; t += HOUR / 2) result.push(row(t, pv, load));
  return result;
}
function near(actual, expected) { assert.ok(Math.abs(actual - expected) < 1e-7, actual + ' != ' + expected); }
function harness(fetchImpl) {
  const nodes = new Map(), charts = [];
  function element() {
    const classes = new Set();
    return { dataset: {}, isConnected: true, textContent: '', innerHTML: '', width:500, height:250,
      addEventListener() {}, querySelectorAll() {return [];},
      classList: {add(name) {classes.add(name);}, remove(name) {classes.delete(name);}, contains(name) {return classes.has(name);}},
      getContext() { return {clearRect() {}}; },
    };
  }
  class Chart {
    static defaults = {font:{}};
    constructor(canvas, config) { this.config = config; charts.push(this); }
    destroy() {}
  }
  class Clock extends Date { static now() {return NOW;} }
  const document = { body:{getAttribute() {return 'device';}},
    getElementById(id) {if(!nodes.has(id)) nodes.set(id,element());return nodes.get(id);},
    querySelectorAll() {return [];}, addEventListener() {},
  };
  const context = vm.createContext({ document, window:{addEventListener(){}}, Date:Clock, Chart,
    fetch:fetchImpl || (async()=>{throw Error('Unexpected network call');}),
    console:{warn(){},error(){},log(){}},
    setTimeout(){},clearTimeout(){},setInterval(){},clearInterval(){},
  });
  vm.runInContext(script.replace(/\bboot\(\);\s*$/, ''),context);
  const api = vm.runInContext('({State, hkEnergyDateKey, buildDailyEnergySeries, dailyEnergyNote, loadDailyEnergyChart})',context);
  return {...api,document,nodes,charts};
}

test('580 Wh carried controller counters do not become today energy; 25 W for 12 h is 300 Wh',()=>{
  const h=harness();
  const records=halfHourly(midnight,NOW);
  records[8].today_load_kwh=0; // Device resets at 04:00 rather than Hong Kong midnight.
  const result=h.buildDailyEnergySeries('a',records,1,NOW)[0];
  near(result.loadWh,300);near(result.pvWh,600);
  near(result.loadCoveredMs,12*HOUR);
  assert.ok(h.dailyEnergyNote([result]).includes('今天截至 12:00'));
});

test('an interval crossing Hong Kong midnight is split using interpolated power',()=>{
  const h=harness();
  const records=[row(midnight-HOUR/2,0,20),row(midnight+HOUR/2,60,20)];
  const result=h.buildDailyEnergySeries('a',records,2,midnight+HOUR/2);
  near(result[0].pvWh,7.5);near(result[1].pvWh,22.5);
  near(result[0].loadWh,10);near(result[1].loadWh,10);
  near(result[0].pvWh+result[1].pvWh,30);
});

test('calendar day is fixed to Hong Kong rather than US server or browser timezone',()=>{
  const h=harness();
  assert.equal(h.hkEnergyDateKey(Date.parse('2026-10-21T16:10:00Z')),'2026-10-22');
  assert.equal(h.hkEnergyDateKey(Date.parse('2026-10-21T15:59:59Z')),'2026-10-21');
});

test('long offline gaps are excluded rather than filled with assumed power',()=>{
  const h=harness();
  const records=[row(midnight),row(midnight+HOUR/2),row(midnight+4*HOUR),row(midnight+4.5*HOUR)];
  const result=h.buildDailyEnergySeries('a',records,1,NOW)[0];
  near(result.loadWh,25);near(result.loadCoveredMs,HOUR);
  near(result.expectedMs,12*HOUR);
});

test('merged CSV, RAM and live readings at the same timestamp count once',()=>{
  const h=harness();
  h.State.history.a=[{t:midnight,pvP:50,loadP:25},{t:midnight+HOUR/2,pvP:50,loadP:25}];
  h.State.data.a={lastUpdate:midnight+HOUR,lastReading:{pvPower:50,loadPower:25}};
  const records=[row(midnight+HOUR/2),row(midnight),row(midnight),row(midnight+HOUR)];
  const result=h.buildDailyEnergySeries('a',records,1,NOW)[0];
  near(result.loadWh,25);near(result.pvWh,50);near(result.loadCoveredMs,HOUR);
});

test('missing metrics remain unknown while measured zero is preserved',()=>{
  const h=harness();
  const result=h.buildDailyEnergySeries('a',[row(midnight,0,''),row(midnight+HOUR,0,null)],1,NOW)[0];
  assert.equal(result.pvWh,0);assert.equal(result.loadWh,null);
  near(result.pvCoveredMs,HOUR);assert.equal(result.loadCoveredMs,0);
});

test('stale live counter readings cannot be assigned to the current day',()=>{
  const h=harness();
  h.State.data.a={lastUpdate:midnight-HOUR/2,lastReading:{pvPower:50,loadPower:25,todayChgKwh:0.58,todayLoadKwh:0.58}};
  const result=h.buildDailyEnergySeries('a',[row(midnight-HOUR)],2,NOW);
  near(result[0].loadWh,12.5);
  assert.equal(result[1].loadWh,null);assert.equal(result[1].pvWh,null);
  assert.ok(h.dailyEnergyNote(result).includes('今天暫無'));
});

test('calendar range includes missing days without replacing them with zero',()=>{
  const h=harness();
  const result=h.buildDailyEnergySeries('a',halfHourly(midnight-2*24*HOUR,midnight-2*24*HOUR+HOUR),3,NOW);
  assert.deepEqual(Array.from(result,d=>d.date),['2026-10-20','2026-10-21','2026-10-22']);
  near(result[0].loadWh,25);assert.equal(result[1].loadWh,null);assert.equal(result[2].loadWh,null);
});

test('invalid, negative and future power samples do not manufacture energy',()=>{
  const h=harness();
  const records=[row(midnight,50,-1),row(midnight+HOUR,50,'not-a-number'),row(NOW+HOUR,50,25)];
  const result=h.buildDailyEnergySeries('a',records,1,NOW)[0];
  assert.equal(result.loadWh,null);near(result.pvWh,50);
});

test('old counter-only CSV and isolated points show unknown instead of 580 Wh or zero',()=>{
  const h=harness();
  const result=h.buildDailyEnergySeries('a',[
    {timestamp_ms:midnight,today_chg_kwh:0.58,today_load_kwh:0.58},row(midnight+HOUR)],1,NOW)[0];
  assert.equal(result.loadWh,null);assert.equal(result.pvWh,null);
});

test('power is not extrapolated after the final reading',()=>{
  const h=harness();
  const result=h.buildDailyEnergySeries('a',halfHourly(midnight,midnight+6*HOUR),1,NOW)[0];
  near(result.loadWh,150);near(result.loadCoveredMs,6*HOUR);
  assert.ok(h.dailyEnergyNote([result]).includes('今天截至 06:00'));
});

test('range boundary clips an earlier interval instead of carrying earlier energy forward',()=>{
  const h=harness();
  const result=h.buildDailyEnergySeries('a',[row(midnight-HOUR/2),row(midnight+HOUR/2)],1,NOW)[0];
  near(result.loadWh,12.5);near(result.loadCoveredMs,HOUR/2);
});

test('rendered chart uses estimated Wh and reports partial coverage in its tooltip',async()=>{
  const h=harness(async()=>({ok:true,json:async()=>({success:true,rows:halfHourly(midnight,midnight+6*HOUR)})}));
  await h.loadDailyEnergyChart('a',1);
  assert.equal(h.charts.length,1);
  const config=h.charts[0].config;
  assert.equal(config.data.datasets[1].data[0],150);
  assert.ok(config.data.datasets[1].label.includes('估算 Wh'));
  const callbacks=config.options.plugins.tooltip.callbacks;
  assert.equal(callbacks.title([{dataIndex:0}]),'2026-10-22（香港時間）');
  assert.ok(callbacks.afterLabel({dataIndex:0,datasetIndex:1}).includes('資料未齊'));
  assert.ok(callbacks.afterBody([{dataIndex:0}])[1].includes('+150 Wh'));
});

test('empty chart states do not present missing data as zero usage',async()=>{
  const h=harness(async()=>({ok:true,json:async()=>({success:true,rows:[]})}));
  await h.loadDailyEnergyChart('a',1);
  assert.equal(h.charts.length,0);
  assert.ok(h.document.getElementById('dvDailyLoading').textContent.includes('暫無可計算'));
});

test('net energy is withheld when PV and load cover different periods',async()=>{
  const records=[row(midnight,50,null),row(midnight+HOUR/2),row(midnight+HOUR)];
  const h=harness(async()=>({ok:true,json:async()=>({success:true,rows:records})}));
  await h.loadDailyEnergyChart('a',1);
  const config=h.charts[0].config;
  assert.ok(config.options.plugins.tooltip.callbacks.afterBody([{dataIndex:0}])[1].includes('覆蓋不一致'));
});

test('a slow previous station response cannot overwrite a newer chart',async()=>{
  let finishFirst;
  const h=harness(async url=>{
    if(url.includes('deviceId=a')) return new Promise(resolve=>{finishFirst=()=>resolve({ok:true,json:async()=>({success:true,rows:halfHourly(midnight,NOW,10,10)})});});
    return {ok:true,json:async()=>({success:true,rows:halfHourly(midnight,NOW,50,25)})};
  });
  const pending=h.loadDailyEnergyChart('a',1);
  await h.loadDailyEnergyChart('b',1);
  finishFirst();await pending;
  assert.equal(h.charts.length,1);
  assert.equal(h.charts[0].config.data.datasets[1].data[0],300);
});
