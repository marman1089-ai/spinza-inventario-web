import io
from typing import Tuple

from PIL import Image
try:
    import img2pdf
except ImportError:  # l'app resta avviabile; serve solo per convertire immagini
    img2pdf = None
from pypdf import PdfReader, PdfWriter

# Impostazioni consigliate per fatture/chiusure:
# - max_side: 1400px (leggibile ma molto più leggero delle foto originali)
# - jpeg_quality: 55 (qualità minima ma leggibile, ottimo rapporto peso/qualità)
DEFAULT_MAX_SIDE = 1400
DEFAULT_JPEG_QUALITY = 55

def _image_bytes_to_pdf(image_bytes: bytes, max_side: int, jpeg_quality: int) -> bytes:
    if img2pdf is None:
        raise RuntimeError('Conversione immagini non disponibile: installa img2pdf da requirements.txt.')
    img = Image.open(io.BytesIO(image_bytes))
    img.load()

    # per PDF/JPEG serve RGB
    if img.mode != "RGB":
        img = img.convert("RGB")

    w, h = img.size
    scale = min(max_side / max(w, h), 1.0)  # non ingrandire mai
    if scale < 1.0:
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

    jpg_buf = io.BytesIO()
    img.save(
        jpg_buf,
        format="JPEG",
        quality=jpeg_quality,
        optimize=True,
        progressive=True,
    )
    return img2pdf.convert(jpg_buf.getvalue())

def ensure_pdf(
    file_bytes: bytes,
    filename: str,
    content_type: str,
    *,
    max_side: int = DEFAULT_MAX_SIDE,
    jpeg_quality: int = DEFAULT_JPEG_QUALITY,
) -> Tuple[bytes, str, str]:
    """Ritorna (pdf_bytes, pdf_filename, 'application/pdf').

    - Se l'upload è un'immagine (image/*) la converte in PDF 1 pagina compresso.
    - Se è già un PDF, lo lascia com'è (ma forza estensione .pdf).
    """
    filename = (filename or "documento").strip() or "documento"
    content_type = (content_type or "application/octet-stream").lower()

    # Alcuni browser/device inviano content_type vuoto o 'application/octet-stream'.
    # In quel caso decidiamo anche dall'estensione del filename.
    ext = (filename.rsplit('.', 1)[-1].lower() if '.' in filename else '')
    is_image_ext = ext in ('jpg','jpeg','png','webp','heic')

    base = filename.rsplit(".", 1)[0] if "." in filename else filename
    pdf_filename = f"{base}.pdf"

    if content_type.startswith("image/") or is_image_ext:
        pdf_bytes = _image_bytes_to_pdf(file_bytes, max_side=max_side, jpeg_quality=jpeg_quality)
        return pdf_bytes, pdf_filename, "application/pdf"

    # già PDF o altro: se non è pdf, comunque lo salviamo come pdf? (no: rischia di corrompere)
    # Qui lasciamo com'è, ma se NON è un PDF, lo salviamo con filename originale.
    if content_type in ("application/pdf", "application/x-pdf"):
        return file_bytes, pdf_filename, "application/pdf"

    # fallback: non è immagine né PDF -> salva originale
    return file_bytes, filename, content_type


def merge_pdfs(pdf_bytes_list):
    """Merge a list of PDF byte strings into a single PDF."""
    if not pdf_bytes_list:
        return b""
    if len(pdf_bytes_list) == 1:
        return pdf_bytes_list[0]
    writer = PdfWriter()
    for b in pdf_bytes_list:
        reader = PdfReader(io.BytesIO(b))
        for page in reader.pages:
            writer.add_page(page)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()
