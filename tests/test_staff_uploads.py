import io
import re
import unittest
from PIL import Image
from pypdf import PdfReader
from pillow_heif import from_pillow
import test_control as fixture

class UploadTests(unittest.TestCase):
    setUp=fixture.ControlTests.setUp
    login=fixture.ControlTests.login
    sql=fixture.ControlTests.sql

    def test_staff_photo_invoice_and_admin_verification(self):
        self.login(2)
        data=io.BytesIO()
        Image.new('RGB',(24,24),'blue').save(data,format='JPEG')
        photo=data.getvalue()
        data=io.BytesIO()
        from_pillow(Image.new('RGB',(24,24),'red')).save(data)
        heic=data.getvalue()
        documents={}
        def send(path,**kwargs):
            return self.client.post(path,follow_redirects=False,**kwargs)
        for kind,table,pages in [('chiusure','closures',2),('fatture','invoices_docs',1)]:
            body={'supplier_choice':'Fornitore Test'} if kind=='fatture' else {}
            files=[('files',('foto-📷.jpg',photo,'image/jpeg'))]
            if kind=='chiusure':
                files.append(('files',('foto-iPhone.heic',heic,'image/heic')))
            response=send('/'+kind+'/upload',data=body,files=files)
            self.assertEqual(response.status_code,303,response.text)
            own_page=self.client.get(response.headers['location'])
            self.assertIn('Documento inviato',own_page.text)
            self.assertIn('In attesa di verifica',own_page.text)
            doc=self.sql(f'SELECT * FROM {table}')[0]
            self.assertEqual(doc['uploaded_by'],'teststaff')
            self.assertEqual(len(PdfReader(io.BytesIO(doc['data'])).pages),pages)
            documents[kind]=doc['id']
            self.assertEqual(self.client.get(f'/{kind}/{doc["id"]}').status_code,200)
            malformed=send('/'+kind+'/upload',files={'files':('falso.pdf',b'%PDF bad','application/pdf')})
            self.assertEqual(malformed.status_code,422)
        self.login(3)
        for kind in ('chiusure','fatture'):
            self.assertEqual(self.client.get(f'/{kind}/{documents[kind]}').status_code,404)
        self.login()
        for kind,doc_id in documents.items():
            review=f'/admin/file-da-verificare/documenti/{kind}/{doc_id}'
            page=self.client.get(review)
            self.assertEqual(page.status_code,200,page.text)
            token=re.search(r'name="csrf" value="([^"]+)"',page.text)[1]
            original=f'/admin/indice-file/{kind}/{doc_id}/originale'
            self.assertEqual(self.client.get(original).status_code,200)
            response=send(review,data={'csrf':token,'filename':'Verificato.pdf','doc_date':self.dt,'supplier':'Fornitore Test'})
            self.assertEqual(response.status_code,303,response.text)
if __name__=='__main__':
    unittest.main()
