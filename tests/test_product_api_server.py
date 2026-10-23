import json, threading, unittest
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from app.product_api_server import APIHandler, AuthStore
from app.product_service import ProductService

class ApiTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.service=ProductService(); cls.auth=AuthStore(); APIHandler.service=cls.service; APIHandler.auth=cls.auth
  cls.server=ThreadingHTTPServer(('127.0.0.1',0),APIHandler); cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True); cls.thread.start(); cls.base=f'http://127.0.0.1:{cls.server.server_port}'
 @classmethod
 def tearDownClass(cls): cls.server.shutdown(); cls.server.server_close(); cls.service.close()
 def call(self,path,method='GET',body=None,token=None,headers=None):
  h=dict(headers or {}); data=None
  if body is not None: data=json.dumps(body).encode(); h['Content-Type']='application/json'
  if token: h['Authorization']='Bearer '+token
  req=Request(self.base+path,data=data,headers=h,method=method)
  try:
   with urlopen(req) as r: return r.status,json.loads(r.read())
  except HTTPError as e: return e.code,json.loads(e.read())
 def test_health_and_auth_required(self):
  self.assertEqual(self.call('/health')[0],200); self.assertEqual(self.call('/wallet/balances')[0],401)
 def test_register_login_wallet_flow(self):
  username='user'+str(id(self)); self.assertEqual(self.call('/auth/register','POST',{'username':username,'password':'a-very-long-test-password'})[0],201)
  status,login=self.call('/auth/login','POST',{'username':username,'password':'a-very-long-test-password'}); self.assertEqual(status,200)
  token=login['access_token']; status,_=self.call('/wallet/deposits/simulated','POST',{'currency':'NGN','amount_minor':5000},token,{'Idempotency-Key':'deposit-'+username}); self.assertEqual(status,201)
  self.assertEqual(self.call('/wallet/balances',token=token)[1]['balances_minor']['NGN'],5000)
 def test_bad_password_and_missing_idempotency(self):
  username='bad'+str(id(self)); self.call('/auth/register','POST',{'username':username,'password':'a-very-long-test-password'})
  self.assertEqual(self.call('/auth/login','POST',{'username':username,'password':'wrong-password'})[0],401)
  token=self.call('/auth/login','POST',{'username':username,'password':'a-very-long-test-password'})[1]['access_token']
  self.assertEqual(self.call('/wallet/deposits/simulated','POST',{'currency':'NGN','amount_minor':1},token)[0],400)
if __name__=='__main__': unittest.main()
