import contextlib, datetime as dt, json, sqlite3, hashlib, os
from zoneinfo import ZoneInfo

def now(): return dt.datetime.now(dt.timezone.utc).isoformat()
def day(): return dt.datetime.now(ZoneInfo('America/New_York')).date().isoformat()
def digest(value): return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()

class Store:
    def __init__(self,path):
        self.path=str(path)
        with self.db() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS firms(id INTEGER PRIMARY KEY,domain TEXT UNIQUE NOT NULL,url TEXT NOT NULL,name TEXT NOT NULL DEFAULT '',city TEXT NOT NULL DEFAULT '',fit TEXT NOT NULL DEFAULT 'unreviewed',source TEXT NOT NULL DEFAULT '',evidence TEXT NOT NULL DEFAULT '',updated TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS people(id INTEGER PRIMARY KEY,firm_id INTEGER NOT NULL REFERENCES firms(id),name TEXT NOT NULL,role TEXT NOT NULL,source TEXT NOT NULL,evidence TEXT NOT NULL,UNIQUE(firm_id,name,role));
            CREATE TABLE IF NOT EXISTS contacts(id INTEGER PRIMARY KEY,firm_id INTEGER NOT NULL REFERENCES firms(id),email TEXT UNIQUE NOT NULL,name TEXT NOT NULL DEFAULT '',role TEXT NOT NULL DEFAULT '',source TEXT NOT NULL DEFAULT '',evidence TEXT NOT NULL DEFAULT '',identity_reviewed INTEGER NOT NULL DEFAULT 0,verification TEXT NOT NULL DEFAULT 'unverified',verified_at TEXT,selected INTEGER NOT NULL DEFAULT 0);
            CREATE UNIQUE INDEX IF NOT EXISTS one_selected_contact ON contacts(firm_id) WHERE selected=1;
            CREATE TABLE IF NOT EXISTS drafts(id INTEGER PRIMARY KEY,contact_id INTEGER NOT NULL REFERENCES contacts(id),kind TEXT NOT NULL,subject TEXT NOT NULL,body TEXT NOT NULL,attachment_hash TEXT NOT NULL DEFAULT '',hash TEXT NOT NULL,approved_hash TEXT,status TEXT NOT NULL DEFAULT 'draft',created TEXT NOT NULL,UNIQUE(contact_id,kind));
            CREATE TABLE IF NOT EXISTS deliveries(id INTEGER PRIMARY KEY,draft_id INTEGER NOT NULL UNIQUE REFERENCES drafts(id),message_key TEXT NOT NULL UNIQUE,provider_id TEXT,thread_id TEXT,status TEXT NOT NULL,created TEXT NOT NULL,sent_at TEXT,error TEXT);
            CREATE TABLE IF NOT EXISTS replies(id TEXT PRIMARY KEY,contact_id INTEGER NOT NULL REFERENCES contacts(id),kind TEXT NOT NULL,excerpt TEXT NOT NULL,received TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS suppression(email TEXT PRIMARY KEY,reason TEXT NOT NULL,created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,data TEXT NOT NULL,created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS cache(url TEXT PRIMARY KEY,payload TEXT NOT NULL,created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS usage(day TEXT NOT NULL,kind TEXT NOT NULL,count INTEGER NOT NULL,PRIMARY KEY(day,kind));
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            ''')
        os.chmod(self.path,0o600)
    @contextlib.contextmanager
    def db(self):
        c=sqlite3.connect(self.path,timeout=30);c.row_factory=sqlite3.Row
        c.execute('PRAGMA foreign_keys=ON');c.execute('PRAGMA busy_timeout=30000')
        try: yield c;c.commit()
        except: c.rollback();raise
        finally:c.close()
    def rows(self,q,args=()):
        with self.db() as c:return [dict(x) for x in c.execute(q,args)]
    def one(self,q,args=()):
        rows=self.rows(q,args);return rows[0] if rows else None
    def execute(self,q,args=()):
        with self.db() as c:return c.execute(q,args).lastrowid
    def event(self,kind,data):self.execute('INSERT INTO events(kind,data,created) VALUES(?,?,?)',(kind,json.dumps(data),now()))
    def reserve(self,kind,limit):
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE');key=day()
            row=c.execute('SELECT count FROM usage WHERE day=? AND kind=?',(key,kind)).fetchone()
            if row and row[0]>=limit:raise RuntimeError(f'Daily {kind} limit reached')
            c.execute('INSERT INTO usage VALUES(?,?,1) ON CONFLICT(day,kind) DO UPDATE SET count=count+1',(key,kind))
    def suppress(self,email,reason):
        email=email.strip().lower()
        with self.db() as c:
            c.execute('INSERT OR REPLACE INTO suppression VALUES(?,?,?)',(email,reason,now()))
            c.execute("UPDATE drafts SET status='suppressed',approved_hash=NULL WHERE contact_id IN (SELECT id FROM contacts WHERE email=?) AND status IN ('draft','approved')",(email,))
        self.event('suppressed',{'email':email,'reason':reason})
    def cached(self,url,hours):
        r=self.one('SELECT * FROM cache WHERE url=?',(url,))
        if r and dt.datetime.fromisoformat(r['created'])>dt.datetime.now(dt.timezone.utc)-dt.timedelta(hours=hours):return json.loads(r['payload'])
    def cache(self,url,value):self.execute('INSERT OR REPLACE INTO cache VALUES(?,?,?)',(url,json.dumps(value),now()))
