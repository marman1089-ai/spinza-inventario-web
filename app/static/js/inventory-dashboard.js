(() => {
 const panel=document.getElementById('activity-content');if(!panel)return;
 const dialog=document.getElementById('activity-journal-dialog'),records=document.getElementById('activity-journal-records'),more=document.getElementById('activity-journal-more');
 let query=null,page=1,loading=false,revision=0,rangeLabel='Tutta la giornata';
 let zoom='1050';
 function layoutTimeline(){
  const timeline=panel.querySelector('.activity-timeline');if(!timeline)return;timeline.style.minWidth=zoom+'px';
  const picker=panel.querySelector('#activity-timeline-zoom');if(picker)picker.value=zoom;
  panel.querySelectorAll('.activity-track').forEach(track=>{
   const lanes=[],width=track.clientWidth;
   track.querySelectorAll('.activity-bar').forEach(bar=>{
    const left=parseFloat(bar.style.left)*width/100,right=left+bar.getBoundingClientRect().width;
    let lane=lanes.findIndex(end=>end+3<=left);if(lane<0)lane=lanes.length;lanes[lane]=right;bar.style.top=(4+lane*28)+'px';
   });
   track.style.height=Math.max(32,lanes.length*28+8)+'px';
  });
 }
 panel.addEventListener('change',event=>{if(event.target.id==='activity-timeline-zoom'&&['1050','2100','4200','8400'].includes(event.target.value)){zoom=event.target.value;layoutTimeline();}});
 window.addEventListener('resize',layoutTimeline);layoutTimeline();
 const add=(parent,tag,text)=>{const el=document.createElement(tag);el.textContent=text;parent.appendChild(el);return el;};
 function renderRecord(r){
  const article=document.createElement('article');article.className='activity-journal-record'+(r.status>=400?' is-error':'')+(r.source==='log'?' is-log':'');
  add(article,'time',r.time);
  const type=r.source==='log'?'Movimento salvato nei Log':r.kind==='access'?'Accesso / pagina aperta':r.kind==='logout'?'Uscita':'Richiesta al server';
  add(article,'small',' · '+type);
  add(article,'h3',r.description);
  if(r.source==='log')add(article,'p',[r.category,r.item,(r.metric_label||'Valore nel Log')+': '+Number(r.delta).toLocaleString('it-IT',{signDisplay:r.metric_label==='Variazione giacenza'?'always':'auto',maximumFractionDigits:4})].filter(Boolean).join(' · '));
  else add(article,'p',(r.status>=400?'Non riuscita / negata':r.status>=300?'Completata con reindirizzamento':'Risposta riuscita')+' · HTTP '+r.status);
  if(r.path)add(article,'small',r.method+' '+r.path);
  if(r.area)add(article,'small',' · '+(r.area==='bibite'?'Sala':r.area==='prodotti'?'Cucina':r.area));
  records.appendChild(article);
 }
 async function load(first){
  if(loading||!query)return;loading=true;more.disabled=true;const run=revision;
  if(first){records.replaceChildren();add(records,'p','Caricamento del registro…');}
  const url=new URL('/admin/fasce-orarie/dettaglio',location.origin);for(const [k,v]of Object.entries(query))url.searchParams.set(k,v);url.searchParams.set('page',page);
  try{
   const response=await fetch(url,{credentials:'same-origin',cache:'no-store'});if(!response.ok)throw new Error('HTTP '+response.status);
   const data=await response.json();if(run!==revision||!dialog.open)return;
   if(first){records.replaceChildren();dialog.scrollTop=0;document.getElementById('activity-journal-title').textContent=data.username+' · Registro attività';document.getElementById('activity-journal-subtitle').textContent=data.store+' · '+data.day+' · '+rangeLabel+' · orari italiani (CET/CEST)';}
   records.querySelectorAll('.activity-load-error').forEach(el=>el.remove());
   data.records.forEach(renderRecord);if(first&&!data.records.length)add(records,'p','Nessuna operazione registrata in questo intervallo. L’accesso può avere solo un segnale di presenza.');
   more.hidden=!data.has_more;page+=1;
  }catch(_){if(run!==revision)return;if(first)records.replaceChildren();const error=add(records,'p','Registro non disponibile. Premi Riprova per ricaricare.');error.className='activity-load-error';more.hidden=false;more.textContent='Riprova';}
  finally{if(run===revision){loading=false;more.disabled=false;}}
 }
 panel.addEventListener('click',event=>{
  const button=event.target.closest('.activity-window');if(!button)return;
  revision++;loading=false;page=1;more.hidden=true;more.textContent='Carica altri 100 eventi';
  query={day:document.querySelector('.activity-filters input[name=day]').value,store:button.dataset.store,user_id:button.dataset.user,username:button.dataset.name};
  rangeLabel='Tutta la giornata';
  if(button.dataset.start){query.start=button.dataset.start;query.end=button.dataset.end;const fmt=value=>new Date(Number(value)*1000).toLocaleTimeString('it-IT',{timeZone:'Europe/Rome'});rangeLabel=fmt(query.start)+' – '+fmt(query.end);}
  const csv=new URL('/admin/fasce-orarie/dettaglio',location.origin);for(const[k,v]of Object.entries(query))csv.searchParams.set(k,v);csv.searchParams.set('download','csv');document.getElementById('activity-journal-csv').href=csv.href;
  document.getElementById('activity-journal-title').textContent=button.dataset.name+' · Registro attività';document.getElementById('activity-journal-subtitle').textContent=button.title||'Tutta la giornata';dialog.showModal();load(true);
 });
 document.getElementById('activity-journal-close').addEventListener('click',()=>dialog.close());
 dialog.addEventListener('close',()=>{revision++;loading=false;});
 more.addEventListener('click',()=>{more.textContent='Carica altri 100 eventi';load(page===1);});
 let pending=false;
 setInterval(async()=>{
  if(panel.dataset.live!=='true'||pending||document.visibilityState!=='visible'||panel.querySelector('details[open]')||dialog.open)return;
  pending=true;
  try{
   const url=new URL(location.href);url.searchParams.delete('download');url.searchParams.set('day',document.querySelector('.activity-filters input[name=day]').value);
   const response=await fetch(url,{credentials:'same-origin',cache:'no-store'});if(!response.ok)throw new Error();
   const doc=new DOMParser().parseFromString(await response.text(),'text/html');const fresh=doc.getElementById('activity-content');
   if(fresh){panel.innerHTML=fresh.innerHTML;panel.dataset.live=fresh.dataset.live;layoutTimeline();document.querySelector('.activity-refresh-note').textContent='';}else throw new Error();
  }catch(_){document.querySelector('.activity-refresh-note').textContent='Aggiornamento momentaneamente non disponibile. Riprovo automaticamente.';}
  finally{pending=false;}
 },15000);
})();
