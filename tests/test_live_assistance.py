import unittest,uuid,json
from unittest.mock import patch
import test_control as fixture
from app.inventory_activity import init_activity_db

class LiveAssistTests(unittest.TestCase):
    setUp=fixture.ControlTests.setUp
    login=fixture.ControlTests.login
    sql=fixture.ControlTests.sql
    def request(self):
        r=self.client.post('/controllo/assistenza/richiedi',data={'target_id':2},headers={'X-Control-Screen':'1'})
        self.assertEqual(r.status_code,200,r.text);return r.json()['session']['id']
    def command(self,rid,action,**data):return self.client.post(f'/controllo/assistenza/{rid}/{action}',data=data,headers={'X-Control-Screen':'1'})
    def test_presence_scope_states_and_no_heartbeat_noise(self):
        self.login(2);token=str(uuid.uuid4());body={'token':token,'store':'spinza','area':'bibite','page':'/inventario','mode':'visible','idle_seconds':0}
        self.assertEqual(self.client.post('/controllo/presenza',data=body).status_code,403)
        for age,state in [(0,'active'),(90,'idle')]:
            body['idle_seconds']=age
            self.assertEqual(self.client.post('/controllo/presenza',data=body,headers={'X-Control-Presence':'1'}).status_code,204)
            self.assertEqual(self.sql('SELECT state FROM control_live_presence')[0]['state'],state)
        self.assertEqual(self.sql('SELECT * FROM control_activity_events'),[])
        body['store']='reburger_maggio';self.assertEqual(self.client.post('/controllo/presenza',data=body,headers={'X-Control-Presence':'1'}).status_code,403)
        self.assertEqual(self.client.get('/admin/controllo-live/dati').status_code,403)
        self.login();r=self.client.get('/admin/controllo-live/dati?initial=1');self.assertEqual(r.status_code,200);self.assertEqual(next(p['state'] for p in r.json()['people'] if p['user_id']==2),'idle')
    def test_no_capture_or_control_before_two_independent_consents(self):
        rid=self.request()
        self.assertEqual(self.command(rid,'accept',capable=1).status_code,403)
        self.assertEqual(self.command(rid,'request-control').status_code,403)
        self.assertEqual(self.command(rid,'offer',sdp='v=0\r\n').status_code,200)
        self.login(2)
        self.assertEqual(self.command(rid,'answer',sdp='v=0\r\n').status_code,403)
        self.assertEqual(self.command(rid,'allow-control').status_code,403)
        self.assertEqual(self.command(rid,'accept',capable=1).status_code,200)
        self.assertEqual(self.command(rid,'answer',sdp='v=0\r\n').status_code,200)
        self.assertEqual(self.command(rid,'allow-control').status_code,403)
        self.login();self.assertEqual(self.command(rid,'request-control').status_code,200)
        self.assertEqual(self.command(rid,'allow-control').status_code,403)
        self.login(2);self.assertEqual(self.command(rid,'allow-control').status_code,200)
        self.assertEqual(self.command(rid,'revoke-control').json()['session']['control_state'],'revoked')
    def test_only_participants_and_one_session_per_employee(self):
        rid=self.request();self.assertEqual(self.client.post('/controllo/assistenza/richiedi',data={'target_id':2},headers={'X-Control-Screen':'1'}).status_code,409)
        self.login(3)
        self.assertEqual(self.client.get('/controllo/assistenza/'+rid).status_code,403)
        self.assertEqual(self.command(rid,'accept',capable=1).status_code,403)
        self.assertEqual(self.command(rid,'stop').status_code,403)
        self.login(2);self.assertEqual(len(self.client.get('/controllo/assistenza/inviti').json()['requests']),1)
        self.assertEqual(self.command(rid,'decline').json()['session']['state'],'declined')
        self.login();self.assertEqual(self.command(rid,'offer',sdp='v=0').status_code,409)
    def test_entire_screen_is_view_only_and_stop_clears_signaling(self):
        rid=self.request();self.command(rid,'offer',sdp='v=0\r\n');self.command(rid,'candidate',candidate=json.dumps({'candidate':'candidate:test','sdpMid':'0','sdpMLineIndex':0}))
        self.login(2);self.command(rid,'accept',capable=0);self.command(rid,'answer',sdp='v=0\r\n')
        self.login();self.assertEqual(self.command(rid,'request-control').status_code,403)
        self.login(2);self.assertEqual(self.command(rid,'stop').status_code,200)
        row=self.sql('SELECT * FROM control_assist_sessions')[0]
        self.assertEqual((row['state'],row['offer'],row['answer']),('stopped','',''))
        self.assertEqual(self.sql('SELECT * FROM control_assist_signals'),[])
        actions=self.sql("SELECT description FROM control_activity_events WHERE kind='assist'")
        self.assertEqual(len(actions),3)
        self.assertNotIn('candidate:test',str(actions))
    def test_expiration_and_live_cursors(self):
        rid=self.request();self.sql('UPDATE control_assist_sessions SET created=created-130 WHERE id=?',(rid,))
        self.login(2);self.assertEqual(self.client.get('/controllo/assistenza/inviti').json()['requests'],[])
        self.assertEqual(self.command(rid,'accept').status_code,409)
        self.login();first=self.client.get('/admin/controllo-live/dati?initial=1').json();cur=first['cursor']
        self.client.get('/inventario')
        next_data=self.client.get('/admin/controllo-live/dati',params={'request_cursor':cur['request'],'log_cursor':cur['log']}).json()
        self.assertTrue(any(e['source']=='request' and e['source_id']>cur['request'] for e in next_data['events']))
        self.assertEqual(self.client.get('/admin/controllo-live/dati?request_cursor=-1').status_code,400)
    def test_pending_operation_is_present_before_completion(self):
        from app import main
        init_activity_db();seen=[];original=main._log
        def check(*args,**kwargs):
            seen.extend(self.sql('SELECT * FROM control_live_operations'))
            return original(*args,**kwargs)
        # Reading through another connection inside a SQLite write transaction is safe.
        with patch.object(main,'_log',side_effect=check):
            self.assertEqual(self.client.post(f'/items/{self.pid}/delta',data={'delta':1},follow_redirects=False).status_code,303)
        self.assertEqual(len(seen),1);self.assertIn('Cola',seen[0]['description'])
        self.assertEqual(self.sql('SELECT * FROM control_live_operations'),[])
