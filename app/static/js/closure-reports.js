(function(){
  'use strict';
  const form=document.getElementById('closing-form');
  if(!form)return;
  const out=document.getElementById('closing-total');
  let dirty=false;
  function update(){
    let total=0,valid=true;
    form.querySelectorAll('[name=amount]').forEach(input=>{
      const value=input.value.trim().replace(',','.');
      if(!/^\d{1,9}(?:\.\d{1,2})?$/.test(value)){valid=false;return;}
      const parts=value.split('.');
      total+=Number(parts[0])*100+Number(((parts[1]||'')+'00').slice(0,2));
    });
    out.textContent=valid?new Intl.NumberFormat('it-IT',{style:'currency',currency:'EUR'}).format(total/100):'Controlla gli importi';
  }
  form.addEventListener('input',()=>{dirty=true;update();});
  form.addEventListener('submit',()=>{dirty=false;document.getElementById('save-closing').disabled=true;});
  document.querySelectorAll('.closing-add form,.closing-channel-action').forEach(channelForm=>{
    channelForm.addEventListener('submit',event=>{
      if(dirty&&!window.confirm('Hai importi o note non salvati. Continuando perderai queste modifiche. Vuoi continuare?')){event.preventDefault();return;}
      if(channelForm.classList.contains('closing-channel-action')){
        const name=channelForm.querySelector('[name=name]').value;
        if(!window.confirm('Eliminare il canale «'+name+'» dalle nuove chiusure? I report salvati e le entrate restano invariati.'))event.preventDefault();
      }
    });
  });
  update();
})();
