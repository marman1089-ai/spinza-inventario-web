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

    def test_cumulative_shift_difference_and_accounting(self):
        self.login()
        def save(start,end,pos,cash,token):
            page=self.client.get('/report-chiusura')
            csrf=re.search(r'name="csrf" value="([^"]+)"',page.text)[1]
            return self.client.post('/report-chiusura/salva',data={
                'csrf':csrf,'submission':token*43,'store':'spinza','closure_date':self.dt,
                'shift_required':'1','shift_start':start,'shift_end':end,'cumulative_mode':'1','cash_fund_mode':'excluded','cash_fund_initial':'0',
                'channel':['POS','Cash'],'amount':[pos,cash]},follow_redirects=False)
        self.assertEqual(save('11:00','15:00','200','100','a').status_code,303)
        self.assertEqual(save('18:00','02:00','500','250','b').status_code,303)
        self.assertEqual(save('18:00','02:00','500','250','b').status_code,303)
        rows=self.sql('SELECT id,total_cents,lines_json FROM closure_reports ORDER BY id')
        self.assertEqual([r['total_cents'] for r in rows],[30000,45000])
        self.assertAlmostEqual(self.sql('SELECT SUM(amount) AS n FROM cash_entries')[0]['n'],750)
        page=self.client.get('/report-chiusura',params={'day':self.dt})
        self.assertIn('Totale incassi 750,00',page.text)
        self.assertIn('450,00',page.text)
        self.assertEqual(save('20:00','03:00','400','250','c').status_code,422)
        self.assertEqual(len(self.sql('SELECT * FROM closure_reports')),2)
        # Backdated earlier report redistributes the later turn and its exports.
        self.assertEqual(save('09:00','10:00','50','25','d').status_code,303)
        self.assertAlmostEqual(self.sql('SELECT SUM(amount) AS n FROM cash_entries')[0]['n'],750)
        rows=self.sql('SELECT total_cents FROM closure_reports ORDER BY id')
        self.assertEqual([r['total_cents'] for r in rows],[22500,45000,7500])
        page=self.client.get('/report-chiusura')
        csrf=re.search(r'name="csrf" value="([^"]+)"',page.text)[1]
        self.assertEqual(self.client.post('/report-chiusura/elimina',data={'csrf':csrf,'store':'spinza','report_id':self.sql('SELECT MAX(id) AS id FROM closure_reports')[0]['id']},follow_redirects=False).status_code,303)
        self.assertAlmostEqual(self.sql('SELECT SUM(amount) AS n FROM cash_entries')[0]['n'],750)
        self.assertEqual(self.sql('SELECT total_cents FROM closure_reports WHERE id=1')[0]['total_cents'],30000)

    def test_pending_cumulative_approval_in_reverse_order(self):
        self.login(2)
        for start,end,pos,cash,letter in [('11:00','15:00','200','100','m'),('18:00','02:00','500','250','n')]:
            page=self.client.get('/report-chiusura')
            csrf=re.search(r'name="csrf" value="([^"]+)"',page.text)[1]
            response=self.client.post('/report-chiusura/salva',data={
                'csrf':csrf,'submission':letter*43,'store':'spinza','closure_date':self.dt,
                'shift_required':'1','shift_start':start,'shift_end':end,'cumulative_mode':'1','cash_fund_mode':'excluded','cash_fund_initial':'0',
                'channel':['POS','Cash'],'amount':[pos,cash]},follow_redirects=False)
            self.assertEqual(response.status_code,303)
        self.assertEqual(len(self.sql('SELECT * FROM cash_entries')),4)
        self.assertAlmostEqual(self.sql('SELECT SUM(amount) AS n FROM cash_entries')[0]['n'],750)
        self.login()
        for rid,start,end,pos,cash in [(2,'18:00','02:00','500','250'),(1,'11:00','15:00','220','110')]:
            page=self.client.get('/admin/file-da-verificare',params={'report_id':rid})
            self.assertIn('name="cumulative_mode" value="1"',page.text)
            csrf=re.search(r'name="csrf" value="([^"]+)"',page.text)[1]
            response=self.client.post('/admin/file-da-verificare/conferma',data={
                'csrf':csrf,'report_id':rid,'closure_date':self.dt,
                'channel':['POS','Cash'],'amount':[pos,cash],'shift_required':'1',
                'shift_start':start,'shift_end':end,'cumulative_mode':'1','cash_fund_mode':'excluded','cash_fund_initial':'0'},follow_redirects=False)
            self.assertEqual(response.status_code,303,response.text)
        self.assertEqual([r['total_cents'] for r in self.sql('SELECT total_cents FROM closure_reports ORDER BY id')],[33000,42000])
        self.assertAlmostEqual(self.sql('SELECT SUM(amount) AS n FROM cash_entries')[0]['n'],750)

    def test_legacy_pending_backfill_is_once_and_deleted_reports_stay_deleted(self):
        self.login(2)
        page=self.client.get('/report-chiusura')
        csrf=re.search(r'name="csrf" value="([^"]+)"',page.text)[1]
        payload=dict(csrf=csrf,submission='v'*43,store='spinza',closure_date=self.dt,
                     channel=['POS','Cash'],amount=['100','50'])
        self.assertEqual(self.client.post('/report-chiusura/salva',data=payload,follow_redirects=False).status_code,303)
        self.assertEqual(len(self.sql('SELECT * FROM cash_entries')),2)
        self.assertIsNone(self.sql('SELECT reviewed_at FROM closure_report_reviews')[0]['reviewed_at'])
        # Recreate the old state: saved staff report, not exported due to the old review gate.
        self.sql('DELETE FROM cash_entries')
        self.sql('DELETE FROM closure_report_exports')
        from app import main
        startup=next(fn for fn in main.app.router.on_startup if fn.__name__=='startup_reports')
        startup();startup()
        self.assertEqual(len(self.sql('SELECT * FROM cash_entries')),2)
        self.assertEqual(self.sql('SELECT SUM(amount) AS n FROM cash_entries')[0]['n'],150)
        self.login()
        page=self.client.get('/report-chiusura')
        csrf=re.search(r'name="csrf" value="([^"]+)"',page.text)[1]
        self.assertEqual(self.client.post('/report-chiusura/elimina',data=dict(csrf=csrf,store='spinza',report_id=1),follow_redirects=False).status_code,303)
        startup();startup()
        self.assertEqual(self.sql('SELECT * FROM cash_entries'),[])
        self.assertEqual(self.sql('SELECT * FROM closure_reports'),[])
        self.assertEqual(self.client.post('/report-chiusura/salva',data=dict(payload,csrf=csrf),follow_redirects=False).status_code,409)

if __name__=='__main__':
    unittest.main()
