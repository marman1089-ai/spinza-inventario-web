import io
from PIL import Image
try:
    import img2pdf
except ImportError:  # l'app resta avviabile; serve solo per convertire immagini
    img2pdf = None

def image_to_compressed_pdf(image_bytes: bytes, max_side: int = 1600, jpeg_quality: int = 70) -> bytes:
    """
    Converte una foto (JPG/PNG) in un PDF da 1 pagina,
    ridimensionando e comprimendo prima l'immagine.
    Ritorna i bytes del PDF.
    """
    if img2pdf is None:
        raise RuntimeError('Conversione immagini non disponibile: installa img2pdf da requirements.txt.')
    img = Image.open(io.BytesIO(image_bytes))

    if img.mode != "RGB":
        img = img.convert("RGB")

    w, h = img.size
    scale = min(max_side / max(w, h), 1.0)
    if scale < 1.0:
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

    jpg_buf = io.BytesIO()
    img.save(jpg_buf, format="JPEG", quality=jpeg_quality, optimize=True, progressive=True)

    pdf_bytes = img2pdf.convert(jpg_buf.getvalue())
    return pdf_bytes