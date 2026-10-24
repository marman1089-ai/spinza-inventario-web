'use strict';
(() => {
  document.querySelectorAll('[data-copy]').forEach(button => button.addEventListener('click', async () => {
    const target = document.getElementById(button.dataset.copy);
    const status = document.getElementById('copy-status');
    try {
      if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(target.value);
      else { target.focus(); target.select(); if (!document.execCommand('copy')) throw new Error(); }
      if (status) status.textContent = 'Testo copiato';
    } catch (_) { target.focus(); target.select(); if (status) status.textContent = 'Testo selezionato: premi Copia o Ctrl+C.'; }
  }));
  const jobPanel = document.querySelector('[data-job-url]');
  if (jobPanel) {
    const poll = async () => {
      try {
        const res = await fetch(jobPanel.dataset.jobUrl);
        if (!res.ok) return;
        const job = await res.json();
        if (job.revision > Number(jobPanel.dataset.jobRevision) || job.state === 'done' || job.state === 'error') { location.reload(); return; }
        if (job.configured) setTimeout(poll, 5000);
      } catch (_) { setTimeout(poll, 15000); }
    };
    setTimeout(poll, 5000);
  }
  const end = document.getElementById('period-end');
  const days = document.getElementById('period-days');
  function updateStart() {
    if (!end || !end.value || !days) return;
    const d = new Date(end.value + 'T12:00:00Z');
    d.setUTCDate(d.getUTCDate() - Number(days.value) + 1);
    document.getElementById('period-start').value = d.toISOString().slice(0,10);
  }
  days?.addEventListener('change', updateStart);
  end?.addEventListener('change', updateStart);
  const one = document.querySelector('[data-analyze-single]');
  async function analyze(url) {
    const response = await fetch(url, {method:'POST', headers:{'Accept':'application/json'}});
    let result;
    try { result = await response.json(); } catch (_) { throw new Error('Risposta non valida: ricarica e controlla se la lettura è stata salvata.'); }
    if (!response.ok || !result.ok) throw new Error(result.message || result.detail || 'Lettura non riuscita');
    return result;
  }
  one?.addEventListener('click', async () => {
    const form = document.getElementById('document-form');
    if ((form?.dataset.dirty === '1' || one.dataset.existingDraft === '1') && !confirm('La lettura AI sostituirà la bozza salvata. Le modifiche non salvate andranno perse. Continuare?')) return;
    one.disabled = true;
    const status = document.getElementById('analysis-status');
    status.textContent = 'Lettura in corso, attendi…';
    try { await analyze(one.dataset.analyzeSingle); location.reload(); }
    catch (error) { status.textContent = error.message; one.disabled = false; }
  });
  document.getElementById('document-form')?.addEventListener('input', e => e.currentTarget.dataset.dirty = '1');
  document.querySelector('[data-analyze-batch]')?.addEventListener('click', async e => {
    const button = e.currentTarget; button.disabled = true;
    const status = document.getElementById('batch-status');
    const rows = [...document.querySelectorAll('[data-analyze-url]')];
    let done = 0; let successes = 0; const errors = [];
    for (const row of rows) {
      status.textContent = `Lettura ${done+1} di ${rows.length}…`;
      try { await analyze(row.dataset.analyzeUrl); successes++; row.style.opacity = '.6'; }
      catch (error) { errors.push(error.message); if (/non attiva|Chiave|credito|Limite/.test(error.message)) break; }
      done++;
    }
    status.textContent = rows.length ? `${successes} documenti letti. ${errors.length ? errors.join(' · ') : 'Ricarica per controllare e verificare le bozze.'}` : 'Nessun documento nuovo da analizzare.';
    if (rows.length && !errors.length) { const a = document.createElement('a'); a.href=location.href; a.textContent=' Ricarica risultati'; status.appendChild(a); }
    button.disabled = false;
  });
  document.querySelector('[data-add-line]')?.addEventListener('click', () => {
    document.getElementById('document-lines').appendChild(document.getElementById('document-line-template').content.cloneNode(true));
  });
  document.getElementById('document-lines')?.addEventListener('click', e => { if (e.target.closest('[data-remove-line]')) e.target.closest('tr').remove(); });
  document.querySelectorAll('[data-expected]').forEach(input => input.addEventListener('input', () => {
    const cell = input.closest('tr').querySelector('[data-count-diff]');
    const diff = Number(input.value)-Number(input.dataset.expected);
    cell.textContent = input.value === '' ? '—' : (diff>0?'+':'') + diff.toLocaleString('it-IT',{maximumFractionDigits:4});
    cell.classList.toggle('control-negative', input.value!=='' && diff<0);
  }));
  document.querySelector('[data-filter-logs]')?.addEventListener('input', e => {
    const q = e.target.value.toLocaleLowerCase('it');
    document.querySelectorAll('[data-log-row]').forEach(row => row.hidden = !row.textContent.toLocaleLowerCase('it').includes(q));
  });
})();
