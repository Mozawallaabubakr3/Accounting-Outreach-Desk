import ssl, os
from pathlib import Path
import json, time, urllib.request, urllib.error, urllib.parse, socket, ipaddress
from html.parser import HTMLParser
from urllib.robotparser import RobotFileParser

AGENT='AbuPortfolioResearch/1.0'
def tls_context():
    bundle=os.getenv('OUTREACH_CA_BUNDLE') or str(Path(__file__).resolve().parents[1]/'assets/cacert.pem')
    context=ssl.create_default_context()
    if Path(bundle).is_file():context.load_verify_locations(cafile=bundle)
    return context
class NetworkError(RuntimeError):pass

def public_url(url):
    p=urllib.parse.urlsplit(url)
    if p.scheme not in ('https','http') or not p.hostname or p.username or p.password or p.port not in (None,80,443):raise NetworkError('Only public HTTP(S) websites are supported')
    try: addresses=socket.getaddrinfo(p.hostname,p.port or (443 if p.scheme=='https' else 80),type=socket.SOCK_STREAM)
    except OSError:raise NetworkError('Website DNS lookup failed') from None
    if any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):raise NetworkError('Private or local network addresses are blocked')
    return url

class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        public_url(newurl)
        return super().redirect_request(req,fp,code,msg,headers,newurl)

class Page(HTMLParser):
    def __init__(self):super().__init__(convert_charrefs=True);self.parts=[];self.links=[];self.skip=0;self.title='';self.in_title=False
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag in ('script','style','noscript'):self.skip+=1
        if tag=='title':self.in_title=True
        if tag=='a' and a.get('href'):self.links.append(a['href'])
        if tag in ('div','p','li','h1','h2','h3','h4','section','br'):self.parts.append('\n')
    def handle_endtag(self,tag):
        if tag in ('script','style','noscript') and self.skip:self.skip-=1
        if tag=='title':self.in_title=False
        if tag in ('div','p','li','h1','h2','h3','h4','section'):self.parts.append('\n')
    def handle_data(self,data):
        if not self.skip:
            self.parts.append(data+' ')
            if self.in_title:self.title+=data
    def result(self,url):
        lines=[' '.join(x.split()) for x in ''.join(self.parts).splitlines()];text='\n'.join(x for x in lines if x)
        return {'url':url,'title':self.title[:250],'text':text[:70000],'links':self.links[:500]}

class Network:
    def __init__(self,store,config):self.store=store;self.config=config;self.last={};self.opener=urllib.request.build_opener(urllib.request.HTTPSHandler(context=tls_context()),SafeRedirect())
    def raw(self,url,robots=False):
        public_url(url);host=urllib.parse.urlsplit(url).hostname
        wait=1-(time.monotonic()-self.last.get(host,0))
        if wait>0:time.sleep(wait)
        self.store.reserve('website_requests',self.config['website_requests_per_day']);self.last[host]=time.monotonic()
        req=urllib.request.Request(url,headers={'User-Agent':AGENT,'Accept':'text/html,text/plain;q=0.8'})
        try:
            with self.opener.open(req,timeout=20) as r:
                content_type=r.headers.get('Content-Type','')
                if not robots and not any(x in content_type for x in ('text/html','text/plain','application/xhtml')):raise NetworkError('Unsupported content type')
                raw=r.read(2_000_001)
                if len(raw)>2_000_000:raise NetworkError('Page exceeds research size limit')
                return raw.decode(r.headers.get_content_charset() or 'utf-8',errors='replace'),r.geturl()
        except urllib.error.HTTPError as e:
            if robots and e.code in (404,410):return '',url
            raise NetworkError(f'Website returned HTTP {e.code}') from None
        except (OSError,urllib.error.URLError):raise NetworkError('Website request failed or timed out') from None
    def allowed(self,url):
        p=urllib.parse.urlsplit(url);rurl=urllib.parse.urlunsplit((p.scheme,p.netloc,'/robots.txt','',''))
        cached=self.store.cached(rurl,24)
        if cached is None:
            txt,_=self.raw(rurl,robots=True);cached={'text':txt};self.store.cache(rurl,cached)
        rp=RobotFileParser();rp.parse(cached['text'].splitlines());return rp.can_fetch(AGENT,url)
    def page(self,url):
        cached=self.store.cached(url,168)
        if cached is not None:return cached
        if not self.allowed(url):raise NetworkError('Website disallows this research page')
        text,final=self.raw(url);parser=Page();parser.feed(text);result=parser.result(final);self.store.cache(url,result);return result

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise NetworkError('Provider redirect blocked to protect credentials')

class API:
    """Fixed provider endpoints. Errors never include credential-bearing URLs or bodies."""
    def request(self,url,method='GET',headers=None,data=None,form=None):
        payload=json.dumps(data).encode() if data is not None else urllib.parse.urlencode(form).encode() if form is not None else None
        hs={'Accept':'application/json',**(headers or {})}
        if payload:hs['Content-Type']='application/x-www-form-urlencoded' if form is not None else 'application/json'
        req=urllib.request.Request(url,data=payload,headers=hs,method=method)
        try:
            with urllib.request.build_opener(urllib.request.HTTPSHandler(context=tls_context()),NoRedirect()).open(req,timeout=30) as r:return json.loads(r.read(5_000_000))
        except urllib.error.HTTPError as e:raise NetworkError(f'Provider returned HTTP {e.code}; check account and quotas') from None
        except (OSError,ValueError):raise NetworkError('Provider request failed; result may be uncertain for a send') from None
