(() => {
  const csrf=__CSRF__;
  const original=window.fetch.bind(window);
  async function paymentMethod(){
    const dialog=document.createElement('dialog');
    dialog.style.cssText='background:#101b2d;color:#eef3ff;border:1px solid #526582;border-radius:18px;padding:24px;max-width:420px;width:85vw;z-index:99999';
    dialog.innerHTML='<form method="dialog"><h2>Come hai pagato?</h2><p>Il metodo collega il pagamento alle uscite del gestionale. Solo i contanti aggiornano i Bilanci cash.</p><select name="method" style="width:100%;padding:12px;margin:12px 0"><option>Bonifico</option><option>Carta</option><option>Contanti</option></select><div style="display:flex;gap:12px"><button value="cancel">Annulla</button><button value="confirm">Conferma pagamento</button></div></form>';
    document.body.append(dialog);
    return await new Promise(resolve=>{dialog.addEventListener('close',()=>{const value=dialog.returnValue==='confirm'?dialog.querySelector('select').value:null;dialog.remove();resolve(value)},{once:true});dialog.showModal()});
  }
  window.fetch=async (input,options={})=>{
    const url=String(input),method=(options.method||'GET').toUpperCase();
    if(url.startsWith('/spese-programma/api/')&&method!=='GET'){
      const headers=new Headers(options.headers||{});headers.set('X-Expense-CSRF',csrf);options={...options,headers};
      if(method==='POST'){
        const body=JSON.parse(options.body||'{}');
        if(url.split('?')[0].endsWith('/pay')||(/\/(orders|purchases)$/.test(url)&&body.payment_status!=='pending')){
          const selected=await paymentMethod();
          if(!selected)return new Response(JSON.stringify({error:'Pagamento annullato: nessuna modifica effettuata'}),{status:409,headers:{'Content-Type':'application/json'}});
          body.payment_method=selected;options.body=JSON.stringify(body);
        }
      }
    }
    return original(input,options);
  };
  const link=document.createElement('a');link.href='/gestionale/tutte-le-spese';link.target='_top';link.textContent='Archivio spese unico ↗';
  link.style.cssText='display:block;margin:12px;padding:12px;color:#cbdcff;border:1px solid #526582;border-radius:12px;text-align:center';
  document.querySelector('.sidebar')?.append(link);
})();
