"""Regression checks for combined log filters and historical area matching."""
import unittest
import test_control as fixture
from app import main, db

class LogFilterTests(unittest.TestCase):
    setUp = fixture.ControlTests.setUp
    login = fixture.ControlTests.login
    sql = fixture.ControlTests.sql
    def seed(self):
        for store,category,name,area,action,user,day in [
            ('spinza','Bibite','Cola','bibite','ADD','testadmin','2026-10-29'),
            ('spinza','Cucina','Farina','prodotti','ADD','testadmin','2026-10-29'),
            ('spinza','Bibite','Cola | 10 → 20','','CARICO','marco','2026-10-28'),
            ('reburger_maggio','Bibite','Cola','prodotti','ADD','marco','2026-10-29'),
            ('spinza','Bibite','Deleted','bibite','DELETE','testadmin','2026-10-29'),
        ]:
            self.sql('INSERT INTO logs(store,category,name,area,action,username,ts,delta) VALUES(?,?,?,?,?,?,?,1)',(store,category,name,area,action,user,day+' 12:00:00'))

    def test_combined_filters_and_store_scope(self):
        self.seed()
        rows=main._fetch_logs(store='spinza',section='inventory',limit=1,q_area='bibite',q_group='Bibite',q_user='ADMIN',q_day='2026-10-29')
        self.assertEqual([r['name'] for r in rows],['Deleted'])
        rows=main._fetch_logs(store='spinza',section='inventory',limit=100,q_area='prodotti')
        self.assertEqual([r['name'] for r in rows],['Farina'])
        self.assertEqual(len(main._fetch_logs(store='ALL',section='inventory',limit=100)),5)

    def test_historical_and_ambiguous(self):
        self.seed()
        rows=main._fetch_logs(store='spinza',section='inventory',limit=100,q_area='bibite',q_user='marco')
        self.assertEqual(len(rows),1)
        self.sql("INSERT INTO products(store,category,name,area,location,qty,min_qty) VALUES('spinza','Bibite','Cola','prodotti','MAGAZZINO',1,0)")
        self.assertEqual(main._fetch_logs(store='spinza',section='inventory',limit=100,q_area='bibite',q_user='marco'),[])

    def test_pages_and_access(self):
        self.seed()
        for path in ['/logs/inventario','/logs/ordini','/logs/documenti']:
            response=self.client.get(path,params={'q_area':'bibite','q_group':'Bibite','q_user':'admin','q_day':'2026-10-29','limit':100})
            self.assertEqual(response.status_code,200)
            for field in ['q_area','q_group','q_user','q_day']:
                self.assertIn('name="'+field+'"',response.text)
        self.login(2)
        self.assertEqual(self.client.get('/logs/inventario',follow_redirects=False).status_code,303)

    def test_area_survives_delete_and_schema_idempotent(self):
        response=self.client.post(f'/items/{self.pid}/delete',data={'store':'spinza'},follow_redirects=False)
        self.assertEqual(response.status_code,303)
        db.init_db()
        db.init_db()
        rows=main._fetch_logs(store='spinza',section='inventory',limit=100,q_area='bibite')
        self.assertEqual(rows[0]['action'],'DELETE')
        self.assertEqual(rows[0]['area'],'bibite')
