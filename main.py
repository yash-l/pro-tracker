import os,sys,asyncio,secrets,logging,io,csv,time,aiosqlite,hypercorn.asyncio,pytz,httpx
from telethon import TelegramClient,errors; from telethon.sessions import StringSession
from telethon.tl.types import UserStatusOnline; from quart import Quart,request,redirect,session,Response
from hypercorn.config import Config; from datetime import datetime

I,H,SS,AT=int(os.getenv("API_ID",9497762)),os.getenv("API_HASH","272c77bf080e4a82846b8ff3dc3df0f4"),os.getenv("SESSION_STRING"),os.getenv("AUTO_TARGETS","")
DB,PF,PI,URL='tracker.db','static/profile_pics',10,os.getenv("RENDER_EXTERNAL_URL","http://localhost:5000")
os.makedirs(PF,exist_ok=1);logging.basicConfig(level=20,format='%(asctime)s %(message)s');L,SEM=asyncio.Lock(),asyncio.Semaphore(5)
app=Quart(__name__,static_folder='static');app.secret_key=secrets.token_hex(16)

class S:c,db,ok,run=None,None,0,1
st=S()

async def q(s,a=(),c=0,f="all"):
 if not st.db:return
 async with L:
  try:
   r=await st.db.execute(s,a)
   if c:await st.db.commit()
   if f=="val":x=await r.fetchone();return x[0] if x else None
   return await r.fetchone() if f=="one" else await r.fetchall()
  except Exception as e:logging.error(e)

def fmt(t):return datetime.fromtimestamp(t,pytz.timezone('Asia/Kolkata')).strftime('%I:%M %p' if datetime.now().date()==datetime.fromtimestamp(t).date() else '%d-%b %I:%M') if t else "-"
def dur(s,e):m,s=divmod(int((e or 0)-s),60);h,m=divmod(m,60);return f"{h}h {m}m" if h else f"{m}m {s}s"
async def safe(x):
 async with SEM:
  try:return await x
  except errors.FloodWaitError as e:await asyncio.sleep(e.seconds);return await safe(x)
  except:return

async def boot():
 st.db=await aiosqlite.connect(DB);st.db.row_factory=aiosqlite.Row
 for t in['targets(id INTEGER PRIMARY KEY,user_id INT UNIQUE,name TEXT,status TEXT,last_seen REAL,pic TEXT)','sessions(id INTEGER PRIMARY KEY,user_id INT,status TEXT,start REAL,end REAL,dur TEXT)','app_config(key TEXT PRIMARY KEY,value TEXT)']:await st.db.execute(f'CREATE TABLE IF NOT EXISTS {t}')
 if not await q("SELECT 1 FROM app_config WHERE key='admin_user'",f="val"):await st.db.executemany("INSERT INTO app_config VALUES(?,?)",[('admin_user','admin'),('admin_pass','admin')]);await st.db.commit()
 s=await q("SELECT value FROM app_config WHERE key='tg_session'",f="val") or SS
 if s:
  try:st.c=TelegramClient(StringSession(s),I,H);await st.c.connect();st.ok=await st.c.is_user_authorized()
  except:pass
 if not st.c:st.c=TelegramClient(StringSession(),I,H);await st.c.connect()

async def loop():
 while not st.ok:await asyncio.sleep(2)
 if AT:
  for x in AT.split(','):
   if x.strip():
    try:
     u=x.strip();e=await safe(st.c.get_entity(int(u) if u.lstrip('-').isdigit() else u))
     if e:p=await st.c.download_profile_photo(e,file=PF);await q('INSERT OR IGNORE INTO targets(user_id,name,status,last_seen,pic) VALUES(?,?,?,?,?)',(e.id,getattr(e,'first_name','')or str(u),'...',time.time(),os.path.basename(p)if p else'default.png'),c=1)
    except:pass
 m,lf={},time.time()
 while st.run:
  try:
   if not st.c.is_connected():await st.c.connect()
   ts=await q('SELECT user_id,name FROM targets');ts=ts or []
   for t in ts:
    try:
     u=await safe(st.c.get_entity(t['user_id']));o='online' if isinstance(u.status,UserStatusOnline) else 'offline';n=time.time()
     if o!=m.get(t['user_id']):
      await q('UPDATE targets SET status=?,last_seen=? WHERE user_id=?',(o,n,t['user_id']),c=1)
      if o=='online':await q('INSERT INTO sessions(user_id,status,start) VALUES(?,?,?)',(t['user_id'],'ON',n),c=1);await safe(st.c.send_message('me',f"🟢 <b>{t['name']}</b> ONLINE",parse_mode='html'))
      elif m.get(t['user_id'])=='online':l=await q('SELECT id,start FROM sessions WHERE user_id=? AND end IS NULL ORDER BY id DESC LIMIT 1',(t['user_id'],),f="one");await q('UPDATE sessions SET end=?,dur=? WHERE id=?',(n,dur(l['start'],n),l['id']),c=1) if l else 0
      m[t['user_id']]=o
     elif n-lf>300:await q('UPDATE targets SET last_seen=? WHERE user_id=?',(n,t['user_id']),c=1)
    except:pass
    await asyncio.sleep(0.2)
   if time.time()-lf>300:lf=time.time()
   await asyncio.sleep(PI)
  except:await asyncio.sleep(PI)

async def ping():
 while st.run:
  try:
   async with httpx.AsyncClient() as h:await h.get(URL)
  except:pass
  await asyncio.sleep(600)

PG="<!doctype html><meta name='viewport' content='width=device-width'><style>body{background:#0f172a;color:#fff;font-family:sans-serif;margin:0}a{text-decoration:none}.nav{padding:15px;background:#1e293b;display:flex;justify-content:space-between}.card{background:#1e293b;padding:15px;border-radius:10px;margin:10px 0;display:flex;align-items:center;justify-content:space-between}img{width:40px;height:40px;border-radius:50%;background:#333}.btn{padding:8px;background:#3b82f6;color:#fff;border-radius:5px;border:0}.on{color:#4ade80}</style>"

@app.before_request
def chk():return redirect('/login') if request.path not in['/login','/auth'] and 'u' not in session and not request.path.startswith('/static') else None

@app.route('/login')
async def li():return f"{PG}<div style='padding:50px;text-align:center'><h3>Login</h3><form action='/auth' method='post'><input name='u' placeholder='User'><br><br><input name='p' type='password' placeholder='Pass'><br><br><button class='btn'>Go</button></form>"

@app.route('/auth',methods=['POST'])
async def au():f=await request.form;u,p=await q("SELECT value FROM app_config WHERE key='admin_user'",f="val"),await q("SELECT value FROM app_config WHERE key='admin_pass'",f="val");session['u']=1 if f['u']==u and f['p']==p else None;return redirect('/')

@app.route('/')
async def idx():
 r=await q("SELECT * FROM targets ORDER BY CASE WHEN status='online' THEN 0 ELSE 1 END,name");tg="" if st.ok else "<a href='/con' class='btn' style='background:#f59e0b;display:block;text-align:center'>⚠️ Connect TG</a><br>"
 h="".join([f"<div class='card'><div style='display:flex;gap:10px;align-items:center'><img src='/static/profile_pics/{x['pic']}' onerror=\"this.src='/static/profile_pics/default.png'\"><div><b>{x['name']}</b><div style='font-size:.8em;color:#aaa'>{fmt(x['last_seen'])}</div></div></div><div style='text-align:right'><div class='{'on' if x['status']=='online' else ''}'>● {x['status'].upper()}</div><a href='/del/{x['user_id']}' style='color:#ef4444;font-size:.8em'>Del</a></div></div>" for x in r])
 return f"{PG}<meta http-equiv='refresh' content='{PI}'><div class='nav'><b>Tracker</b><a href='/out' style='color:#ef4444'>Exit</a></div><div style='padding:20px;max-width:600px;margin:auto'>{tg}<form action='/add' method='post' style='display:flex;gap:5px'><input name='u' placeholder='User/ID' style='flex:1'><button class='btn'>+</button></form>{h}<br><center><a href='/csv' style='color:#888'>Download Logs</a></center></div>"

@app.route('/con')
async def con():return f"{PG}<div style='padding:50px;text-align:center'><h3>TG Connect</h3><form action='/otp' method='post'><input name='p' placeholder='+91..'><button class='btn'>OTP</button></form>"

@app.route('/otp',methods=['POST'])
async def otp():
 try:c=TelegramClient(StringSession(),I,H);await c.connect();p=(await request.form)['p'];r=await c.send_code_request(p);return f"{PG}<div style='padding:50px;text-align:center'><form action='/votp' method='post'><input type='hidden' name='s' value='{StringSession.save(c.session)}'><input type='hidden' name='h' value='{r.phone_code_hash}'><input type='hidden' name='p' value='{p}'><input name='c' placeholder='12345'><button class='btn'>Verify</button></form>"
 except Exception as e:return f"Err: {e} <a href='/con'>Back</a>"

@app.route('/votp',methods=['POST'])
async def votp():
 try:
  f=await request.form;c=TelegramClient(StringSession(f['s']),I,H);await c.connect();await c.sign_in(f['p'],f['c'],phone_code_hash=f['h']);s=StringSession.save(c.session)
  print(f"\n{'='*40}\n✅ SESSION STRING:\n{s}\n{'='*40}\n");await q("INSERT OR REPLACE INTO app_config VALUES('tg_session',?)",(s,),c=1);await c.disconnect();await boot();return redirect('/')
 except Exception as e:return f"Err: {e} <a href='/con'>Retry</a>"

@app.route('/add',methods=['POST'])
async def add():
 try:u=(await request.form)['u'];e=await safe(st.c.get_entity(int(u) if u.lstrip('-').isdigit() else u));p=await st.c.download_profile_photo(e,file=PF)if e else'';await q('INSERT OR IGNORE INTO targets(user_id,name,status,last_seen,pic) VALUES(?,?,?,?,?)',(e.id,getattr(e,'first_name','')or str(u),'...',time.time(),os.path.basename(p)if p else'default.png'),c=1)
 except:pass
 return redirect('/')

@app.route('/del/<int:u>')
async def dl(u):await q('DELETE FROM targets WHERE user_id=?',(u,),c=1);return redirect('/')

@app.route('/csv')
async def dl_csv():r=await q('SELECT * FROM sessions ORDER BY id DESC');s=io.StringIO();w=csv.writer(s);w.writerow(['ID','UID','St','Start','End','Dur']);w.writerows([[x[0],x[1],x[2],fmt(x[3]),fmt(x[4]),x[5]]for x in r]);return Response(s.getvalue(),mimetype='text/csv',headers={"Content-Disposition":"attachment;filename=log.csv"})

@app.route('/out')
async def out():session.clear();return redirect('/login')

@app.before_serving
async def up():await boot();app.add_background_task(loop);app.add_background_task(ping)

@app.after_serving
async def down():st.run=0;await st.c.disconnect() if st.c else 0;await st.db.close() if st.db else 0

if __name__=='__main__':c=Config();c.bind=[f"0.0.0.0:{int(os.getenv('PORT',5000))}"];asyncio.run(hypercorn.asyncio.serve(app,c))
 
