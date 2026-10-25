/* Input separati con lo stesso nome 'files': FastAPI riceve tutte le pagine in ordine.
   La fotocamera mobile consente di aggiungere una foto alla volta senza perdere le precedenti. */
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('[data-document-upload]').forEach(holder => {
    const form = holder.closest('form');
    const hint = form.querySelector('.upload-count');
    holder.innerHTML = `<div class="du-header"><strong>📎 Pagine del documento</strong><span class="du-badge" aria-live="polite">0 file</span></div>
      <p class="du-help">Aggiungi tutte le pagine, anche fotografandole una alla volta. Verranno salvate in un solo PDF, nell'ordine mostrato qui.</p>
      <div class="du-actions"><button type="button" class="du-add" data-add="file">＋ 1 file</button><button type="button" class="du-add du-camera" data-add="camera">📷 Scatta foto</button></div>
      <ol class="du-items" aria-label="Pagine selezionate"></ol>`;
    const items = holder.querySelector('.du-items');
    const badge = holder.querySelector('.du-badge');
    const valid = /\.(pdf|jpe?g|png|webp)$/i;
    function update() {
      let count=0;
      items.querySelectorAll('li').forEach(li=> { count += Array.from(li.querySelector('input').files).length; });
      badge.textContent=count+' file';
      if(hint) hint.textContent=count ? `✓ ${count} file pront${count===1?'o':'i'} per un unico documento` : '';
    }
    function add(kind) {
      const input=document.createElement('input');
      input.type='file'; input.name='files';
      input.accept=kind==='camera'?'image/*':'application/pdf,image/jpeg,image/png,image/webp';
      if(kind==='camera') input.setAttribute('capture','environment');
      else input.multiple=true;
      input.className='du-file-input';
      const li=document.createElement('li');
      li.className='du-item du-pending';
      li.append(input);
      items.append(li);
      input.addEventListener('change',()=>{
        const files=Array.from(input.files||[]);
        if(!files.length) { li.remove(); update(); return; }
        if(files.some(f=> !valid.test(f.name) && !(kind==='camera' && f.type.startsWith('image/')))) {
          alert('Sono consentiti soltanto PDF e immagini JPG, PNG o WEBP.'); li.remove(); update(); return;
        }
        li.classList.remove('du-pending');
        const label=document.createElement('span'); label.className='du-names';
        label.textContent=files.map(f=>f.name || 'Foto scattata').join(', ');
        const remove=document.createElement('button');remove.type='button';remove.className='du-remove';remove.textContent='✕ Rimuovi';
        remove.addEventListener('click',()=>{li.remove();update();});
        li.append(label,remove); update();
      });
      input.click();
      // If the picker is cancelled the empty field remains invisible and doesn't block upload.
    }
    holder.querySelectorAll('[data-add]').forEach(btn=>btn.addEventListener('click',()=>add(btn.dataset.add)));
    form.addEventListener('submit',(e)=>{
      const count=Array.from(items.querySelectorAll('input[type=file]')).reduce((n,x)=>n+x.files.length,0);
      if(!count){ e.preventDefault(); alert('Aggiungi almeno una foto o un PDF prima di salvare.'); }
    });
    update();
  });
});
