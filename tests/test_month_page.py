import unittest
from datetime import date
from unittest.mock import patch
import test_control as fixture
from app import main

class MonthPageTests(unittest.TestCase):
    setUp=fixture.ControlTests.setUp
    login=fixture.ControlTests.login
    sql=fixture.ControlTests.sql
    def test_current_month_first_and_store_switch_retains_range(self):
        self.sql("INSERT INTO cash_entries(store,flow_date,amount,created_by) VALUES('spinza','2026-09-01',15,'testadmin')")
        with patch.object(main,'_rome_today',return_value=date(2026,11,4)):
            response=self.client.get('/gestionale/storico-mensile')
            self.assertEqual(response.status_code,200)
            self.assertLess(response.text.index('data-month="2026-11"'),response.text.index('data-month="2026-09"'))
            self.assertIn('Mese corrente',response.text)
            switched=self.client.post('/set-active-store',data={'store':'reburger_maggio','next_url':'/gestionale/storico-mensile?from_month=2026-09&to_month=2026-11'},follow_redirects=False)
            self.assertEqual(switched.status_code,303)
            self.assertEqual(switched.headers['location'],'/gestionale/storico-mensile?from_month=2026-09&to_month=2026-11')
            page=self.client.get(switched.headers['location'])
            self.assertEqual(page.status_code,200)
            self.assertIn('Reburger San Felice',page.text)
            self.assertNotIn('15,00',page.text)

if __name__=='__main__': unittest.main()
