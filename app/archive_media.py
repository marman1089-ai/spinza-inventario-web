"""Small derived previews; originals and permissions remain unchanged."""
import io
from functools import lru_cache
from fastapi import Request,HTTPException
from fastapi.responses import Response
from PIL import Image,ImageOps,UnidentifiedImageError
from . import db

@lru_cache(maxsize=128)
def thumbnail(database_key,file_id,store,stamp,size):
    ph='%s' if db.using_postgres() else '?'
    with db.connect() as conn:
        row=conn.cursor().execute(f'SELECT data,ts,size_bytes FROM archive_files WHERE id={ph} AND store={ph}',(file_id,store)).fetchone()
    if not row or str(row['ts'])!=stamp or int(row['size_bytes'])!=size: return None
    try:
        with Image.open(io.BytesIO(bytes(row['data']))) as image:
            image.draft('RGB',(960,640))
            image=ImageOps.exif_transpose(image)
            image.thumbnail((480,320))
            image=image.convert('RGB')
            output=io.BytesIO();image.save(output,format='JPEG',quality=72)
            return output.getvalue()
    except (UnidentifiedImageError,OSError,ValueError,Image.DecompressionBombError): return None

def install(app,api):
    @app.get('/file-archiviati/file/{file_id}/anteprima')
    def preview(request:Request,file_id:int):
        user=api['_archive_admin_user'](request)
        if not user: raise HTTPException(403,'Accesso riservato.')
        scope=api['_archive_scope'](request,user);ph=api['_ph']()
        with db.connect() as conn:
            row=conn.cursor().execute(f'SELECT ts,size_bytes FROM archive_files WHERE id={ph} AND store={ph}',(file_id,scope)).fetchone()
        if not row: raise HTTPException(404,'File non trovato.')
        key=db._database_url() or str(db.SQLITE_PATH.resolve())
        data=thumbnail(key,file_id,scope,str(row['ts']),int(row['size_bytes']))
        if data is None: return Response(status_code=204)
        return Response(data,media_type='image/jpeg',headers={'Cache-Control':'private, no-cache','X-Content-Type-Options':'nosniff'})
