const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const {test}=require('node:test');
const html=fs.readFileSync(path.join(__dirname,'..','solar_dashboard.html'),'utf8');
const script=Array.from(html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi)).map(m=>m[1]).find(s=>s.trim());
function harness(saved){
 const nodes=new Map(), storage=new Map(), calls=[];
 if(saved!==undefined)storage.set('ty_solar_sizing_v1',saved);
 const node=id=>{
  if(!nodes.has(id)){
   const classes=new Set(),attributes={};const listeners={};
   nodes.set(id,{id,value:'',type:'text',checked:false,hidden:false,dataset:{},style:{},innerHTML:'',textContent:'',listeners,
    classList:{add(x){classes.add(x)},remove(x){classes.delete(x)},contains(x){return classes.has(x)},toggle(x,on){on?classes.add(x):classes.delete(x)}},
    setAttribute(k,v){attributes[k]=v},getAttribute(k){return attributes[k]},removeAttribute(k){delete attributes[k]},
    addEventListener(k,cb){listeners[k]=cb},focus(){},scrollIntoView(){},replaceWith(old){nodes.set(id,old)},
    querySelectorAll(sel){return query(sel)},querySelector(){return null}});
  }
  return nodes.get(id);
 };
 const panels=['sizing','devices','brand','general','email'].map(s=>Object.assign(node('panel-'+s),{dataset:{settingsSection:s}}));
 const links=['sizing','devices','brand','general','email'].map(s=>Object.assign(node('link-'+s),{dataset:{settingsTab:s}}));
 const inputs=['powerW','hoursPerDay','backupDays','voltageV','batteryCapacityAh'].map(s=>Object.assign(node('sizing-'+s),{dataset:{sizingField:s},type:s==='batteryCapacityAh'?'select-one':'number'}));
 function query(sel){
  if(sel==='[data-sizing-field]')return inputs;
  if(sel==='[data-settings-section]')return panels;
  if(sel==='[data-settings-tab]')return links;
  if(sel==='[id^="cfg-"]')return [...nodes.values()].filter(x=>x.id.startsWith('cfg-'));
  return [];
 }
 const attrs={};
 const document={getElementById:node,querySelectorAll:query,querySelector(){return null},addEventListener(){},
  body:{setAttribute(k,v){attrs[k]=v},getAttribute(k){return attrs[k]},classList:{add(){},remove(){}}}};
 const context=vm.createContext({document,location:{hash:''},window:{addEventListener(){}},
  localStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v)},
  console:{log(){},warn(){},error(){}},setTimeout(){},clearTimeout(){},setInterval(){},clearInterval(){},
  fetch:async(url,options)=>{calls.push({url,options});return {ok:true,json:async()=>({success:true,config:{}})}}});
 vm.runInContext(script.replace(/\bboot\(\);\s*$/,''),context);
 vm.runInContext("var rendered=[];toast=()=>{};renderDashboardView=()=>rendered.push('dashboard');renderDeviceView=id=>rendered.push(id);renderTopStats=()=>{};var emailLoads=0;renderEmailConfigSection=()=>{emailLoads++;};",context);
 const api=vm.runInContext('({calculateSolarSizing,selectStandardBattery,loadSizingInputs,wireSizingCalculator,updateSizingResults,parseHashRoute,openSettings,closeSettings,renderAllCards,showSettingsSection,renderSettingsBody,SettingsUI,State,getInputs:()=>sizingInputs,emailLoads:()=>emailLoads})',context);
 return {...api,context,nodes,node,inputs,panels,links,calls,storage,document};
}
const base={powerW:50,hoursPerDay:24,backupDays:5,voltageV:12.8};
test('user defaults use four sun hours and both battery factors at 95%',()=>{
 const h=harness(),r=h.calculateSolarSizing(base);
 assert.equal(r.ok,true);assert.equal(r.dailyWh,1200);assert.equal(r.designWh,1440);
 assert.equal(r.pvWp,515);assert.equal(r.batteryAh,624);assert.equal(r.standardAh,null);
});
test('standard battery sizes always round up and expose over-400 requirements',()=>{
 const h=harness();
 for(const [input,output] of [[0,0],[1,60],[60,60],[60.1,120],[120,120],[121,200],[200,200],[201,400],[400,400],[400.1,null],[-1,null],[Infinity,null]])assert.equal(h.selectStandardBattery(input),output);
});
test('two backup days select 400 Ah and one backup day selects 200 Ah',()=>{
 const h=harness();
 const a=h.calculateSolarSizing({...base,backupDays:2}),b=h.calculateSolarSizing({...base,backupDays:1});
 assert.equal(a.batteryAh,250);assert.equal(a.standardAh,400);assert.equal(b.batteryAh,125);assert.equal(b.standardAh,200);
 assert.equal(a.pvWp,b.pvWp);
});
test('voltage changes battery Ah, while valid zero demand and zero reserve remain zero',()=>{
 const h=harness();
 assert.equal(h.calculateSolarSizing({...base,voltageV:25.6}).batteryAh,312);
 for(const k of ['powerW','hoursPerDay']){const r=h.calculateSolarSizing({...base,[k]:0});assert.equal(r.pvWp,0);assert.equal(r.batteryAh,0);assert.equal(r.standardAh,0)}
 const r=h.calculateSolarSizing({...base,backupDays:0});assert.equal(r.batteryAh,0);assert.equal(r.pvWp,515);
});
test('missing, malformed and out-of-range inputs do not produce healthy numbers',()=>{
 const h=harness();
 for(const key of Object.keys(base))for(const invalid of ['',null,undefined,'abc',true,[],NaN,-1]){
  const r=h.calculateSolarSizing({...base,[key]:invalid});assert.equal(r.ok,false,JSON.stringify({key,invalid}));assert.equal(r.field,key);
 }
 assert.equal(h.calculateSolarSizing({...base,hoursPerDay:24.1}).ok,false);
 assert.equal(h.calculateSolarSizing({...base,voltageV:0}).ok,false);
 assert.equal(h.calculateSolarSizing({...base,powerW:1e308}).ok,false);
});
test('fractional inputs calculate energy without truncating watts or hours',()=>{
 const r=harness().calculateSolarSizing({...base,powerW:'12.5',hoursPerDay:'8.5',backupDays:'1.5'});
 assert.equal(r.dailyWh,106.25);assert.equal(r.pvWp,46);assert.equal(r.batteryAh,17);assert.equal(r.standardAh,60);
});
test('calculator restores only its own saved input keys and tolerates broken storage',()=>{
 const h=harness(JSON.stringify({powerW:'80',backupDays:2,devices:[{id:'do-not-import'}]}));
 assert.equal(h.loadSizingInputs().powerW,'80');assert.equal(h.loadSizingInputs().backupDays,'2');assert.equal(h.getInputs().devices,undefined);
 assert.equal(harness('{broken').loadSizingInputs().powerW,'50');
});
test('calculator updates and persists inputs without station edits or server writes',()=>{
 const h=harness();h.loadSizingInputs();h.wireSizingCalculator();
 const stationState=JSON.stringify({d:h.State.devices,g:h.State.globalSettings});
 const p=h.node('sizing-powerW');p.value='15';p.listeners.input();
 assert.equal(h.node('sizing-standard').textContent,'400');
 assert.equal(JSON.parse(h.storage.get('ty_solar_sizing_v1')).powerW,'15');
 assert.equal(JSON.stringify({d:h.State.devices,g:h.State.globalSettings}),stationState);assert.equal(h.calls.length,0);
 p.value='';p.listeners.input();assert.equal(h.node('sizing-pv').textContent,'—');assert.equal(p.getAttribute('aria-invalid'),'true');
 h.node('sizing-reset').listeners.click();assert.equal(h.node('sizing-pv').textContent,'515');assert.equal(h.node('sizing-standard').textContent,'400');
});
test('settings routes coexist with device routes and support unknown-section fallback',()=>{
 const h=harness();
 for(const [hash,view,section] of [['','dashboard'],['#/device/site%201','device'],['#/settings','settings','sizing'],['#/settings/email','settings','email'],['#/settings/unknown','settings','sizing']]){
  h.context.location.hash=hash;const r=h.parseHashRoute();assert.equal(r.view,view);if(section)assert.equal(r.section,section);
 }
});
test('settings opens as a page, exposes context actions and returns to originating device',()=>{
 const h=harness();h.context.location.hash='#/device/site-1';h.openSettings('site-1');
 assert.equal(h.context.location.hash,'#/settings/devices');assert.equal(h.node('settingsPage').hidden,false);
 assert.equal(h.node('btnAddDevice').hidden,false);assert.equal(h.node('btnSaveSettings').hidden,false);
 assert.equal(h.document.body.getAttribute('data-view'),'settings');
 h.closeSettings();assert.equal(h.context.location.hash,'#/device/site-1');assert.equal(h.node('settingsPage').hidden,true);
});
test('section navigation preserves drafts and never reloads the mounted settings form',()=>{
 const h=harness();h.openSettings();const form=h.node('settingsBody').innerHTML;
 const brand=h.node('cfg-brandName');brand.value='Unsaved project name';
 h.context.location.hash='#/settings/brand';h.renderAllCards();h.context.location.hash='#/settings/sizing';h.renderAllCards();
 assert.equal(h.node('settingsBody').innerHTML,form);assert.equal(brand.value,'Unsaved project name');
 assert.equal(h.node('btnSaveSettings').hidden,true);assert.equal(h.node('btnAddDevice').hidden,true);
});
test('mail settings load only on first mail-tab visit and survive tab changes',()=>{
 const h=harness();h.openSettings();assert.equal(h.emailLoads(),0);
 h.context.location.hash='#/settings/email';h.renderAllCards();assert.equal(h.emailLoads(),1);
 h.context.location.hash='#/settings/sizing';h.renderAllCards();h.context.location.hash='#/settings/email';h.renderAllCards();
 assert.equal(h.emailLoads(),1);
});
test('re-rendering device settings retains unsaved global inputs and mounted mail controls',()=>{
 const h=harness();h.openSettings();h.node('cfg-brandName').value='Draft brand';
 h.showSettingsSection('email');const original=h.node('emailConfigSection');
 h.renderSettingsBody();assert.equal(h.node('cfg-brandName').value,'Draft brand');assert.equal(h.node('emailConfigSection'),original);assert.equal(h.emailLoads(),1);
});

test('authoritative config hydrates an untouched initial settings page',async()=>{
 const h=harness();h.openSettings();
 h.context.fetch=async()=>({ok:true,json:async()=>({success:true,exists:true,config:{devices:[],globalSettings:{brandName:'Server brand'}}})});
 await vm.runInContext('loadFromServer()',h.context);
 assert.equal(h.SettingsUI.mounted,false);
 h.renderAllCards();assert.ok(h.node('settingsBody').innerHTML.includes('Server brand'));assert.equal(h.SettingsUI.mounted,true);
});
test('initial config does not replace a settings form once the user has started editing',async()=>{
 const h=harness();h.openSettings();h.node('cfg-brandName').value='Local draft';
 h.node('settingsBody').listeners.input({target:{closest:()=>({dataset:{settingsSection:'brand'}})}});
 assert.equal(h.SettingsUI.formDirty,true);
 const form=h.node('settingsBody').innerHTML;
 h.context.fetch=async()=>({ok:true,json:async()=>({success:true,exists:true,config:{devices:[],globalSettings:{brandName:'Server brand'}}})});
 await vm.runInContext('loadFromServer()',h.context);h.renderAllCards();
 assert.equal(h.node('settingsBody').innerHTML,form);assert.equal(h.node('cfg-brandName').value,'Local draft');
});
test('dedicated mail save does not submit station configuration or navigate away',async()=>{
 const h=harness();h.openSettings();h.context.location.hash='#/settings/email';h.renderAllCards();
 assert.equal(h.node('btnSaveEmailSettings').hidden,false);assert.equal(h.node('btnSaveSettings').hidden,true);
 vm.runInContext('var mailSaves=0;saveEmailConfig=async()=>{mailSaves++;return true;};',h.context);
 await h.node('btnSaveEmailSettings').listeners.click();
 assert.equal(vm.runInContext('mailSaves',h.context),1);assert.equal(h.calls.length,0);assert.equal(h.context.location.hash,'#/settings/email');
});

test('battery specification selector contains only the four available single-battery sizes',()=>{
 const h=harness();const markup=vm.runInContext('sizingMarkup()',h.context);
 const select=markup.match(/<select id="sizing-batteryCapacityAh"[\s\S]*?<\/select>/)[0];
 assert.deepEqual([...select.matchAll(/<option value="(\d+)"/g)].map(m=>Number(m[1])),[60,120,200,400]);
 assert.match(select,/value="400" selected/);
 assert.ok(!markup.includes('計算所需總容量'));
});
test('all selected sizes remain unchanged and expose insufficient single-battery reserve',()=>{
 const h=harness();
 for(const capacity of [60,120,200,400]){
  const r=h.calculateSolarSizing({...base,batteryCapacityAh:capacity});
  assert.equal(r.selectedAh,capacity);assert.equal(r.batterySufficient,false);
  assert.ok(Math.abs(r.backupDaysAvailable-capacity*12.8*.95*.95/1440)<1e-12);
  assert.equal(r.pvWp,515);
 }
 const enough=h.calculateSolarSizing({...base,backupDays:2,batteryCapacityAh:400});
 const short=h.calculateSolarSizing({...base,backupDays:2,batteryCapacityAh:200});
 assert.equal(enough.batterySufficient,true);assert.equal(short.batterySufficient,false);
});
test('unsupported battery capacities are rejected and invalid saved capacities recover to a valid choice',()=>{
 const h=harness();
 for(const value of [0,59,61,125,624,800,'',null,undefined,true,[],{},NaN,Infinity]){
  const r=h.calculateSolarSizing({...base,batteryCapacityAh:value});assert.equal(r.ok,false);assert.equal(r.field,'batteryCapacityAh');
 }
 assert.equal(harness(JSON.stringify({batteryCapacityAh:624})).loadSizingInputs().batteryCapacityAh,'400');
 assert.equal(harness(JSON.stringify({batteryCapacityAh:'120'})).loadSizingInputs().batteryCapacityAh,'120');
 assert.equal(harness(JSON.stringify({powerW:'80'})).loadSizingInputs().batteryCapacityAh,'400');
});
test('the result displays the selected available size rather than calculated nonstandard Ah',()=>{
 const h=harness();h.loadSizingInputs();h.wireSizingCalculator();
 assert.equal(h.node('sizing-standard').textContent,'400');
 assert.equal(h.node('sizing-autonomy').textContent,'3.2 天');
 assert.match(h.node('sizing-battery-note').textContent,/容量不足/);
 for(const name of ['sizing-standard','sizing-battery-note','sizing-autonomy'])assert.ok(!h.node(name).textContent.includes('624'));
 h.node('sizing-batteryCapacityAh').value='120';h.node('sizing-batteryCapacityAh').listeners.change();
 assert.equal(h.node('sizing-standard').textContent,'120');
 assert.equal(JSON.parse(h.storage.get('ty_solar_sizing_v1')).batteryCapacityAh,'120');
 assert.equal(h.calls.length,0);
});
test('selection updates adequacy and zero-demand display without creating a second battery',()=>{
 const h=harness();h.loadSizingInputs();h.wireSizingCalculator();
 h.node('sizing-backupDays').value='2';h.node('sizing-backupDays').listeners.input();
 assert.match(h.node('sizing-battery-note').textContent,/容量足夠/);
 h.node('sizing-batteryCapacityAh').value='200';h.node('sizing-batteryCapacityAh').listeners.change();
 assert.match(h.node('sizing-battery-note').textContent,/容量不足/);
 h.node('sizing-powerW').value='0';h.node('sizing-powerW').listeners.input();
 assert.equal(h.node('sizing-standard').textContent,'200');assert.equal(h.node('sizing-autonomy').textContent,'無用電需求');
});

test('reserve duration uses hours below one day without rounding short durations to zero',()=>{
 const h=harness();assert.equal(vm.runInContext('formatSizingAutonomy(0.481333333333)',h.context),'11.5 小時');assert.equal(vm.runInContext('formatSizingAutonomy(0.0001)',h.context),'少於 0.1 小時');assert.equal(vm.runInContext('formatSizingAutonomy(null)',h.context),'無用電需求');
});
