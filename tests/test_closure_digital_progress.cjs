const assert=require('node:assert/strict');
const fs=require('node:fs');const vm=require('node:vm');
const source=fs.readFileSync('app/static/js/closure-digital-progress.js','utf8');
async function scenario(states){
  const timers=[];const calls=[];let ready;let replacement;
  const node={isConnected:true,dataset:{documentStatus:'/schede/chiusure/1/stato',digitalUrl:'/schede/chiusure/1'},textContent:'In corso',removeAttribute(){},replaceWith(value){replacement=value;this.isConnected=false;}};
  const document={hidden:false,addEventListener(type,fn){if(type==='DOMContentLoaded')ready=fn;},querySelectorAll(selector){return selector==='[data-document-status]'?[node]:[];},createElement(){return {};}};
  const context={document,addEventListener(){},setTimeout(fn){timers.push(fn);return timers.length;},clearTimeout(){},fetch:async(url,options)=>{calls.push({url,options});return{ok:true,json:async()=>({state:states.shift()})};}};
  vm.runInNewContext(source,context);ready();
  await new Promise(setImmediate);
  if(timers.length){timers.shift()();await new Promise(setImmediate);}
  return{node,replacement,calls};
}
(async()=>{
  const completed=await scenario(['working','done']);
  assert.equal(completed.replacement.textContent,'Apri scheda digitale');
  assert.equal(completed.replacement.href,'/schede/chiusure/1');
  assert.equal(completed.calls.length,2);
  assert(completed.calls.every(call=>!call.options.method||call.options.method==='GET'));
  const failed=await scenario(['error']);
  assert.equal(failed.node.textContent,'Lettura non riuscita');
  assert.equal(failed.replacement,undefined);
  console.log('Progress indicator: ready and failed states passed; no AI actions requested.');
})().catch(error=>{console.error(error);process.exitCode=1;});
