import io
import re
import unittest
import test_control as fixture

class ShiftTests(unittest.TestCase):
    setUp=fixture.ControlTests.setUp
    login=fixture.ControlTests.login
    sql=fixture.ControlTests.sql

    def test_two_shifts_daily_total_review_legacy_and_overnight(self):
        self.login(2)
        for start,end,pos,cash in [('10:00','16:00','100','50'),('18:00','02:00','200','70')]:
            page=self.client.get('/report-chiusura')
            csrf=re.search(r'name="csrf" value="([^"]+)"',page.text)[1]
            submission=re.search(r'name="submission" value="([^"]+)"',page.text)[1]
            data={'csrf':csrf,'submission':submission,'store':'spinza','closure_date':self.dt,
                  'shift_required':'1','shift_start':start,'shift_end':end,
                  'channel':['POS','Cash'],'amount':[pos,cash]}
            response=self.client.post('/report-chiusura/salva',data=data,follow_redirects=False)
            self.assertEqual(response.status_code,303,response.text)
            self.assertEqual(self.client.post('/report-chiusura/salva',data=data,follow_redirects=False).status_code,303)
        self.assertEqual(len(self.sql('SELECT * FROM closure_reports')),2)
        self.assertEqual(len(self.sql('SELECT * FROM closure_report_shifts')),2)
        page=self.client.get('/report-chiusura',params={'day':self.dt})
        self.assertIn('Totale incassi 420,00',page.text)
        self.assertIn('Turno 10:00–16:00',page.text)
        self.assertIn('Turno 18:00–02:00 (+1 giorno)',page.text)
        self.assertIn('teststaff',page.text)
        data['submission']='z'*43
        data['shift_end']=data['shift_start']
        self.assertEqual(self.client.post('/report-chiusura/salva',data=data).status_code,422)
        self.login()
        for row in self.sql('SELECT id FROM closure_reports'):
            page=self.client.get('/admin/file-da-verificare',params={'report_id':row['id']})
            self.assertIn('name="shift_start"',page.text)
            token=re.search(r'name="csrf" value="([^"]+)"',page.text)[1]
            response=self.client.post('/admin/file-da-verificare/conferma',data={
                'csrf':token,'report_id':row['id'],'closure_date':self.dt,
                'channel':['POS','Cash'],'amount':['10','20'],'shift_required':'1',
                'shift_start':'10:00' if row['id']==1 else '18:00',
                'shift_end':'16:00' if row['id']==1 else '02:00'},follow_redirects=False)
            self.assertEqual(response.status_code,303,response.text)
        self.assertAlmostEqual(self.sql('SELECT SUM(amount) AS total FROM cash_entries')[0]['total'],60)
        from app.closure_reports import init_reports
        init_reports()
        self.assertEqual(len(self.sql('SELECT * FROM closure_report_shifts')),2)

if __name__=='__main__':
    unittest.main()
