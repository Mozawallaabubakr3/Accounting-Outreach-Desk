import datetime as dt, email.utils, hashlib, json, os, re, secrets, urllib.parse
from pathlib import Path
from email.message import EmailMessage
from zoneinfo import ZoneInfo
from .store import Store, now, digest, day
from .net import Network
from .providers import Brave,Hunter,Gmail,headers,inbound_text

EMAIL=re.compile(r'(?<![\w.+-])[A-Za-z0-9.!#$%&\'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,}(?![\w.-])')
HEAD=re.compile(r'\b(owner|founder|co-founder|managing partner|partner|president|principal|practice manager|chief executive|CEO)\b',re.I)
BAD_DOMAINS={'yelp.com','facebook.com','linkedin.com','instagram.com','google.com','clutch.co','yellowpages.com','bbb.org','indeed.com','ziprecruiter.com'}

class GateError(RuntimeError):pass

def domain(url):
    p=urllib.parse.urlsplit(url)
    if p.scheme not in ('http','https') or not p.hostname or p.username or p.password:raise GateError('Enter a public firm website URL')
    return p.hostname.lower().removeprefix('www.')
def norm_email(value):
    value=value.strip().lower()
    if not EMAIL.fullmatch(value) or '\n' in value or '\r' in value:raise GateError('Invalid business email')
    return value

def load_env(path):
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip() and not line.lstrip().startswith('#') and '=' in line:
                k,v=line.split('=',1);os.environ.setdefault(k.strip(),v.strip().strip('"\''))

def business_due(sent,days):
    at=dt.datetime.fromisoformat(sent).astimezone(ZoneInfo('America/New_York'));count=0
    while count<days:
        at+=dt.timedelta(days=1)
        if at.weekday()<5:count+=1
    return at.astimezone(dt.timezone.utc)

class Agent:
    def __init__(self,root,net=None,brave=None,hunter=None,gmail=None):
        self.root=Path(root).resolve();(self.root/'data').mkdir(exist_ok=True);os.chmod(self.root/'data',0o700)
        load_env(self.root/'.env');self.config=json.loads((self.root/'config.json').read_text())
        if (self.root/'config.local.json').exists():self.config.update(json.loads((self.root/'config.local.json').read_text()))
        self.store=Store(self.root/'data/outreach.sqlite3');self.net=net or Network(self.store,self.config)
        self.brave=brave or Brave(self.store,self.config,os.getenv('BRAVE_SEARCH_API_KEY',''))
        self.hunter=hunter or Hunter(self.store,self.config,os.getenv('HUNTER_API_KEY',''))
        self.gmail=gmail or Gmail(self.root)
    def add_firm(self,url,name='',city='',source=''):
        dom=domain(url)
        if any(dom==x or dom.endswith('.'+x) for x in BAD_DOMAINS):return None
        url=urllib.parse.urlunsplit((*urllib.parse.urlsplit(url)[:2],'/','',''))
        self.store.execute('INSERT INTO firms(domain,url,name,city,source,updated) VALUES(?,?,?,?,?,?) ON CONFLICT(domain) DO NOTHING',(dom,url,name[:160],city,source,now()))
        return self.store.one('SELECT * FROM firms WHERE domain=?',(dom,))['id']
    def discover(self):
        count=0
        for q in self.config['search_queries']:
            for r in self.brave.search(q):
                if self.add_firm(r['url'],r['title'],source='Search: '+q):count+=1
        self.store.event('discovery',{'results_processed':count});return count
    def add_contact(self,firm_id,email,name='',role='',source='',evidence=''):
        value=norm_email(email)
        firm=self.store.one('SELECT * FROM firms WHERE id=?',(firm_id,))
        if not firm:raise GateError('Firm does not exist')
        self.store.execute('INSERT INTO contacts(firm_id,email,name,role,source,evidence) VALUES(?,?,?,?,?,?) ON CONFLICT(email) DO NOTHING',(firm_id,value,name[:100],role[:100],source[:500],evidence[:2000]))
        existing=self.store.one('SELECT * FROM contacts WHERE email=?',(value,))
        if existing['firm_id']==firm_id and not existing['identity_reviewed'] and name:
            self.store.execute('UPDATE contacts SET name=?,role=?,source=?,evidence=? WHERE id=?',(name[:100],role[:100],source[:500],evidence[:2000],existing['id']))
        return existing['id']
    def scrape(self,firm_id):
        f=self.store.one('SELECT * FROM firms WHERE id=?',(firm_id,))
        if not f:raise GateError('Firm not found')
        first=self.net.page(f['url']);pages=[first];links=[]
        for href in first['links']:
            u=urllib.parse.urljoin(first['url'],href);p=urllib.parse.urlsplit(u)
            if p.scheme not in ('http','https'):continue
            if domain(u)==f['domain'] and re.search(r'about|team|leadership|people|partner|contact|service',p.path,re.I):
                clean=urllib.parse.urlunsplit((p.scheme,p.netloc,p.path,'',''))
                if clean!=first['url'] and clean not in links:links.append(clean)
        def page_priority(u):
            path=urllib.parse.urlsplit(u).path.lower()
            return 0 if re.search(r'contact|team|leadership|people|partner',path) else 1 if 'about' in path else 2
        links.sort(key=page_priority)
        for u in links[:self.config['max_pages_per_firm']-1]:
            try:pages.append(self.net.page(u))
            except RuntimeError as e:self.store.event('page_skipped',{'url':u,'reason':str(e)})
        excerpts=[];seen=0
        for page in pages:
            text=page['text'];self.extract_people(firm_id,page);excerpts.append({'url':page['url'],'text':text[:9000]})
            addresses=set(EMAIL.findall(text))
            for href in page['links']:
                if href.lower().startswith('mailto:'):addresses.update(EMAIL.findall(urllib.parse.unquote(href[7:].split('?')[0])))
            for addr in addresses:
                # Publicly listed business email only; personal mailboxes require explicit review later.
                local,host=addr.lower().rsplit('@',1)
                if host!=f['domain']:continue
                if any(local.startswith(x) for x in ('noreply','no-reply','privacy','webmaster','careers','billing')):continue
                pos=text.lower().find(addr.lower());context=text[max(0,pos-300):pos+len(addr)+300] if pos>=0 else 'Published mailto link on this page'
                role=HEAD.search(context)
                name_match=re.search(r'([A-Z][a-z]+(?: [A-Z]\.)? [A-Z][a-z]+(?:[- ][A-Z][a-z]+)?)\s*(?:,|\||-|\n)\s*(Managing Partner|Partner|Owner|Founder|President|Principal|CEO)\b',context)
                suggested=name_match.group(1) if name_match else ''
                self.add_contact(firm_id,addr,name=suggested,role=role.group(0) if role else '',source=page['url'],evidence=context);seen+=1
        joined='\n'.join(p['text'] for p in pages)
        cities=[c for c in self.config['cities'] if re.search(r'\b'+re.escape(c)+r'\b',joined,re.I)]
        self.store.execute('UPDATE firms SET evidence=?,city=?,updated=? WHERE id=?',(json.dumps(excerpts),', '.join(cities),now(),firm_id))
        self.store.event('scraped',{'firm_id':firm_id,'pages':len(pages),'email_candidates':seen});return {'pages':len(pages),'contacts':seen,'city_evidence':cities}
    def extract_people(self,firm_id,page):
        for line in page['text'].splitlines():
            match=HEAD.search(line)
            if not match or len(line)>180:continue
            before=line[:match.start()].strip(' ,:-|')
            before=re.sub(r',?\s*\b(CPA|EA|CAMS|RIA)\b','',before).strip(' ,:-|')
            after=line[match.end():].strip(' ,:-|')
            candidate=before if before else re.split(r'\s+[-|]\s+',after)[0]
            parts=candidate.split()
            if not 2<=len(parts)<=4 or not all(p[0].isupper() and re.fullmatch(r"[^\W\d_]+(?:['’.-][^\W\d_]+)*\.?",p) for p in parts):continue
            role=match.group(0)
            self.store.execute('INSERT INTO people(firm_id,name,role,source,evidence) VALUES(?,?,?,?,?) ON CONFLICT(firm_id,name,role) DO UPDATE SET source=excluded.source,evidence=excluded.evidence',(firm_id,candidate,role,page['url'],line))
    def find_sourced_people(self,firm_id):
        added=0
        for person in self.store.rows('SELECT * FROM people WHERE firm_id=? ORDER BY id LIMIT 3',(firm_id,)):
            if self.store.one('SELECT 1 FROM contacts WHERE firm_id=? AND name=?',(firm_id,person['name'])):continue
            parts=person['name'].split()
            try:self.find_email(firm_id,parts[0],' '.join(parts[1:]),person['role'],person['source'],person['evidence']);added+=1
            except GateError as e:self.store.event('named_email_unavailable',{'person_id':person['id'],'reason':str(e)})
        return added
    def enrich(self,firm_id):
        f=self.store.one('SELECT * FROM firms WHERE id=?',(firm_id,))
        if not f:raise GateError('Firm not found')
        records=self.hunter.domain(f['domain']);added=0
        for r in records:
            position=r.get('position') or ''
            if not HEAD.search(position):continue
            name=' '.join(x for x in (r.get('first_name'),r.get('last_name')) if x)
            sources=r.get('sources') or []
            source=next((x.get('uri','') for x in sources if x.get('uri','').startswith('http')),'')
            if not source or not name:continue
            self.add_contact(firm_id,r['value'],name,position,source,json.dumps({'provider':'Hunter','sources':sources},ensure_ascii=False));added+=1
        return added
    def qualify(self,firm_id):
        """Automatically qualify only source-backed matches; leave ambiguous records for review."""
        f=self.store.one('SELECT * FROM firms WHERE id=?',(firm_id,))
        if not f or f['fit']=='excluded':return {'firm':False,'contact':None,'reason':'No active firm'}
        pages=json.loads(f['evidence'] or '[]');text='\n'.join(p['text'] for p in pages)
        city=next((c for c in self.config['cities'] if re.search(r'\b'+re.escape(c)+r'\s*,?\s*(?:FL|Florida)\s+\d{5}\b',text,re.I)),None)
        accounting=bool(re.search(r'\b(accounting|certified public accountants?|CPA|bookkeeping)\b',text,re.I))
        if not city or not accounting:return {'firm':False,'contact':None,'reason':'Accounting and local office evidence need review'}
        if f['fit']!='confirmed':self.review_firm(f['id'],f['name'] or f['domain'],city,'Automatic source review: accounting services and a '+city+' FL postal address appear in researched pages.')
        candidates=[]
        for c in self.store.rows('SELECT * FROM contacts WHERE firm_id=?',(firm_id,)):
            if c['identity_reviewed'] and c['selected']:return {'firm':True,'contact':c['id'],'reason':'Existing selected contact'}
            if not c['name'] or not HEAD.search(c['role']) or not c['source']:continue
            if c['email'].rsplit('@',1)[1]!=f['domain']:continue
            try:
                page=next((p for p in pages if p['url']==c['source']),None) or self.net.page(c['source'])
                if domain(page['url'])!=f['domain']:continue
            except RuntimeError:continue
            pos=page['text'].lower().find(c['name'].lower())
            if pos<0:continue
            start=page['text'].rfind('\n',0,pos);end=page['text'].find('\n',pos+len(c['name']))
            block=page['text'][max(0,start):end if end!=-1 else len(page['text'])]
            # An exact name and role on the same heading/block is required for unattended selection.
            if not re.search(r'\b'+re.escape(c['role'])+r'\b',block,re.I):continue
            rank=0 if re.search('managing|owner|founder',c['role'],re.I) else 1 if re.search('president|principal|CEO',c['role'],re.I) else 2
            candidates.append((rank,c,page['url'],block))
        if not candidates:return {'firm':True,'contact':None,'reason':'Decision-maker association needs review'}
        candidates.sort(key=lambda x:(x[0],x[1]['id']));_,c,url,evidence=candidates[0]
        self.review_contact(c['id'],c['name'],c['role'],url,evidence)
        return {'firm':True,'contact':c['id'],'reason':'Exact name and role checked in firm website block'}
    def find_email(self,firm_id,first,last,role,source,evidence):
        f=self.store.one('SELECT * FROM firms WHERE id=?',(firm_id,))
        if not f or not first.strip() or not last.strip() or not source or not evidence:raise GateError('Verified name, source and role evidence are required')
        r=self.hunter.find(f['domain'],first,last)
        if not r.get('email'):raise GateError('No email found')
        return self.add_contact(firm_id,r['email'],first+' '+last,role,source,evidence)
    def verify(self,contact_id):
        c=self.store.one('SELECT * FROM contacts WHERE id=?',(contact_id,))
        if not c:raise GateError('Contact not found')
        result=self.hunter.verify(c['email']);status=result.get('status','unknown')
        if status!='valid' or result.get('accept_all') or result.get('disposable') or result.get('webmail'):status='review_required' if status=='valid' else status
        self.store.execute('UPDATE contacts SET verification=?,verified_at=? WHERE id=?',(status,now(),contact_id))
        self.store.event('verified',{'contact_id':contact_id,'status':status});return status
    def review_firm(self,firm_id,name,city,evidence):
        if city not in self.config['cities'] or not name.strip() or len(evidence.strip())<20:raise GateError('Confirm firm name, target city and supporting accounting/location evidence')
        self.store.execute("UPDATE firms SET name=?,city=?,fit='confirmed',updated=? WHERE id=?",(name,city,now(),firm_id));self.store.event('firm_review',{'firm_id':firm_id,'evidence':evidence})
    def review_contact(self,contact_id,name,role,source,evidence):
        if not name.strip() or not HEAD.search(role) or not source.startswith('https://') or len(evidence.strip())<20:raise GateError('A named decision maker, senior role, HTTPS source and evidence are required')
        c=self.store.one('SELECT * FROM contacts WHERE id=?',(contact_id,))
        if not c:raise GateError('Contact not found')
        with self.store.db() as db:
            db.execute('UPDATE contacts SET selected=0 WHERE firm_id=?',(c['firm_id'],))
            db.execute('UPDATE contacts SET name=?,role=?,source=?,evidence=?,identity_reviewed=1,selected=1 WHERE id=?',(name,role,source,evidence,contact_id))
            db.execute("UPDATE drafts SET status='draft',approved_hash=NULL WHERE contact_id IN (SELECT id FROM contacts WHERE firm_id=?) AND status IN ('draft','approved')",(c['firm_id'],))
        self.store.event('contact_review',{'contact_id':contact_id})
    def contact(self,contact_id):
        c=self.store.one('SELECT c.*,f.fit,f.name firm_name,f.domain FROM contacts c JOIN firms f ON f.id=c.firm_id WHERE c.id=?',(contact_id,))
        if not c:raise GateError('Contact not found')
        return c
    def eligible(self,c):
        if c['fit']!='confirmed':raise GateError('Firm type and location need review')
        if not c['identity_reviewed'] or not c['selected'] or not c['name'] or not HEAD.search(c['role']):raise GateError('Review and select the firm decision maker first')
        if c['verification']!='valid' or not c['verified_at']:raise GateError('A valid verified business email is required')
        if dt.datetime.fromisoformat(c['verified_at'])<dt.datetime.now(dt.timezone.utc)-dt.timedelta(days=self.config['verification_max_age_days']):raise GateError('Email verification expired')
        if self.store.one('SELECT 1 FROM suppression WHERE email=?',(c['email'],)):raise GateError('Contact opted out or is suppressed')
        if self.store.one('SELECT 1 FROM replies WHERE contact_id=?',(c['id'],)):raise GateError('Contact already replied; follow up personally')
        if self.store.one("SELECT 1 FROM deliveries d JOIN drafts dr ON dr.id=d.draft_id JOIN contacts c2 ON c2.id=dr.contact_id WHERE c2.firm_id=? AND c2.id!=? AND d.status IN ('sending','sent','uncertain')",(c['firm_id'],c['id'])):raise GateError('This firm already has an outreach recipient')
    def attachment(self):
        p=(self.root/self.config['attachment']).resolve()
        if not p.is_file() or p.suffix.lower()!='.pdf' or p.stat().st_size>8_000_000:raise GateError('Portfolio PDF is unavailable or too large')
        data=p.read_bytes();return p,data,hashlib.sha256(data).hexdigest()
    def footer(self):
        address=self.config['postal_address'].strip()
        if not address:return '\n\n[Postal address required before sending]\nPromotional outreach. Reply “no thanks” to stop future emails.'
        return '\n\n'+address+'\nPromotional outreach. Reply “no thanks” to stop future emails.'
    def content(self,c,kind):
        first=c['name'].split()[0];url=self.config['portfolio_url'];attach=''
        if kind=='initial':
            subject='A quick question about your firm'
            body=f'Hi {first},\n\nHope you are well! I’m {self.config["sender_name"]}. I’ve been exploring ways to help businesses with AI and think your accounting workflow could benefit from the systems I build. My work would be completely pro bono as I’m looking to learn more about the industry and build my portfolio.\n\nWould you have time to chat over the next few days? I’ve attached a few things I’ve built for context.\n\nThanks,\nAbu'
            _,_,attach=self.attachment()
        else:
            subject='Re: A quick question about your firm'
            body=f'Hi {first},\n\nJust following up in case you missed my note. I’d love to hear about your firm’s workflow and see whether I could build something useful completely pro bono.\n\nWould you have time for a short chat this week?\n\nThanks,\nAbu'
        body+='\n\nPortfolio: '+url+self.footer()
        return subject,body,attach
    def draft_hash(self,c,kind,subject,body,attach):
        return digest({'recipient':c['email'],'name':c['name'],'role':c['role'],'kind':kind,'subject':subject,'body':body,'attachment':attach,'sender':self.config['sender_email'],'sender_name':self.config['sender_name'],'portfolio':self.config['portfolio_url'],'postal_address':self.config['postal_address'],'postal_address_confirmed':self.config['postal_address_confirmed']})
    def make_draft(self,contact_id,kind='initial'):
        if kind not in ('initial','followup'):raise GateError('Invalid draft kind')
        c=self.contact(contact_id);self.eligible(c)
        if kind=='followup':self.followup_gate(c)
        subject,body,attach=self.content(c,kind);h=self.draft_hash(c,kind,subject,body,attach)
        old=self.store.one('SELECT * FROM drafts WHERE contact_id=? AND kind=?',(contact_id,kind))
        if old and self.store.one('SELECT 1 FROM deliveries WHERE draft_id=?',(old['id'],)):raise GateError('This draft has a delivery record; do not replace it')
        self.store.execute("INSERT INTO drafts(contact_id,kind,subject,body,attachment_hash,hash,created) VALUES(?,?,?,?,?,?,?) ON CONFLICT(contact_id,kind) DO UPDATE SET subject=excluded.subject,body=excluded.body,attachment_hash=excluded.attachment_hash,hash=excluded.hash,approved_hash=NULL,status='draft'",(contact_id,kind,subject,body,attach,h,now()))
        return self.store.one('SELECT * FROM drafts WHERE contact_id=? AND kind=?',(contact_id,kind))
    def approve(self,draft_id,expected_hash):
        d=self.store.one('SELECT * FROM drafts WHERE id=?',(draft_id,))
        if not d or d['status']!='draft' or d['hash']!=expected_hash:raise GateError('Draft changed; review its current version')
        self.eligible(self.contact(d['contact_id']))
        self.store.execute("UPDATE drafts SET approved_hash=hash,status='approved' WHERE id=? AND hash=?",(draft_id,expected_hash));self.store.event('draft_approved',{'id':draft_id,'hash':expected_hash})
    def followup_gate(self,c):
        r=self.store.one("SELECT d.* FROM deliveries d JOIN drafts dr ON dr.id=d.draft_id WHERE dr.contact_id=? AND dr.kind='initial' AND d.status='sent'",(c['id'],))
        if not r:raise GateError('No sent initial message for this contact')
        if dt.datetime.now(dt.timezone.utc)<business_due(r['sent_at'],self.config['followup_business_days']):raise GateError('Follow-up is not due yet')
        return r
    def sending_config(self,check_window=True):
        if not self.config['send_enabled']:raise GateError('Sending is disabled in settings')
        norm_email(self.config['sender_email'])
        if not self.config['postal_address_confirmed'] or not self.config['postal_address'].strip():raise GateError('Confirm a valid postal address in settings')
        if not self.config['portfolio_public_confirmed']:raise GateError('Confirm the portfolio links are public')
        if check_window:
            t=dt.datetime.now(ZoneInfo(self.config['timezone']))
            if t.weekday()>4 or not self.config['send_start_hour']<=t.hour<self.config['send_end_hour']:raise GateError('Outside weekday sending hours')
        profile=self.gmail.profile()
        if profile['emailAddress'].lower()!=self.config['sender_email'].lower():raise GateError('Connected Gmail account does not match the sender mailbox')
    def make_message(self,d,c,key):
        msg=EmailMessage();msg['From']=email.utils.formataddr((self.config['sender_name'],self.config['sender_email']));msg['To']=c['email'];msg['Subject']=d['subject'];msg['Date']=email.utils.format_datetime(dt.datetime.now(dt.timezone.utc));msg['Message-ID']=key
        msg['Reply-To']=self.config['sender_email'];msg.set_content(d['body'])
        thread=None
        if d['kind']=='initial':
            p,data,h=self.attachment()
            if h!=d['attachment_hash']:raise GateError('Portfolio changed; regenerate and review the draft')
            msg.add_attachment(data,maintype='application',subtype='pdf',filename=p.name)
        else:
            initial=self.followup_gate(c);msg['In-Reply-To']=initial['message_key'];msg['References']=initial['message_key'];thread=initial['thread_id']
        return msg,thread
    def send(self,draft_id,check_window=True):
        self.sending_config(check_window);self.sync_replies()
        d=self.store.one('SELECT * FROM drafts WHERE id=?',(draft_id,))
        if not d:raise GateError('Draft not found')
        c=self.contact(d['contact_id']);self.eligible(c)
        if d['kind']=='followup':self.followup_gate(c)
        subject,body,attach=self.content(c,d['kind']);current=self.draft_hash(c,d['kind'],d['subject'],d['body'],attach)
        if d['status']!='approved' or d['approved_hash']!=d['hash'] or current!=d['hash']:raise GateError('Draft or sending configuration changed; review again')
        key='<'+secrets.token_hex(16)+'@'+self.config['sender_email'].split('@')[1]+'>';msg,thread=self.make_message(d,c,key)
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM deliveries WHERE draft_id=?',(draft_id,)).fetchone():raise GateError('This draft already has a delivery attempt; reconcile it instead of resending')
            if db.execute('SELECT 1 FROM suppression WHERE email=?',(c['email'],)).fetchone():raise GateError('Contact is suppressed')
            row=db.execute("SELECT count FROM usage WHERE day=? AND kind='send_attempts'",(day(),)).fetchone()
            if row and row[0]>=self.config['daily_send_limit']:raise GateError('Daily send limit reached')
            db.execute("INSERT INTO usage VALUES(?,'send_attempts',1) ON CONFLICT(day,kind) DO UPDATE SET count=count+1",(day(),))
            db.execute("INSERT INTO deliveries(draft_id,message_key,status,created) VALUES(?,?,'sending',?)",(draft_id,key,now()))
            db.execute("UPDATE drafts SET status='sending' WHERE id=?",(draft_id,))
        try:
            result=self.gmail.send(msg,thread)
            if not result.get('id') or not result.get('threadId'):raise RuntimeError('Gmail did not return complete delivery identifiers')
        except Exception as e:
            self.store.execute("UPDATE deliveries SET status='uncertain',error=? WHERE draft_id=?",('Provider outcome needs reconciliation',draft_id));self.store.execute("UPDATE drafts SET status='uncertain' WHERE id=?",(draft_id,));raise GateError('Send outcome is uncertain. Do not resend; reconcile with Gmail first') from None
        self.store.execute("UPDATE deliveries SET status='sent',provider_id=?,thread_id=?,sent_at=? WHERE draft_id=?",(result['id'],result['threadId'],now(),draft_id));self.store.execute("UPDATE drafts SET status='sent' WHERE id=?",(draft_id,));self.store.event('sent',{'draft_id':draft_id,'message_id':result['id']});return result
    def reconcile(self):
        out=[]
        for r in self.store.rows("SELECT * FROM deliveries WHERE status IN ('sending','uncertain')"):
            results=self.gmail.find_sent(r['message_key'])
            if len(results)==1:
                m=self.gmail.message(results[0]['id']);sent=dt.datetime.fromtimestamp(int(m['internalDate'])/1000,dt.timezone.utc).isoformat()
                self.store.execute("UPDATE deliveries SET status='sent',provider_id=?,thread_id=?,sent_at=?,error=NULL WHERE id=?",(m['id'],m['threadId'],sent,r['id']));self.store.execute("UPDATE drafts SET status='sent' WHERE id=?",(r['draft_id'],));out.append({'draft_id':r['draft_id'],'status':'sent'})
            else:out.append({'draft_id':r['draft_id'],'status':'unresolved','matches':len(results)})
        return out
    def record_reply(self,c,message):
        h=headers(message);sender=email.utils.parseaddr(h.get('from',''))[1].lower();text=inbound_text(message);labels=message.get('labelIds',[])
        if 'SENT' in labels or sender==self.config['sender_email'].lower():return
        if self.store.one('SELECT 1 FROM replies WHERE id=?',(message['id'],)):return
        bounce=bool(re.search(r'mailer-daemon|postmaster',sender,re.I)) and bool(re.search(r'undeliver|delivery.{0,30}(fail|status)|not delivered|550\b|5\.1\.1',h.get('subject','')+' '+text,re.I))
        if sender!=c['email'] and not bounce:return
        # Ignore quoted original message when checking opt-outs, which contains the opt-out footer.
        fresh=re.split(r'\n(?:On .{0,200}wrote:|[-_]{3,}|From:)',text,maxsplit=1)[0]
        fresh='\n'.join(x for x in fresh.splitlines() if not x.lstrip().startswith('>'))
        optout=bool(re.search(r'\b(unsubscribe|remove me|stop (emailing|contacting|sending)|do not (email|contact)|no thanks|not interested)\b',fresh,re.I))
        kind='bounce' if bounce else 'optout' if optout else 'reply'
        self.store.execute('INSERT OR IGNORE INTO replies VALUES(?,?,?,?,?)',(message['id'],c['id'],kind,fresh[:1500],now()))
        if kind in ('bounce','optout'):self.store.suppress(c['email'],kind)
        else:self.store.execute("UPDATE drafts SET status='replied',approved_hash=NULL WHERE contact_id=? AND status IN ('draft','approved')",(c['id'],))
        self.store.event('reply',{'contact_id':c['id'],'kind':kind})
    def sync_replies(self):
        # Every known outbound thread is checked before sending; failure prevents a send.
        records=self.store.rows("SELECT DISTINCT c.id,c.email,d.thread_id FROM contacts c JOIN drafts dr ON dr.contact_id=c.id JOIN deliveries d ON d.draft_id=dr.id WHERE d.status='sent' AND d.thread_id IS NOT NULL")
        for r in records:
            for m in self.gmail.thread(r['thread_id']).get('messages',[]):self.record_reply(r,m)
        # Catch replies started in new threads and out-of-thread bounce notices.
        contacts=self.store.rows("SELECT DISTINCT c.* FROM contacts c JOIN drafts dr ON dr.contact_id=c.id JOIN deliveries d ON d.draft_id=dr.id WHERE d.status='sent'")
        if contacts:
            first=self.store.one("SELECT min(sent_at) at FROM deliveries WHERE status='sent'")['at'];date=(dt.datetime.fromisoformat(first)-dt.timedelta(days=1)).strftime('%Y/%m/%d')
            sender_filter='{'+' '.join('from:'+c['email'] for c in contacts)+' from:mailer-daemon from:postmaster}'
            for ref in self.gmail.recent_messages('after:'+date+' -in:sent -in:drafts '+sender_filter):
                m=self.gmail.message(ref['id']);h=headers(m);sender=email.utils.parseaddr(h.get('from',''))[1].lower();txt=inbound_text(m)
                for c in contacts:
                    if sender==c['email'] or ('mailer-daemon' in sender or 'postmaster' in sender) and c['email'] in txt:self.record_reply(c,m)
        self.store.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)',('inbox_synced',now()));return len(records)
    def generate_ready(self):
        out=[]
        for c in self.store.rows('SELECT id FROM contacts WHERE selected=1'):
            try:
                initial=self.store.one("SELECT dr.id,d.status FROM drafts dr LEFT JOIN deliveries d ON d.draft_id=dr.id WHERE dr.contact_id=? AND dr.kind='initial'",(c['id'],))
                if not initial:out.append(self.make_draft(c['id']))
                elif initial['status']=='sent' and not self.store.one("SELECT id FROM drafts WHERE contact_id=? AND kind='followup'",(c['id'],)):out.append(self.make_draft(c['id'],'followup'))
            except GateError:continue
        return out
    def run(self,send=False):
        result={'discovered':0,'scraped':0,'enriched':0,'verified':0,'drafted':0,'sent':0,'errors':[]}
        for label,fn in [('discovery',self.discover)]:
            try:result['discovered']=fn()
            except RuntimeError as e:result['errors'].append({'step':label,'message':str(e)})
        for f in self.store.rows("SELECT * FROM firms WHERE fit!='excluded'")[:25]:
            try:self.scrape(f['id']);result['scraped']+=1
            except RuntimeError as e:result['errors'].append({'step':'scrape','firm_id':f['id'],'message':str(e)})
            if not self.store.one('SELECT 1 FROM contacts WHERE firm_id=? AND identity_reviewed=1',(f['id'],)):
                try:result['enriched']+=self.enrich(f['id'])
                except RuntimeError as e:result['errors'].append({'step':'enrichment','firm_id':f['id'],'message':str(e)})
                try:result['enriched']+=self.find_sourced_people(f['id'])
                except RuntimeError as e:result['errors'].append({'step':'named_email_lookup','firm_id':f['id'],'message':str(e)})
            try:self.qualify(f['id'])
            except RuntimeError as e:result['errors'].append({'step':'qualification','firm_id':f['id'],'message':str(e)})
        cutoff=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(days=self.config['verification_max_age_days'])).isoformat()
        for c in self.store.rows("SELECT * FROM contacts WHERE selected=1 AND (verification!='valid' OR verified_at IS NULL OR verified_at<?)",(cutoff,)):
            try:self.verify(c['id']);result['verified']+=1
            except RuntimeError as e:result['errors'].append({'step':'verification','contact_id':c['id'],'message':str(e)})
        result['drafted']=len(self.generate_ready())
        if send:
            for d in self.store.rows("SELECT id FROM drafts WHERE status='approved' ORDER BY id"):
                try:self.send(d['id']);result['sent']+=1
                except RuntimeError as e:
                    result['errors'].append({'step':'send','draft_id':d['id'],'message':str(e)})
                    break
        self.store.event('run',result);return result
    def test_mail(self):
        """User-triggered round-trip test to the configured sender only."""
        profile=self.gmail.profile();sender=norm_email(self.config['sender_email'])
        if profile['emailAddress'].lower()!=sender:raise GateError('Gmail does not match the configured sender')
        p,data,h=self.attachment();key='<'+secrets.token_hex(16)+'@'+sender.rsplit('@',1)[1]+'>'
        msg=EmailMessage();msg['From']=email.utils.formataddr((self.config['sender_name'],sender));msg['To']=sender;msg['Subject']='Outreach Desk connection test';msg['Date']=email.utils.format_datetime(dt.datetime.now(dt.timezone.utc));msg['Message-ID']=key
        msg.set_content('This is a test of your Accounting Outreach Desk. No prospect was contacted. The portfolio PDF is attached.\n\nPortfolio: '+self.config['portfolio_url']);msg.add_attachment(data,maintype='application',subtype='pdf',filename=p.name)
        attempt=self.store.event('test_mail_attempt',{'to':sender,'message_key':key})
        try:r=self.gmail.send(msg)
        except RuntimeError:raise GateError('Test delivery is uncertain. Check Gmail Sent before clicking test again') from None
        found=self.gmail.find_sent(key);result={'recipient':sender,'provider_id':r.get('id'),'accepted':bool(r.get('id')),'found_in_sent':any(x.get('id')==r.get('id') for x in found),'attachment_hash':h}
        self.store.event('test_mail_result',result);return result
    def status(self):
        c=self.config
        return {'counts':{table:self.store.one('SELECT count(*) n FROM '+table)['n'] for table in ('firms','people','contacts','drafts','deliveries','replies','suppression')},'tools':{'brave':bool(os.getenv('BRAVE_SEARCH_API_KEY')),'hunter':bool(os.getenv('HUNTER_API_KEY')),'gmail_oauth':(self.root/'data/gmail-token.json').exists()},'send_enabled':c['send_enabled'],'sender':c['sender_email'],'postal_address_ready':bool(c['postal_address'] and c['postal_address_confirmed']),'portfolio_ready':c['portfolio_public_confirmed'],'daily_limit':c['daily_send_limit'],'usage':self.store.rows('SELECT * FROM usage WHERE day=?',(day(),)),'model_calls':0}
