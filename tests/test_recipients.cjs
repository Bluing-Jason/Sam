const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const {test}=require('node:test');
const html=fs.readFileSync(path.join(__dirname,'..','solar_dashboard.html'),'utf8');
const script=Array.from(html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi)).map(m=>m[1]).find(s=>s.trim());
function harness(fetchImpl){
 const nodes=new Map();
 const document={body:{getAttribute(){return 'dashboard';}},getElementById(id){
   if(!nodes.has(id)){const classes=new Set();nodes.set(id,{value:'',checked:false,innerHTML:'',textContent:'',isConnected:true,validation:'',
     classList:{add(x){classes.add(x);},remove(x){classes.delete(x);},toggle(x,on){on?classes.add(x):classes.delete(x);},contains(x){return classes.has(x);}},
     appendChild(){},addEventListener(){},setCustomValidity(value){this.validation=value;},reportValidity(){return !this.validation;},focus(){}});}
   return nodes.get(id);
 },createElement(){return {style:{},remove(){}};},querySelectorAll(selector){return selector==='.daily-time-input'?[{value:'09:00'},{value:'18:00'}]:[];},addEventListener(){}};
 const calls=[];
 const context=vm.createContext({document,window:{addEventListener(){}},console:{warn(){},error(){},log(){}},
   fetch:async(url,options={})=>{calls.push({url,body:options.body?JSON.parse(options.body):null});return fetchImpl?fetchImpl(url,options):{ok:true,json:async()=>({success:true})};},
   setTimeout(){return 1;},clearTimeout(){},setInterval(){return 1;},clearInterval(){}});
 vm.runInContext(script.replace(/\bboot\(\);\s*$/,''),context);
 const api=vm.runInContext('({normaliseEmailRecipients,validRecipientEmail,renderEmailRecipientRows,queueEmailRecipientsSave,addEmailRecipient,collectEmailConfigFromForm,saveEmailConfig,setRows(rows){_emailRecipientRows=rows;},getRows(){return _emailRecipientRows;},settled(){return _emailRecipientSaveQueue;}})',context);
 for(const [id,value] of Object.entries({'email-smtp-host':'smtp.example.invalid','email-smtp-port':'587','email-smtp-user':'sender@example.com','email-smtp-pass':'***','email-from':'sender@example.com','email-from-name':'Solar','email-company-name':'Solar','email-cooldown':'60'}))document.getElementById(id).value=value;
 document.getElementById('email-enabled').checked=true;document.getElementById('alert-email-enabled').checked=true;
 return {...api,document,nodes,calls};
}
const plain=x=>JSON.parse(JSON.stringify(x));
const row=(email,enabled=true,alerts=true,reports=true)=>({email,enabled,alerts,reports});

test('legacy string recipients default to enabled with both message types',()=>{
 const h=harness();assert.deepEqual(plain(h.normaliseEmailRecipients([' old@example.com '])),[row('old@example.com')]);
});
test('disabled recipients retain independent choices and are copied without mutating input',()=>{
 const h=harness(),rows=[row('a@example.com',false,true,false)];const result=h.normaliseEmailRecipients(rows);
 assert.deepEqual(plain(result),rows);assert.notEqual(result[0],rows[0]);
});
test('invalid addresses, malformed flags and case-insensitive duplicates are rejected',()=>{
 const h=harness();for(const entries of [['bad'],['a@example.com','A@example.com'],[{email:'a@example.com',enabled:'false'}]])assert.throws(()=>h.normaliseEmailRecipients(entries));
 assert.equal(h.validRecipientEmail('ops+solar@example.com.hk'),true);
 assert.equal(h.validRecipientEmail('one@example.com\r\nBcc:other@example.com'),false);
});
test('rendered rows escape addresses and expose separate accessible switches',()=>{
 const h=harness();h.setRows([row("o'connor@example.com",false,true,false)]);h.renderEmailRecipientRows();
 const content=h.document.getElementById('email-recipient-list').innerHTML;
 assert.ok(content.includes('is-disabled'));assert.ok(content.includes('o&#39;connor@example.com'));assert.ok(content.includes('role="switch"'));assert.ok(content.includes('data-recipient-field="alerts"'));assert.ok(content.includes('data-recipient-field="reports"'));
});
test('adding a valid address uses default flags and sends only the recipient payload',async()=>{
 const h=harness();h.document.getElementById('email-recipient-new').value='new@example.com';assert.equal(h.addEmailRecipient(),true);await h.settled();
 assert.deepEqual(plain(h.getRows()),[row('new@example.com')]);assert.equal(h.calls[0].url,'/api/email_recipients');assert.deepEqual(Object.keys(h.calls[0].body),['recipients']);
 assert.equal(h.document.getElementById('email-recipient-new').value,'');assert.ok(h.document.getElementById('email-recipient-status').textContent.includes('已儲存'));
});
test('duplicate and invalid additions do not change the list or issue a save',()=>{
 const h=harness();h.setRows([row('a@example.com')]);const input=h.document.getElementById('email-recipient-new');input.value='A@example.com';assert.equal(h.addEmailRecipient(),false);assert.ok(input.validation.includes('已在'));
 input.value='bad';assert.equal(h.addEmailRecipient(),false);assert.equal(h.getRows().length,1);assert.equal(h.calls.length,0);
});
test('rapid changes save serially and snapshot each choice without reordering',async()=>{
 let release;const h=harness(async()=>{if(!release)return new Promise(resolve=>{release=()=>resolve({ok:true,json:async()=>({success:true})});});return {ok:true,json:async()=>({success:true})};});
 h.setRows([row('a@example.com',false,true,false)]);const first=h.queueEmailRecipientsSave();
 h.setRows([row('a@example.com',true,false,true)]);const second=h.queueEmailRecipientsSave();
 await new Promise(setImmediate);assert.equal(h.calls.length,1);release();await Promise.all([first,second]);
 assert.equal(h.calls.length,2);assert.equal(h.calls[0].body.recipients[0].enabled,false);assert.equal(h.calls[1].body.recipients[0].reports,true);assert.equal(h.calls[1].body.recipients[0].alerts,false);
});
test('a failed save is shown and later edits can retry successfully',async()=>{
 let attempts=0;const h=harness(async()=>++attempts===1?{ok:false,json:async()=>({success:false,error:'test failure'})}:{ok:true,json:async()=>({success:true})});
 h.setRows([row('a@example.com')]);assert.equal(await h.queueEmailRecipientsSave(),false);
 assert.ok(h.document.getElementById('email-recipient-status').classList.contains('is-error'));
 assert.equal(await h.queueEmailRecipientsSave(),true);assert.equal(h.document.getElementById('email-recipient-status').classList.contains('is-error'),false);
});
test('full settings saves share the recipient queue so later changes remain last',async()=>{
 const h=harness();h.setRows([row('a@example.com',false,true,false)]);const first=h.queueEmailRecipientsSave();const full=h.saveEmailConfig();
 h.setRows([row('a@example.com',true,false,true)]);const last=h.queueEmailRecipientsSave();await Promise.all([first,full,last]);
 assert.deepEqual(h.calls.map(c=>c.url),['/api/email_recipients','/api/email_config','/api/email_recipients']);
 assert.equal(h.calls[1].body.config.smtp_pass,'***');assert.equal(h.calls[1].body.config.recipients[0].enabled,false);assert.equal(h.calls[2].body.recipients[0].enabled,true);
});
test('collecting the form preserves all recipient flags and report times',()=>{
 const h=harness();const rows=[row('a@example.com',false,true,false),row('b@example.com',true,false,true)];h.setRows(rows);
 const cfg=plain(h.collectEmailConfigFromForm());assert.deepEqual(cfg.recipients,rows);assert.deepEqual(cfg.daily_report_times,['09:00','18:00']);assert.equal(cfg.smtp_pass,'***');
});
