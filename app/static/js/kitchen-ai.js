(() => {
 const panel=document.getElementById('kitchen-ai');if(!panel)return;
 let dirty=false;const review=document.getElementById('kitchen-ai-review'),status=document.getElementById('kitchen-ai-status'),refresh=document.getElementById('kitchen-ai-refresh');
 document.querySelectorAll('#kitchen-ai-review,.kw-panel form').forEach(form=>form.addEventListener('input',()=>dirty=true));
 refresh.addEventListener('click',()=>{if(!dirty||confirm('Ricaricare? Le modifiche non salvate andranno perse.'))location.reload();});
 const selects=review?[...review.querySelectorAll('select[name^=product_]')]:[];
 function updateRow(select){const row=select.closest('tr'),input=row.querySelector('input[type=number]'),unit=row.querySelector('[data-inventory-unit]');input.required=select.value!=='ignore';input.disabled=select.value==='ignore';unit.textContent=select.selectedOptions[0]?.dataset.unit||'Scegli il prodotto';}
 selects.forEach(select=>{select.addEventListener('change',()=>updateRow(select));updateRow(select);});
 if(panel.dataset.pending==='1'){
  async function poll(){try{const response=await fetch(panel.dataset.statusUrl);if(!response.ok)return;const state=await response.json();if(state.token!==panel.dataset.statusToken){if(!dirty){location.reload();return;}status.textContent='Nuovi risultati disponibili. Salva prima le modifiche oppure premi Mostra i nuovi risultati.';refresh.hidden=false;return;}setTimeout(poll,5000);}catch{setTimeout(poll,15000);}}
  setTimeout(poll,5000);
 }
 review?.addEventListener('submit',async event=>{
  event.preventDefault();if(window.SpinzaUIFinishLoading)window.SpinzaUIFinishLoading();
  if(!review.reportValidity())return;
  const button=review.querySelector('button[type=submit]'),message=document.getElementById('kitchen-ai-review-message');if(button.disabled)return;
  const data=new FormData(review);button.disabled=true;message.textContent='Salvataggio degli arrivi verificati…';
  try{const response=await fetch(review.action,{method:'POST',body:data,headers:{Accept:'application/json'}});const result=await response.json();if(!response.ok)throw Error(typeof result.detail==='string'?result.detail:'Controlla le righe prima di confermare.');if(!result.redirect)throw Error('Salvataggio non confermato.');dirty=false;location.assign(result.redirect);}catch(error){button.disabled=false;message.textContent=error.message||'Collegamento non riuscito. Le correzioni restano nel modulo.';}
 });
})();
