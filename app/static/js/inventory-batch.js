(() => {
  const dock=document.getElementById('inventory-save-dock'); if(!dock)return;
  // Keep the fixed bar outside the animated inventory wrapper.
  document.body.appendChild(dock);
  const save=document.getElementById('inventory-save-all'), status=document.getElementById('inventory-save-status');
  const forms=[...document.querySelectorAll('.inv-set-form')], pending=new Map(); let busy=false;
  function formFor(id){return forms.find(f=>Number(f.dataset.itemId)===Number(id));}
  function refresh(){save.disabled=busy||!pending.size;status.textContent=busy?'Salvataggio in corso…':pending.size?`${pending.size} prodotti da salvare`:'Nessuna modifica da salvare';}
  function stage(form){
    const input=form.elements.qty;
    if(!input.checkValidity()||!Number.isFinite(input.valueAsNumber)||input.valueAsNumber<0){input.reportValidity();return false;}
    const id=Number(form.dataset.itemId), original=Number(form.dataset.original), qty=input.valueAsNumber;
    if(Math.abs(qty-original)<1e-8)pending.delete(id);else pending.set(id,{id,store:form.elements.store.value,qty,original});
    form.closest('.inv-row').classList.toggle('inv-pending',pending.has(id));refresh();return true;
  }
  forms.forEach(form=>{
    form.elements.qty.min='0';form.elements.qty.step='any';
    form.elements.qty.addEventListener('input',()=>{if(!busy)stage(form);});
    form.addEventListener('submit',e=>{e.preventDefault();if(!busy)stage(form);if(window.SpinzaUIFinishLoading)window.SpinzaUIFinishLoading();});
  });
  window.inventoryBatchCurrent=(id,current)=>Number(formFor(id)?.elements.qty.value??current);
  window.inventoryBatchStage=(id,qty)=>{if(busy)return true;const form=formFor(id);if(!form)return false;form.elements.qty.value=String(Math.round(qty*1e8)/1e8);stage(form);return true;};
  window.addEventListener('beforeunload',e=>{if(pending.size){e.preventDefault();e.returnValue='';}});
  save.addEventListener('click',async()=>{
    if(busy)return;
    if(forms.some(f=>!f.elements.qty.checkValidity())){forms.find(f=>!f.elements.qty.checkValidity()).elements.qty.reportValidity();return;}
    if(!pending.size)return;
    busy=true;const controls=[...document.querySelectorAll('.inv-set-form input,.inv-action-cell button')];controls.forEach(c=>c.disabled=true);refresh();
    try{
      const response=await fetch('/inventario/salva-quantita',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({csrf:dock.dataset.csrf,changes:[...pending.values()]})});
      const result=await response.json();if(!response.ok)throw new Error(typeof result.detail==='string'?result.detail:'Salvataggio non riuscito. Le modifiche restano in attesa.');
      pending.clear();status.textContent=`Salvati ${result.saved} prodotti`;window.location.reload();
    }catch(error){busy=false;controls.forEach(c=>c.disabled=false);refresh();status.textContent=error.message||'Errore di connessione. Riprova il salvataggio.';}
  });
})();
