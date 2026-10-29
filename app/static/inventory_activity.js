(() => {
  if (!/^\/inventario(?:$|\/|\-controllo)/.test(location.pathname) || location.pathname.includes('/report')) return;
  const root = document.getElementById('inventory-activity-context');
  if (!root || !root.dataset.store || root.dataset.store === 'ALL') return;
  const token = crypto.randomUUID();
  let lastAction = -Infinity, busy = false;
  const touch = () => { lastAction = performance.now(); };
  ['pointerdown','keydown','input','scroll','touchstart'].forEach(event => document.addEventListener(event,touch,{passive:true,capture:true}));
  // No activity is credited just for opening a tab. First interaction starts sampling.
  async function ping() {
    if (busy || document.visibilityState !== 'visible' || !document.hasFocus() || performance.now()-lastAction > 60000) return;
    busy = true;
    try {
      await fetch('/inventario/attivita',{method:'POST',credentials:'same-origin',headers:{'X-Inventory-Activity':'1'},body:new URLSearchParams({token,store:root.dataset.store,area:root.dataset.area})});
    } catch (_) {} finally { busy = false; }
  }
  setInterval(ping,15000);
})();
