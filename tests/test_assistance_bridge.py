import json
import unittest
from test_live_assistance import LiveAssistTests

class BridgeTests(LiveAssistTests):
    def connect_bridge(self):
        rid=self.request();self.login(2)
        self.assertEqual(self.command(rid,'accept',transport='bridge').json()['session']['state'],'active')
        return rid
    def frame(self):
        return json.dumps(dict(id='frame1',width=390,height=800,nodes=[]))
    def test_bridge_consent_roles_delivery_and_cleanup(self):
        rid=self.connect_bridge()
        self.assertEqual(self.command(rid,'bridge-frame',frame=self.frame()).status_code,200)
        self.login(3);self.assertEqual(self.client.get('/controllo/assistenza/'+rid).status_code,403)
        self.login()
        self.assertEqual(self.command(rid,'bridge-frame',frame=self.frame()).status_code,403)
        self.assertEqual(self.client.get('/controllo/assistenza/'+rid).json()['bridge']['frame']['id'],'frame1')
        self.assertEqual(self.command(rid,'bridge-command',command='{}').status_code,403)
        self.assertEqual(self.command(rid,'request-control').status_code,200)
        self.login(2);self.assertEqual(self.command(rid,'allow-control').status_code,200)
        grant=self.client.get('/controllo/assistenza/'+rid).json()['bridge']['grant'];self.assertTrue(grant)
        self.login();cmd=json.dumps(dict(type='click',frame='frame1',target='1',grant=grant))
        self.assertEqual(self.command(rid,'bridge-command',command=cmd).status_code,200)
        self.login(2);data=self.client.get('/controllo/assistenza/'+rid).json()['bridge']
        self.assertIsNone(data['frame']);self.assertEqual(len(data['commands']),1)
        seq=data['commands'][0]['seq']
        self.assertEqual(self.command(rid,'bridge-frame',frame=self.frame(),ack=seq).status_code,200)
        self.assertEqual(self.client.get('/controllo/assistenza/'+rid).json()['bridge']['commands'],[])
        self.command(rid,'revoke-control');self.login()
        self.assertEqual(self.command(rid,'bridge-command',command=cmd).status_code,403)
        self.command(rid,'stop');self.assertEqual(self.sql('SELECT * FROM control_assist_bridge'),[])
    def test_bridge_limits_and_expiry(self):
        rid=self.connect_bridge()
        self.assertEqual(self.command(rid,'bridge-frame',frame='{}').status_code,400)
        self.assertEqual(self.command(rid,'bridge-frame',frame='x'*450001).status_code,400)
        self.command(rid,'bridge-frame',frame=self.frame())
        self.sql('UPDATE control_assist_sessions SET target_ping=target_ping-60 WHERE id=?',(rid,))
        self.assertEqual(self.command(rid,'bridge-frame',frame=self.frame()).status_code,409)
        self.assertEqual(self.sql('SELECT * FROM control_assist_bridge'),[])
    def test_bridge_stale_grant_after_reapproval(self):
        rid=self.connect_bridge();self.login();self.command(rid,'request-control');self.login(2);self.command(rid,'allow-control')
        old=self.client.get('/controllo/assistenza/'+rid).json()['bridge']['grant']
        self.command(rid,'revoke-control');self.login();self.command(rid,'request-control');self.login(2);self.command(rid,'allow-control')
        new=self.client.get('/controllo/assistenza/'+rid).json()['bridge']['grant'];self.assertNotEqual(old,new)
        self.login();self.assertEqual(self.command(rid,'bridge-command',command=json.dumps(dict(type='click',grant=old))).status_code,403)

if __name__=='__main__':unittest.main()
