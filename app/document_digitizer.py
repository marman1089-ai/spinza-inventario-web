"""Estrazione assistita da documento: output da verificare prima della conferma."""
import base64
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request


def _pdf_pages(pdf_bytes):
    import fitz
    doc = fitz.open(stream=pdf_bytes, filetype='pdf')
    if len(doc) > 15:
        raise ValueError('Massimo 15 pagine per documento')
    texts = [page.get_text(sort=True) for page in doc]
    imgs = []
    for page in doc:
        pix = page.get_pixmap(matrix=fitz.Matrix(1.6, 1.6), alpha=False)
        imgs.append(pix.tobytes('jpeg'))
    doc.close()
    return texts, imgs


def _heuristic(text, kind):
    lines = []
    money_re = re.compile(r'(?<!\d)(\d{1,7}[.,]\d{2})(?!\d)')
    qty_re = re.compile(r'^\s*(\d+(?:[.,]\d+)?)\s*(?:x|pz|kg|lt|l)?\s+', re.I)
    for line in text.splitlines():
        line = ' '.join(line.split()).strip()
        if not line or len(line) < 5: continue
        match = list(money_re.finditer(line))
        if not match: continue
        if re.search(r'^(?:totale|iva|imponibile|resto|pagato|importo|subtotale)\b', line, re.I): continue
        title = line[:match[-1].start()].strip(' :-')
        if len(title) < 3: continue
        qm = qty_re.match(title)
        qty = qm.group(1).replace(',', '.') if qm else '1'
        if qm: title = title[qm.end():]
        try: amount = float(match[-1].group(1).replace('.', '').replace(',', '.'))
        except ValueError: continue
        if amount < 0 or amount > 100000: continue
        lines.append({'descrizione': title[:180], 'quantita': qty, 'prezzo_unitario': '', 'totale': amount, 'iva': ''})
    totals = re.findall(r'(?:totale\s*(?:documento|fattura|pagato)?|incasso\s*totale)\s*[:€ ]*([\d.,]+)', text, flags=re.I)
    total = totals[-1] if totals else ''
    return {'fornitore': '', 'numero_documento': '', 'data': '', 'totale': total, 'imponibile': '', 'iva': '', 'pagamento': '', 'note': '', 'righe': lines[:150], 'testo_originale': text[:25000], 'metodo': 'testo/OCR locale (verifica manuale)'}


def _vision(images, kind):
    key = os.getenv('OPENAI_API_KEY', '').strip()
    if not key: return None
    instruction = ('Leggi queste immagini di un documento italiano (' + kind + '). '
        'Trascrivi TUTTI i prodotti/le voci uno per uno, senza omettere righe e senza inventare dati. '
        'Per fatture estrai fornitore, numero, data, imponibile, IVA, totale e per OGNI prodotto descrizione, quantita, prezzo_unitario, totale riga, iva. '
        'Per chiusure estrai tutte le voci di incasso, contante, POS, delivery, fondo cassa e totale, come righe. '
        'Per spese estrai esercente, tutte le voci e totale. '
        'Per dati assenti usa stringa vuota, importi in formato numerico con punto decimale. '
        'Rispondi SOLO JSON oggetto con chiavi fornitore, numero_documento, data, imponibile, iva, totale, pagamento, note, righe; '
        'righe array di oggetti con descrizione, quantita, prezzo_unitario, totale, iva. '
        'Se una riga non è leggibile segnala in note e non inventare.')
    content = [{'type': 'text', 'text': instruction}]
    for img in images:
        content.append({'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,'+base64.b64encode(img).decode(), 'detail':'high'}})
    request_body = {'model': os.getenv('DOCUMENT_VISION_MODEL','gpt-4.1-mini'), 'messages':[{'role':'user','content':content}], 'response_format': {'type':'json_object'}, 'temperature':0, 'max_tokens':12000}
    req = urllib.request.Request('https://api.openai.com/v1/chat/completions', data=json.dumps(request_body).encode(), headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=110) as response:
        obj = json.load(response)
    result = json.loads(obj['choices'][0]['message']['content'])
    if not isinstance(result, dict): raise ValueError('Formato risposta non valido')
    result['metodo'] = 'Visione AI (richiede verifica)'
    result['testo_originale'] = ''
    return result


def extract_document(pdf_bytes, kind):
    texts, images = _pdf_pages(pdf_bytes)
    source_text = '\n\n--- PAGINA ---\n\n'.join(texts)
    error = ''
    if os.getenv('OPENAI_API_KEY','').strip():
        try: return _vision(images, kind)
        except Exception as exc: error = 'Analisi AI non disponibile: ' + str(exc)[:170]
    if shutil.which('tesseract'):
        for idx, image in enumerate(images):
            if len(texts[idx].strip()) > 100: continue
            try:
                with tempfile.NamedTemporaryFile(suffix='.jpg') as tmp:
                    tmp.write(image); tmp.flush()
                    lang = 'ita+eng'
                    r = subprocess.run(['tesseract',tmp.name,'stdout','-l',lang], capture_output=True,text=True, timeout=35)
                    if r.returncode: r = subprocess.run(['tesseract',tmp.name,'stdout','-l','eng'], capture_output=True,text=True,timeout=35)
                    texts[idx] = r.stdout
            except Exception as exc: error = str(exc)[:150]
        source_text = '\n\n--- PAGINA ---\n\n'.join(texts)
    result = _heuristic(source_text, kind)
    if error: result['note'] = error
    if not source_text.strip():
        result['note'] = (result['note']+' | ' if result['note'] else '')+'Nessun testo riconosciuto. Configura OPENAI_API_KEY per la lettura delle foto, o installa Tesseract nel server.'
    return result
