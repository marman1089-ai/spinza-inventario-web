import unittest,re
import test_control as fixture
class InventoryBatchTests(unittest.TestCase):
    setUp=fixture.ControlTests.setUp
    login=fixture.ControlTests.login
    sql=fixture.ControlTests.sql
    def token(self):
        page=self.client.get('/inventario')
        self.assertEqual(page.status_code,200)
        self.assertIn('inventory-save-dock',page.text)
        return re.search(r'data-csrf="([^"]+)"',page.text)[1]
    def send(self,changes,csrf=None):
        return self.client.post('/inventario/salva-quantita',json=dict(csrf=csrf or self.token(),changes=changes))
    def test_batch_and_conflict_are_atomic(self):
        csrf=self.token()
        changes=[dict(id=self.pid,store='spinza',qty=22,original=20),dict(id=self.sala,store='spinza',qty=9,original=5)]
        response=self.send(changes,csrf)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['saved'],2)
        self.assertEqual(self.sql('SELECT qty FROM products WHERE id=?',(self.pid,))[0]['qty'],22)
        changes=[dict(id=self.pid,store='spinza',qty=30,original=22),dict(id=self.sala,store='spinza',qty=15,original=5)]
        self.assertEqual(self.send(changes,csrf).status_code,409)
        self.assertEqual(self.sql('SELECT qty FROM products WHERE id=?',(self.pid,))[0]['qty'],22)
    def test_permissions_csrf_and_invalid_quantities(self):
        self.login(uid=2)
        csrf=self.token()
        change=dict(id=self.pid,store='spinza',qty=21,original=20)
        self.assertEqual(self.send([change],csrf).status_code,200)
        self.assertEqual(self.send([dict(change,store='reburger_maggio')],csrf).status_code,403)
        self.assertEqual(self.send([dict(change,qty=-1)],csrf).status_code,422)
        self.assertEqual(self.send([change],'wrong').status_code,403)
        self.assertEqual(self.send([change,change],csrf).status_code,422)
    def test_archive_lists_metadata_without_reading_file_contents(self):
        from unittest.mock import patch
        from contextlib import contextmanager
        from app import main,db
        self.sql("INSERT INTO archive_files(store,filename,content_type,size_bytes,data,created_by) VALUES('spinza','ricevuta-test.pdf','application/pdf',100,?,'testadmin')",(b'x'*100,))
        statements=[]
        @contextmanager
        def traced_connect():
            with db.connect() as conn:
                conn.set_trace_callback(statements.append)
                yield conn
        with patch.object(main,'connect',traced_connect):
            for url in ['/file-archiviati','/file-archiviati?q=ricevuta-test']:
                response=self.client.get(url)
                self.assertEqual(response.status_code,200)
                self.assertIn('ricevuta-test.pdf',response.text)
        listings=[q for q in statements if 'FROM archive_files WHERE store=' in q and q.startswith('SELECT')]
        self.assertTrue(listings)
        self.assertTrue(all('SELECT *' not in q and ' data' not in q for q in listings))
    def test_archive_pagination_and_upload_confirmation(self):
        from app import db
        with db.connect() as conn:
            conn.cursor().executemany("INSERT INTO archive_files(store,filename,content_type,size_bytes,data,created_by) VALUES('spinza',?,'text/plain',1,?,'testadmin')",[(f'archive-{n:03}.txt',b'x') for n in range(50)])
        first=self.client.get('/file-archiviati?q=archive-')
        second=self.client.get('/file-archiviati?q=archive-&page=2')
        self.assertEqual(first.text.count('class="archive-item js-file-card"'),48)
        self.assertEqual(second.text.count('class="archive-item js-file-card"'),2)
        self.assertIn('archive-049.txt',second.text)
        self.assertNotIn('archive-049.txt',first.text)
        result=self.client.post('/file-archiviati/upload',files={'files':('upload-test.txt',b'hello','text/plain')},headers={'Accept':'application/json'})
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()['saved'],1)
        self.assertIn('/file-archiviati',result.json()['redirect'])
        self.assertEqual(self.sql("SELECT data FROM archive_files WHERE filename='upload-test.txt'")[0]['data'],b'hello')
    def test_archive_light_preview_and_original(self):
        import io
        from PIL import Image
        image=io.BytesIO();Image.new('RGB',(1800,1200),'red').save(image,format='PNG');original=image.getvalue()
        file_id=self.sql("INSERT INTO archive_files(store,filename,content_type,size_bytes,data,created_by) VALUES('spinza','foto.png','image/png',?,?,'testadmin')",(len(original),original))
        preview=self.client.get(f'/file-archiviati/file/{file_id}/anteprima')
        self.assertEqual(preview.status_code,200)
        self.assertEqual(preview.headers['content-type'],'image/jpeg')
        with Image.open(io.BytesIO(preview.content)) as small:
            self.assertLessEqual(small.width,480);self.assertLessEqual(small.height,320)
        self.assertEqual(self.client.get(f'/file-archiviati/file/{file_id}/download').content,original)
        self.login(store='reburger_maggio')
        self.assertEqual(self.client.get(f'/file-archiviati/file/{file_id}/anteprima').status_code,404)
        self.login(uid=2)
        self.assertEqual(self.client.get(f'/file-archiviati/file/{file_id}/anteprima').status_code,403)
if __name__=='__main__':unittest.main()
