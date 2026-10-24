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
