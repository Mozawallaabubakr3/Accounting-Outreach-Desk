"""Desktop Gmail OAuth: loopback callback, state, PKCE, local-only token storage."""
import base64, hashlib, http.server, json, os, secrets, threading, time, urllib.parse
from pathlib import Path
from .net import API

class GmailSetup:
    def __init__(self,root):self.root=root;self.pending=None;self.server=None;self.result='Not connected'
    def start(self,client_file):
        if self.pending and self.pending['expires']>time.time():return self.pending['url']
        if self.server:
            self.server.shutdown();self.server.server_close()
        raw=json.loads(Path(client_file).expanduser().read_text());c=raw.get('installed')
        if not c or not c.get('client_id'):raise RuntimeError('Use a Google Desktop app OAuth client JSON file')
        state=secrets.token_urlsafe(32);verifier=secrets.token_urlsafe(48);challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=');outer=self
        class Callback(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                p=urllib.parse.urlsplit(self.path);qs=urllib.parse.parse_qs(p.query)
                if p.path!='/callback' or qs.get('state',[''])[0]!=state:
                    self.send_error(400,'Invalid OAuth state');return
                if not outer.pending or time.time()>outer.pending['expires']:
                    self.send_error(400,'Connection request expired');return
                try:
                    if 'error' in qs:raise RuntimeError('Google authorization was declined')
                    code=qs.get('code',[''])[0]
                    if not code:raise RuntimeError('Google did not return an authorization code')
                    token=API().request('https://oauth2.googleapis.com/token','POST',form={'client_id':c['client_id'],'client_secret':c.get('client_secret',''),'code':code,'code_verifier':verifier,'redirect_uri':outer.pending['redirect'],'grant_type':'authorization_code'})
                    if not token.get('refresh_token'):raise RuntimeError('No offline refresh token was returned; reconnect with consent')
                    path=outer.root/'data/gmail-token.json';record={k:c.get(k,'') for k in ('client_id','client_secret')};record['refresh_token']=token['refresh_token']
                    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
                    with os.fdopen(fd,'w') as f:json.dump(record,f)
                    os.chmod(path,0o600);outer.result='Gmail connected. Return to the outreach dashboard.';outer.pending=None
                    self.send_response(200);self.send_header('Content-Type','text/plain; charset=utf-8');self.end_headers();self.wfile.write(outer.result.encode())
                except Exception:
                    outer.result='Gmail connection failed; check the OAuth client and try again.';self.send_error(400,outer.result)
                finally:threading.Thread(target=outer.server.shutdown,daemon=True).start()
        self.server=http.server.HTTPServer(('127.0.0.1',0),Callback);redirect=f'http://127.0.0.1:{self.server.server_port}/callback'
        params={'client_id':c['client_id'],'redirect_uri':redirect,'response_type':'code','scope':'https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/gmail.send','access_type':'offline','prompt':'consent','state':state,'code_challenge':challenge,'code_challenge_method':'S256'}
        url='https://accounts.google.com/o/oauth2/v2/auth?'+urllib.parse.urlencode(params);self.pending={'url':url,'expires':time.time()+600,'redirect':redirect};threading.Thread(target=self.server.serve_forever,daemon=True).start();self.result='Waiting for you to authorize Gmail in Google';return url
