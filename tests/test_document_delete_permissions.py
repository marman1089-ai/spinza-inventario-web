"""Document ownership and Rome calendar-day deletion, verified through HTTP."""
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import patch
import test_control as fixture
from app import main

class DocumentDeleteTests(unittest.TestCase):
    setUp=fixture.ControlTests.setUp
    login=fixture.ControlTests.login
    sql=fixture.ControlTests.sql
    docs=[('invoices_docs','fatture','doc_date'),('closures','chiusure','closure_date'),('secondary_expenses','spese-secondarie','expense_date')]

    def add(self,table,date_column,owner='teststaff',ts='2026-10-29T08:00:00+00:00',store='spinza'):
        columns=f'store,{date_column},uploaded_by,ts,filename,content_type,data'
        values=[store,'2000-01-01',owner,ts,'original.pdf','application/pdf',b'original']
        if table=='invoices_docs': columns+=',supplier'; values+=['Fornitore']
        return self.sql(f'INSERT INTO {table}({columns}) VALUES({",".join("?" for _ in values)})',values)

    def test_employee_own_same_day_only_and_visibility(self):
        self.login(2)
        with patch.object(main,'_document_delete_now',return_value=datetime(2026,10,29,12,tzinfo=ZoneInfo('Europe/Rome'))):
            for table,path,date_column in self.docs:
                with self.subTest(path=path):
                    own=self.add(table,date_column)
                    other=self.add(table,date_column,owner='testadmin')
                    yesterday=self.add(table,date_column,ts='2026-10-28T23:59:00+01:00')
                    foreign=self.add(table,date_column,store='reburger_maggio')
                    page=self.client.get('/'+path)
                    self.assertEqual(page.status_code,200)
                    self.assertIn(f'action="/{path}/{own}/delete"',page.text)
                    for doc in [other,yesterday,foreign]:
                        self.assertNotIn(f'action="/{path}/{doc}/delete"',page.text)
                        self.assertEqual(self.client.post(f'/{path}/{doc}/delete',follow_redirects=False).status_code,403)
                        self.assertEqual(len(self.sql(f'SELECT id FROM {table} WHERE id=?',(doc,))),1)
                    self.assertEqual(self.client.post(f'/{path}/{own}/delete',follow_redirects=False).status_code,303)
                    self.assertEqual(self.sql(f'SELECT id FROM {table} WHERE id=?',(own,)),[])
                    self.assertTrue(self.sql("SELECT id FROM logs WHERE username='teststaff' AND action='DELETE'"))

    def test_admin_any_owner_any_day_any_store(self):
        self.login()
        for table,path,date_column in self.docs:
            with self.subTest(path=path):
                doc=self.add(table,date_column,owner='someone_else',ts='2000-01-01',store='reburger_maggio')
                self.assertEqual(self.client.post(f'/{path}/{doc}/delete',follow_redirects=False).status_code,303)
                self.assertEqual(self.sql(f'SELECT id FROM {table} WHERE id=?',(doc,)),[])
                self.assertEqual(self.sql("SELECT store FROM logs WHERE action='DELETE' ORDER BY id DESC LIMIT 1")[0]['store'],'reburger_maggio')

    def test_midnight_utc_and_invalid_timestamps(self):
        user={'role':'staff','store':'spinza','username':'teststaff'}
        document={'store':'spinza','uploaded_by':'teststaff'}
        with patch.object(main,'_document_delete_now',return_value=datetime(2026,10,30,0,1,tzinfo=ZoneInfo('Europe/Rome'))):
            for timestamp,allowed in [('2026-10-29 23:00:00',True),('2026-10-29 22:59:59',False),('invalid',False),(None,False),('2026-10-31 00:00:00',False)]:
                self.assertEqual(main._can_delete_uploaded_document(user,{**document,'ts':timestamp}),allowed,timestamp)
        with patch.object(main,'_document_delete_now',return_value=datetime(2026,10,30,0,1,tzinfo=ZoneInfo('Europe/Rome'))):
            for table,path,date_column in self.docs:
                self.login(2)
                doc=self.add(table,date_column,ts='2026-10-29T23:59:00+01:00')
                self.assertEqual(self.client.post(f'/{path}/{doc}/delete',follow_redirects=False).status_code,403)
