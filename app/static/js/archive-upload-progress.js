(() => {
 const form=document.getElementById('archive-upload-form');if(!form)return;
 const progress=document.getElementById('archive-upload-progress'),status=document.getElementById('archive-upload-status'),button=form.querySelector('button[type="submit"]');let busy=false;
 window.addEventListener('beforeunload',e=>{if(busy){e.preventDefault();e.returnValue='';}});
 form.addEventListener('submit',e=>{
  e.preventDefault();if(busy||!form.reportValidity())return;
  const data=new FormData(form),xhr=new XMLHttpRequest();busy=true;button.disabled=true;progress.hidden=false;progress.value=0;status.textContent='Invio dei file…';
  xhr.open('POST',form.action);xhr.setRequestHeader('Accept','application/json');
  xhr.upload.onprogress=event=>{if(event.lengthComputable){const percent=Math.round(event.loaded/event.total*100);progress.value=percent;status.textContent=percent<100?`Invio dei file: ${percent}%`:'File inviati. Salvataggio in archivio…';}};
  function failed(){busy=false;button.disabled=false;status.textContent='Caricamento non confermato. Controlla l’archivio prima di riprovare per evitare doppioni.';}
  xhr.onerror=failed;xhr.onabort=failed;
  xhr.onload=()=>{
   if(xhr.status<200||xhr.status>=300){failed();return;}
   try{const result=JSON.parse(xhr.responseText);if(!result.redirect)throw Error();busy=false;window.location.assign(result.redirect);}
   catch(error){if(xhr.responseURL&&new URL(xhr.responseURL).origin===window.location.origin){busy=false;window.location.assign(xhr.responseURL);}else failed();}
  };
  xhr.send(data);
 });
})();
