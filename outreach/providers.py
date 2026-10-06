import base64, datetime as dt, json, time, urllib.parse
from email import policy
from email.parser import BytesParser
from .net import API
from .store import now

class Brave:
    def __init__(self,store,config,key,api=None):self.store=store;self.config=config;self.key=key;self.api=api or API();self.last=0
    def search(self,query):
        if not self.key:raise RuntimeError('Add BRAVE_SEARCH_API_KEY locally')
        cache='brave:'+query;old=self.store.cached(cache,168)
        if old is not None:return old
        pause=1.1-(time.monotonic()-self.last)
        if pause>0:time.sleep(pause)
        self.last=time.monotonic()
        self.store.reserve('search_requests',self.config['search_requests_per_day'])
        data=self.api.request('https://api.search.brave.com/res/v1/web/search?'+urllib.parse.urlencode({'q':query,'count':20,'country':'US','search_lang':'en'}),headers={'X-Subscription-Token':self.key})
        result=[{'url':r['url'],'title':r.get('title',''),'description':r.get('description','')} for r in data.get('web',{}).get('results',[])]
        self.store.cache(cache,result);return result

class Hunter:
    def __init__(self,store,config,key,api=None):self.store=store;self.config=config;self.key=key;self.api=api or API()
    def call(self,endpoint,params,cache_hours=168):
        if not self.key:raise RuntimeError('Add HUNTER_API_KEY locally')
        cache='hunter:'+endpoint+':'+json.dumps(params,sort_keys=True);old=self.store.cached(cache,cache_hours)
        if old is not None:return old
        self.store.reserve('hunter_requests',self.config['hunter_requests_per_day'])
        data=self.api.request('https://api.hunter.io/v2/'+endpoint+'?'+urllib.parse.urlencode(params),headers={'Authorization':'Bearer '+self.key})
        if data.get('errors'):raise RuntimeError('Hunter returned an error; check its dashboard')
        result=data['data'];self.store.cache(cache,result);return result
    def domain(self,domain):return self.call('domain-search',{'domain':domain,'limit':10}).get('emails',[])
    def find(self,domain,first,last):return self.call('email-finder',{'domain':domain,'first_name':first,'last_name':last})
    def verify(self,email):return self.call('email-verifier',{'email':email},24)

class Gmail:
    def __init__(self,root,api=None):self.root=root;self.api=api or API();self.access=None;self.expires=0
    @property
    def token_path(self):return self.root/'data/gmail-token.json'
    def credentials(self):
        if not self.token_path.exists():raise RuntimeError('Connect Gmail using the local Gmail setup first')
        return json.loads(self.token_path.read_text())
    def token(self):
        if self.access and time.time()<self.expires-60:return self.access
        c=self.credentials();r=self.api.request('https://oauth2.googleapis.com/token','POST',form={'client_id':c['client_id'],'client_secret':c.get('client_secret',''),'refresh_token':c['refresh_token'],'grant_type':'refresh_token'})
        self.access=r['access_token'];self.expires=time.time()+r.get('expires_in',3600);return self.access
    def call(self,path,method='GET',data=None):return self.api.request('https://gmail.googleapis.com/gmail/v1/users/me/'+path,method,headers={'Authorization':'Bearer '+self.token()},data=data)
    def profile(self):return self.call('profile')
    def send(self,msg,thread=None):
        body={'raw':base64.urlsafe_b64encode(msg.as_bytes()).decode()}
        if thread:body['threadId']=thread
        return self.call('messages/send','POST',body)
    def find_sent(self,message_key):
        q='in:sent rfc822msgid:'+message_key.strip('<>');r=self.call('messages?'+urllib.parse.urlencode({'q':q,'maxResults':10}))
        return r.get('messages',[])
    def thread(self,thread_id):return self.call('threads/'+urllib.parse.quote(thread_id,safe='')+'?format=full')
    def recent_messages(self,query,max_pages=10):
        token=None;out=[]
        for _ in range(max_pages):
            p={'q':query,'maxResults':100}
            if token:p['pageToken']=token
            r=self.call('messages?'+urllib.parse.urlencode(p));out.extend(r.get('messages',[]));token=r.get('nextPageToken')
            if not token:return out
        raise RuntimeError('Mailbox sync exceeded its bounded page limit; narrow the date range before sending')
    def message(self,message_id):return self.call('messages/'+urllib.parse.quote(message_id,safe='')+'?format=full')

def headers(message):return {x['name'].lower():x['value'] for x in message.get('payload',{}).get('headers',[])}
def text_parts(payload):
    out=[];typ=payload.get('mimeType','')
    if typ=='text/plain' and payload.get('body',{}).get('data'):
        d=payload['body']['data'];out.append(base64.urlsafe_b64decode(d+'='*((4-len(d)%4)%4)).decode(errors='replace'))
    for p in payload.get('parts',[]):out.extend(text_parts(p))
    return out

def inbound_text(message):return '\n'.join(text_parts(message.get('payload',{}))) or message.get('snippet','')
