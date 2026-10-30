import unittest
from starlette.websockets import WebSocketDisconnect
from test_live_assistance import LiveAssistTests
from app.assistance_socket import ROOMS

class SocketTests(LiveAssistTests):
    def socket(self,rid,origin='http://testserver'):
        return self.client.websocket_connect('/controllo/assistenza/'+rid+'/live',headers={'origin':origin})
    def test_socket_requires_consent_origin_and_participant(self):
        rid=self.request()
        with self.assertRaises(WebSocketDisconnect):
            with self.socket(rid):pass
        self.login(2);self.command(rid,'accept',transport='bridge')
        with self.assertRaises(WebSocketDisconnect):
            with self.socket(rid,'https://untrusted.invalid'):pass
        self.login(3)
        with self.assertRaises(WebSocketDisconnect):
            with self.socket(rid):pass
        self.login(2)
        with self.socket(rid) as ws:
            self.assertEqual(ws.receive_json()['type'],'ready')
            data=ws.receive_json();self.assertEqual((data['type'],data['state']),('consent','none'))
            ws.send_json({'type':'ping'});data=ws.receive_json();self.assertEqual(data['type'],'pong')
        self.assertEqual(ROOMS,{})
    def test_socket_refreshes_revocation_and_closes_on_stop(self):
        rid=self.request();self.login(2);self.command(rid,'accept',transport='bridge')
        self.login();self.command(rid,'request-control');self.login(2);self.command(rid,'allow-control')
        with self.socket(rid) as ws:
            ws.receive_json();self.assertEqual(ws.receive_json()['state'],'accepted')
            self.command(rid,'revoke-control')
            self.assertEqual(ws.receive_json()['state'],'revoked')
            self.command(rid,'stop')
            with self.assertRaises(WebSocketDisconnect):ws.receive_json()
        self.assertEqual(ROOMS,{})

if __name__=='__main__':unittest.main()
