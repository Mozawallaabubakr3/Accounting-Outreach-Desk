import base64,datetime as dt,json,tempfile,threading,unittest
from pathlib import Path
from email import policy
from email.parser import BytesParser
from outreach.agent import Agent,GateError,business_due
from outreach.net import Page,public_url,NetworkError
from outreach.providers import Hunter,Gmail
ROOT=Path(__file__).resolve().parents[1]
class Net:
 def page(self,url):
  p=Page();p.feed('<title>Example CPA</title><script>bad@example.cpa</script><h1>Example Accounting</h1><p>Miami, FL 33101</p><h2>Jane Doe, Managing Partner</h2><p>jane@example.cpa</p><a href="mailto:jane@example.cpa">Email Jane</a><a href="/team">Our team</a>');return p.result(url)
class Search:
 def search(self,q):return [{'url':'https://example.cpa/','title':'Example CPA'},{'url':'https://www.example.cpa/team','title':'Team'},{'url':'https://yelp.com/a','title':'Directory'}]
class Verify:
 def __init__(self):self.status='valid'
 def domain(self,d):return [{'value':'jane@example.cpa','first_name':'Jane','last_name':'Doe','position':'Managing Partner','sources':[{'uri':'https://example.cpa/team'}]}]
 def find(self,d,f,l):return {'email':'jane@example.cpa'}
 def verify(self,e):return {'status':self.status,'accept_all':False,'disposable':False,'webmail':False}
class Mail:
 def __init__(self):self.sent=[];self.messages=[];self.fail=False;self.lookup=[];self.sync_fail=False
 def profile(self):return {'emailAddress':'candidate@example.com'}
 def send(self,m,thread=None):
  self.sent.append((m,thread))
  if self.fail:raise RuntimeError('Connection lost after acceptance')
  return {'id':'out-'+str(len(self.sent)),'threadId':thread or 'thread-1'}
 def thread(self,t):
  if self.sync_fail:raise RuntimeError('Inbox unavailable')
  return {'messages':self.messages}
 def recent_messages(self,q):return []
 def find_sent(self,k):return self.lookup
 def message(self,id):return {'id':id,'threadId':'thread-1','internalDate':str(int(dt.datetime.now(dt.timezone.utc).timestamp()*1000))}
def reply(text,sender='jane@example.cpa',subject='Re: A quick question about your firm'):
 return {'id':'reply-1','labelIds':['INBOX'],'payload':{'mimeType':'text/plain','headers':[{'name':'From','value':sender},{'name':'Subject','value':subject}],'body':{'data':base64.urlsafe_b64encode(text.encode()).decode().rstrip('=')}}}
class Workflow(unittest.TestCase):
 def setUp(self):
  self.t=tempfile.TemporaryDirectory();self.root=Path(self.t.name);(self.root/'config.json').write_text((ROOT/'config.json').read_text());(self.root/'portfolio.pdf').write_bytes(b'%PDF test attachment');self.g=Mail();self.h=Verify();self.a=Agent(self.root,net=Net(),brave=Search(),hunter=self.h,gmail=self.g);self.a.config.update(sender_email='candidate@example.com',sender_name='Synthetic sender',portfolio_url='https://example.com/',portfolio_public_confirmed=True,attachment='portfolio.pdf',postal_address='123 Test Road, Miami, FL 33101',postal_address_confirmed=True,send_enabled=True)
 def tearDown(self):self.t.cleanup()
 def ready(self):
  f=self.a.add_firm('https://example.cpa/','Example CPA');self.a.scrape(f);self.a.enrich(f);c=self.a.store.one('SELECT id FROM contacts')['id'];self.a.review_firm(f,'Example CPA','Miami','Example CPA offers accounting at its Miami FL 33101 office.');self.a.review_contact(c,'Jane Doe','Managing Partner','https://example.cpa/team','Jane Doe is the managing partner at Example CPA.');self.a.verify(c);d=self.a.make_draft(c);self.a.approve(d['id'],d['hash']);return c,d
 def sent(self):
  c,d=self.ready();self.a.send(d['id'],False);return c,d
 def due(self):self.a.store.execute('UPDATE deliveries SET sent_at=?',((dt.datetime.now(dt.timezone.utc)-dt.timedelta(days=15)).isoformat(),))
 def blocked(self,d):
  with self.assertRaises(RuntimeError):self.a.send(d['id'],False)
 def test_end_to_end(self):
  c,d=self.sent();m=BytesParser(policy=policy.default).parsebytes(self.g.sent[0][0].as_bytes());self.assertEqual(m['To'],'jane@example.cpa');self.assertEqual(len(list(m.iter_attachments())),1);self.assertIn('pro bono',m.get_body(preferencelist=('plain',)).get_content());self.assertIn('123 Test Road',m.get_body(preferencelist=('plain',)).get_content());self.blocked(d);self.assertEqual(len(self.g.sent),1)
 def test_domain_deduplication(self):self.a.discover();self.assertEqual(self.a.store.one('SELECT count(*) n FROM firms')['n'],1)
 def test_scrape_email_deduplication(self):
  f=self.a.add_firm('https://example.cpa/');self.assertEqual(self.a.scrape(f)['pages'],2);self.assertEqual(self.a.store.one('SELECT count(*) n FROM contacts')['n'],1)
 def test_unreviewed_identity(self):
  f=self.a.add_firm('https://example.cpa/');self.a.scrape(f)
  with self.assertRaises(GateError):self.a.make_draft(1)
 def test_catchall(self):c,d=self.ready();self.h.status='accept_all';self.a.verify(c);self.blocked(d)
 def test_invalid_email(self):c,d=self.ready();self.h.status='invalid';self.a.verify(c);self.blocked(d)
 def test_expired_verification(self):
  c,d=self.ready();self.a.store.execute('UPDATE contacts SET verified_at=? WHERE id=?',((dt.datetime.now(dt.timezone.utc)-dt.timedelta(days=20)).isoformat(),c));self.blocked(d)
 def test_changed_pdf(self):c,d=self.ready();(self.root/'portfolio.pdf').write_bytes(b'changed');self.blocked(d)
 def test_changed_address(self):c,d=self.ready();self.a.config['postal_address']='New address';self.blocked(d)
 def test_changed_sender(self):c,d=self.ready();self.a.config['sender_email']='different@example.com';self.blocked(d)
 def test_changed_body(self):c,d=self.ready();self.a.store.execute('UPDATE drafts SET body=? WHERE id=?',('changed',d['id']));self.blocked(d)
 def test_stale_approval(self):
  c,d=self.ready();self.a.store.execute("UPDATE drafts SET status='draft' WHERE id=?",(d['id'],))
  with self.assertRaises(GateError):self.a.approve(d['id'],'old hash')
 def test_optout(self):c,d=self.ready();self.a.store.suppress('jane@example.cpa','no thanks');self.blocked(d);self.assertEqual(len(self.g.sent),0)
 def test_uncertain_and_reconcile(self):
  c,d=self.ready();self.g.fail=True;self.blocked(d);self.blocked(d);self.assertEqual(len(self.g.sent),1);self.assertEqual(self.a.reconcile()[0]['status'],'unresolved');self.g.lookup=[{'id':'accepted'}];self.assertEqual(self.a.reconcile()[0]['status'],'sent');self.assertEqual(len(self.g.sent),1)
 def test_reply_stops_followup_without_false_optout(self):
  c,d=self.sent();self.g.messages=[reply('Happy to talk.\n\nOn Monday User wrote:\nReply “no thanks” to stop future emails.')];self.a.sync_replies();self.assertEqual(self.a.store.one('SELECT * FROM replies')['kind'],'reply');self.assertIsNone(self.a.store.one('SELECT * FROM suppression'))
  with self.assertRaises(GateError):self.a.make_draft(c,'followup')
 def test_no_thanks_suppresses(self):
  c,d=self.sent();self.g.messages=[reply('No thanks, please remove me.')];self.a.sync_replies();self.assertEqual(self.a.store.one('SELECT * FROM replies')['kind'],'optout');self.assertIsNotNone(self.a.store.one('SELECT * FROM suppression'))
 def test_bounce(self):
  c,d=self.sent();self.g.messages=[reply('Delivery failed for jane@example.cpa 550 5.1.1',sender='mailer-daemon@googlemail.com',subject='Delivery Status Notification (Failure)')];self.a.sync_replies();self.assertEqual(self.a.store.one('SELECT * FROM replies')['kind'],'bounce')
 def test_inbox_failure(self):
  c,d=self.sent();self.g.sync_fail=True
  with self.assertRaises(RuntimeError):self.a.sync_replies()
 def test_followup_due_and_threaded(self):
  c,d=self.sent()
  with self.assertRaises(GateError):self.a.make_draft(c,'followup')
  self.due();f=self.a.make_draft(c,'followup');self.a.approve(f['id'],f['hash']);self.a.send(f['id'],False);m,t=self.g.sent[-1];self.assertEqual(t,'thread-1');self.assertIsNotNone(m['In-Reply-To']);self.assertEqual(len(list(m.iter_attachments())),0)
 def test_business_days(self):self.assertEqual(business_due('2026-10-02T14:00:00+00:00',1).date().isoformat(),'2026-10-05')
 def test_daily_cap(self):
  c,d=self.ready();self.a.config['daily_send_limit']=1;self.a.send(d['id'],False);self.due();f=self.a.make_draft(c,'followup');self.a.approve(f['id'],f['hash']);self.blocked(f)
 def test_missing_address(self):c,d=self.ready();self.a.config['postal_address_confirmed']=False;self.blocked(d)
 def test_disabled_send(self):c,d=self.ready();self.a.config['send_enabled']=False;self.blocked(d)
 def test_second_recipient_same_firm(self):
  c,d=self.sent();c2=self.a.add_contact(1,'john@example.cpa','John Doe','Owner','https://example.cpa/team','John Doe is the owner of Example CPA.');self.a.review_contact(c2,'John Doe','Owner','https://example.cpa/team','John Doe is the owner of Example CPA.');self.a.verify(c2)
  with self.assertRaises(GateError):self.a.make_draft(c2)
 def test_concurrent_send_once(self):
  c,d=self.ready();errors=[]
  def send():
   try:self.a.send(d['id'],False)
   except RuntimeError as e:errors.append(e)
  t1=threading.Thread(target=send);t2=threading.Thread(target=send);t1.start();t2.start();t1.join();t2.join();self.assertEqual(len(self.g.sent),1);self.assertEqual(len(errors),1)
 def test_automatic_pipeline_qualifies_and_drafts(self):
  r=self.a.run();self.assertEqual(r['drafted'],1);self.assertEqual(self.a.store.one('SELECT * FROM contacts')['name'],'Jane Doe');self.assertEqual(self.a.store.one('SELECT * FROM firms')['fit'],'confirmed');self.assertEqual(len(self.g.sent),0)
 def test_service_area_alone_does_not_qualify(self):
  f=self.a.add_firm('https://example.cpa/');self.a.store.execute('UPDATE firms SET evidence=? WHERE id=?',(json.dumps([{'url':'https://example.cpa/','text':'We offer accounting services to Miami and Fort Lauderdale nationwide.'}]),f));r=self.a.qualify(f);self.assertFalse(r['firm'])
 def test_unrelated_role_on_separate_line_not_auto_qualified(self):
  f=self.a.add_firm('https://example.cpa/');self.a.add_contact(f,'jane@example.cpa','Jane Doe','Managing Partner','https://example.cpa/','Candidate from provider');self.a.store.execute('UPDATE firms SET evidence=? WHERE id=?',(json.dumps([{'url':'https://example.cpa/','text':'Accounting Miami FL 33101\nJane Doe\nContact our Managing Partner for help'}]),f));r=self.a.qualify(f);self.assertIsNone(r['contact'])
 def test_test_mail_only_to_sender_and_has_pdf(self):
  r=self.a.test_mail();self.assertTrue(r['accepted']);self.assertEqual(self.g.sent[0][0]['To'],'candidate@example.com');self.assertEqual(len(list(self.g.sent[0][0].iter_attachments())),1)
 def test_people_extraction_from_realistic_heading_formats(self):
  f=self.a.add_firm('https://example.cpa/');self.a.extract_people(f,{'url':'https://example.cpa/team','text':'Founder - Felipe Ruiz - Certified Public Accountant\nSteven Samuels, CPA Partner\nAnastasia Panfilova, CPA Partner\nOur customers can speak with the owner'});names={r['name'] for r in self.a.store.rows('SELECT * FROM people')};self.assertEqual(names,{'Felipe Ruiz','Steven Samuels','Anastasia Panfilova'})
 def test_sourced_name_email_finder_retains_evidence(self):
  f=self.a.add_firm('https://example.cpa/');self.a.extract_people(f,{'url':'https://example.cpa/team','text':'Jane Doe, Managing Partner'});self.assertEqual(self.a.find_sourced_people(f),1);r=self.a.store.one('SELECT * FROM contacts');self.assertEqual(r['name'],'Jane Doe');self.assertEqual(r['source'],'https://example.cpa/team')
class Providers(unittest.TestCase):
 def test_script_ignored(self):
  p=Page();p.feed('<script>secret@example.com</script><h2>Jane Doe</h2><a href="mailto:jane@example.com">Email</a>');r=p.result('https://example.com');self.assertNotIn('secret',r['text']);self.assertIn('Jane Doe',r['text']);self.assertEqual(r['links'],['mailto:jane@example.com'])
 def test_private_url_blocked(self):
  for u in ('file:///tmp/key','http://user:pass@example.com/','http://127.0.0.1/','http://169.254.169.254/','http://[::1]/'):
   with self.subTest(url=u),self.assertRaises(NetworkError):public_url(u)
 def test_hunter_key_not_in_url(self):
  from outreach.store import Store
  class A:
   def request(self,url,*args,**kw):self.url=url;self.kw=kw;return {'data':{'status':'valid'}}
  with tempfile.TemporaryDirectory() as t:
   api=A();h=Hunter(Store(Path(t)/'x.db'),{'hunter_requests_per_day':10},'SECRET',api);h.verify('jane@example.com');self.assertNotIn('SECRET',api.url);self.assertEqual(api.kw['headers']['Authorization'],'Bearer SECRET')
 def test_gmail_mime(self):
  from email.message import EmailMessage
  class A:
   def request(self,url,method='GET',headers=None,data=None,form=None):
    if 'oauth2' in url:return {'access_token':'token','expires_in':3600}
    self.data=data;return {'id':'sent','threadId':'t'}
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);(root/'data').mkdir();(root/'data/gmail-token.json').write_text(json.dumps({'client_id':'id','client_secret':'secret','refresh_token':'refresh'}));api=A();g=Gmail(root,api);m=EmailMessage();m['To']='jane@example.com';m.set_content('Hello');g.send(m);r=BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(api.data['raw']));self.assertEqual(r['To'],'jane@example.com')
if __name__=='__main__':unittest.main()
