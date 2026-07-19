/* Professional UI Refresh: transitions, safe confirmations and interaction feedback. */
(function(){
  'use strict';

  const state = { started:false, value:0, timer:null, prefetched:new Set(), confirmedForms:new WeakSet() };
  const prefersReducedMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  function qs(id){ return document.getElementById(id); }
  function isInternalUrl(url){
    try{ return new URL(url, window.location.href).origin === window.location.origin; }
    catch(e){ return false; }
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
    if(bar){ bar.classList.add('is-visible'); bar.style.width = '8%'; }
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
      setTimeout(function(){ bar.classList.remove('is-visible'); bar.style.width = '0%'; }, 260);
    }
    if(toast) toast.classList.remove('is-visible');
    document.body.classList.remove('is-leaving');
    state.started = false;
    state.value = 0;
  }
  window.SpinzaUIStartLoading = startLoading;
  window.SpinzaUIFinishLoading = finishLoading;

  function buildConfirmDialog(){
    if(qs('uiConfirmBackdrop')) return;
    const wrap = document.createElement('div');
    wrap.id = 'uiConfirmBackdrop';
    wrap.className = 'ui-confirm-backdrop';
    wrap.setAttribute('aria-hidden','true');
    wrap.innerHTML = '<div class="ui-confirm-dialog" role="alertdialog" aria-modal="true" aria-labelledby="uiConfirmTitle" aria-describedby="uiConfirmMessage">' +
      '<div class="ui-confirm-icon" aria-hidden="true">⚠️</div>' +
      '<h3 id="uiConfirmTitle">Conferma operazione</h3>' +
      '<p id="uiConfirmMessage"></p>' +
      '<div class="ui-confirm-actions"><button type="button" class="btn2" id="uiConfirmCancel">Annulla</button><button type="button" class="btn ui-confirm-danger" id="uiConfirmAccept">Conferma</button></div>' +
      '</div>';
    document.body.appendChild(wrap);
  }

  function requestConfirmation(form){
    buildConfirmDialog();
    const backdrop = qs('uiConfirmBackdrop');
    const title = qs('uiConfirmTitle');
    const message = qs('uiConfirmMessage');
    const cancel = qs('uiConfirmCancel');
    const accept = qs('uiConfirmAccept');
    if(!backdrop || !title || !message || !cancel || !accept) return Promise.resolve(window.confirm(form.dataset.confirm || 'Confermare?'));
    title.textContent = form.dataset.confirmTitle || 'Conferma operazione';
    message.textContent = form.dataset.confirm || 'Confermare questa operazione?';
    accept.textContent = form.dataset.confirmAction || 'Conferma';
    accept.classList.toggle('ui-confirm-danger', form.dataset.confirmDanger === 'true');
    backdrop.classList.add('is-open');
    backdrop.setAttribute('aria-hidden','false');
    document.body.style.overflow = 'hidden';
    setTimeout(function(){ cancel.focus(); }, 20);
    return new Promise(function(resolve){
      let done = false;
      function close(value){
        if(done) return;
        done = true;
        backdrop.classList.remove('is-open');
        backdrop.setAttribute('aria-hidden','true');
        document.body.style.overflow = '';
        cancel.removeEventListener('click', onCancel);
        accept.removeEventListener('click', onAccept);
        backdrop.removeEventListener('click', onBackdrop);
        document.removeEventListener('keydown', onKey);
        resolve(value);
      }
      function onCancel(){ close(false); }
      function onAccept(){ close(true); }
      function onBackdrop(ev){ if(ev.target === backdrop) close(false); }
      function onKey(ev){ if(ev.key === 'Escape') close(false); }
      cancel.addEventListener('click', onCancel);
      accept.addEventListener('click', onAccept);
      backdrop.addEventListener('click', onBackdrop);
      document.addEventListener('keydown', onKey);
    });
  }

  function markSubmitting(form){
    if(!form || form.dataset.uiSubmitting === '1') return false;
    form.dataset.uiSubmitting = '1';
    const submitters = form.querySelectorAll('button[type="submit"],input[type="submit"]');
    submitters.forEach(function(btn){
      btn.disabled = true;
      btn.classList.add('is-submitting');
      if(btn.tagName === 'BUTTON'){
        btn.dataset.originalText = btn.textContent;
        const label = form.dataset.submitLabel || btn.dataset.submitLabel;
        if(label) btn.textContent = label;
      }
    });
    return true;
  }

  function showToast(message, type){
    if(!message) return;
    let stack = qs('uiToastStack');
    if(!stack){ stack = document.createElement('div'); stack.id = 'uiToastStack'; stack.className = 'ui-toast-stack'; document.body.appendChild(stack); }
    const toast = document.createElement('div');
    toast.className = 'ui-toast ' + (type === 'error' ? 'is-error' : 'is-success');
    toast.setAttribute('role', type === 'error' ? 'alert' : 'status');
    toast.textContent = message;
    stack.appendChild(toast);
    setTimeout(function(){ toast.style.opacity = '0'; toast.style.transform = 'translateY(10px)'; }, 4200);
    setTimeout(function(){ toast.remove(); }, 4550);
  }
  window.SpinzaUIToast = showToast;

  function animateCounters(){
    if(prefersReducedMotion) return;
    document.querySelectorAll('[data-count-up]').forEach(function(el){
      const end = Number(el.dataset.countUp);
      if(!Number.isFinite(end)) return;
      const prefix = el.dataset.countPrefix || '';
      const suffix = el.dataset.countSuffix || '';
      const duration = 650;
      const started = performance.now();
      function frame(now){
        const p = Math.min(1, (now - started) / duration);
        const eased = 1 - Math.pow(1 - p, 3);
        const value = end * eased;
        el.textContent = prefix + value.toLocaleString('it-IT', {minimumFractionDigits:2, maximumFractionDigits:2}) + suffix;
        if(p < 1) requestAnimationFrame(frame);
      }
      requestAnimationFrame(frame);
    });
  }

  document.addEventListener('DOMContentLoaded', function(){
    finishLoading();
    document.documentElement.classList.add('ui-ready');
    buildConfirmDialog();
    document.querySelectorAll('.card,.chart-card,.report-card,.sum-box,.insight,.kpi').forEach(function(el, idx){
      if(idx > 22) return;
      el.style.animationDelay = Math.min(idx * 18, 180) + 'ms';
      el.classList.add('ui-enter');
    });
    animateCounters();
  });

  window.addEventListener('pageshow', function(){ finishLoading(); });
  window.addEventListener('beforeunload', function(){ setProgress(92); });

  document.addEventListener('click', function(ev){
    const a = ev.target && ev.target.closest ? ev.target.closest('a') : null;
    if(shouldIgnoreLink(a)) return;
    if(ev.defaultPrevented || ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) return;
    startLoading('Apro la pagina...');
  }, true);

  // Conferma prima del submit. Il caricamento parte solo dopo la conferma: niente loader bloccato se si annulla.
  document.addEventListener('submit', function(ev){
    const form = ev.target;
    if(!form || !form.matches) return;
    if(form.dataset.confirm && !state.confirmedForms.has(form)){
      ev.preventDefault();
      requestConfirmation(form).then(function(accepted){
        if(!accepted) return;
        state.confirmedForms.add(form);
        form.dataset.uiConfirmed = '1';
        if(typeof form.requestSubmit === 'function') form.requestSubmit();
        else form.submit();
      });
      return;
    }
    state.confirmedForms.delete(form);
    delete form.dataset.uiConfirmed;
    // Rinvia di un tick: rispetta eventuali handler onsubmit che annullano il form.
    setTimeout(function(){
      if(ev.defaultPrevented) return;
      if(!markSubmitting(form)) return;
      const hasFile = form.querySelector && form.querySelector('input[type="file"]');
      startLoading(hasFile ? 'Carico il file...' : 'Salvo e aggiorno...');
    }, 0);
  }, false);

  document.addEventListener('pointerdown', function(ev){
    if(prefersReducedMotion) return;
    const target = ev.target && ev.target.closest ? ev.target.closest('button,.btn,.btn2') : null;
    if(!target || target.disabled) return;
    const rect = target.getBoundingClientRect();
    const ripple = document.createElement('span');
    const size = Math.max(rect.width, rect.height) * 1.8;
    ripple.className = 'ui-ripple';
    ripple.style.width = size + 'px';
    ripple.style.height = size + 'px';
    ripple.style.left = (ev.clientX - rect.left) + 'px';
    ripple.style.top = (ev.clientY - rect.top) + 'px';
    target.appendChild(ripple);
    setTimeout(function(){ ripple.remove(); }, 600);
  }, {passive:true});

  function prefetch(a){
    if(shouldIgnoreLink(a)) return;
    const href = new URL(a.getAttribute('href'), window.location.href).href;
    if(state.prefetched.has(href)) return;
    state.prefetched.add(href);
    const link = document.createElement('link');
    link.rel = 'prefetch'; link.href = href; link.as = 'document';
    document.head.appendChild(link);
  }
  document.addEventListener('mouseover', function(ev){ const a = ev.target && ev.target.closest ? ev.target.closest('a') : null; if(a) prefetch(a); }, {passive:true});
  document.addEventListener('touchstart', function(ev){ const a = ev.target && ev.target.closest ? ev.target.closest('a') : null; if(a) prefetch(a); }, {passive:true});
})();
