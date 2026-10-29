(() => {
  if (!/^\/inventario(?:$|\/|\-controllo)/.test(location.pathname) || location.pathname.includes('/report')) return;
  const root = document.getElementById('inventory-activity-context');
  if (!root || !root.dataset.store || root.dataset.store === 'ALL') return;
  const token = crypto.randomUUID();
  let lastAction = -Infinity, lastPing = -Infinity, busy = false, pendingRequest = Promise.resolve();
  const body = state => new URLSearchParams({token,store:root.dataset.store,area:root.dataset.area,state});
  function send(state) {
    pendingRequest=pendingRequest.catch(()=>{}).then(()=>fetch('/inventario/attivita',{method:'POST',credentials:'same-origin',keepalive:true,headers:{'X-Inventory-Activity':'1'},body:body(state)}));
    return pendingRequest;
  }
  async function ping() {
    if(performance.now()-lastAction>60000 && lastPing!==-Infinity) return pause();
    if (busy || document.visibilityState !== 'visible' || !document.hasFocus() || performance.now()-lastAction > 60000 || performance.now()-lastPing < 12000) return;
    busy = true;lastPing=performance.now();
    try { await send('active'); }
    catch (_) {} finally { busy = false; }
  }
  const touch = () => { lastAction=performance.now();return ping(); };
  const pause = () => {
    if(lastPing === -Infinity) return;
    lastAction=-Infinity;lastPing=-Infinity;
    return send('paused').catch(()=>{});
  };
  ['pointerdown','keydown','input','scroll','touchstart'].forEach(event => document.addEventListener(event,touch,{passive:true,capture:true}));
  document.addEventListener('visibilitychange',()=>{if(document.visibilityState!=='visible')pause();});
  window.addEventListener('blur',pause);window.addEventListener('pagehide',pause);
  setInterval(ping,15000);
})();
