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


def _parse_decimal(raw):
    token = str(raw or '').replace('€','').replace(' ', '').strip()
    if not token: return ''
    if ',' in token and '.' in token:
        token = token.replace('.', '').replace(',', '.') if token.rfind(',') > token.rfind('.') else token.replace(',', '')
    elif ',' in token: token = token.replace(',', '.')
    try: return str(round(float(token), 4))
    except ValueError: return ''


def _heuristic(text, kind):
    """OCR best-effort (non fiscal certification); every field is editable by user."""
    rawlines = [' '.join(line.split()).strip() for line in text.splitlines() if line.strip()]
    result = {'fornitore':'','numero_documento':'','data':'','ora':'','totale':'','imponibile':'',
        'iva':'','pagamento':'','note':'','righe':[],'testo_originale':text[:40000],
        'metodo':'OCR gratuito: risultato da verificare'}
    date_match = re.search(r'\b(\d{2})[./-](\d{2})[./-](20\d{2})\b', text)
    if date_match: result['data'] = f'{date_match.group(3)}-{date_match.group(2)}-{date_match.group(1)}'
    time_match = re.search(r'\b([01]\d|2[0-3]):([0-5]\d)\b',text)
    if time_match: result['ora']=time_match.group(0)
    for ln in rawlines[:12]:
        if len(ln)>3 and len(ln)<95 and not re.search(r'\b(?:P\.?IVA|C\.?F\.?|TEL|FATTURA|DOCUMENTO|PAGINA|DATA|QUANTIT|DESCRIZIONE)\b',ln,re.I) and not re.match(r'^[\d\W]+$',ln):
            result['fornitore']=ln[:100]; break
    num = re.search(r'\b(?:fattura|documento|doc\.)\s*(?:n\.?|numero|nr\.?|n°)?\s*[:#-]?\s*([\w/-]{2,28})',text,re.I)
    if num: result['numero_documento']=num.group(1)
    money=r'(\d{1,7}(?:[.,]\d{3})*[.,]\d{2})'
    patterns = {
      'totale':[r'\b(?:totale\s+(?:documento|fattura|dovuto|euro|pagare)|totale\s+complessivo|totale\s+vendite|totale\s+spesa|totale\s+incassi|importo\s+totale|totale)\s*[:€ ]*'+money],
      'imponibile':[r'\bimponibile\s*[:€ ]*'+money],
      'iva':[r'\b(?:totale\s+iva|iva\s+totale)\s*[:€ ]*'+money],
    }
    if kind=='chiusure':
      patterns.update({'contanti':[r'\bcontanti\s*[:€ ]*'+money], 'pos':[r'\b(?:carta|pos|carte|bancomat)\s*[:€ ]*'+money], 'ticket':[r'\bticket\s*[:€ ]*'+money], 'sconti':[r'\bsconti\s*[:€ ]*'+money], 'annullamenti':[r'\bannullament[oi]\s*[:€ ]*'+money]})
    if kind=='spese':
      patterns.update({'resto':[r'\bresto\s*[:€ ]*'+money], 'importo_consegnato':[r'\b(?:contante\s+ricevuto|contanti\s+ricevuti)\s*[:€ ]*'+money]})
    for key,expressions in patterns.items():
        values=[]
        for exp in expressions: values += re.findall(exp,text,re.I)
        if values: result[key]=_parse_decimal(values[-1])
    skip = re.compile(r'^(?:totale|iva|imponibile|resto|pagato|importo|subtotale|carta|pos|contanti|p\.?iva|cod\.?|numero|data|fattura|pagamento|documento|sconto|annullam|aliquota)',re.I)
    for ln in rawlines:
        if skip.match(ln) or len(ln)<5: continue
        nums = list(re.finditer(money,ln))
        if not nums: continue
        # amount is at the end; never invent unit prices when source is ambiguous
        match=nums[-1]
        desc=ln[:match.start()].strip(' :-•')
        if len(desc)<3 or not re.search('[A-Za-zÀ-ÿ]',desc): continue
        qty=''; unit=''; unit_price=''
        q=re.match(r'^(\d+(?:[.,]\d+)?)\s*(kg|pz|p[ez]z[i.]?|lt|l|cf|x|nr)?\s+',desc,re.I)
        if q:qty=_parse_decimal(q.group(1));unit=q.group(2) or '';desc=desc[q.end():]
        if len(nums)>=2:
            # leftmost money column could be price; leave blank if likely uncertain
            unit_price=_parse_decimal(nums[-2].group(1))
        amount=_parse_decimal(match.group(1))
        result['righe'].append({'descrizione':desc[:180],'quantita':qty,'unita_misura':unit,'prezzo_unitario':unit_price,'totale':amount,'iva':'','reparto':'','categoria':''})
        if len(result['righe'])>=250: break
    if not text.strip():result['note']='Non è stato rilevato testo leggibile. Compila manualmente.'
    return result


def extract_document(pdf_bytes, kind):
    """Free, offline extraction for text PDFs; optional Tesseract if installed.
    Scanned PDFs are supported by the browser OCR flow on the document page.
    """
    texts, images = _pdf_pages(pdf_bytes)
    errors = []
    if shutil.which('tesseract'):
        for idx, image in enumerate(images):
            if len(texts[idx].strip()) > 100: continue
            try:
                with tempfile.NamedTemporaryFile(suffix='.jpg') as tmp:
                    tmp.write(image); tmp.flush()
                    proc = subprocess.run(['tesseract', tmp.name, 'stdout', '-l', 'ita+eng'],
                                          capture_output=True, text=True, timeout=40)
                    if proc.returncode:
                        proc = subprocess.run(['tesseract', tmp.name, 'stdout', '-l', 'eng'],
                                              capture_output=True, text=True, timeout=40)
                    if proc.returncode == 0: texts[idx] = proc.stdout
            except Exception as exc:
                errors.append(str(exc)[:120])
    result = _heuristic('\n\n--- PAGINA ---\n\n'.join(texts), kind)
    if errors: result['note'] = 'OCR parziale. Usa OCR gratuito dal browser. ' + '; '.join(errors[:2])
    if not result['testo_originale'].strip():
        result['note'] = 'Questo PDF è una scansione: usa il pulsante OCR gratuito nel browser oppure compila manualmente.'
    return result
