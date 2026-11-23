(() => {
  const key='store-switch-position';
  const scrollable='.ct-scroll,.expense-table,.control-table,[data-preserve-scroll]';
  window.rememberStoreSwitch=()=>{
    try{
      const frame=document.getElementById('expense-program-frame');
      frame?.contentWindow?.saveUiState?.();
      const details=Array.from(document.querySelectorAll('.wrap details')).map((el,index)=>({index,open:el.open}));
      const panels=Array.from(document.querySelectorAll(scrollable)).map((el,index)=>({index,x:el.scrollLeft,y:el.scrollTop}));
      sessionStorage.setItem(key,JSON.stringify({path:location.pathname,x:scrollX,y:scrollY,details,panels}));
    }catch{}
  };
  function restore(){
    let saved;try{saved=JSON.parse(sessionStorage.getItem(key)||'null')}catch{return}
    if(!saved||saved.path!==location.pathname)return;
    const details=document.querySelectorAll('.wrap details');
    for(const item of saved.details||[])if(details[item.index])details[item.index].open=item.open;
    const apply=()=>{window.scrollTo(saved.x||0,saved.y||0);const panels=document.querySelectorAll(scrollable);for(const item of saved.panels||[])if(panels[item.index])panels[item.index].scrollTo(item.x,item.y)};
    apply();requestAnimationFrame(apply);setTimeout(apply,100);
    sessionStorage.removeItem(key);
  }
  if(document.readyState==='complete')restore();else window.addEventListener('load',restore,{once:true});
  window.addEventListener('message',event=>{
    const frame=document.getElementById('expense-program-frame');
    if(event.origin!==location.origin||event.source!==frame?.contentWindow||event.data?.type!=='expense-program-store')return;
    const select=document.getElementById('header-store');
    if(!select||select.value===event.data.store||!Array.from(select.options).some(x=>x.value===event.data.store))return;
    select.value=event.data.store;select.dispatchEvent(new Event('change',{bubbles:true}));
  });
})();
