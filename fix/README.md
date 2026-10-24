# Upload immagini -> PDF compressi

Questo modulo permette di:
- prendere una foto (JPG/PNG)
- ridimensionarla
- comprimerla
- convertirla in PDF (1 pagina)

## Installazione
Aggiungi nel tuo requirements.txt:
Pillow
img2pdf

## Uso
from pdf_utils import image_to_compressed_pdf

pdf_bytes = image_to_compressed_pdf(file_bytes)

Salva poi `pdf_bytes` nel database o in Supabase Storage.