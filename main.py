import os, sys, asyncio, secrets, logging, io, csv, time, glob, json

# --- 1. AUTO-INSTALLER ---
try:
    from telethon import TelegramClient, errors
    from telethon.sessions import StringSession
    from telethon.tl.types import UserStatusOnline, InputPhoneContact
    from telethon.tl.functions.contacts import ImportContactsRequest
    from quart import Quart, render_template_string, request, redirect, session, Response, url_for
    import aiosqlite
    import python_socks
    import hypercorn.asyncio
    from hypercorn.config import Config
    import pytz
    import httpx
except ImportError:
    print("🛡️ Installing V30 Hybrid dependencies...")
    os.system("pip install telethon quart aiosqlite python-socks hypercorn pytz httpx")
    os.execv(sys.executable, ['python'] + sys.argv)

from datetime import datetime

# --- 2. CONFIGURATION ---
API_ID = int(os.environ.get("API_ID", 9497762))
API_HASH = os.environ.get("API_HASH", "272c77bf080e4a82846b8ff3dc3df0f4")
DB_FILE = 'tracker.db'
PIC_FOLDER = 'static/profile_pics'
POLL_INTERVAL = 10  
HEARTBEAT_FLUSH_RATE = 300 
SELF_PING_URL = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:5000")

os.makedirs(PIC_FOLDER, exist_ok=True)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("Tracker")

# --- 3. APP STATE ---
app = Quart(__name__, static_folder='static')
app.secret_key = secrets.token_hex(16)

# Locks
db_lock = asyncio.Lock()
api_sem = asyncio.Semaphore(5)

class AppState:
    def __init__(self):
        self.client = None
        self.db = None
        self.tg_logged_in = False
        self.status = "Initializing..."
        self.temp_phone = None 
        self.temp_hash = None
        self.memory = {} 
        self.running = True

state = AppState()

# --- 4. DATABASE ---
async def init_db():
    state.db = await aiosqlite.connect(DB_FILE)
    state.db.row_factory = aiosqlite.Row
    
    await state.db.execute('CREATE TABLE IF NOT EXISTS targets (id INTEGER PRIMARY KEY, user_id INT UNIQUE, name TEXT, status TEXT, last_seen REAL, pic TEXT)')
    await state.db.execute('CREATE TABLE IF NOT EXISTS sessions (id INTEGER PRIMARY KEY, user_id INT, status TEXT, start REAL, end REAL, dur TEXT)')
    await state.db.execute('CREATE TABLE IF NOT EXISTS app_config (key TEXT PRIMARY KEY, value TEXT)')
    
    # Default Admin
    cursor = await state.db.execute("SELECT value FROM app_config WHERE key='admin_user'")
    if not await cursor.fetchone():
        await state.db.execute("INSERT INTO app_config (key, value) VALUES ('admin_user', 'admin')")
        await state.db.execute("INSERT INTO app_config (key, value) VALUES ('admin_pass', 'admin')")
    
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
            if fetch == "val": 
                res = await cursor.fetchone()
                return res[0] if res else None
        except Exception as e:
            logger.error(f"DB Error: {e}")
            return None

# --- 5. HELPERS ---
def format_ts(ts, minimal=True):
    if not ts: return "Never"
    try:
        dt = datetime.fromtimestamp(ts, pytz.timezone('Asia/Kolkata'))
        now = datetime.now(pytz.timezone('Asia/Kolkata'))
        if minimal and dt.date() == now.date():
            return dt.strftime('%I:%M %p')
        return dt.strftime('%d-%b %I:%M %p')
    except: return "-"

def calc_duration(start_ts, end_ts):
    try:
        if not start_ts or not end_ts: return "-"
        seconds = int(end_ts - start_ts)
        if seconds < 60: return "Just now"
        m, s = divmod(seconds, 60)
        h, m = divmod(m, 60)
        if h > 0: return f"{h}h {m}m"
        return f"{m}m {s}s"
    except: return "-"

async def get_chart_data(uid):
    hourly = [0] * 24
    rows = await db_query("SELECT start FROM sessions WHERE user_id = ? ORDER BY id DESC LIMIT 50", (uid,))
    if rows:
        for r in rows:
            try:
                dt = datetime.fromtimestamp(r['start'], pytz.timezone('Asia/Kolkata'))
                hourly[dt.hour] += 1
            except: pass
    return hourly

async def safe_api_call(coroutine):
    async with api_sem:
        try:
            return await coroutine
        except errors.FloodWaitError as e:
            logger.warning(f"🌊 FloodWait: Sleeping {e.seconds}s...")
            await asyncio.sleep(e.seconds)
            return await safe_api_call(coroutine)
        except Exception: return None

async def download_pic(user):
    try:
        if state.client and state.client.is_connected():
            p = await state.client.download_profile_photo(user, file=PIC_FOLDER)
            if p: return os.path.basename(p)
    except: pass
    return "default.png"

# --- 6. TRACKER ENGINE ---
async def init_client():
    sess_str = await db_query("SELECT value FROM app_config WHERE key='tg_session'", fetch="val")
    if not sess_str: sess_str = os.environ.get("SESSION_STRING", None)

    if sess_str:
        try:
            state.client = TelegramClient(StringSession(sess_str), API_ID, API_HASH)
            await state.client.connect()
            if await state.client.is_user_authorized():
                state.tg_logged_in = True
                state.status = "Active"
                logger.info("✅ Telegram Connected")
        except: pass
    
    if not state.client:
        state.client = TelegramClient(StringSession(), API_ID, API_HASH)
        await state.client.connect()

async def tracker_loop():
    while not state.tg_logged_in:
        state.status = "Waiting for Telegram Login..."
        await asyncio.sleep(2)
    
    try:
        now = time.time()
        for f in glob.glob(os.path.join(PIC_FOLDER, "*")):
            if "default" not in f and os.stat(f).st_mtime < now - 259200: os.remove(f)
    except: pass

    state.status = "Monitoring"
    logger.info("🚀 Tracker V30 Started")
    last_flush = time.time()

    while state.running:
        try:
            if not state.client or not state.client.is_connected():
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
                        
                except: pass
                await asyncio.sleep(0.1)
            
            if time.time() - last_flush > HEARTBEAT_FLUSH_RATE: last_flush = time.time()
            await asyncio.sleep(POLL_INTERVAL)
            
        except Exception as e:
            logger.error(f"Loop Crash: {e}")
            await asyncio.sleep(POLL_INTERVAL)

async def keep_alive():
    while state.running:
        await asyncio.sleep(600)
        try:
            async with httpx.AsyncClient() as h: await h.get(SELF_PING_URL)
        except: pass

# --- 7. HTML TEMPLATES (V30 Hybrid UI) ---
STYLE = """
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<style>
    * { box-sizing: border-box; }
    body { background: #0f172a; font-family: 'Inter', sans-serif; margin: 0; padding: 0; color: white; display: flex; flex-direction: column; min-height: 100vh; }
    .auth-container { display: flex; align-items: center; justify-content: center; min-height: 100vh; padding: 20px; }
    .auth-card { background: #1e293b; padding: 30px; border-radius: 20px; width: 100%; max-width: 400px; border: 1px solid #334155; box-shadow: 0 10px 30px rgba(0,0,0,0.5); }
    .title { text-align: center; font-size: 1.5rem; font-weight: 700; margin-bottom: 25px; color: #38bdf8; }
    .input { width: 100%; padding: 14px; background: #020617; border: 1px solid #334155; border-radius: 12px; color: white; margin-bottom: 15px; font-size: 16px; outline:none; transition:0.3s; }
    .input:focus { border-color: #38bdf8; }
    .btn { width: 100%; padding: 14px; background: #3b82f6; color: white; border: none; border-radius: 12px; font-weight: 600; cursor: pointer; font-size: 16px; margin-top: 10px; transition:0.2s; }
    .btn:hover { opacity: 0.9; transform: scale(0.98); }
    .nav { display: flex; justify-content: space-between; align-items: center; padding: 15px 20px; border-bottom: 1px solid #334155; background: #0f172a; position: sticky; top: 0; z-index: 100; backdrop-filter: blur(10px); opacity: 0.95; }
    .brand { font-size: 1.2rem; font-weight: 800; color: #f8fafc; text-decoration: none; }
    .container { padding: 20px; width: 100%; max-width: 800px; margin: 0 auto; }
    .grid { display: grid; grid-template-columns: 1fr; gap: 15px; padding: 15px 0; }
    @media (min-width: 768px) { .grid { grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); } }
    .card { background: #1e293b; border-radius: 16px; padding: 20px; border: 1px solid #334155; position: relative; transition: 0.2s; }
    .card:active { transform: scale(0.98); }
    .row { display: flex; align-items: center; gap: 15px; }
    .avatar { width: 50px; height: 50px; border-radius: 50%; object-fit: cover; border: 2px solid #334155; }
    .avatar.online { border-color: #10b981; box-shadow: 0 0 10px rgba(16, 185, 129, 0.4); }
    .status-badge { font-size: 0.75rem; font-weight: 600; padding: 4px 10px; border-radius: 20px; background: #334155; display: inline-block; margin-top: 5px; }
    .status-badge.online { background: rgba(16, 185, 129, 0.2); color: #10b981; }
    .fab { position:fixed; bottom:25px; right:25px; background:#3b82f6; width:55px; height:55px; border-radius:50%; display:flex; align-items:center; justify-content:center; font-size:1.5rem; color:white; text-decoration:none; box-shadow: 0 4px 15px rgba(59, 130, 246, 0.4); }
    .menu-link { color: #94a3b8; text-decoration: none; margin-left: 15px; font-size: 0.9rem; font-weight: 600; }
    .alert { background: rgba(239,68,68,0.2); color: #fca5a5; padding: 10px; border-radius: 8px; text-align: center; margin-bottom: 15px; border: 1px solid rgba(239,68,68,0.5); }
</style>
<link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
"""

P_LOGIN = """<!DOCTYPE html><html><head>""" + STYLE + """</head><body>
<div class="auth-container"><div class="auth-card">
    <div class="title">🔐 Admin Access</div>
    {% if error %}<div class="alert">{{ error }}</div>{% endif %}
    <form action="/do_web_login" method="post">
        <input type="text" name="username" class="input" placeholder="Username (Default: admin)" required>
        <input type="password" name="password" class="input" placeholder="Password (Default: admin)" required>
        <button class="btn">Sign In</button>
    </form>
</div></div></body></html>"""

P_TG_CONNECT = """<!DOCTYPE html><html><head>""" + STYLE + """</head><body>
<div class="auth-container"><div class="auth-card">
    <div class="title"><i class="fab fa-telegram"></i> Connect Telegram</div>
    <p style="text-align:center; color:#94a3b8; margin-bottom:20px;">Enter phone to start tracking.</p>
    <form action="/send_otp" method="post">
        <input type="text" name="ph" class="input" placeholder="+91..." required>
        <button class="btn" style="background:#0ea5e9">Get OTP</button>
    </form>
</div></div></body></html>"""

P_TG_VERIFY = """<!DOCTYPE html><html><head>""" + STYLE + """</head><body>
<div class="auth-container"><div class="auth-card">
    <div class="title">🔐 Verify OTP</div>
    <form action="/verify_otp" method="post">
        <input type="hidden" name="ph" value="{{ ph }}">
        <input type="hidden" name="hash" value="{{ hash }}">
        <input type="hidden" name="session_str" value="{{ session_str }}">
        <input type="text" name="code" class="input" placeholder="12345" style="text-align:center; letter-spacing:5px;" required>
        <button class="btn" style="background:#10b981">Start Tracking</button>
    </form>
</div></div></body></html>"""

P_DASH = """<!DOCTYPE html><html><head>""" + STYLE + """</head><body>
<div class="nav">
    <a href="/" class="brand"><i class="fas fa-radar"></i> Tracker</a>
    <div>
        <a href="/profile" class="menu-link"><i class="fas fa-user-cog"></i></a>
        <a href="/logout" class="menu-link" style="color:#ef4444;"><i class="fas fa-power-off"></i></a>
    </div>
</div>
<div class="container">
    {% if not tg_active %}
    <a href="/connect_telegram" style="display:block; background:#f59e0b; color:black; padding:10px; border-radius:8px; text-align:center; text-decoration:none; font-weight:bold; margin-bottom:15px;">⚠️ Tracker Disconnected. Click to Connect.</a>
    {% endif %}
    <div class="grid">
        {% for t in targets %}
        <a href="/target/{{ t.user_id }}" style="text-decoration:none; color:inherit;">
            <div class="card">
                <div class="row">
                    <img src="/static/profile_pics/{{ t.pic }}" class="avatar {{ 'online' if t.status == 'online' else '' }}" onerror="this.src='/static/profile_pics/default.png'">
                    <div>
                        <div style="font-weight:700;">{{ t.name }}</div>
                        <div class="status-badge {{ 'online' if t.status == 'online' else '' }}">{{ t.status.upper() }}</div>
                    </div>
                </div>
                <div style="margin-top:15px; display:flex; justify-content:space-between; font-size:0.8rem; color:#94a3b8;">
                    <span>Last Seen</span><span style="color:white">{{ t.last_seen_fmt }}</span>
                </div>
            </div>
        </a>
        {% else %}<div style="grid-column:1/-1; text-align:center; padding:40px; color:#94a3b8;">No targets found. Tap + to add.</div>{% endfor %}
    </div>
</div>
<a href="/add_page" class="fab"><i class="fas fa-plus"></i></a>
</body></html>"""

P_DETAIL = """<!DOCTYPE html><html><head>""" + STYLE + """</head><body>
<div class="nav"><a href="/" class="brand">&larr; Back</a></div>
<div class="container">
    <div class="card" style="display:flex; justify-content:space-between; align-items:center;">
        <div class="row">
            <img src="/static/profile_pics/{{ t.pic }}" class="avatar" style="width:60px; height:60px;">
            <div><h2>{{ t.name }}</h2><div style="color:#94a3b8;">ID: {{ t.user_id }}</div></div>
        </div>
        <div>
            <a href="/delete/{{ t.user_id }}" class="btn" style="background:#ef4444; width:auto; padding:10px 15px; display:inline-block;" onclick="return confirm('Delete?')"><i class="fas fa-trash"></i></a>
        </div>
    </div>
    <div class="card" style="margin-top:15px;"><h3 style="margin-top:0;">Activity Heatmap (Hourly)</h3><div style="height:200px;"><canvas id="chart"></canvas></div></div>
    <div class="card" style="margin-top:15px;">
        <h3 style="margin-top:0; display:flex; justify-content:space-between;">Recent Sessions <a href="/download_db" style="font-size:0.8rem; color:#3b82f6;">Download CSV</a></h3>
        {% for s in sessions %}
        <div style="display:flex; justify-content:space-between; padding:10px 0; border-bottom:1px solid #334155; font-size:0.9rem;">
            <div><span style="color:#10b981;">●</span> {{ s.start_fmt }}</div><div style="color:#94a3b8;">{{ s.dur }}</div>
        </div>{% endfor %}
    </div>
</div>
<script>
new Chart(document.getElementById('chart'), {
    type: 'bar',
    data: { labels: Array.from({length:24},(_,i)=>i+':00'), datasets: [{ label: 'Sessions', data: {{ chart_data }}, backgroundColor: '#3b82f6', borderRadius: 4 }] },
    options: { responsive: true, maintainAspectRatio: false, scales: { y: { beginAtZero: true, grid: { color: '#334155' } }, x: { grid: { display: false } } }, plugins: { legend: { display: false } } }
});
</script></body></html>"""

P_ADD = """<!DOCTYPE html><html><head>""" + STYLE + """</head><body>
<div class="nav"><a href="/" class="brand">&larr; Cancel</a></div>
<div class="container"><div class="card"><h3>Add New Target</h3><form action="/add_target" method="post"><label style="color:#94a3b8; font-size:0.9rem;">Identifier</label><input type="text" name="u" class="input" placeholder="Username, ID, or +91..." required><button class="btn">Track User</button></form></div></div></body></html>"""

P_PROFILE = """<!DOCTYPE html><html><head>""" + STYLE + """</head><body>
<div class="nav"><a href="/" class="brand">&larr; Back</a></div>
<div class="container"><div class="card"><h3>Admin Settings</h3><form action="/update_admin" method="post"><label style="color:#94a3b8; font-size:0.9rem;">New Password</label><input type="password" name="pw" class="input" placeholder="Leave empty to keep current"><button class="btn" style="background:#334155">Update Password</button></form></div></div></body></html>"""

# --- 8. ROUTES ---
@app.before_request
def guard():
    if request.path.startswith('/static'): return
    if request.path in ['/web_login', '/do_web_login']: return
    if 'admin_user' not in session: return redirect('/web_login')

@app.route('/web_login')
async def web_login(): return render_template_string(P_LOGIN, error=request.args.get('error'))

@app.route('/do_web_login', methods=['POST'])
async def do_web_login():
    f = await request.form
    u, p = f['username'], f['password']
    db_u = await db_query("SELECT value FROM app_config WHERE key='admin_user'", fetch="val")
    db_p = await db_query("SELECT value FROM app_config WHERE key='admin_pass'", fetch="val")
    if u == db_u and p == db_p:
        session['admin_user'] = u
        return redirect('/')
    return redirect('/web_login?error=Invalid Credentials')

@app.route('/logout')
async def logout():
    session.clear()
    return redirect('/web_login')
@app.route('/connect_telegram')
async def connect_telegram(): return render_template_string(P_TG_CONNECT)

@app.route('/send_otp', methods=['POST'])
async def send_otp():
    try:
        temp = TelegramClient(StringSession(), API_ID, API_HASH)
        await temp.connect()
        ph = (await request.form)['ph']
        s = await temp.send_code_request(ph)
        ses = StringSession.save(temp.session)
        await temp.disconnect()
        return render_template_string(P_TG_VERIFY, ph=ph, hash=s.phone_code_hash, session_str=ses)
    except Exception as e: return f"Error: {e} <a href='/connect_telegram'>Back</a>"

@app.route('/verify_otp', methods=['POST'])
async def verify_otp():
    f = await request.form
    try:
        c = TelegramClient(StringSession(f['session_str']), API_ID, API_HASH)
        await c.connect()
        await c.sign_in(f['ph'], f['code'], phone_code_hash=f['hash'])
        final_str = StringSession.save(c.session)
        await db_query("INSERT OR REPLACE INTO app_config (key, value) VALUES ('tg_session', ?)", (final_str,), commit=True)
        await c.disconnect()
        await init_client() 
        return redirect('/')
    except Exception as e: return f"Error: {e} <a href='/connect_telegram'>Retry</a>"

@app.route('/')
async def index():
    rows = await db_query("SELECT * FROM targets ORDER BY CASE WHEN status='online' THEN 0 ELSE 1 END, name ASC")
    targets = []
    for r in rows:
        d = dict(r)
        d['last_seen_fmt'] = format_ts(d['last_seen'])
        targets.append(d)
    return render_template_string(P_DASH, targets=targets, tg_active=state.tg_logged_in)

@app.route('/target/<int:uid>')
async def target_detail(uid):
    t_row = await db_query("SELECT * FROM targets WHERE user_id=?", (uid,), fetch="one")
    if not t_row: return redirect('/')
    
    sess_rows = await db_query("SELECT * FROM sessions WHERE user_id=? ORDER BY id DESC LIMIT 20", (uid,))
    sessions = []
    for s in sess_rows:
        sd = dict(s)
        sd['start_fmt'] = format_ts(sd['start'], minimal=False)
        sessions.append(sd)
    return render_template_string(P_DETAIL, t=dict(t_row), sessions=sessions, chart_data=await get_chart_data(uid))

@app.route('/add_page')
async def add_page(): return render_template_string(P_ADD)

@app.route('/add_target', methods=['POST'])
async def add_target():
    try:
        u = (await request.form)['u']
        e = None
        if u.startswith('+'):
            contact = InputPhoneContact(client_id=0, phone=u, first_name=u, last_name="")
            result = await state.client(ImportContactsRequest([contact]))
            if result.users: e = result.users[0]
        else:
            try: e = await state.client.get_entity(int(u) if u.lstrip('-').isdigit() else u)
            except: pass
        
        if e:
            pic = await download_pic(e)
            name = getattr(e, 'first_name', '') or getattr(e, 'username', '') or u
            if getattr(e, 'last_name', None): name += f" {e.last_name}"
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
    clean = []
    for r in rows:
        l = list(r)
        l[3] = format_ts(l[3], False)
        l[4] = format_ts(l[4], False)
        clean.append(tuple(l))
    si = io.StringIO(); cw = csv.writer(si)
    cw.writerow(['ID', 'UID', 'Status', 'Start', 'End', 'Duration'])
    cw.writerows(clean)
    return Response(si.getvalue(), mimetype='text/csv', headers={"Content-Disposition": "attachment; filename=logs.csv"})

@app.route('/profile')
async def profile(): return render_template_string(P_PROFILE)

@app.route('/update_admin', methods=['POST'])
async def update_admin():
    pw = (await request.form)['pw']
    if pw: await db_query("UPDATE app_config SET value=? WHERE key='admin_pass'", (pw,), commit=True)
    return redirect('/')

@app.after_serving
async def shutdown():
    state.running = False
    if state.client: await state.client.disconnect()
    if state.db: await state.db.close()

@app.before_serving
async def startup():
    await init_db()
    await init_client()
    app.add_background_task(tracker_loop)
    app.add_background_task(keep_alive)

if __name__ == '__main__':
    config = Config()
    config.bind = [f"0.0.0.0:{int(os.environ.get('PORT', 5000))}"]
    asyncio.run(hypercorn.asyncio.serve(app, config))
