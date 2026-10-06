import http.server,json,os,secrets,threading,urllib.parse
from pathlib import Path
from .agent import Agent,GateError,norm_email
from .auth import GmailSetup

PUBLIC_SETTINGS={'sender_name':str,'sender_email':str,'postal_address':str,'postal_address_confirmed':bool,'portfolio_public_confirmed':bool,'send_enabled':bool,'daily_send_limit':int}

class Service:
    def __init__(self,root):self.root=root;self.token=secrets.token_urlsafe(32);self.jobs={};self.lock=threading.Lock();self.auth=GmailSetup(root)
    def task(self,name,fn):
        with self.lock:
            if any(x['status']=='running' for x in self.jobs.values()):raise GateError('A workflow is already running; wait for its result')
            key=secrets.token_hex(8);self.jobs[key]={'name':name,'status':'running','result':None}
        def work():
            try:result=fn();self.jobs[key].update(status='complete',result=result)
            except Exception as e:self.jobs[key].update(status='failed',result=str(e) if isinstance(e,(RuntimeError,ValueError)) else 'Task failed; inspect local logs')
        threading.Thread(target=work,daemon=True).start();return {'job':key}
    def state(self):
        a=Agent(self.root)
        return {'status':a.status(),'firms':a.store.rows('SELECT * FROM firms ORDER BY id DESC'),'people':a.store.rows('SELECT p.*,f.name firm_name FROM people p JOIN firms f ON f.id=p.firm_id ORDER BY p.id DESC'),'contacts':a.store.rows('SELECT c.*,f.name firm_name FROM contacts c JOIN firms f ON f.id=c.firm_id ORDER BY c.selected DESC,c.id DESC'),'drafts':a.store.rows('SELECT d.*,c.email,c.name contact_name FROM drafts d JOIN contacts c ON c.id=d.contact_id ORDER BY d.id DESC'),'replies':a.store.rows('SELECT r.*,c.name,c.email FROM replies r JOIN contacts c ON c.id=r.contact_id ORDER BY r.received DESC'),'deliveries':a.store.rows('SELECT * FROM deliveries ORDER BY id DESC'),'events':a.store.rows('SELECT * FROM events ORDER BY id DESC LIMIT 30'),'settings':{k:a.config[k] for k in PUBLIC_SETTINGS},'jobs':list(self.jobs.values())[-10:],'auth_status':self.auth.result}
    def action(self,data):
        a=Agent(self.root);action=data.get('action');id=int(data.get('id',0))
        if action=='settings':
            updates=data.get('settings',{})
            for k,v in updates.items():
                if k not in PUBLIC_SETTINGS or type(v)!=PUBLIC_SETTINGS[k]:raise GateError('Invalid setting')
                if isinstance(v,str) and (len(v)>500 or '\r' in v):raise GateError('Invalid setting length')
            if 'sender_email' in updates:norm_email(updates['sender_email'])
            if 'daily_send_limit' in updates and not 1<=updates['daily_send_limit']<=50:raise GateError('Daily limit must be between 1 and 50')
            cfg={};p=self.root/'config.local.json'
            if p.exists():cfg=json.loads(p.read_text())
            cfg.update(updates);p.write_text(json.dumps(cfg,indent=2));os.chmod(p,0o600)
            if updates:a.store.execute("UPDATE drafts SET status='draft',approved_hash=NULL WHERE status='approved'")
            return {'saved':True}
        if action=='keys':
            lines=[]
            for k in ('BRAVE_SEARCH_API_KEY','HUNTER_API_KEY','GMAIL_CLIENT_FILE'):
                old=os.environ.get(k,'');v=data.get(k,'').strip() or old
                if '\n' in v or '\r' in v or len(v)>1000:raise GateError('Invalid credential format')
                os.environ[k]=v;lines.append(k+'='+v)
            p=self.root/'.env';fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
            with os.fdopen(fd,'w') as f:f.write('\n'.join(lines)+'\n')
            os.chmod(p,0o600);return {'saved':True}
        if action=='gmail-connect':
            file=os.getenv('GMAIL_CLIENT_FILE','')
            if not file:raise GateError('Set the path to your Google Desktop OAuth client file first')
            return {'url':self.auth.start(file)}
        if action=='add-firm':return {'id':a.add_firm(data['url'],data.get('name',''),source='Added in dashboard')}
        if action=='review-firm':a.review_firm(id,data['name'],data['city'],data['evidence']);return {'saved':True}
        if action=='review-contact':a.review_contact(id,data['name'],data['role'],data['source'],data['evidence']);return {'saved':True}
        if action=='add-contact':return {'id':a.add_contact(int(data['firm_id']),data['email'],data.get('name',''),data.get('role',''),data.get('source',''),data.get('evidence',''))}
        if action=='exclude-firm':a.store.execute("UPDATE firms SET fit='excluded' WHERE id=?",(id,));return {'saved':True}
        if action=='approve':a.approve(id,data['hash']);return {'approved':True}
        if action=='suppress':a.store.suppress(norm_email(data['email']),'User suppression');return {'suppressed':True}
        tasks={'discover':a.discover,'run':a.run,'scrape':lambda:a.scrape(id),'find-sourced':lambda:a.find_sourced_people(id),'enrich':lambda:a.enrich(id),'qualify':lambda:a.qualify(id),'verify':lambda:a.verify(id),'draft':lambda:a.make_draft(id,data.get('kind','initial')),'send':lambda:a.send(id),'sync':a.sync_replies,'reconcile':a.reconcile,'test-mail':a.test_mail,'provider-check':lambda:provider_check(a)}
        if action not in tasks:raise GateError('Unknown action')
        return self.task(action,tasks[action])

def provider_check(a):
    results={}
    for name,fn in [('brave',lambda:len(a.brave.search(a.config['search_queries'][0]))),('hunter',lambda:a.hunter.call('account',{},0)),('gmail',a.gmail.profile)]:
        try:
            value=fn()
            results[name]={'ok':True,'detail':str(value) if name=='brave' else value.get('emailAddress','Connected') if name=='gmail' else 'Account accessible'}
        except RuntimeError as e:results[name]={'ok':False,'detail':str(e)}
    return results

def serve(root,port=8788):
    service=Service(Path(root));origin=f'http://127.0.0.1:{port}'
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def response(self,code,value,kind='application/json'):
            raw=json.dumps(value,ensure_ascii=False).encode() if kind=='application/json' else value
            self.send_response(code);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(raw)));self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.send_header('Referrer-Policy','no-referrer');self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'");self.end_headers();self.wfile.write(raw)
        def trusted(self):
            if self.headers.get('Host')!=f'127.0.0.1:{port}':return False
            if self.headers.get('Origin') not in (None,origin):return False
            return secrets.compare_digest(self.headers.get('X-Outreach-Token',''),service.token)
        def do_GET(self):
            path=urllib.parse.urlsplit(self.path).path
            if self.headers.get('Host')!=f'127.0.0.1:{port}':self.send_error(403);return
            if path=='/api/state':
                if not self.trusted():self.response(403,{'error':'Open the private launch link from the launcher'});return
                self.response(200,service.state());return
            if path=='/api/export':
                if not self.trusted():self.response(403,{'error':'Private launch link required'});return
                self.response(200,service.state());return
            if path not in ('/','/app.js','/style.css'):self.send_error(404);return
            file=service.root/'static'/('index.html' if path=='/' else path[1:]);kind={'html':'text/html; charset=utf-8','js':'text/javascript; charset=utf-8','css':'text/css; charset=utf-8'}[file.suffix[1:]];self.response(200,file.read_bytes(),kind)
        def do_POST(self):
            if self.path!='/api/action' or not self.trusted():self.response(403,{'error':'Private launch link required'});return
            try:
                n=int(self.headers.get('Content-Length',0))
                if not 0<n<=20000:raise GateError('Invalid request size')
                data=json.loads(self.rfile.read(n));result=service.action(data);self.response(200,result)
            except (RuntimeError,ValueError,KeyError) as e:self.response(400,{'error':str(e)})
            except Exception:self.response(500,{'error':'Action failed; check local setup'})
    server=http.server.ThreadingHTTPServer(('127.0.0.1',port),Handler)
    launch=origin+'/#token='+service.token
    (service.root/'data/launch-url.txt').write_text(launch+'\n');os.chmod(service.root/'data/launch-url.txt',0o600)
    print('Outreach dashboard: '+launch,flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()
