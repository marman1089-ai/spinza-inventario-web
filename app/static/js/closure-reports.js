(function(){
  'use strict';
  const form=document.getElementById('closing-form');
  if(!form)return;
  const out=document.getElementById('closing-total');
  let dirty=false;
  function update(){
    let total=0,cash=0,valid=true,cashInput;
    form.querySelectorAll('[name=amount]').forEach(input=>{
      if(input.dataset?.channel==='Cash')cashInput=input;
      const value=input.value.trim().replace(',','.');
      if(!/^\d{1,9}(?:\.\d{1,2})?$/.test(value)){valid=false;return;}
      const parts=value.split('.');
      const valueCents=Number(parts[0])*100+Number(((parts[1]||'')+'00').slice(0,2));
      total+=valueCents;
      if(input.dataset?.channel==='Cash')cash=valueCents;
    });
    let expenses=0,added=0,fundExpenses=0,expensesValid=true;
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
      const mode=row.querySelector('[name=expense_mode]').value;
      if(mode==='add_to_cash')added+=cents;
      else if(mode==='cash_fund')fundExpenses+=cents;
    });
    const money=c=>new Intl.NumberFormat('it-IT',{style:'currency',currency:'EUR'}).format(c/100);
    const fundInput=document.getElementById('closing-fund-initial');
    const fundMode=document.getElementById('closing-fund-mode');
    const needsFund=Boolean(fundMode)||fundExpenses>0;
    document.getElementById('closing-fund-section').hidden=false;
    fundInput.required=needsFund;fundInput.disabled=false;fundInput.setCustomValidity('');
    let fundInitial=0,fundValid=true;
    if(needsFund||fundInput.value.trim()){
      const value=fundInput.value.trim().replace(',','.');
      if(!/^\d{1,9}(?:\.\d{1,2})?$/.test(value)){
        fundInput.setCustomValidity('Inserisci il fondo cassa iniziale.');fundValid=false;
      }else{
        const parts=value.split('.');fundInitial=Number(parts[0])*100+Number(((parts[1]||'')+'00').slice(0,2));
        if(fundInitial<fundExpenses){fundInput.setCustomValidity('Le spese superano il fondo cassa iniziale.');fundValid=false;}
      }
    }
    const deduction=fundMode?.value==='included'?fundInitial:0;
    if(cashInput){
      const insufficient=valid&&fundValid&&cash<deduction;
      cashInput.setCustomValidity(insufficient?'Il contante inserito è inferiore al fondo cassa.':'');
      if(insufficient)valid=false;
    }
    total-=deduction;
    cash-=deduction;
    if(fundMode&&(!fundMode.value||!fundValid))valid=false;
    document.getElementById('closing-fund-expenses').textContent=money(fundExpenses);
    document.getElementById('closing-fund-remaining').textContent=fundValid?(fundInput.value.trim()?money(fundInitial-fundExpenses):'Non indicato'):'Controlla il fondo iniziale';
    document.getElementById('closing-expense-total').textContent=expensesValid?money(expenses):'Controlla le spese';
    document.getElementById('closing-net-total').textContent=valid&&expensesValid?money(total+added-(expenses-fundExpenses)):'Controlla gli importi';
    const expected=document.getElementById('closing-expected-cash');
    if(expected)expected.textContent=valid&&expensesValid&&fundValid?money(cash+added+fundInitial-expenses):'Controlla gli importi';
    out.textContent=valid&&expensesValid?money(total+added):'Controlla gli importi';
  }
  form.addEventListener('input',()=>{dirty=true;update();});
  form.addEventListener('change',()=>{dirty=true;update();});
  form.addEventListener('submit',event=>{update();if(!form.reportValidity()){event.preventDefault();return;}dirty=false;document.getElementById('save-closing').disabled=true;});
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
    const row=rowTemplate.cloneNode(true);
    row.querySelectorAll('input').forEach(input=>{input.value='';input.defaultValue='';});
    row.querySelector('select').value='add_to_cash';
    expenseRows.appendChild(row);
    row.querySelector('input').focus();dirty=true;update();
  });
  expenseRows.addEventListener('click',event=>{
    const button=event.target.closest('.remove-closing-expense');
    if(!button)return;
    button.closest('.closing-expense-row').remove();dirty=true;update();
  });
  update();
})();
