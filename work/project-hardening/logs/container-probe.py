import json,os,pathlib,sqlite3,subprocess,sys
sys.path.insert(0,"/app")
import news_bot as bot
assert os.getuid()==10001 and os.getgid()==10001
status={line.split(':',1)[0]:line.split(':',1)[1].strip() for line in pathlib.Path('/proc/self/status').read_text().splitlines() if line.startswith(('CapInh:','CapPrm:','CapEff:','CapBnd:','CapAmb:','NoNewPrivs:'))}
assert all(status[name]=='0000000000000000' for name in ('CapInh','CapPrm','CapEff','CapBnd','CapAmb')),status
assert status['NoNewPrivs']=='1'
assert not os.access('/app/news_bot.py',os.W_OK)
assert not os.access('/app',os.W_OK)
bot._production_preflight()
bot.init_db()
bot._acquire_singleton_lock()
child=subprocess.run([sys.executable,'-c','import news_bot; news_bot._acquire_singleton_lock()'],capture_output=True,text=True,timeout=10)
assert child.returncode==1 and 'Refusing' in child.stderr,child.stderr
bot._record_heartbeat()
with sqlite3.connect('/data/news.db') as db:
 assert db.execute('select title from processed_news where link=?',('synthetic-ledger-row',)).fetchone()==('original-sentinel',)
 db.execute('insert or ignore into processed_news(link,title,pub_date) values(?,?,?)',('synthetic-write-row','persistent-write','2026-11-03'))
 ledger_rows=db.execute('select count(*) from processed_news').fetchone()[0]
 assert db.execute('pragma integrity_check').fetchone()==('ok',)
for name in ('claude_transcreation','gemini_transcreation','openai_transcreation','openrouter_transcreation'):
 module=__import__(name);assert module._load_prompt()
route=subprocess.run(['ip','route','show','default'],capture_output=True,text=True,check=True).stdout.strip()
assert 'via 172.30.247.1' in route,route
change=subprocess.run(['ip','route','replace','default','via','172.30.247.254'],capture_output=True,text=True)
assert change.returncode!=0
leaks=[str(x) for x in pathlib.Path('/app').rglob('*') if x.is_file() and (x.name.startswith('.env') or any(x.name.endswith(s) for s in ('.db','.db-wal','.db-shm','.sqlite3-journal')))]
assert not leaks,leaks
assert not pathlib.Path('/run/secrets/proxy_ca').exists()
print(json.dumps({'uid':os.getuid(),'gid':os.getgid(),'capabilities':status,'code_writable':False,'production_preflight':'passed','lock_contender_refused':True,'ledger_rows':ledger_rows,'integrity':'ok','prompt_loaders':4,'route':route,'route_change_denied':True,'excluded_state_and_secrets':True},sort_keys=True))
