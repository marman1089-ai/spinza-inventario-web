"""Navigation must render on legacy pages that omit an explicit request."""
import unittest
import test_control as fixture
from app import main

class AdminNavigationTests(unittest.TestCase):
    setUp=fixture.ControlTests.setUp
    login=fixture.ControlTests.login
    def test_legacy_pages_receive_request_and_preserve_names(self):
        for url in ('/gestionale','/fatture','/chiusure','/spese-secondarie','/profile','/admin'):
            response=self.client.get(url)
            self.assertEqual(response.status_code,200,url)
            self.assertIn('Dashboard gestionale',response.text)
            self.assertIn('Report chiusura',response.text)
            self.assertIn('Movimenti in contanti',response.text)
            if url!='/admin':
                self.assertIn('aria-current="page"',response.text)
    def test_template_without_request_is_safe_and_staff_menu_is_unchanged(self):
        html=main.templates.get_template('_admin_navigation.html').render(mobile=False)
        self.assertIn('Dashboard gestionale',html)
        self.login(2)
        response=self.client.get('/chiusure')
        self.assertEqual(response.status_code,200)
        self.assertNotIn('admin-nav-summary',response.text)
        self.assertIn('navInventario',response.text)
