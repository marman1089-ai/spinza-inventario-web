import io
import re
import unittest
from PIL import Image
import test_control as fixture

class MultiStoreTests(unittest.TestCase):
    setUp=fixture.ControlTests.setUp
    login=fixture.ControlTests.login
    sql=fixture.ControlTests.sql

    def assign(self, stores):
        page=self.client.get('/admin')
        self.assertEqual(page.status_code,200,page.text)
        token=re.search(r'action="/admin/users/2/stores">\s*<input type="hidden" name="csrf" value="([^"]+)"',page.text)[1]
        return self.client.post('/admin/users/2/stores',data={'csrf':token,'stores':stores},follow_redirects=False)

    def test_staff_switch_upload_inventory_reports_and_revocation(self):
        self.assertEqual(self.assign(['spinza','reburger_maggio']).status_code,303)
        self.login(2)
        page=self.client.get('/inventario')
        self.assertIn('aria-label="Cambia negozio"',page.text)
        self.assertNotIn('value="ALL"',re.search(r'<select id="header-store".*?</select>',page.text,re.S)[0])
        self.assertEqual(self.client.post('/set-active-store',data={'store':'reburger_camaldoli'},follow_redirects=False).status_code,403)
        self.assertEqual(self.client.post('/set-active-store',data={'store':'ALL'},follow_redirects=False).status_code,403)
        response=self.client.post('/set-active-store',data={'store':'reburger_maggio','next_url':'/report-chiusura'},follow_redirects=False)
        self.assertEqual(response.status_code,303)
        page=self.client.get(response.headers['location'])
        self.assertIn('name="store" value="reburger_maggio"',page.text)
        self.assertEqual(self.client.get('/report-chiusura?store=spinza').status_code,403)
        self.assertNotIn('Farina',self.client.get('/inventario?area=prodotti').text)
        page=self.client.get('/report-chiusura')
        csrf=re.search(r'name="csrf" value="([^"]+)"',page.text)[1]
        submission=re.search(r'name="submission" value="([^"]+)"',page.text)[1]
        response=self.client.post('/report-chiusura/salva',data={
            'csrf':csrf,'submission':submission,'store':'reburger_maggio','closure_date':self.dt,
            'shift_required':'1','shift_start':'11:00','shift_end':'15:00','cumulative_mode':'1',
            'cash_fund_mode':'excluded','cash_fund_initial':'106',
            'channel':['POS','Cash'],'amount':['100','50']},follow_redirects=False)
        self.assertEqual(response.status_code,303,response.text)
        self.assertEqual(self.sql('SELECT store FROM closure_reports')[0]['store'],'reburger_maggio')
        self.assertEqual(self.sql('SELECT COUNT(*) AS n FROM closure_report_reviews WHERE reviewed_at IS NULL')[0]['n'],1)

        data=io.BytesIO()
        Image.new('RGB',(20,20),'white').save(data,format='JPEG')
        for kind,table in [('chiusure','closures'),('fatture','invoices_docs')]:
            r=self.client.post('/'+kind+'/upload',files={'files':('photo.jpg',data.getvalue(),'image/jpeg')},data={'supplier':'Test'},follow_redirects=False)
            self.assertEqual(r.status_code,303,r.text)
            row=self.sql(f'SELECT id,store,uploaded_by FROM {table}')[0]
            self.assertEqual(row['store'],'reburger_maggio')
            self.assertEqual(row['uploaded_by'],'teststaff')
            self.assertEqual(self.client.get(f'/{kind}/{row["id"]}').status_code,200)
        for path in ['/admin','/contanti-nei-negozi','/admin/file-da-verificare']:
            r=self.client.get(path,follow_redirects=False)
            self.assertIn(r.status_code,[303,403],path)
        self.assertEqual(self.client.post('/admin/users/2/stores',data={'stores':['reburger_camaldoli']},follow_redirects=False).status_code,403)
        cookie=list(self.client.cookies.jar)[-1].value
        self.login()
        self.assertEqual(self.assign(['spinza']).status_code,303)
        self.client.cookies.clear()
        self.client.cookies.set('session',cookie)
        self.assertEqual(self.client.get('/chiusure/1').status_code,404)
        self.assertEqual(self.client.post('/set-active-store',data={'store':'reburger_maggio'},follow_redirects=False).status_code,403)
        page=self.client.get('/report-chiusura')
        self.assertIn('name="store" value="spinza"',page.text)

    def test_login_from_assigned_store_and_profile_identity(self):
        from app.security import make_password
        salt,hashed=make_password('staff-password')
        self.sql('UPDATE users SET pw_salt=?,pw_hash=? WHERE id=2',(salt,hashed))
        self.assertEqual(self.assign(['spinza','reburger_maggio']).status_code,303)
        self.client.cookies.clear()
        self.client.get('/select-store?store=reburger_maggio')
        response=self.client.post('/login',data={'username':'teststaff','password':'staff-password'},follow_redirects=False)
        self.assertEqual(response.status_code,303,response.text)
        self.assertIn('name="store" value="reburger_maggio"',self.client.get('/report-chiusura').text)
        response=self.client.post('/profile/password',data={'current_password':'staff-password','new_password':'new-password','confirm_password':'new-password'})
        self.assertEqual(response.status_code,200)
        self.assertIn('Password aggiornata',response.text)
        self.assertEqual(self.sql('SELECT store FROM users WHERE id=2')[0]['store'],'spinza')

    def test_default_single_store_and_three_store_assignment(self):
        self.login(2)
        self.assertNotIn('aria-label="Cambia negozio"',self.client.get('/inventario').text)
        self.login()
        self.assertEqual(self.assign([]).status_code,422)
        self.assertEqual(self.assign(['ALL']).status_code,422)
        self.assertEqual(self.assign(list(fixture.main.STORES)).status_code,303)
        self.login(2)
        for store in fixture.main.STORES:
            self.assertEqual(self.client.post('/set-active-store',data={'store':store},follow_redirects=False).status_code,303)
            self.assertEqual(self.client.get('/report-chiusura?store='+store).status_code,200)
        self.assertEqual(self.sql('SELECT role FROM users WHERE id=2')[0]['role'],'staff')

if __name__=='__main__':
    unittest.main()
