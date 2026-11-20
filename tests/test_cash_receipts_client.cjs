const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../app/static/js/cash-receipts.js'),'utf8');
function setup(values={},selected=true){
 const fields=Object.fromEntries(['day','description','store','payer'].map(k=>[k,{value:values[k]||''}]));
 const select={selectedOptions:[{value:selected?'expense:1':'',dataset:{day:'2025-01-03',description:'Fornitura',store:'spinza',payer:'S&C SRLS'}}],addEventListener(n,f){this.change=f;}};
 vm.runInNewContext(source,{document:{getElementById:id=>id==='receipt-upload'?{elements:{namedItem:n=>fields[n]}}:select}});
 return {fields,select};
}
test('selecting an expense fills only empty fields',()=>{
 const {fields,select}=setup({description:'La mia descrizione',payer:'Pagante già inserito'},false);
 assert.equal(fields.day.value,'');select.selectedOptions[0].value='expense:1';select.change();
 assert.equal(fields.day.value,'2025-01-03');assert.equal(fields.store.value,'spinza');assert.equal(fields.description.value,'La mia descrizione');assert.equal(fields.payer.value,'Pagante già inserito');
});
test('unlinked receipt stays free and deep link fills all missing fields',()=>{
 const free=setup({},false);assert.equal(free.fields.description.value,'');
 const linked=setup();assert.equal(linked.fields.payer.value,'S&C SRLS');assert.equal(linked.fields.description.value,'Fornitura');
});
