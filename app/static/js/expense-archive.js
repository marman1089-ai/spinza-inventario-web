/* Payment choices are fetched only when a linking form is opened. */
(() => {
  const money=new Intl.NumberFormat('it-IT',{style:'currency',currency:'EUR'});
  for(const box of document.querySelectorAll('[data-expense-match]')){
    const details=box.closest('details'),search=box.querySelector('[data-payment-search]'),select=box.querySelector('[data-payment-options]'),status=box.querySelector('[data-payment-status]'),more=box.querySelector('[data-payment-more]');
    let page=1,loaded=false,controller,timer;
    async function load(append=false){
      controller?.abort();controller=new AbortController();status.textContent='Caricamento…';more.hidden=true;
      try{
        const query=new URLSearchParams({q:search.value,exclude:box.dataset.exclude,page:String(page)});
        const response=await fetch('/gestionale/tutte-le-spese/pagamenti?'+query,{signal:controller.signal});
        if(!response.ok)throw new Error();
        const data=await response.json();
        if(!append)select.replaceChildren(new Option('Scegli il pagamento',''));
        for(const item of data.items)select.add(new Option(`${item.day} · ${item.description} · ${money.format(item.amount/100)}`,item.key));
        more.hidden=!data.has_more;status.textContent=data.items.length?'Seleziona il pagamento corrispondente.':'Nessun risultato. Prova un’altra descrizione o data.';loaded=true;
      }catch(error){if(error.name!=='AbortError'){loaded=false;status.textContent='Caricamento non riuscito. Riapri questa sezione per riprovare.'}}
    }
    details.addEventListener('toggle',()=>{if(details.open&&!loaded){page=1;load()}});
    search.addEventListener('input',()=>{clearTimeout(timer);controller?.abort();timer=setTimeout(()=>{page=1;load()},300)});
    more.addEventListener('click',()=>{page++;load(true)});
  }
})();
