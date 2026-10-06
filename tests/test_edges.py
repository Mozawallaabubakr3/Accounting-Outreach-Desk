import datetime as dt,json,tempfile,unittest,urllib.request,urllib.error,urllib.parse
from pathlib import Path
from unittest.mock import patch
from outreach.net import Network,NetworkError,API
from outreach.store import Store
from outreach.agent import Agent,GateError
ROOT=Path(__file__).resolve().parents[1]
class NetworkTests(unittest.TestCase):
 def setUp(self):self.t=tempfile.TemporaryDirectory();self.s=Store(Path(self.t.name)/'db');self.net=Network(self.s,{'website_requests_per_day':5})
 def tearDown(self):self.t.cleanup()
 def test_disallowed_robots(self):
  self.net.raw=lambda url,robots=False:('User-agent: *\nDisallow: /team',url)
  self.assertFalse(self.net.allowed('https://example.com/team'));self.assertTrue(self.net.allowed('https://example.com/contact'))
 def test_robots_error_is_not_ignored(self):
  def fail(*args,**kw):raise NetworkError('robots unavailable')
  self.net.raw=fail
  with self.assertRaises(NetworkError):self.net.page('https://example.com/team')
 def test_page_cache_avoids_refetch(self):
  self.s.cache('https://example.com/',{'text':'cached','links':[]});self.net.raw=lambda *a,**kw:(_ for _ in ()).throw(AssertionError('Should not refetch'));self.assertEqual(self.net.page('https://example.com/')['text'],'cached')
 def test_budget_is_transactional(self):
  self.s.reserve('requests',1)
  with self.assertRaises(RuntimeError):self.s.reserve('requests',1)
class OAuthTests(unittest.TestCase):
 def test_desktop_client_validation(self):
  from outreach.auth import GmailSetup
  with tempfile.TemporaryDirectory() as t:
   p=Path(t)/'client.json';p.write_text(json.dumps({'web':{'client_id':'wrong-type'}}))
   with self.assertRaises(RuntimeError):GmailSetup(Path(t)).start(p)
 def test_pkce_state_and_callback_storage(self):
  from outreach.auth import GmailSetup
  class Api:
   def request(self,url,method='GET',**kw):self.form=kw['form'];return {'refresh_token':'test-refresh','access_token':'test-access'}
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);(root/'data').mkdir();client=root/'client.json';client.write_text(json.dumps({'installed':{'client_id':'test-id','client_secret':'test-secret'}}));api=Api()
   with patch('outreach.auth.API',return_value=api):
    flow=GmailSetup(root);url=flow.start(client);query=urllib.parse.parse_qs(urllib.parse.urlsplit(url).query);self.assertEqual(query['code_challenge_method'],['S256']);self.assertIn('gmail.send',query['scope'][0]);self.assertNotIn('gmail.modify',query['scope'][0]);redirect=query['redirect_uri'][0]
    try:
     with self.assertRaises(urllib.error.HTTPError):urllib.request.urlopen(redirect+'?state=wrong&code=code',timeout=3)
     self.assertFalse((root/'data/gmail-token.json').exists())
     with urllib.request.urlopen(redirect+'?'+urllib.parse.urlencode({'state':query['state'][0],'code':'test-code'}),timeout=3) as r:self.assertEqual(r.status,200)
     record=json.loads((root/'data/gmail-token.json').read_text());self.assertEqual(record['refresh_token'],'test-refresh');self.assertIn('code_verifier',api.form);self.assertEqual((root/'data/gmail-token.json').stat().st_mode&0o777,0o600)
    finally:flow.server.shutdown();flow.server.server_close()
class DashboardTests(unittest.TestCase):
 def test_keys_are_excluded_from_exports(self):
  from outreach.dashboard import Service
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);(root/'config.json').write_text((ROOT/'config.json').read_text());(root/'.env').write_text('BRAVE_SEARCH_API_KEY=test-secret-1\nHUNTER_API_KEY=test-secret-2\n')
   with patch.dict('os.environ',{},clear=True):
    state=Service(root).state();text=json.dumps(state);self.assertNotIn('test-secret-1',text);self.assertNotIn('test-secret-2',text)
 def test_setting_change_invalidates_approval(self):
  from outreach.dashboard import Service
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);(root/'config.json').write_text((ROOT/'config.json').read_text());service=Service(root)
   with self.assertRaises(GateError):service.action({'action':'settings','settings':{'daily_send_limit':500}})
   service.action({'action':'settings','settings':{'postal_address':'Test Address','postal_address_confirmed':True}});self.assertEqual(Agent(root).config['postal_address'],'Test Address')
if __name__=='__main__':unittest.main()
