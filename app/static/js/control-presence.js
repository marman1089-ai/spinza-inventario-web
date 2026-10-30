(() => {
 if(location.pathname.startsWith('/admin/controllo-live')||location.pathname.startsWith('/admin/fasce-orarie'))return;
 const root=document.getElementById('inventory-activity-context');if(!root||!root.dataset.store)return;
 const token=crypto.randomUUID();let lastAction=performance.now(),lastSend=-Infinity,queue=Promise.resolve();
 const mode=()=>document.visibilityState==='visible'&&document.hasFocus()?'visible':'away';
 function send(state=mode(),force=false){
  const now=performance.now();if(!force&&now-lastSend<2000)return queue;lastSend=now;
  const payload=new URLSearchParams({token,store:root.dataset.store,area:root.dataset.area||'',page:location.pathname,mode:state,idle_seconds:String(Math.max(0,(now-lastAction)/1000))});
  queue=queue.catch(()=>{}).then(()=>fetch('/controllo/presenza',{method:'POST',credentials:'same-origin',keepalive:true,headers:{'X-Control-Presence':'1'},body:payload})).catch(()=>{});return queue;
 }
 const touch=()=>{lastAction=performance.now();return send();};
 ['pointerdown','keydown','input','scroll','touchstart'].forEach(name=>document.addEventListener(name,touch,{passive:true,capture:true}));
 document.addEventListener('visibilitychange',()=>send(mode(),true));window.addEventListener('focus',()=>send('visible',true));window.addEventListener('blur',()=>send('away',true));window.addEventListener('pagehide',()=>send('offline',true));
 setInterval(()=>{if(mode()==='visible')send('visible',true);},10000);send(mode(),true);
})();
