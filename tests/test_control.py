"""Integration regression checks. Uses only isolated SQLite files; never the attached databases."""
import base64
import io
import json
import os
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
from itsdangerous import TimestampSigner
from fastapi.testclient import TestClient
from pypdf import PdfWriter

os.environ.pop('DATABASE_URL',None)
os.environ['SESSION_SECRET']='integration-test-only'
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import db
from app import main
from app import control

class ControlTests(unittest.TestCase):
    def setUp(self):
        folder=Path(__file__).resolve().parents[1]/'work'/'test-databases'
        folder.mkdir(parents=True,exist_ok=True)
        db.SQLITE_PATH=folder/(str(uuid.uuid4())+'.sqlite')
        db.init_db()
        control.init_control_db()
        # Migration is idempotent.
        control.init_control_db()
        with db.connect() as conn:
            cur=conn.cursor()
            for st,username,role in [('spinza','testadmin','admin'),('spinza','teststaff','staff'),('reburger_maggio','otherstaff','staff')]:
                cur.execute('INSERT INTO users(store,username,role) VALUES(?,?,?)',(st,username,role))
            cur.execute("INSERT INTO products(store,category,name,area,location,unit,qty,min_qty,supplier) VALUES('spinza','Bibite','Cola','bibite','MAGAZZINO','pz',20,5,'Fornitore')")
            self.pid=cur.lastrowid
            cur.execute("INSERT INTO products(store,category,name,area,location,unit,qty,min_qty,supplier) VALUES('spinza','Bibite','Cola','bibite','SALA','pz',5,2,'Fornitore')")
            self.sala=cur.lastrowid
            cur.execute("INSERT INTO products(store,category,name,area,location,unit,qty,min_qty,supplier) VALUES('spinza','Cucina','Farina','prodotti','MAGAZZINO','kg',10,3,'Fornitore')")
            self.food=cur.lastrowid
        self.client=TestClient(main.app,raise_server_exceptions=True)
        self.login()
        self.dt=control.today().isoformat()

    def login(self,uid=1,store='spinza'):
        user={'id':uid,'username':'testadmin' if uid==1 else 'teststaff','role':'admin' if uid==1 else 'staff','store':'spinza' if uid!=3 else 'reburger_maggio'}
        payload=base64.b64encode(json.dumps({'user':user,'active_store':store,'selected_area':'bibite'}).encode())
        cookie=TimestampSigner('integration-test-only').sign(payload).decode()
        self.client.cookies.clear()
        self.client.cookies.set('session',cookie)

    def sql(self,query,args=()):
        with db.connect() as conn:
            cur=conn.cursor(); cur.execute(query,args)
            return cur.fetchall() if cur.description else cur.lastrowid

    def qty(self,pid):
        return self.sql('SELECT qty FROM products WHERE id=?',(pid,))[0]['qty']

    def order(self,qty=10,kind='ordine',store='spinza',source=None,product=True):
        oid=self.sql('INSERT INTO orders(store,supplier,status,created_by,kind,from_store,to_store) VALUES(?,?,?,?,?,?,?)',
            (store,'Fornitore','in_corso','testadmin',kind,'spinza' if kind=='transfer' else None,store if kind=='transfer' else None))
        lid=self.sql('INSERT INTO order_lines(order_id,product_id,category,name,qty,area,unit,source_product_id) VALUES(?,?,?,?,?,?,?,?)',
            (oid,self.pid if product else None,'Bibite','Cola',qty,'bibite','pz',source))
        return oid,lid

    def pdf(self):
        out=io.BytesIO(); w=PdfWriter();w.add_blank_page(width=300,height=400);w.write(out);return out.getvalue()

    def doc(self,kind='fatture',data=None):
        table,col,_=control.KINDS[kind]
        if kind=='fatture':
            return self.sql(f'INSERT INTO {table}(store,supplier,{col},uploaded_by,filename,content_type,data) VALUES(?,?,?,?,?,?,?)',('spinza','Fornitore',self.dt,'testadmin','test.pdf','application/pdf',data or self.pdf()))
        return self.sql(f'INSERT INTO {table}(store,{col},uploaded_by,filename,content_type,data) VALUES(?,?,?,?,?,?)',('spinza',self.dt,'testadmin','test.pdf','application/pdf',data or self.pdf()))

    def save(self,did,kind='fatture',revision=0,confirm=True,pid=None,stock=3,amount='12.50'):
        return self.client.post(f'/documenti/{kind}/{did}/salva',data={
            'revision':revision,'doc_date':self.dt,'supplier':'Fornitore','amount':amount,'currency':'EUR',
            'description':'Acquisto test','raw_text':'Trascrizione test','warnings':'',
            'line_description':'Cola','line_qty':'3','line_unit':'pz','line_amount':'12.50',
            'product_id':str(pid or self.pid),'stock_qty':str(stock),**({'confirm':'1'} if confirm else {})},follow_redirects=False)

    def test_pages_and_role_scoping(self):
        self.doc(); oid,lid=self.order()
        qid=self.sql('INSERT INTO order_queue(store,product_id,category,name,qty_to_order,added_by) VALUES(?,?,?,?,?,?)',('spinza',self.pid,'Bibite','Cola',4,'testadmin'))
        for url in ['/admin/controllo','/report-documenti','/documenti/fatture/1','/inventario-controllo?area=bibite','/inventario-controllo?area=prodotti','/ordini/preparazione','/ordinati','/storico-ordini','/inventario','/fatture','/chiusure','/spese-secondarie']:
            r=self.client.get(url);self.assertEqual(r.status_code,200,(url,r.text[:150]))
        self.login(2)
        for url in ['/admin/controllo','/report-documenti']:
            self.assertEqual(self.client.get(url).status_code,403)
        self.assertEqual(self.client.get('/inventario-controllo').status_code,200)
        self.login(1,'reburger_maggio')
        self.assertEqual(self.client.get('/documenti/fatture/1').status_code,404)

    def test_partial_delivery_requeues_only_remainder_and_preserves_metadata(self):
        oid,lid=self.order()
        self.sql('INSERT INTO order_queue(store,product_id,category,name,qty_to_order,added_by) VALUES(?,?,?,?,?,?)',('spinza',self.pid,'Bibite','Cola',2,'testadmin'))
        r=self.client.post('/ordinati/arrivato',data={'order_id':oid,f'received_{lid}':'6'},follow_redirects=False)
        self.assertEqual(r.status_code,303,r.text)
        self.assertEqual(self.qty(self.pid),26)
        line=self.sql('SELECT * FROM order_lines WHERE id=?',(lid,))[0]
        self.assertEqual((line['received_qty'],line['is_missing'],line['area'],line['unit']),(6,1,'bibite','pz'))
        self.assertEqual(self.sql('SELECT qty_to_order FROM order_queue')[0]['qty_to_order'],6)
        self.assertEqual(self.client.post('/ordinati/arrivato',data={'order_id':oid,f'received_{lid}':'6'}).status_code,409)
        self.assertEqual(self.qty(self.pid),26)
        self.assertIn('Consegna parziale',self.client.get('/storico-ordini').text)

    def test_missing_checkbox_saves_zero_received(self):
        oid,lid=self.order()
        r=self.client.post('/ordinati/arrivato',data={'order_id':oid,f'received_{lid}':'10','missing_ids':str(lid)},follow_redirects=False)
        self.assertEqual(r.status_code,303)
        self.assertEqual(self.qty(self.pid),20)
        self.assertEqual(self.sql('SELECT received_qty FROM order_lines')[0]['received_qty'],0)
        self.assertEqual(self.sql('SELECT qty_to_order FROM order_queue')[0]['qty_to_order'],10)

    def test_invalid_delivery_rolls_back(self):
        oid,lid=self.order()
        for val in ['nan','inf','-1','11','']:
            self.assertEqual(self.client.post('/ordinati/arrivato',data={'order_id':oid,f'received_{lid}':val}).status_code,422,val)
        self.assertEqual(self.qty(self.pid),20)
        self.assertEqual(self.sql('SELECT status FROM orders')[0]['status'],'in_corso')

    def test_transfer_creates_receiver_and_conserves_stock(self):
        oid,lid=self.order(4,'transfer','reburger_maggio',self.pid,False)
        self.login(1,'ALL')
        r=self.client.post('/ordinati/arrivato',data={'order_id':oid,f'received_{lid}':'4'},follow_redirects=False)
        self.assertEqual(r.status_code,303,r.text)
        self.assertEqual(self.qty(self.pid),16)
        self.assertEqual(self.sql("SELECT qty FROM products WHERE store='reburger_maggio'")[0]['qty'],4)
        self.assertEqual(self.qty(self.sala),5)

    def test_transfer_insufficient_stock_is_atomic(self):
        oid,lid=self.order(30,'transfer','reburger_maggio',self.pid,False)
        self.login(1,'ALL')
        self.assertEqual(self.client.post('/ordinati/arrivato',data={'order_id':oid,f'received_{lid}':'30'}).status_code,409)
        self.assertEqual(self.qty(self.pid),20)
        self.assertFalse(self.sql("SELECT id FROM products WHERE store='reburger_maggio'"))

    def test_queue_confirmation_all_and_duplicate(self):
        qid=self.sql('INSERT INTO order_queue(store,product_id,category,name,qty_to_order,added_by) VALUES(?,?,?,?,?,?)',('spinza',self.pid,'Bibite','Cola',4,'testadmin'))
        self.assertEqual(self.client.get('/ordini/preparazione').status_code,200)
        self.assertEqual(self.client.post('/ordini/confirm',data={'supplier':'Fornitore','queue_ids':qid},follow_redirects=False).status_code,303)
        self.assertEqual(self.client.post('/ordini/confirm',data={'supplier':'Fornitore','queue_ids':qid}).status_code,409)
        self.assertEqual(len(self.sql('SELECT id FROM orders')),1)
        l=self.sql('SELECT * FROM order_lines')[0]
        self.assertEqual((l['area'],l['unit']),('bibite','pz'))

    def test_stock_positions_and_count(self):
        r=self.client.post('/inventario-controllo/movimento',data={'product_id':self.pid,'qty':'3','reason':'Rifornimento','action':'trasferisci','target':'SALA'},follow_redirects=False)
        self.assertEqual(r.status_code,303,r.text)
        self.assertEqual((self.qty(self.pid),self.qty(self.sala)),(17,8))
        self.assertEqual(self.client.post(f'/items/{self.sala}/set',data={'qty':'7','store':'spinza'},follow_redirects=False).status_code,303)
        self.assertEqual((self.qty(self.pid),self.qty(self.sala)),(17,7))
        r=self.client.post('/inventario-controllo/conteggio',data={'area':'bibite','days':1,f'actual_{self.pid}':'15',f'expected_{self.pid}':'17','notes':'Test'},follow_redirects=False)
        self.assertEqual(r.status_code,303,r.text)
        self.assertEqual(self.sql('SELECT difference FROM stock_count_lines')[0]['difference'],-2)
        self.assertEqual(self.qty(self.pid),15)
        page=self.client.get('/inventario-controllo').text
        self.assertIn('Conteggio da eseguire',page) # Sala position not yet counted.
        self.assertEqual(self.client.post('/inventario-controllo/conteggio',data={'area':'bibite',f'actual_{self.pid}':'14',f'expected_{self.pid}':'17'}).status_code,409)

    def test_movement_rollback_when_target_invalid(self):
        r=self.client.post('/inventario-controllo/movimento',data={'product_id':self.pid,'qty':'3','reason':'Test','action':'trasferisci','target':'MAGAZZINO'})
        self.assertEqual(r.status_code,422)
        self.assertEqual(self.qty(self.pid),20)

    def test_digital_reports_review_and_receipt_idempotence(self):
        did=self.doc()
        self.assertEqual(self.save(did).status_code,303)
        r=self.client.get('/report-documenti',params={'start':self.dt,'end':self.dt,'download':'txt'})
        self.assertEqual(r.status_code,200)
        self.assertIn('Totale Fatture verificato: 12,50 EUR',r.text)
        self.assertEqual(self.save(did,revision=0).status_code,409)
        self.assertEqual(self.client.post(f'/documenti/fatture/{did}/carica-inventario',data={'mode':'carica'},follow_redirects=False).status_code,303)
        self.assertEqual(self.qty(self.pid),23)
        self.assertEqual(self.client.post(f'/documenti/fatture/{did}/carica-inventario',data={'mode':'carica'},follow_redirects=False).status_code,303)
        self.assertEqual(self.qty(self.pid),23)
        self.assertEqual(self.client.get(f'/documenti/fatture/{did}').status_code,200)
        other=self.doc()
        self.assertEqual(self.save(other).status_code,303)
        self.assertEqual(self.client.post(f'/documenti/fatture/{other}/carica-inventario',data={'mode':'carica'}).status_code,409)
        self.assertEqual(self.qty(self.pid),23)

    def test_already_received_invoice_does_not_load(self):
        did=self.doc()
        self.save(did)
        self.assertEqual(self.client.post(f'/documenti/fatture/{did}/carica-inventario',data={'mode':'gia-caricata'},follow_redirects=False).status_code,303)
        self.assertEqual(self.qty(self.pid),20)
        self.assertEqual(self.sql('SELECT mode FROM document_receipts')[0]['mode'],'gia-caricata')

    def test_ai_mock_boards_and_no_key(self):
        did=self.doc()
        with patch.dict(os.environ,{'OPENAI_API_KEY':''}):
            r=self.client.post(f'/documenti/fatture/{did}/analizza')
            self.assertEqual(r.status_code,422)
            self.assertIn('non attiva',r.json()['message'])
        result=dict(date=self.dt,supplier='Fornitore',amount=12.50,currency='EUR',description='Cola',text='Test',warnings=['Controllare la quantità'],lines=[dict(description='Cola',qty=3,unit='pz',amount=12.50)])
        with patch('app.control.extract_document',return_value=result):
            self.assertEqual(self.client.post(f'/documenti/fatture/{did}/analizza').status_code,200)
        self.assertEqual(self.qty(self.pid),20)
        self.assertEqual(self.sql('SELECT state FROM digital_documents')[0]['state'],'bozza')
        self.assertEqual(self.client.get(f'/documenti/fatture/{did}').status_code,200)

    def test_closures_expenses_and_csv_never_count_draft(self):
        for kind in ['chiusure','spese-secondarie']:
            did=self.doc(kind)
            self.assertEqual(self.save(did,kind,confirm=False).status_code,303)
            self.assertEqual(self.client.get(f'/documenti/{kind}/{did}').status_code,200)
        r=self.client.get('/report-documenti',params={'start':self.dt,'end':self.dt,'download':'txt'})
        self.assertNotIn('Totale Chiusure verificato',r.text)
        self.assertIn('Esclusi dai totali',r.text)
        self.assertEqual(self.client.get('/report-documenti?download=csv').status_code,200)

    def test_document_upload_opens_digital_review(self):
        for kind,form in [('fatture',{'supplier':'Fornitore','doc_date':self.dt}),('chiusure',{'closure_date':self.dt}),('spese-secondarie',{'expense_date':self.dt})]:
            r=self.client.post(f'/{kind}/upload',data=form,files=[('files',('test.pdf',self.pdf(),'application/pdf'))],follow_redirects=False)
            self.assertEqual(r.status_code,303,r.text)
            self.assertIn(f'/documenti/{kind}/',r.headers['location'])

    def test_transfer_creation_remembers_exact_source(self):
        r=self.client.post('/scambi/submit',data={'from_store':'spinza','to_store':'reburger_maggio',f'qty_{self.sala}':'2'},follow_redirects=False)
        self.assertEqual(r.status_code,303,r.text)
        lines=self.sql('SELECT * FROM order_lines')
        self.assertEqual(lines[0]['source_product_id'],self.sala)

    def test_cancel_preserves_history_without_changing_stock(self):
        oid,lid=self.order()
        self.assertEqual(self.client.post('/ordinati/annulla',data={'order_id':oid},follow_redirects=False).status_code,303)
        self.assertEqual(self.qty(self.pid),20)
        self.assertEqual(self.sql('SELECT status FROM orders')[0]['status'],'annullato')
        self.assertIn('Ordine annullato',self.client.get('/storico-ordini').text)
        self.assertEqual(self.client.post('/ordinati/arrivato',data={'order_id':oid,f'received_{lid}':'10'}).status_code,409)

    def test_history_date_filter_and_null_order_kind(self):
        oid,lid=self.order()
        self.sql('UPDATE orders SET kind=NULL WHERE id=?',(oid,))
        self.client.post('/ordinati/arrivato',data={'order_id':oid,f'received_{lid}':'10'},follow_redirects=False)
        r=self.client.get('/storico-ordini',params={'kind':'ordine','start':'2000-01-01','end':'2099-12-31'})
        self.assertIn('Cola',r.text)
        r=self.client.get('/storico-ordini',params={'start':'2000-01-01','end':'2000-01-02'})
        self.assertNotIn('Cola',r.text)

    def test_concurrent_delivery_only_loads_once(self):
        from concurrent.futures import ThreadPoolExecutor
        oid,lid=self.order()
        cookie=self.client.cookies.get('session')
        def deliver():
            client=TestClient(main.app)
            client.cookies.set('session',cookie)
            return client.post('/ordinati/arrivato',data={'order_id':oid,f'received_{lid}':'10'},follow_redirects=False).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=sorted(pool.map(lambda _: deliver(),range(2)))
        self.assertEqual(results,[303,409])
        self.assertEqual(self.qty(self.pid),30)

    def test_transfer_edit_uses_source_position(self):
        r=self.client.post('/scambi/submit',data={'from_store':'spinza','to_store':'reburger_maggio',f'qty_{self.sala}':'2'},follow_redirects=False)
        oid=self.sql('SELECT id FROM orders')[0]['id']
        page=self.client.get(f'/scambi/modifica/{oid}')
        self.assertEqual(page.status_code,200)
        self.assertIn('SALA',page.text)
        r=self.client.post('/scambi/modifica/submit',data={'order_id':oid,f'qty_{self.sala}':'3'},follow_redirects=False)
        self.assertEqual(r.status_code,303,r.text)
        line=self.sql('SELECT * FROM order_lines')[0]
        self.assertEqual((line['source_product_id'],line['qty']),(self.sala,3))
        self.login(1,'ALL')
        self.assertEqual(self.client.post('/ordinati/arrivato',data={'order_id':oid,f'received_{line["id"]}':'3'},follow_redirects=False).status_code,303)
        self.assertEqual((self.qty(self.pid),self.qty(self.sala)),(20,2))

    def test_staff_photo_only_and_originals_protected(self):
        self.login(2)
        for kind in ['fatture','chiusure','spese-secondarie']:
            r=self.client.post(f'/{kind}/upload',files=[('files',('photo.pdf',self.pdf(),'application/pdf'))],follow_redirects=False)
            self.assertEqual(r.status_code,303,r.text)
            url=r.headers['location'];did=int(url.split('/')[-1])
            page=self.client.get(url)
            self.assertEqual(page.status_code,200,page.text[:200])
            self.assertNotIn('Foto / PDF originale',page.text)
            self.assertNotIn('id="document-form"',page.text)
            self.assertEqual(self.client.get(f'/{kind}/{did}').status_code,403)
            self.assertEqual(self.client.get(f'/registrazione-digitale/{kind}').status_code,403)
            self.assertEqual(self.save(did,kind).status_code,403)
            self.assertEqual(self.client.post(f'/{kind}/{did}/delete').status_code,403)
        self.assertEqual(self.client.get('/fatture/importa-prodotti').status_code,403)
        self.assertEqual(self.client.get('/fatture/importa-prodotti/draft/1/image').status_code,403)

    def test_automatic_history_processing_and_staff_corrections(self):
        from app import document_jobs
        did=self.doc()
        self.assertEqual(document_jobs.enqueue_history(),1)
        self.assertEqual(document_jobs.enqueue_history(),0)
        response=dict(date=self.dt,supplier='Fornitore AI',amount=12.50,currency='EUR',description='Cola',text='Trascrizione AI',warnings=[],lines=[dict(description='Cola',qty=3,unit='pz',amount=12.50)])
        with patch.dict(os.environ,{'OPENAI_API_KEY':'test-key'}),patch('app.document_jobs.extract_document',return_value=response):
            self.assertTrue(document_jobs.process_one())
        self.assertEqual(self.sql('SELECT state FROM document_ai_jobs')[0]['state'],'done')
        self.assertEqual(self.qty(self.pid),20)
        self.login(2)
        page=self.client.get(f'/documenti/fatture/{did}')
        self.assertIn('Trascrizione AI',page.text)
        self.assertNotIn('Foto / PDF originale',page.text)
        self.assertEqual(self.save(did,revision=1).status_code,303)
        self.assertEqual(len(self.sql('SELECT id FROM digital_document_versions')),1)

    def test_admin_can_create_written_record_without_original(self):
        r=self.client.post('/registrazione-digitale/spese-secondarie',data={'doc_date':self.dt,'supplier':'Supermercato','amount':'25.00','description':'Detersivo'},follow_redirects=False)
        self.assertEqual(r.status_code,303,r.text)
        page=self.client.get(r.headers['location'])
        self.assertIn('senza foto',page.text)
        self.assertNotIn('Foto / PDF originale',page.text)
        row=self.sql('SELECT * FROM secondary_expenses')[0]
        self.assertIsNone(row['data'])
        from app.document_jobs import enqueue_history
        self.assertEqual(enqueue_history(),0)

    def test_csv_admin_import_preview_dedup_and_reconciliation(self):
        from app.pos_reports import parse_csv
        sources={
            'paid':b'"DATO","PAGATO"\n"2026-10-12","100.00"\n',
            'methods':b'"DATO","SERVO","PAGATO"\n"2026-10-12","Contanti","20.00"\n"2026-10-12","Carta","50.00"\n"2026-10-12","Pagamento non riscosso","30.00"\n',
            'vat':b'"DATO","FILIALE","TIPO DI IVA","TURNOVER INCL.","TURNOVER EXCL.","IMPORTO IVA"\n"12-10-26","REBURGER","B","100.00","90.91","9.09"\n',
            'products':b',,,Excl. ,Incl. discount,Incl. discount\nName,ID,Total,Incl. VAT,Excl. VAT,Incl. VAT\nOG SMASH,1,10,100.00,81.82,90.00\n-Cipolla,2,2,0,0,0\nTotal,,12,100.00,81.82,90.00\n'}
        for kind,data in sources.items():
            parsed=parse_csv(data,kind,'2026-10-12','2026-10-12')
            self.assertTrue(parsed['rows'])
            r=self.client.post('/admin/cassa/importa',data={'kind':kind,'store':'spinza','start':'2026-10-12','end':'2026-10-12'},files={'file':('report.csv',data,'text/csv')},follow_redirects=False)
            self.assertEqual(r.status_code,303,r.text)
            url=r.headers['location'];self.assertEqual(self.client.get(url).status_code,200)
            self.assertEqual(self.client.post(url+'/conferma',follow_redirects=False).status_code,303)
            again=self.client.post('/admin/cassa/importa',data={'kind':kind,'store':'spinza','start':'2026-10-12','end':'2026-10-12'},files={'file':('report.csv',data,'text/csv')},follow_redirects=False)
            self.assertEqual(again.headers['location'],url)
        self.assertEqual(len(self.sql("SELECT id FROM pos_import_batches WHERE status='active'")),4)
        page=self.client.get('/admin/cassa/report?start=2026-10-12&end=2026-10-12')
        self.assertEqual(page.status_code,200,page.text[:200])
        self.assertIn('10.00',page.text)
        self.assertIn('Delivery',page.text)
        self.assertIn('Panini',page.text)
        self.assertEqual(self.client.get('/admin/cassa/report?start=2026-10-12&end=2026-10-12&download=csv').status_code,200)
        self.login(2)
        for url in ['/admin/cassa','/admin/cassa/report','/admin/digitalizzazione']:
            self.assertEqual(self.client.get(url).status_code,403)

    def test_product_period_crossing_month_not_split(self):
        raw=b',,,Excl. ,Incl. discount,Incl. discount\nName,ID,Total,Incl. VAT,Excl. VAT,Incl. VAT\nOG SMASH,1,10,100,90,99\nTotal,,10,100,90,99\n'
        r=self.client.post('/admin/cassa/importa',data={'kind':'products','store':'spinza','start':'2026-09-28','end':'2026-10-04'},files={'file':('report.csv',raw,'text/csv')},follow_redirects=False)
        self.client.post(r.headers['location']+'/conferma',follow_redirects=False)
        page=self.client.get('/admin/cassa/report?month=2026-10')
        self.assertEqual(page.status_code,200)
        self.assertIn('attraversa i confini',page.text)
        self.assertIn('Nessun report prodotti interamente compreso',page.text)

    def test_csv_replacement_requires_confirmation_and_partial_overlap_rejected(self):
        def upload(raw):
            return self.client.post('/admin/cassa/importa',data={'kind':'paid','store':'spinza'},files={'file':('paid.csv',raw,'text/csv')},follow_redirects=False).headers['location']
        a=upload(b'DATO,PAGATO\n2026-10-12,100\n2026-10-18,200\n')
        self.assertEqual(self.client.post(a+'/conferma',follow_redirects=False).status_code,303)
        b=upload(b'DATO,PAGATO\n2026-10-12,110\n2026-10-18,210\n')
        self.assertEqual(self.client.post(b+'/conferma',follow_redirects=False).status_code,409)
        self.assertEqual(self.client.post(b+'/conferma',data={'replace':'yes'},follow_redirects=False).status_code,303)
        self.assertEqual(len(self.sql("SELECT * FROM pos_import_batches WHERE status='active'")),1)
        c=upload(b'DATO,PAGATO\n2026-10-16,50\n2026-10-19,100\n')
        self.assertEqual(self.client.post(c+'/conferma',data={'replace':'yes'},follow_redirects=False).status_code,409)

    def test_recipe_consumption_uses_doses_and_does_not_modify_stock(self):
        from app.pos_reports import parse_csv
        raw=b',,,Excl. ,Incl. discount,Incl. discount\nName,ID,Total,Incl. VAT,Excl. VAT,Incl. VAT\nCOCA COLA,39,3,12,10,11\nTotal,,3,12,10,11\n'
        data=parse_csv(raw,'products','2026-10-12','2026-10-18')
        self.sql('INSERT INTO pos_import_batches(store,kind,start_date,end_date,filename,digest,raw_csv,parsed_json,status,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            ('spinza','products','2026-10-12','2026-10-18','p.csv','test',raw,json.dumps(data),'active','testadmin',self.dt))
        r=self.client.post('/admin/cassa/ricetta',data={'store':'spinza','product_code':'39','inventory_product_id':self.pid,'qty_per_sale':'1'},follow_redirects=False)
        self.assertEqual(r.status_code,303,r.text)
        page=self.client.get('/admin/cassa/report?start=2026-10-12&end=2026-10-18')
        self.assertEqual(page.status_code,200,page.text[:200])
        self.assertIn('3.00 pz',page.text)
        self.assertEqual(self.qty(self.pid),20)

    def test_history_ai_preserves_human_verified_values(self):
        from app import document_jobs
        did=self.doc();self.save(did)
        document_jobs.enqueue_history()
        response=dict(date=self.dt,supplier='AI diverso',amount=999,currency='EUR',description='Controllo',text='Lettura archivio',warnings=[],lines=[])
        with patch.dict(os.environ,{'OPENAI_API_KEY':'test-key'}),patch('app.document_jobs.extract_document',return_value=response):
            self.assertTrue(document_jobs.process_one())
        row=self.sql('SELECT * FROM digital_documents')[0]
        self.assertEqual(row['supplier'],'Fornitore')
        self.assertEqual(row['amount'],12.50)
        self.assertIn('Lettura archivio',self.client.get(f'/documenti/fatture/{did}').text)
        self.login(2)
        self.assertNotIn('Lettura archivio',self.client.get(f'/documenti/fatture/{did}').text)

    def test_monthly_sales_counts_own_values_once(self):
        rid=self.sql('INSERT INTO sales_report_periods(store,month_key,label,created_by) VALUES(?,?,?,?)',('spinza',self.dt[:7],'Test','testadmin'))
        parent=self.sql('INSERT INTO sales_report_groups(period_id,name,amount,quantity,created_by) VALUES(?,?,?,?,?)',(rid,'Gruppo',10,1,'testadmin'))
        self.sql('INSERT INTO sales_report_groups(period_id,parent_id,name,amount,quantity,created_by) VALUES(?,?,?,?,?,?)',(rid,parent,'Cola',20,4,'testadmin'))
        page=self.client.get('/admin/controllo').text
        self.assertIn('30,00',page)
        self.assertIn('5.0 unità',page)

if __name__=='__main__':
    unittest.main(verbosity=2)
