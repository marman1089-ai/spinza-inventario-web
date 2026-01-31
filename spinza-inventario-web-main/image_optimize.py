import io
from typing import Tuple, Literal
from PIL import Image

OutputFormat = Literal["jpeg", "png"]

def _open_as_rgb(image_bytes: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(image_bytes))
    img.load()
    if img.mode != "RGB":
        img = img.convert("RGB")
    return img

def _resize_max_side(img: Image.Image, max_side: int) -> Image.Image:
    w, h = img.size
    scale = min(max_side / max(w, h), 1.0)  # never upscale
    if scale < 1.0:
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    return img

def encode_jpeg(img: Image.Image, quality: int = 60) -> bytes:
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=quality, optimize=True, progressive=True)
    return out.getvalue()

def encode_png(img: Image.Image) -> bytes:
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True, compress_level=9)
    return out.getvalue()

def optimize_image_bytes(
    image_bytes: bytes,
    *,
    max_side: int = 1600,
    jpeg_quality: int = 60,
    try_png: bool = True,
    keep_png_only_if_smaller: bool = True,
) -> Tuple[bytes, OutputFormat, str]:
    """
    Prende bytes immagine (JPG/PNG) e restituisce bytes ottimizzati.
    - Default: produce JPEG compresso.
    - Se try_png=True, produce anche PNG e (se keep_png_only_if_smaller) tiene quello più piccolo.
    Ritorna: (bytes_ottimizzati, formato, content_type)
    """
    img = _open_as_rgb(image_bytes)
    img = _resize_max_side(img, max_side=max_side)

    best_bytes = encode_jpeg(img, quality=jpeg_quality)
    best_fmt: OutputFormat = "jpeg"
    best_ct = "image/jpeg"

    if try_png:
        png_bytes = encode_png(img)
        if (not keep_png_only_if_smaller) or (len(png_bytes) < len(best_bytes)):
            best_bytes = png_bytes
            best_fmt = "png"
            best_ct = "image/png"

    return best_bytes, best_fmt, best_ct
