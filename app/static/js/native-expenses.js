/* Native interactions only: dialogs, local filters and persisted selection. No polling. */
(() => {
  const root=document.querySelector('[data-native-expenses]');if(!root)return;
  const currency=new Intl.NumberFormat('it-IT',{style:'currency',currency:'EUR'});
  let baseline=new Map();
  try{for(const item of JSON.parse(document.getElementById('np-selection-data')?.textContent||'[]'))baseline.set(item.key,item)}catch{}
  const pending=new Map();let queue=Promise.resolve();
  const dock=document.querySelector('.np-selection-dock');
  function message(text){for(const el of document.querySelectorAll('[data-selection-status]'))el.textContent=text}
  function render(){
    let count=0,total=0;
    for(const [key,item] of baseline){const selected=pending.has(key)?pending.get(key):item.selected;if(selected){count++;total+=item.amount}}
    for(const el of document.querySelectorAll('[data-selection-count]'))el.textContent=String(count);
    for(const el of document.querySelectorAll('[data-selection-total]'))el.textContent=currency.format(total/100);
    for(const form of document.querySelectorAll('[data-native-selection]')){
      const key=form.dataset.key,item=baseline.get(key),selected=pending.has(key)?pending.get(key):Boolean(item?.selected);
      form.elements.namedItem('selected').value=selected?'0':'1';
      const button=form.querySelector('button');button.textContent=selected?'Rimuovi selezione':'Seleziona';button.setAttribute('aria-pressed',String(selected));button.disabled=pending.has(key)||!item;
    }
    for(const row of document.querySelectorAll('[data-selected-row]')){
      const key=row.dataset.selectedRow;row.hidden=!(pending.has(key)?pending.get(key):baseline.get(key)?.selected);
    }
    dock?.setAttribute('aria-busy',String(pending.size>0));
  }
  function accept(data){baseline=new Map(data.items.map(item=>[item.key,item]))}
  async function flush(){do{await queue}while(pending.size)}
  document.addEventListener('submit',event=>{
    const form=event.target;if(!(form instanceof HTMLFormElement))return;
    if(!form.matches('[data-native-selection]')){
      if(pending.size){event.preventDefault();event.stopImmediatePropagation();const button=event.submitter;flush().then(()=>form.requestSubmit(button))}
      return;
    }
    event.preventDefault();const key=form.dataset.key;if(pending.has(key))return;
    const body=new FormData(form),desired=body.get('selected')==='1';
    pending.set(key,desired);render();message('Salvataggio…');
    queue=queue.then(async()=>{
      try{
        const response=await fetch('/gestionale/spese-pagamenti/selezione',{method:'POST',body,keepalive:true});
        const data=await response.json();if(!response.ok)throw new Error(data.error||data.detail||'Selezione non salvata.');
        accept(data);pending.delete(key);
        for(const other of document.querySelectorAll('[data-native-selection]'))if(other.dataset.key===key)other.elements.namedItem('operation').value=data.next_operation;
        render();message(pending.size?'Salvataggio…':'Selezione salvata');
      }catch(error){
        pending.delete(key);
        try{const response=await fetch('/gestionale/spese-pagamenti/selezione');if(response.ok)accept(await response.json())}catch{}
        render();message(error.message||'Controlla la connessione e ricarica la selezione.');
      }
    });
  },true);
  document.addEventListener('change',event=>{
    const select=event.target;if(select.id!=='header-store'||!pending.size)return;
    event.preventDefault();event.stopImmediatePropagation();const value=select.value;
    select.value=root.dataset.store;
    flush().then(()=>{select.value=value;select.dispatchEvent(new Event('change',{bubbles:true}))});
  },true);
  document.addEventListener('click',event=>{
    const link=event.target.closest('a[href]');if(!link||!pending.size||event.ctrlKey||event.metaKey||event.shiftKey||link.target==='_blank')return;
    const url=new URL(link.href,location.href);if(url.origin!==location.origin)return;
    if(url.pathname===location.pathname&&url.search===location.search&&url.hash)return;
    event.preventDefault();event.stopImmediatePropagation();flush().then(()=>location.assign(link.href));
  },true);
  document.addEventListener('click',event=>{
    const open=event.target.closest('[data-open-dialog]');if(open){const dialog=document.getElementById(open.dataset.openDialog);if(dialog&&!dialog.open)dialog.showModal()}
    const close=event.target.closest('[data-close-dialog]');if(close)close.closest('dialog')?.close();
  });
  for(const dialog of document.querySelectorAll('.np-dialog'))dialog.addEventListener('click',event=>{
    if(event.target!==dialog)return;const r=dialog.getBoundingClientRect();if(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom)dialog.close();
  });
  function commandFilter(filter,save=true){
    for(const button of document.querySelectorAll('[data-command-filter]'))button.setAttribute('aria-pressed',String(button.dataset.commandFilter===filter));
    let count=0;
    for(const row of document.querySelectorAll('[data-command-bucket]')){
      const bucket=row.dataset.commandBucket;row.hidden=!(filter==='all'||filter==='now'&&['overdue','today'].includes(bucket)||filter==='week'&&['overdue','today','week'].includes(bucket)||filter==='no_date'&&bucket==='no_date');if(!row.hidden)count++;
    }
    const empty=document.querySelector('[data-command-empty]');if(empty)empty.hidden=count>0;
    if(save){const url=new URL(location.href);url.searchParams.set('command_filter',filter);history.replaceState(null,'',url)}
  }
  for(const button of document.querySelectorAll('[data-command-filter]'))button.addEventListener('click',()=>commandFilter(button.dataset.commandFilter));
  for(const link of document.querySelectorAll('[data-show-urgent]'))link.addEventListener('click',()=>commandFilter('now'));
  const current=document.querySelector('[data-command-filter][aria-pressed="true"]');if(current)commandFilter(current.dataset.commandFilter,false);
  const nav=document.querySelector('.np-tabs'),active=nav?.querySelector('[aria-current]');
  if(nav&&active&&nav.scrollWidth>nav.clientWidth)nav.scrollLeft=active.offsetLeft-nav.offsetLeft-nav.clientWidth/2+active.offsetWidth/2;
  render();
})();
