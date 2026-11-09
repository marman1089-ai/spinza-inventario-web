import io
import json
import re
import unittest
import zipfile
from datetime import date
from unittest.mock import patch
import test_control as fixture
from app import main,db,admin_dashboard as ad

class AdminDashboardTests(unittest.TestCase):
    setUp=fixture.ControlTests.setUp
    login=fixture.ControlTests.login
    sql=fixture.ControlTests.sql
    pdf=fixture.ControlTests.pdf
    def file(self,name='nota.txt',content=b'Fornitore segreto: acquisto detergente',store='spinza',mime='text/plain',folder=None):
        return self.sql('INSERT INTO archive_files(store,folder_id,filename,content_type,size_bytes,data,created_by) VALUES(?,?,?,?,?,?,?)',(store,folder,name,mime,len(content),content,'testadmin'))
    def test_board_all_and_one_store_preserves_admin_functions(self):
        self.login(store='ALL')
        for store in main.STORES:
            self.sql('INSERT INTO closure_reports(store,closure_date,lines_json,total_cents,notes,created_by,created_at,submission_token) VALUES(?,?,?,?,?,?,?,?)',(store,'2026-11-03','[]',12000,'','testadmin','2026-11-03T23:00:00',store))
            self.sql('INSERT INTO closures(store,closure_date,uploaded_by,filename,content_type,data) VALUES(?,?,?,?,?,?)',(store,'2026-11-04','testadmin','foto-'+store+'.pdf','application/pdf',self.pdf()))
        with patch.object(main,'_rome_today',return_value=date(2026,11,4)):
            result=self.client.get('/admin')
            self.assertEqual(result.status_code,200,result.text[:200])
            self.assertEqual(result.text.count('class="card ad-shop"'),3)
            self.assertIn('04/11/2026',result.text);self.assertIn('03/11/2026',result.text)
            self.assertIn('foto-reburger_maggio.pdf',result.text)
            self.assertIn('Crea account',result.text);self.assertIn('Lista account',result.text)
            switched=self.client.post('/set-active-store',data={'store':'reburger_maggio','next_url':'/admin'},follow_redirects=False)
            result=self.client.get(switched.headers['location'])
            self.assertEqual(result.text.count('class="card ad-shop"'),1)
            self.assertIn('ad-single',result.text)
            self.assertNotIn('foto-spinza.pdf',result.text)
    def test_search_full_content_folders_and_pagination(self):
        root=self.sql("INSERT INTO archive_folders(store,name,created_by) VALUES('spinza','Contratti','testadmin')")
        child=self.sql("INSERT INTO archive_folders(store,parent_id,name,created_by) VALUES('spinza',?,'2026','testadmin')",(root,))
        fid=self.file(folder=child)
        self.assertTrue(ad.index_one('archivio',fid));self.assertFalse(ad.index_one('archivio',fid))
        result=self.client.get('/admin/indice-file?q=detergente').json()
        self.assertEqual(result['total'],1)
        self.assertEqual(result['rows'][0]['folder_path'],'Contratti / 2026')
        self.assertIn('Fornitore segreto',result['rows'][0]['excerpt'])
        for i in range(45):self.file(name=f'pagina-{i}.txt')
        result=self.client.get('/admin/indice-file?q=pagina-').json()
        self.assertEqual(result['total'],45);self.assertEqual(len(result['rows']),40)
        result=self.client.get('/admin/indice-file?q=pagina-&page=1').json()
        self.assertEqual(len(result['rows']),5)
        self.assertEqual(self.client.get('/admin/indice-file?q=%25').json()['total'],0)
    def test_permissions_and_originals_across_all_stores(self):
        fid=self.file(store='reburger_maggio');general=self.file(store='ALL')
        self.assertEqual(self.client.get(f'/admin/indice-file/archivio/{fid}/originale').status_code,404)
        self.login(store='ALL')
        self.assertEqual(self.client.get(f'/admin/indice-file/archivio/{fid}/originale').status_code,200)
        self.assertEqual(self.client.get(f'/admin/indice-file/archivio/{general}/originale').status_code,200)
        self.login(2)
        for url in ['/admin/indice-file',f'/admin/indice-file/archivio/{fid}/testo',f'/admin/indice-file/archivio/{fid}/originale']:
            self.assertEqual(self.client.get(url).status_code,403)
        self.assertEqual(self.client.get('/admin',follow_redirects=False).status_code,303)
    def test_same_size_text_edit_invalidates_preview(self):
        fid=self.file(content=b'alpha');ad.index_one('archivio',fid)
        response=self.client.post(f'/file-archiviati/file/{fid}/modifica',data={'content':'bravo'},follow_redirects=False)
        self.assertEqual(response.status_code,303)
        self.assertEqual(self.client.get('/admin/indice-file?q=alpha').json()['total'],0)
        self.assertTrue(ad.index_one('archivio',fid))
        self.assertEqual(self.client.get('/admin/indice-file?q=bravo').json()['total'],1)
        self.assertEqual(self.client.get(f'/admin/indice-file/archivio/{fid}/testo').json()['text'],'bravo')
        self.client.post(f'/file-archiviati/file/{fid}/sostituisci',files={'file':('test.txt',b'charl','text/plain')},follow_redirects=False)
        self.assertTrue(ad.index_one('archivio',fid))
        self.assertEqual(self.client.get('/admin/indice-file?q=bravo').json()['total'],0)
        self.assertEqual(self.client.get('/admin/indice-file?q=charl').json()['total'],1)
    def test_pdf_preview_and_unsupported_and_safe_original(self):
        import pymupdf as fitz
        with fitz.open() as pdf:
            page=pdf.new_page();page.insert_text((50,50),'Documento esempio: ricevuta 123')
            raw=pdf.tobytes()
        fid=self.file('ricevuta.pdf',raw,mime='application/pdf');ad.index_one('archivio',fid)
        result=self.client.get('/admin/indice-file?q=ricevuta').json()['rows'][0]
        self.assertIn('ricevuta 123',result['excerpt'])
        self.assertTrue(result['thumbnail'].startswith('data:image/jpeg;base64,'))
        bad=self.file('rotto.pdf',b'broken',mime='application/pdf');ad.index_one('archivio',bad)
        state=self.sql("SELECT state FROM admin_file_index WHERE kind='archivio' AND file_id=?",(bad,))[0]['state']
        self.assertEqual(state,'error')
        svg=self.file('unsafe.svg',b'<svg/>',mime='image/svg+xml')
        result=self.client.get(f'/admin/indice-file/archivio/{svg}/originale')
        self.assertTrue(result.headers['content-disposition'].startswith('attachment'))
        self.assertEqual(result.headers['cache-control'],'no-store')
        z=io.BytesIO()
        with zipfile.ZipFile(z,'w') as archive:archive.writestr('spese/dettagli.txt','qualcosa')
        text,_,state,_=ad.extract(z.getvalue(),'documenti.zip','application/zip')
        self.assertIn('spese/dettagli.txt',text)
    def test_default_recent_files_and_specific_date(self):
        old=self.file('archivio-storico.txt')
        self.sql("UPDATE archive_files SET ts='2026-10-01 10:00:00' WHERE id=?",(old,))
        today=self.file('archivio-oggi.txt')
        self.sql("UPDATE archive_files SET ts='2026-11-09T10:00:00+00:00' WHERE id=?",(today,))
        boundary=self.file('fuori-periodo.txt')
        self.sql("UPDATE archive_files SET ts='2026-11-07T21:00:00+00:00' WHERE id=?",(boundary,))
        with patch.object(main,'_rome_today',return_value=date(2026,11,9)):
            recent=self.client.get('/admin/indice-file').json()
            self.assertEqual([row['id'] for row in recent['rows']],[today])
            past=self.client.get('/admin/indice-file?day=2026-10-01').json()
            self.assertEqual([row['id'] for row in past['rows']],[old])

    def test_staff_documents_are_released_only_by_admin_review(self):
        for kind, table in [('fatture','invoices_docs'),('chiusure','closures'),('spese-secondarie','secondary_expenses')]:
            self.login(2)
            upload=self.client.post(f'/{kind}/upload',data={'supplier':'Fornitore iniziale'},files=[('files',('attesa.pdf',self.pdf(),'application/pdf'))],follow_redirects=False)
            self.assertEqual(upload.status_code,303,upload.text)
            doc_id=self.sql(f'SELECT MAX(id) AS id FROM {table}')[0]['id']
            url=f'/admin/file-da-verificare/documenti/{kind}/{doc_id}'
            self.assertEqual(self.client.get(url).status_code,403)
            self.assertNotIn('attesa.pdf',self.client.get(f'/{kind}').text)
            self.login(store='ALL')
            queue=self.client.get('/admin/file-da-verificare')
            self.assertIn(url,queue.text)
            page=self.client.get(url)
            csrf=re.search(r'name="csrf" value="([^"]+)"',page.text).group(1)
            payload={'csrf':csrf,'doc_date':'2026-11-08','filename':f'confermato-{kind}.pdf','supplier':'Fornitore corretto'}
            self.assertEqual(self.client.post(url,data=payload,follow_redirects=False).status_code,303)
            self.assertEqual(self.client.post(url,data=payload,follow_redirects=False).status_code,409)
            self.assertNotIn(url,self.client.get('/admin/file-da-verificare').text)
            self.assertIn(f'confermato-{kind}.pdf',self.client.get(f'/{kind}').text)
            self.login(2)
            self.assertEqual(self.client.post(f'/{kind}/{doc_id}/delete',follow_redirects=False).status_code,403)

    def test_metadata_page_does_not_read_original_blobs(self):
        self.file(content=b'private original')
        with db.connect() as conn:
            statements=[];conn.set_trace_callback(statements.append)
            result=ad.dashboard(conn.cursor(),'spinza',main.STORES,date(2026,11,4))
        self.assertEqual(result['file_count'],1)
        self.assertTrue(all('SELECT data FROM' not in query for query in statements))

if __name__=='__main__':unittest.main()
