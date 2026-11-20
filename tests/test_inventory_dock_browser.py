import unittest,sys,threading,socket
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
import test_control as fixture
try:
    from playwright.sync_api import sync_playwright,expect
except ImportError:
    sync_playwright=None
@unittest.skipUnless(sync_playwright and Path('C:/Program Files/Google/Chrome/Application/chrome.exe').exists(),'Richiede Playwright e Chrome locale.')
class InventoryDockTests(unittest.TestCase):
    login=fixture.ControlTests.login
    sql=fixture.ControlTests.sql
    def setUp(self):
        fixture.ControlTests.setUp(self)
        with fixture.db.connect() as conn:
            conn.cursor().executemany("INSERT INTO products(store,category,name,area,location,unit,qty,min_qty,supplier) VALUES('spinza','Test',?,'bibite','MAGAZZINO','pz',10,0,'Fornitore')",[(f'Prodotto {n:03}',) for n in range(60)])
    def check_dock(self,width,height,uid):
        import uvicorn
        self.login(uid=uid)
        listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(128)
        base='http://127.0.0.1:'+str(listener.getsockname()[1])
        server=uvicorn.Server(uvicorn.Config(fixture.main.app,log_level='error',lifespan='off'))
        thread=threading.Thread(target=lambda:server.run(sockets=[listener]),daemon=True);thread.start()
        def stop():server.should_exit=True;thread.join(timeout=5);listener.close()
        self.addCleanup(stop)
        with sync_playwright() as pw:
            browser=pw.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True)
            context=browser.new_context(viewport=dict(width=width,height=height))
            context.add_cookies([dict(name='session',value=self.client.cookies.get('session'),url=base)])
            page=context.new_page();page.goto(base+'/inventario');page.wait_for_load_state('networkidle')
            button=page.locator('#inventory-save-all');dock=page.locator('#inventory-save-dock')
            def rect():return dock.evaluate('(e)=>{const r=e.getBoundingClientRect();return {top:r.top,bottom:r.bottom,left:r.left,right:r.right}}')
            self.assertEqual(page.locator('#inventory-defer').count(),0)
            self.assertEqual(page.locator('.inv-set-form button[type=submit]').count(),0)
            first=rect()
            self.assertGreaterEqual(first['top'],0)
            self.assertLessEqual(first['bottom'],height)
            page.evaluate('window.scrollTo(0,1200)');page.wait_for_timeout(100)
            second=rect();self.assertAlmostEqual(first['top'],second['top'],delta=2)
            self.assertGreater(page.evaluate('window.scrollY'),500)
            field=page.locator('.inv-set-form [name=qty]').first
            field.fill('11')
            field.press('Enter')
            self.assertEqual(len(self.sql("SELECT id FROM products WHERE name LIKE 'Prodotto %' AND qty=11")),0)
            self.assertTrue(button.is_enabled())
            page.evaluate('window.scrollTo(0,document.documentElement.scrollHeight)');page.wait_for_timeout(100)
            last=rect();self.assertAlmostEqual(first['top'],last['top'],delta=2)
            button.click(trial=True)
            self.assertLessEqual(last['right'],width)
            self.assertGreaterEqual(last['left'],0)
            context.on('dialog',lambda d:d.accept())
            button.click();page.wait_for_load_state('networkidle')
            expect(page.locator('#inventory-save-status')).to_contain_text('Nessuna modifica')
            self.assertEqual(len(self.sql("SELECT id FROM products WHERE name LIKE 'Prodotto %' AND qty=11")),1)
            browser.close()
    def test_admin_desktop(self):self.check_dock(1440,1000,1)
    def test_staff_phone(self):self.check_dock(390,844,2)
if __name__=='__main__':unittest.main()
