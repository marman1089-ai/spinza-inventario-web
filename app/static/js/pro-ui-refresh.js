/* Professional UI Refresh: page loading, transitions, safe prefetch. */
(function(){
  'use strict';

  const state = {
    started:false,
    value:0,
    timer:null,
    prefetched:new Set()
  };

  function qs(id){ return document.getElementById(id); }
  function isInternalUrl(url){
    try{
      const u = new URL(url, window.location.href);
      return u.origin === window.location.origin;
    }catch(e){ return false; }
  }
  function shouldIgnoreLink(a){
    if(!a) return true;
    const href = a.getAttribute('href') || '';
    if(!href || href.startsWith('#') || href.startsWith('javascript:') || href.startsWith('mailto:') || href.startsWith('tel:')) return true;
    if(a.target && a.target !== '_self') return true;
    if(a.hasAttribute('download')) return true;
    return !isInternalUrl(href);
  }
  function setProgress(v){
    const bar = qs('pageProgress');
    if(!bar) return;
    state.value = Math.max(state.value, Math.min(v, 96));
    bar.style.width = state.value + '%';
  }
  function startLoading(label){
    if(state.started) return;
    state.started = true;
    state.value = 0;
    document.body.classList.add('is-leaving');
    const bar = qs('pageProgress');
    const toast = qs('pageLoaderToast');
    const text = qs('pageLoaderText');
    if(text && label) text.textContent = label;
    if(bar){
      bar.classList.add('is-visible');
      bar.style.width = '8%';
    }
    if(toast) toast.classList.add('is-visible');
    state.value = 8;
    clearInterval(state.timer);
    state.timer = setInterval(function(){
      const next = state.value < 55 ? state.value + 8 : state.value < 82 ? state.value + 3 : state.value + 1;
      setProgress(next);
    }, 220);
  }
  function finishLoading(){
    const bar = qs('pageProgress');
    const toast = qs('pageLoaderToast');
    clearInterval(state.timer);
    if(bar){
      bar.classList.add('is-visible');
      bar.style.width = '100%';
      setTimeout(function(){
        bar.classList.remove('is-visible');
        bar.style.width = '0%';
      }, 260);
    }
    if(toast) toast.classList.remove('is-visible');
    document.body.classList.remove('is-leaving');
    state.started = false;
    state.value = 0;
  }
  window.SpinzaUIStartLoading = startLoading;
  window.SpinzaUIFinishLoading = finishLoading;

  document.addEventListener('DOMContentLoaded', function(){
    finishLoading();
    document.documentElement.classList.add('ui-ready');

    // Make page sections feel more responsive without touching app logic.
    document.querySelectorAll('.card,.chart-card,.report-card,.sum-box,.insight,.kpi').forEach(function(el, idx){
      if(idx > 18) return;
      el.style.animationDelay = Math.min(idx * 18, 180) + 'ms';
      el.classList.add('ui-enter');
    });
  });

  window.addEventListener('pageshow', function(){ finishLoading(); });
  window.addEventListener('beforeunload', function(){ setProgress(92); });

  document.addEventListener('click', function(ev){
    const a = ev.target && ev.target.closest ? ev.target.closest('a') : null;
    if(shouldIgnoreLink(a)) return;
    if(ev.defaultPrevented || ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) return;
    startLoading('Apro la pagina...');
  }, true);

  document.addEventListener('submit', function(ev){
    if(ev.defaultPrevented) return;
    const form = ev.target;
    const hasFile = form && form.querySelector && form.querySelector('input[type="file"]');
    startLoading(hasFile ? 'Carico il file...' : 'Salvo e aggiorno...');
  }, true);

  // Prefetch leggero quando passi sopra o tocchi un link interno.
  function prefetch(a){
    if(shouldIgnoreLink(a)) return;
    const href = new URL(a.getAttribute('href'), window.location.href).href;
    if(state.prefetched.has(href)) return;
    state.prefetched.add(href);
    const link = document.createElement('link');
    link.rel = 'prefetch';
    link.href = href;
    link.as = 'document';
    document.head.appendChild(link);
  }
  document.addEventListener('mouseover', function(ev){
    const a = ev.target && ev.target.closest ? ev.target.closest('a') : null;
    if(a) prefetch(a);
  }, {passive:true});
  document.addEventListener('touchstart', function(ev){
    const a = ev.target && ev.target.closest ? ev.target.closest('a') : null;
    if(a) prefetch(a);
  }, {passive:true});
})();
