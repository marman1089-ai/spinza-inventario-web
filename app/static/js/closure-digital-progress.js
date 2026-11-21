/* Read status only: polling never starts or repeats an AI request. */
document.addEventListener('DOMContentLoaded',()=>{
  const timers=new Set();let active=true;
  const later=(fn,ms)=>{if(!active)return;const id=setTimeout(()=>{timers.delete(id);fn();},ms);timers.add(id);};
  document.querySelectorAll('[data-document-status]').forEach(node=>{
    const finish=text=>{node.textContent=text;node.removeAttribute('data-document-status');};
    async function check(){
      if(!active||!node.isConnected)return;
      if(document.hidden){later(check,1500);return;}
      try{
        const response=await fetch(node.dataset.documentStatus,{credentials:'same-origin',cache:'no-store'});
        if(!response.ok){if([401,403,404].includes(response.status)){finish('Documento non disponibile');return;}later(check,2500);return;}
        const result=await response.json();
        if(result.state==='done'){
          const link=document.createElement('a');link.className='shine-button';link.href=node.dataset.digitalUrl;link.textContent='Apri scheda digitale';node.replaceWith(link);return;
        }
        if(result.state==='error'){finish('Lettura non riuscita');return;}
        if(result.state==='paused'){finish('Lettura sospesa');return;}
        later(check,1000);
      }catch(error){later(check,2500);}
    }
    check();
  });
  document.querySelectorAll('[data-closure-upload]').forEach(form=>{
    let submitting=false;
    form.addEventListener('submit',event=>{
      if(event.defaultPrevented)return;
      if(submitting){event.preventDefault();return;}
      submitting=true;form.querySelectorAll('button[type="submit"],button:not([type])').forEach(button=>{button.disabled=true;});
    });
  });
  addEventListener('pagehide',()=>{active=false;timers.forEach(clearTimeout);timers.clear();},{once:true});
});
