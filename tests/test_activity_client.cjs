const assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const script=fs.readFileSync('app/static/inventory_activity.js','utf8');
function setup(path='/inventario/conteggio'){
 let now=0,focused=true,interval,requests=[];const handlers={},wh={};
 const document={visibilityState:'visible',hasFocus:()=>focused,getElementById:()=>({dataset:{store:'spinza',area:'bibite'}}),addEventListener:(e,fn)=>{(handlers[e]??=[]).push(fn)}};
 const ctx={location:{pathname:path},document,window:{addEventListener:(e,fn)=>wh[e]=fn},crypto:{randomUUID:()=> '00000000-0000-4000-8000-000000000001'},performance:{now:()=>now},setInterval:fn=>interval=fn,fetch:async(url,options)=>{requests.push(options.body.get('state'));return{status:204}},URLSearchParams};
 vm.runInNewContext(script,ctx);
 return{requests,document,wh,handlers,tick:()=>interval?.(),time:n=>now=n,focus:v=>focused=v};
}
(async()=>{
 let t=setup();await new Promise(setImmediate);assert.deepEqual(t.requests,['active'],'Page entry must signal before interaction');
 await t.tick();assert.equal(t.requests.length,1);t.time(15000);await t.tick();assert.equal(t.requests.length,2);
 t.document.visibilityState='hidden';for(const f of t.handlers.visibilitychange)await f();await new Promise(setImmediate);assert.equal(t.requests.at(-1),'paused');
 t.time(20000);t.document.visibilityState='visible';for(const f of t.handlers.visibilitychange)await f();await new Promise(setImmediate);assert.equal(t.requests.at(-1),'active');
 t.time(85000);await t.tick();assert.equal(t.requests.at(-1),'paused','Idle must suspend measurement');
 t.time(86000);await t.wh.focus();assert.equal(t.requests.at(-1),'active');
 await t.wh.pagehide();assert.equal(t.requests.at(-1),'paused');
 let global=setup('/fatture');await new Promise(setImmediate);assert.deepEqual(global.requests,['active'],'Other app sections must signal entry');
 let report=setup('/inventario/report');await new Promise(setImmediate);assert.deepEqual(report.requests,['active']);
 let monitor=setup('/admin/fasce-orarie');await new Promise(setImmediate);assert.equal(monitor.requests.length,0,'Monitoring page must not create presence noise');
 console.log('PASS: immediate access, all sections, throttling, visibility, idle, focus, pagehide and monitoring exclusion');
})();
