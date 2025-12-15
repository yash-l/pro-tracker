import os, sys, asyncio, secrets, logging, io, csv, time, glob

# --- 1. ROBUST AUTO-INSTALLER ---
try:
    from telethon import TelegramClient, errors
    from telethon.sessions import StringSession
    from telethon.tl.types import UserStatusOnline
    from quart import Quart, render_template_string, request, redirect, session, Response
    import aiosqlite
    import python_socks
    import hypercorn.asyncio
    from hypercorn.config import Config
    import pytz
    import httpx
except ImportError:
    print("🛡️ Installing Final V28 dependencies...")
    os.system("pip install telethon quart aiosqlite python-socks hypercorn pytz httpx")
    os.execv(sys.executable, ['python'] + sys.argv)

from datetime import datetime

# --- 2. CONFIGURATION ---
API_ID = int(os.environ.get("API_ID", 9497762))
API_HASH = os.environ.get("API_HASH", "272c77bf080e4a82846b8ff3dc3df0f4")
SESSION_STRING = os.environ.get("SESSION_STRING", None)

DB_FILE = 'tracker.db'
PIC_FOLDER = 'static/profile_pics'
POLL_INTERVAL = 10  
HEARTBEAT_FLUSH_RATE = 300 
SELF_PING_URL = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:5000")

os.makedirs(PIC_FOLDER, exist_ok=True)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("ProTracker")

# --- 3. GLOBAL STATE & APP ---
app = Quart(__name__, static_folder='static')
app.secret_key = secrets.token_hex(16)

# Locks & Semaphores
db_lock = asyncio.Lock()
api_sem = asyncio.Semaphore(5)

class AppState:
    def __init__(self):
        self.client = None
        self.db = None
        self.logged_in = False
        self.status = "Initializing..."
        self.temp_phone = None 
        self.temp_hash = None
        self.memory = {} 
        self.running = True

state = AppState()

# --- 4. PERSISTENT DATABASE ---
async def init_db():
    state.db = await aiosqlite.connect(DB_FILE)
    state.db.row_factory = aiosqlite.Row
    # Using REAL (float) for timestamps to ensure perfect math
    await state.db.execute('CREATE TABLE IF NOT EXISTS targets (id INTEGER PRIMARY KEY, user_id INT UNIQUE, name TEXT, status TEXT, last_seen REAL, pic TEXT)')
    await state.db.execute('CREATE TABLE IF NOT EXISTS sessions (id INTEGER PRIMARY KEY, user_id INT, status TEXT, start REAL, end REAL, dur TEXT)')
    await state.db.commit()
    logger.info("✅ Database initialized")

async def db_query(sql, args=(), commit=False, fetch="all"):
    if not state.db: return None
    async with db_lock:
        try:
            cursor = await state.db.execute(sql, args)
            if commit: await state.db.commit()
            if fetch == "one": return await cursor.fetchone()
            if fetch == "all": return await cursor.fetchall()
        except Exception as e:
            logger.error(f"DB Error: {e}")
            return None

# --- 5. HELPERS ---
def format_ts(ts):
    """Converts Unix Timestamp to Readable 12-hr format"""
    if not ts: return "-"
    try:
        dt = datetime.fromtimestamp(ts, pytz.timezone('Asia/Kolkata'))
        now = datetime.now(pytz.timezone('Asia/Kolkata'))
        # If today, show time only. If older, show date + time
        if dt.date() == now.date():
            return dt.strftime('%I:%M %p')
        return dt.strftime('%d-%b %I:%M %p')
    except: return "-"

def calc_duration(start_ts, end_ts):
    """Calculates duration from Unix Timestamps"""
    try:
        if not start_ts or not end_ts: return "-"
        seconds = int(end_ts - start_ts)
        m, s = divmod(seconds, 60)
        h, m = divmod(m, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"
    except Exception as e:
        logger.error(f"Duration Calc Error: {e}")
        return "-"

async def cleanup_pics():
    """Disk Janitor: Deletes pics modified > 3 days ago"""
    try:
        now = time.time()
        for f in glob.glob(os.path.join(PIC_FOLDER, "*")):
            if os.path.basename(f) == "default.png": continue
            if os.stat(f).st_mtime < now - (3 * 86400):
                os.remove(f)
                logger.info(f"🧹 Cleaned up old pic: {f}")
    except Exception as e:
        logger.warning(f"Cleanup Error: {e}")

async def safe_api_call(coroutine):
    async with api_sem:
        try:
            return await coroutine
        except errors.FloodWaitError as e:
            logger.warning(f"🌊 FloodWait: Sleeping {e.seconds}s...")
            await asyncio.sleep(e.seconds)
            return await safe_api_call(coroutine)
        except Exception as e:
            logger.error(f"API Error: {e}")
            return None

async def download_pic(user):
    try:
        if state.client and state.client.is_connected():
            p = await state.client.download_profile_photo(user, file=PIC_FOLDER)
            if p: return os.path.basename(p)
    except: pass
    return "default.png"

# --- 6. CORE ENGINE ---
async def init_client():
    if SESSION_STRING:
        try:
            state.client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
            await state.client.connect()
            if await state.client.is_user_authorized():
                state.logged_in = True
                state.status = "Cloud Session Active"
                print(f"\n{'='*50}\n✅ LOGGED IN VIA ENV VAR\n{'='*50}\n")
        except Exception as e: logger.error(f"Env Login Failed: {e}")
    
    if not state.client:
        state.client = TelegramClient(StringSession(), API_ID, API_HASH)
        await state.client.connect()

async def tracker_loop():
    while not state.logged_in:
        state.status = "Waiting for Login..."
        await asyncio.sleep(2)
    
    # Load Memory
    initial_rows = await db_query('SELECT user_id, status FROM targets')
    if initial_rows:
        for r in initial_rows: state.memory[r['user_id']] = r['status']
    
    # Run Cleanup Once
    await cleanup_pics()
    
    state.status = f"Scanning ({POLL_INTERVAL}s)"
    logger.info("🚀 Tracker V28 Started")
    last_flush = time.time()

    while state.running:
        try:
            if not state.client or not state.client.is_connected():
                state.status = "Reconnecting..."
                await state.client.connect()
                await asyncio.sleep(5); continue

            targets = await db_query('SELECT user_id, name FROM targets')
            if not targets: await asyncio.sleep(5); continue

            for t in targets:
                uid, name = t['user_id'], t['name']
                try:
                    u = await safe_api_call(state.client.get_entity(uid))
                    if not u: continue 
                    
                    is_online = isinstance(u.status, UserStatusOnline)
                    stat = 'online' if is_online else 'offline'
                    now_ts = time.time()

                    if stat != state.memory.get(uid):
                        await db_query('UPDATE targets SET status=?, last_seen=? WHERE user_id=?', (stat, now_ts, uid), commit=True)
                        
                        if stat == 'online':
                            await db_query('INSERT INTO sessions (user_id, status, start) VALUES (?,?,?)', (uid, 'ONLINE', now_ts), commit=True)
                            await safe_api_call(state.client.send_message('me', f"🟢 <b>{name}</b> is ONLINE", parse_mode='html'))
                        
                        elif state.memory.get(uid) == 'online':
                            last_sess = await db_query('SELECT id, start FROM sessions WHERE user_id=? AND end IS NULL ORDER BY id DESC LIMIT 1', (uid,), fetch="one")
                            if last_sess:
                                dur = calc_duration(last_sess['start'], now_ts)
                                await db_query('UPDATE sessions SET end=?, dur=? WHERE id=?', (now_ts, dur, last_sess['id']), commit=True)
                        
                        state.memory[uid] = stat
                    
                    elif time.time() - last_flush > HEARTBEAT_FLUSH_RATE:
                        await db_query('UPDATE targets SET last_seen=? WHERE user_id=?', (now_ts, uid), commit=True)
                        
                except Exception as e: 
                    logger.debug(f"User Check Fail ({uid}): {e}")
                
                await asyncio.sleep(0.1)
            
            if time.time() - last_flush > HEARTBEAT_FLUSH_RATE: last_flush = time.time()
            await asyncio.sleep(POLL_INTERVAL)
            
        except Exception as e:
            logger.error(f"Loop Crash: {e}")
            await asyncio.sleep(POLL_INTERVAL)

async def keep_alive():
    """Exponential Backoff Ping"""
    retries = 0
    while state.running:
        await asyncio.sleep(600)
        try:
            async with httpx.AsyncClient() as h: 
                await h.get(SELF_PING_URL, timeout=10)
            retries = 0
        except: 
            wait = min(60, 2 ** retries)
            await asyncio.sleep(wait)
            retries += 1

# --- 7. UI ---
TAILWIND = '<script src="https://cdn.tailwindcss.com"></script><style>body{background:#0f172a;font-family:sans-serif}img{background:#1e293b}</style>'

P_LOGIN = f"""<!DOCTYPE html><html class="dark"><head><meta name="viewport" content="width=device-width">{TAILWIND}</head><body class="flex items-center justify-center min-h-screen text-white"><div class="bg-slate-800 p-8 rounded-xl border border-slate-700 w-96 shadow-2xl"><h2 class="text-2xl font-bold mb-2 text-cyan-400">⚡ V28 Pro</h2><form action="/send_otp" method="post" class="space-y-4"><input name="ph" placeholder="+91..." class="w-full bg-slate-900 border border-slate-600 rounded p-3 outline-none focus:border-cyan-500" required><button class="w-full bg-cyan-600 p-3 rounded font-bold hover:brightness-110">Get Code</button></form></div></body></html>"""

P_CODE = f"""<!DOCTYPE html><html class="dark"><head><meta name="viewport" content="width=device-width">{TAILWIND}</head><body class="flex items-center justify-center min-h-screen text-white"><div class="bg-slate-800 p-8 rounded-xl border border-slate-700 w-96 shadow-2xl"><h2 class="text-xl font-bold mb-2">🔐 Verify</h2><p class="text-xs text-slate-400 mb-4">Check Telegram App</p><form action="/verify_otp" method="post" class="space-y-4"><input name="code" placeholder="12345" class="w-full bg-slate-900 border border-slate-600 rounded p-3 text-center tracking-widest outline-none focus:border-green-500" required><button class="w-full bg-green-600 p-3 rounded font-bold">Login</button></form></div></body></html>"""

P_PASS = f"""<!DOCTYPE html><html class="dark"><head><meta name="viewport" content="width=device-width">{TAILWIND}</head><body class="flex items-center justify-center min-h-screen text-white"><div class="bg-slate-800 p-8 rounded-xl border border-slate-700 w-96 shadow-2xl"><h2 class="text-xl font-bold mb-2 text-red-400">🔐 2FA Required</h2><p class="text-xs text-slate-400 mb-4">Enter Cloud Password</p><form action="/verify_pass" method="post" class="space-y-4"><input type="password" name="pw" placeholder="Password..." class="w-full bg-slate-900 border border-slate-600 rounded p-3 outline-none focus:border-red-500" required><button class="w-full bg-red-600 p-3 rounded font-bold">Unlock</button></form></div></body></html>"""

P_DASH = f"""<!DOCTYPE html><html class="dark"><head><meta name="viewport" content="width=device-width"><meta http-equiv="refresh" content="10">{TAILWIND}</head><body class="text-slate-200 pb-20"><nav class="sticky top-0 z-50 bg-slate-900/95 backdrop-blur border-b border-slate-800 p-4 flex justify-between items-center shadow-lg"><div class="font-bold text-lg text-cyan-400">ProTracker <span class="text-[10px] text-slate-500 border border-slate-700 rounded px-1">{{STATUS}}</span></div><a href="/logout" class="text-xs text-red-400 font-bold hover:text-red-300">EXIT</a></nav><div class="max-w-md mx-auto p-4 space-y-4"><div class="bg-slate-800 rounded-lg p-3 border border-slate-700 shadow flex gap-2"><form action="/add" method="post" class="flex-1 flex gap-2"><input name="u" placeholder="Username / ID..." class="flex-1 bg-slate-900 border border-slate-600 rounded p-2 text-sm outline-none focus:border-cyan-500"><button class="bg-cyan-600 px-4 rounded font-bold text-white">+</button></form></div><div class="grid gap-3">{{ROWS}}</div><div class="text-center pt-6 space-y-2"><a href="/download_db" class="text-xs text-slate-500 underline hover:text-cyan-400 block">Download Logs</a></div></div></body></html>"""

@app.before_request
async def guard():
    if request.path.startswith('/static'): return
    if not state.logged_in and request.path not in ['/tg_login', '/send_otp', '/verify_otp', '/verify_pass']:
        return redirect('/tg_login')

@app.route('/tg_login')
async def tg_login(): return await render_template_string(P_LOGIN)

@app.route('/send_otp', methods=['POST'])
async def send_otp():
    try:
        state.client = TelegramClient(StringSession(), API_ID, API_HASH)
        await state.client.connect()
        ph = (await request.form)['ph']
        state.temp_phone = ph
        state.temp_hash = (await state.client.send_code_request(ph)).phone_code_hash
        return await render_template_string(P_CODE)
    except Exception as e: return f"Error: {e} <a href='/tg_login'>Back</a>"

@app.route('/verify_otp', methods=['POST'])
async def verify_otp():
    try:
        await state.client.sign_in(state.temp_phone, (await request.form)['code'], phone_code_hash=state.temp_hash)
        state.logged_in = True
        print(f"\n✅ SESSION STRING:\n{StringSession.save(state.client.session)}\n")
        return redirect('/')
    except errors.SessionPasswordNeededError: return await render_template_string(P_PASS)
    except Exception as e: return f"Error: {e} <a href='/tg_login'>Back</a>"

@app.route('/verify_pass', methods=['POST'])
async def verify_pass():
    try:
        await state.client.sign_in(password=(await request.form)['pw'])
        state.logged_in = True
        print(f"\n✅ SESSION STRING:\n{StringSession.save(state.client.session)}\n")
        return redirect('/')
    except Exception as e: return f"Error: {e} <a href='/tg_login'>Back</a>"

@app.route('/')
async def index():
    rows = await db_query("SELECT * FROM targets ORDER BY CASE WHEN status='online' THEN 0 ELSE 1 END, name ASC")
    html = ""
    for r in rows:
        color = "text-green-400" if r['status'] == 'online' else "text-slate-500"
        border = "border-green-500/40" if r['status'] == 'online' else "border-slate-700"
        bg = "bg-green-500/10" if r['status'] == 'online' else "bg-slate-800"
        
        last_seen_display = format_ts(r['last_seen'])
        
        html += f"""<div class="{bg} p-3 rounded-lg border {border} flex items-center justify-between transition-all"><div class="flex items-center gap-3"><img src="/static/profile_pics/{r['pic']}" onerror="this.src='/static/profile_pics/default.png'; this.onerror=null;" class="w-10 h-10 rounded-full bg-slate-900 object-cover border border-slate-700"><div><div class="font-bold text-sm text-white">{r['name']}</div><div class="text-[10px] text-slate-400">{last_seen_display}</div></div></div><div class="flex items-center gap-3"><div class="font-bold text-[10px] uppercase {color}">{r['status']}</div><a href="/delete/{r['user_id']}" class="text-slate-600 hover:text-red-500 px-2">🗑️</a></div></div>"""
    return await render_template_string(P_DASH.replace("{{ROWS}}", html if html else "<div class='text-center text-slate-500 text-sm'>No targets.</div>").replace("{{STATUS}}", state.status))

@app.route('/add', methods=['POST'])
async def add():
    try:
        u = (await request.form)['u']
        e = None
        try:
            e = await state.client.get_entity(int(u) if u.lstrip('-').isdigit() else u)
        except: pass
            
        if e:
            pic = await download_pic(e)
            name = getattr(e, 'first_name', '') or getattr(e, 'username', '') or u
            if getattr(e, 'last_name', None): name += f" {e.last_name}"
            # Insert with timestamp
            await db_query('INSERT OR IGNORE INTO targets (user_id, name, status, last_seen, pic) VALUES (?,?,?,?,?)', (e.id, name, '...', time.time(), pic), commit=True)
    except: pass
    return redirect('/')

@app.route('/delete/<int:uid>')
async def delete(uid):
    await db_query('DELETE FROM targets WHERE user_id = ?', (uid,), commit=True)
    return redirect('/')

@app.route('/download_db')
async def download_db():
    rows = await db_query('SELECT * FROM sessions ORDER BY id DESC')
    clean_rows = []
    for r in rows:
        r_list = list(r)
        r_list[3] = format_ts(r_list[3]) # Start
        r_list[4] = format_ts(r_list[4]) # End
        clean_rows.append(tuple(r_list))
        
    si = io.StringIO(); cw = csv.writer(si)
    cw.writerow(['ID', 'User ID', 'Status', 'Start Time', 'End Time', 'Duration'])
    cw.writerows(clean_rows)
    return Response(si.getvalue(), mimetype='text/csv', headers={"Content-Disposition": "attachment; filename=logs.csv"})

@app.route('/logout')
async def logout():
    session.clear(); state.logged_in = False
    if state.client: await state.client.disconnect()
    state.client = None
    return redirect('/tg_login')

@app.before_serving
async def startup():
    await init_db()
    await init_client()
    app.add_background_task(tracker_loop)
    app.add_background_task(keep_alive)

@app.after_serving
async def shutdown():
    print("🛑 Shutting down...")
    state.running = False
    if state.client: await state.client.disconnect()
    if state.db: await state.db.close()

if __name__ == '__main__':
    # PROD SERVER: Hypercorn Direct Launch
    config = Config()
    config.bind = [f"0.0.0.0:{int(os.environ.get('PORT', 5000))}"]
    asyncio.run(hypercorn.asyncio.serve(app, config))
           
