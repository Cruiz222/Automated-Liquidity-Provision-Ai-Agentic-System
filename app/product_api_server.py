"""Small local development API for simulated wallets and DEX LP positions.
Not production-ready: sessions are in-memory and there is no TLS/rate limiting.
"""
from __future__ import annotations
import hashlib, hmac, json, os, secrets, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from .product_service import ProductService, ProductServiceError, NotFound
from .wallet_ledger import LedgerError, InvalidOperation, InsufficientFunds, IdempotencyConflict

class AuthStore:
    def __init__(self, ttl_seconds=3600): self.users={}; self.sessions={}; self.ttl=ttl_seconds; self.lock=threading.RLock()
    @staticmethod
    def _hash(password,salt): return hashlib.pbkdf2_hmac('sha256',password.encode(),salt,200_000).hex()
    def register(self,username,password):
        if not isinstance(username,str) or not 3<=len(username)<=80 or any(c.isspace() for c in username): raise ValueError('username must be 3-80 characters without spaces')
        if not isinstance(password,str) or len(password)<12: raise ValueError('password must contain at least 12 characters')
        key=username.casefold()
        with self.lock:
            if key in self.users: raise FileExistsError('username already exists')
            salt=secrets.token_bytes(16); self.users[key]={'user_id':key,'salt':salt.hex(),'password_hash':self._hash(password,salt)}
        return {'user_id':key,'username':username}
    def login(self,username,password):
        if not isinstance(username,str) or not isinstance(password,str): raise ValueError('username and password required')
        with self.lock:
            user=self.users.get(username.casefold())
            if not user or not hmac.compare_digest(user['password_hash'],self._hash(password,bytes.fromhex(user['salt']))): raise PermissionError('invalid credentials')
            token=secrets.token_urlsafe(32); self.sessions[token]=(user['user_id'],time.time()+self.ttl)
            return {'access_token':token,'token_type':'Bearer','expires_in':self.ttl}
    def resolve(self,token):
        with self.lock:
            item=self.sessions.get(token)
            if not item: return None
            if item[1]<=time.time(): self.sessions.pop(token,None); return None
            return item[0]
    def logout(self,token):
        with self.lock: self.sessions.pop(token,None)

class APIHandler(BaseHTTPRequestHandler):
    service: ProductService|None=None
    auth: AuthStore|None=None
    server_version='DexLiquidityDev/0.1'
    def log_message(self,fmt,*args): pass
    def _json(self,status,payload):
        body=json.dumps(payload,default=str).encode(); self.send_response(status); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Content-Length',str(len(body))); self.send_header('Cache-Control','no-store'); self.end_headers(); self.wfile.write(body)
    def _body(self):
        try: length=int(self.headers.get('Content-Length','0'))
        except ValueError: raise ValueError('invalid Content-Length')
        if length<0 or length>65536: raise ValueError('request body too large')
        try: data=json.loads(self.rfile.read(length) or b'{}')
        except json.JSONDecodeError: raise ValueError('body must be valid JSON') from None
        if not isinstance(data,dict): raise ValueError('JSON body must be an object')
        return data
    def _user(self):
        value=self.headers.get('Authorization','')
        if not value.startswith('Bearer '): return None,None
        token=value[7:].strip(); return self.auth.resolve(token),token
    def do_GET(self):
        path=urlparse(self.path).path
        if path=='/health': return self._json(200,{'status':'ok','mode':'simulation_only'})
        user,_=self._user()
        if not user: return self._json(401,{'error':'bearer authentication required'})
        try:
            if path=='/wallet/balances': return self._json(200,{'balances_minor':self.service.balances(user),'units':{'NGN':'kobo','USDT':'micro-USDT'}})
            if path=='/wallet/transactions':
                limit=int(parse_qs(urlparse(self.path).query).get('limit',['50'])[0]); return self._json(200,{'transactions':self.service.transactions(user,limit)})
            if path=='/lp/positions': return self._json(200,{'positions':self.service.list_positions(user,parse_qs(urlparse(self.path).query).get('status',[None])[0])})
            if path.startswith('/lp/positions/'): return self._json(200,self.service.get_position(user,path.rsplit('/',1)[1]))
            return self._json(404,{'error':'not found'})
        except (ValueError,LedgerError) as e: return self._json(400,{'error':str(e)})
        except NotFound as e: return self._json(404,{'error':str(e)})
    def do_POST(self):
        path=urlparse(self.path).path
        try: data=self._body()
        except ValueError as e: return self._json(400,{'error':str(e)})
        try:
            if path=='/auth/register':
                try: return self._json(201,self.auth.register(data.get('username'),data.get('password')))
                except FileExistsError as e: return self._json(409,{'error':str(e)})
                except ValueError as e: return self._json(400,{'error':str(e)})
            if path=='/auth/login':
                try: return self._json(200,self.auth.login(data.get('username'),data.get('password')))
                except PermissionError: return self._json(401,{'error':'invalid credentials'})
                except ValueError as e: return self._json(400,{'error':str(e)})
            user,token=self._user()
            if not user: return self._json(401,{'error':'bearer authentication required'})
            if path=='/auth/logout': self.auth.logout(token); return self._json(200,{'status':'logged_out'})
            if path in ('/wallet/deposits/simulated','/wallet/withdrawals/simulated'):
                idem=self.headers.get('Idempotency-Key','').strip()
                if not idem: return self._json(400,{'error':'Idempotency-Key header required'})
                fn=self.service.deposit_simulated if path.endswith('deposits/simulated') else self.service.withdraw_simulated
                result=fn(user,data.get('currency'),data.get('amount_minor'),idem); return self._json(201,result)
            if path=='/lp/positions': return self._json(201,self.service.create_position(user,data))
            return self._json(404,{'error':'not found'})
        except IdempotencyConflict as e: return self._json(409,{'error':str(e)})
        except InsufficientFunds as e: return self._json(422,{'error':str(e)})
        except (ValueError,LedgerError) as e: return self._json(400,{'error':str(e)})
        except NotFound as e: return self._json(404,{'error':str(e)})
    def do_DELETE(self):
        path=urlparse(self.path).path; user,_=self._user()
        if not user: return self._json(401,{'error':'bearer authentication required'})
        if not path.startswith('/lp/positions/'): return self._json(404,{'error':'not found'})
        try: return self._json(200,self.service.close_position(user,path.rsplit('/',1)[1]))
        except NotFound as e: return self._json(404,{'error':str(e)})
        except LedgerError as e: return self._json(400,{'error':str(e)})

def main():
    host=os.environ.get('DEX_API_HOST','127.0.0.1')
    if host not in ('127.0.0.1','::1','localhost'): raise SystemExit('Development API must bind to loopback only')
    port=int(os.environ.get('DEX_API_PORT','8010')); db=os.environ.get('DEX_API_DB','data/product_api.sqlite3')
    from pathlib import Path
    Path(db).parent.mkdir(parents=True,exist_ok=True)
    APIHandler.service=ProductService(db); APIHandler.auth=AuthStore()
    server=ThreadingHTTPServer((host,port),APIHandler)
    print(f'DEX simulation API listening on http://{host}:{port} (simulation only)')
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close(); APIHandler.service.close()
if __name__=='__main__': main()
