(() => {
 const panel=document.getElementById('activity-content');if(!panel||panel.dataset.live!=='true')return;
 let pending=false;
 setInterval(async()=>{
  if(panel.dataset.live!=='true'||pending||document.visibilityState!=='visible'||panel.querySelector('details[open]'))return;
  pending=true;
  try{
   const url=new URL(location.href);url.searchParams.delete('download');url.searchParams.set('day',document.querySelector('.activity-filters input[name=day]').value);
   const response=await fetch(url,{credentials:'same-origin',cache:'no-store'});
   if(!response.ok)throw new Error();
   const doc=new DOMParser().parseFromString(await response.text(),'text/html');const fresh=doc.getElementById('activity-content');
   if(fresh){panel.innerHTML=fresh.innerHTML;panel.dataset.live=fresh.dataset.live;document.querySelector('.activity-refresh-note').textContent='';}
   else throw new Error();
  }catch(_){document.querySelector('.activity-refresh-note').textContent='Aggiornamento momentaneamente non disponibile. Riprovo automaticamente.';}
  finally{pending=false;}
 },15000);
})();
