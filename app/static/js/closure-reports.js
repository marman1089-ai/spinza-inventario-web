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
    let expenses=0,expensesValid=true;
    form.querySelectorAll('.closing-expense-row').forEach(row=>{
      const name=row.querySelector('[name=expense_name]'),amount=row.querySelector('[name=expense_amount]');
      const value=amount.value.trim().replace(',','.');
      name.setCustomValidity('');amount.setCustomValidity('');
      if(!name.value.trim()&&!value)return;
      if(!name.value.trim()){name.setCustomValidity('Inserisci il nome della spesa.');expensesValid=false;}
      if(!/^\d{1,9}(?:\.\d{1,2})?$/.test(value)){amount.setCustomValidity('Inserisci un importo valido.');expensesValid=false;return;}
      const parts=value.split('.'),cents=Number(parts[0])*100+Number(((parts[1]||'')+'00').slice(0,2));
      if(cents<=0){amount.setCustomValidity('Inserisci un importo maggiore di zero.');expensesValid=false;}
      expenses+=cents;
    });
    const money=c=>new Intl.NumberFormat('it-IT',{style:'currency',currency:'EUR'}).format(c/100);
    document.getElementById('closing-expense-total').textContent=expensesValid?money(expenses):'Controlla le spese';
    document.getElementById('closing-net-total').textContent=valid&&expensesValid?money(total):'Controlla gli importi';
    out.textContent=valid&&expensesValid?money(total+expenses):'Controlla gli importi';
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
  document.querySelectorAll('.closing-report-delete').forEach(deleteForm=>{
    deleteForm.addEventListener('submit',event=>{
      const warning='Eliminare questo report e le relative entrate e uscite?'+(dirty?' Gli importi non salvati nel modulo andranno persi.':'');
      if(!window.confirm(warning)){event.preventDefault();return;}
      deleteForm.querySelector('button[type=submit]').disabled=true;
    });
  });
  const expenseRows=document.getElementById('closing-expense-rows');
  const rowTemplate=expenseRows.firstElementChild.cloneNode(true);
  document.getElementById('add-closing-expense').addEventListener('click',()=>{
    if(expenseRows.children.length>=100)return;
    const row=rowTemplate.cloneNode(true);expenseRows.appendChild(row);
    row.querySelector('input').focus();dirty=true;update();
  });
  expenseRows.addEventListener('click',event=>{
    const button=event.target.closest('.remove-closing-expense');
    if(!button)return;
    button.closest('.closing-expense-row').remove();dirty=true;update();
  });
  update();
})();
