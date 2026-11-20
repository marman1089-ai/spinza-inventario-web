const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const source=fs.readFileSync(require('node:path').join(__dirname,'../app/static/js/inventory-batch.js'),'utf8');
function setup(){
 const element=extra=>Object.assign({disabled:false,handlers:{},addEventListener(n,f){this.handlers[n]=f;}},extra);
 const input=element({value:'20',get valueAsNumber(){return Number(this.value);},checkValidity(){return this.value!==''&&Number(this.value)>=0;},reportValidity(){}});
 Object.defineProperty(input,'valueAsNumber',{get(){return Number(this.value);}});
 const form=element({dataset:{itemId:'1',original:'20'},elements:{qty:input,store:{value:'spinza'}},closest(){return {classList:{toggle(){}}};}});
 const mode=element({checked:true}),button=element({}),status={},dock={dataset:{csrf:'abc'}};
 const nodes={'inventory-save-dock':dock,'inventory-defer':mode,'inventory-save-all':button,'inventory-save-status':status};
 let request,reloaded=false,fail=false;
 const window={addEventListener(){},location:{reload(){reloaded=true;}}};
 const context={document:{body:{appendChild(node){node.parentIsBody=true;}},getElementById:id=>nodes[id],querySelectorAll:s=>s==='.inv-set-form'?[form]:[input,mode]},window,fetch:async(url,opts)=>{request=JSON.parse(opts.body);return {ok:!fail,json:async()=>fail?{detail:'Conflitto'}:{saved:1}};}};
 vm.runInNewContext(source,context);assert.equal(dock.parentIsBody,true);
 return {input,mode,button,status,form,window,get request(){return request;},get reloaded(){return reloaded;},set fail(v){fail=v;}};
}
test('stages typing and calculator without requests, then sends one batch',async()=>{
 const ui=setup();ui.input.value='25';ui.input.handlers.input();assert.equal(ui.button.disabled,false);assert.equal(ui.request,undefined);
 assert.equal(ui.window.inventoryBatchCurrent(1,20),25);ui.window.inventoryBatchStage(1,28);
 await ui.button.handlers.click();assert.equal(ui.request.changes[0].qty,28);assert.equal(ui.request.changes[0].original,20);assert.equal(ui.reloaded,true);
});
test('failed save keeps pending changes and permits retry',async()=>{
 const ui=setup();ui.input.value='23';ui.input.handlers.input();ui.fail=true;await ui.button.handlers.click();assert.equal(ui.reloaded,false);assert.equal(ui.status.textContent,'Conflitto');assert.equal(ui.input.value,'23');assert.equal(ui.button.disabled,false);
 let prevented=false;ui.form.handlers.submit({preventDefault(){prevented=true;}});assert.equal(prevented,true);assert.equal(ui.request.changes[0].qty,23);
 ui.fail=false;await ui.button.handlers.click();assert.equal(ui.reloaded,true);
});
