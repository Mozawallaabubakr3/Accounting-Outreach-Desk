import argparse,json,sys,time
from pathlib import Path
from .agent import Agent
ROOT=Path(__file__).resolve().parents[1]
def main():
    p=argparse.ArgumentParser(description='Accounting Outreach Agent');p.add_argument('--root',default=str(ROOT));sub=p.add_subparsers(dest='command',required=True)
    sub.add_parser('status');sub.add_parser('run').add_argument('--send',action='store_true');sub.add_parser('sync');sub.add_parser('reconcile')
    x=sub.add_parser('worker');x.add_argument('--interval',type=int,default=900);x.add_argument('--send',action='store_true')
    x=sub.add_parser('serve');x.add_argument('--port',type=int,default=8788)
    x=sub.add_parser('add-firm');x.add_argument('url');x.add_argument('--name',default='');x.add_argument('--city',default='')
    for cmd in ('scrape','enrich','qualify','verify','draft'):
        x=sub.add_parser(cmd);x.add_argument('id',type=int)
    x=sub.add_parser('approve');x.add_argument('id',type=int);x.add_argument('--hash',required=True)
    x=sub.add_parser('send');x.add_argument('id',type=int)
    x=sub.add_parser('suppress');x.add_argument('email');x.add_argument('--reason',default='User opt-out')
    args=p.parse_args();a=Agent(args.root)
    try:
        if args.command=='serve':
            from .dashboard import serve
            serve(a.root,args.port);return
        if args.command=='worker':
            if args.interval<60:raise RuntimeError('Worker interval must be at least 60 seconds')
            while True:
                # Cache prevents repeat discovery calls. Queued drafts are never auto-approved.
                if (a.root/'data/gmail-token.json').exists():
                    try:a.sync_replies();a.reconcile()
                    except RuntimeError as e:a.store.event('worker_inbox_error',{'error':str(e)})
                result=a.run(args.send);print(json.dumps(result),flush=True);time.sleep(args.interval);a=Agent(args.root)
        elif args.command=='status':out=a.status()
        elif args.command=='run':out=a.run(args.send)
        elif args.command=='sync':out=a.sync_replies()
        elif args.command=='reconcile':out=a.reconcile()
        elif args.command=='add-firm':out=a.add_firm(args.url,args.name,args.city,'Manually added')
        elif args.command=='scrape':out=a.scrape(args.id)
        elif args.command=='enrich':out=a.enrich(args.id)
        elif args.command=='qualify':out=a.qualify(args.id)
        elif args.command=='verify':out=a.verify(args.id)
        elif args.command=='draft':out=a.make_draft(args.id)
        elif args.command=='approve':out=a.approve(args.id,args.hash)
        elif args.command=='send':out=a.send(args.id)
        elif args.command=='suppress':out=a.store.suppress(args.email,args.reason)
        print(json.dumps(out,indent=2,ensure_ascii=False))
    except (RuntimeError,ValueError) as e:print(str(e),file=sys.stderr);sys.exit(1)
if __name__=='__main__':main()
