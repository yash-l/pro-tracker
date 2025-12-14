import logging
import asyncio
import os
import sys
import json
import csv
import io
import pytz
import aiosqlite
import secrets
from datetime import datetime
from telethon import TelegramClient
from telethon.tl.types import UserStatusOnline, UserStatusOffline, UserStatusRecently, InputPhoneContact
from telethon.tl.functions.contacts import ImportContactsRequest
import python_socks
from quart import Quart, render_template_string, request, redirect, Response, session, url_for

# --- CONFIGURATION ---
CONFIG_FILE = 'config.json'
DB_FILE = 'tracker.db'
PIC_FOLDER = 'static/profile_pics'

DEFAULT_CONFIG = {
    "api_id": 9497762,
    "api_hash": "272c77bf080e4a82846b8ff3dc3df0f4",
    "phone": "+918849404331",
    "bot_api_key": "7949002012:AAG9ifTdXAMIAk_vPFfOnM1wxHOela9o87w",
    "chat_id": 1184218529,
    "timezone": "Asia/Kolkata",
    "admin_username": "admin",
    "admin_password": "password",
    "recovery_key": secrets.token_hex(8),
    "secret_key": secrets.token_hex(16),
    "proxy_enabled": False,
    "proxy_ip": "127.0.0.1",
    "proxy_port": 8080,
    "is_setup_done": False
}

os.makedirs(PIC_FOLDER, exist_ok=True)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- DATABASE ---
async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute('''CREATE TABLE IF NOT EXISTS targets (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER UNIQUE, username TEXT, 
            display_name TEXT, current_status TEXT, last_seen TEXT, pic_path TEXT)''')
        await db.execute('''CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, status TEXT, 
            start_time TEXT, end_time TEXT, duration TEXT, 
            FOREIGN KEY(user_id) REFERENCES targets(user_id))''')
        await db.commit()

# --- CONFIG ---
def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f: 
                c = json.load(f)
                for k, v in DEFAULT_CONFIG.items():
                    if k not in c: c[k] = v
                return c
        except: return DEFAULT_CONFIG
    return DEFAULT_CONFIG

def save_config(new_config):
    with open(CONFIG_FILE, 'w') as f: json.dump(new_config, f, indent=4)

cfg = load_config()

PROXY_CONFIG = None
if cfg.get('proxy_enabled'):
    PROXY_CONFIG = (python_socks.HTTP, cfg['proxy_ip'], int(cfg['proxy_port']), True, 'user', 'pass')

client = TelegramClient('session_pro', cfg['api_id'], cfg['api_hash'], proxy=PROXY_CONFIG)
app = Quart(__name__, static_folder='static')
app.secret_key = cfg['secret_key']

# Fix cookies for Android/Cloud
app.config['SESSION_COOKIE_SECURE'] = False
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# --- HELPERS ---
def get_tz():
    return pytz.timezone(cfg['timezone'])

def fmt_time(dt_obj):
    if not dt_obj: return "-"
    if dt_obj.tzinfo is None: dt_obj = dt_obj.replace(tzinfo=pytz.utc)
    return dt_obj.astimezone(get_tz()).strftime('%I:%M %p')

async def get_hourly_data(user_id=None):
    hourly_counts = [0] * 24
    query = 'SELECT start_time FROM sessions'
    args = ()
    if user_id:
        query += ' WHERE user_id = ?'
        args = (user_id,)
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(query, args) as cursor:
            rows = await cursor.fetchall()
    for row in rows:
        try:
            dt = datetime.strptime(row[0], '%I:%M %p')
            hourly_counts[dt.hour] += 1
        except: pass
    return hourly_counts

async def get_ai_insight(user_id):
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute('SELECT start_time FROM sessions WHERE user_id = ? ORDER BY id DESC LIMIT 50', (user_id,)) as c:
            sessions = await c.fetchall()
    if not sessions: return "No data yet."
    hours = []
    for s in sessions:
        try: hours.append(datetime.strptime(s[0], '%I:%M %p').hour)
        except: pass
    if not hours: return "Analyzing..."
    from collections import Counter
    peak = Counter(hours).most_common(1)[0][0]
    peak_str = datetime.strptime(str(peak), "%H").strftime("%I %p")
    return f"Most active around {peak_str}"

# --- TRACKER CORE ---
async def update_target_info(user_id, status, last_seen):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute('UPDATE targets SET current_status = ?, last_seen = ? WHERE user_id = ?', (status, last_seen, user_id))
        await db.commit()

async def log_event(user_id, status, time_str):
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute('SELECT id, start_time FROM sessions WHERE user_id = ? AND end_time IS NULL ORDER BY id DESC LIMIT 1', (user_id,))
        last_session = await cursor.fetchone()
        if last_session:
            sid, start_str = last_session
            await db.execute('UPDATE sessions SET end_time = ? WHERE id = ?', (time_str, sid))
        await db.execute('INSERT INTO sessions (user_id, status, start_time) VALUES (?, ?, ?)', (user_id, status, time_str))
        await db.commit()

async def download_pic(user_entity):
    try:
        path = await client.download_profile_photo(user_entity, file=PIC_FOLDER)
        if path:
            filename = os.path.basename(path)
            async with aiosqlite.connect(DB_FILE) as db:
                await db.execute('UPDATE targets SET pic_path = ? WHERE user_id = ?', (filename, user_entity.id))
                await db.commit()
            return filename
    except: pass
    return "default.png"

async def tracker_loop():
    # In cloud, setup might be done via env vars, but we keep this logic for safety
    if not cfg['is_setup_done']: 
        # Auto-create dummy file if config exists to allow cloud start
        if os.path.exists(CONFIG_FILE): pass 
        else: return 
        
    await client.start(phone=cfg['phone'])
    memory = {} 
    while True:
        try:
            async with aiosqlite.connect(DB_FILE) as db:
                async with db.execute('SELECT user_id, display_name FROM targets') as cursor:
                    targets = await cursor.fetchall()
            if not targets:
                await asyncio.sleep(5)
                continue
            for (uid, name) in targets:
                try:
                    user = await client.get_entity(uid)
                    status = 'online' if isinstance(user.status, UserStatusOnline) else 'offline'
                    now_str = fmt_time(datetime.now(pytz.utc))
                    last_status = memory.get(uid)
                    if status != last_status:
                        await update_target_info(uid, status, now_str)
                        if status == 'online':
                            await log_event(uid, 'ONLINE', now_str)
                        elif last_status == 'online' and status != 'online':
                             async with aiosqlite.connect(DB_FILE) as db:
                                 await db.execute('UPDATE sessions SET end_time = ? WHERE user_id = ? AND end_time IS NULL', (now_str, uid))
                                 await db.commit()
                        memory[uid] = status
                    await update_target_info(uid, status, now_str)
                except: pass
                await asyncio.sleep(1)
            await asyncio.sleep(4)
        except: await asyncio.sleep(5)

# --- MIDDLEWARE ---
@app.before_request
def check_auth():
    if request.path.startswith('/static'): return
    if request.path in ['/login', '/do_login', '/setup', '/do_setup', '/forgot_password', '/do_reset']: return
    if not cfg['is_setup_done']: return redirect('/setup')
    if 'user' not in session: return redirect('/login')

# --- HTML STYLES ---
STYLE = """
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<style>
    * { box-sizing: border-box; }
    body { background: #0f172a; font-family: 'Inter', sans-serif; margin: 0; padding: 0; color: white; display: flex; flex-direction: column; min-height: 100vh; overflow-x: hidden; }
    .auth-container { display: flex; align-items: center; justify-content: center; min-height: 100vh; padding: 20px; width: 100%; }
    .auth-card { background: #1e293b; padding: 30px; border-radius: 20px; width: 100%; max-width: 400px; border: 1px solid #334155; box-shadow: 0 10px 30px rgba(0,0,0,0.5); }
    .title { text-align: center; font-size: 1.5rem; font-weight: 700; margin-bottom: 25px; }
    .input { width: 100%; padding: 14px; background: #020617; border: 1px solid #334155; border-radius: 12px; color: white; margin-bottom: 15px; font-size: 16px; }
    .btn { width: 100%; padding: 14px; background: #3b82f6; color: white; border: none; border-radius: 12px; font-weight: 600; cursor: pointer; font-size: 16px; margin-top: 10px; }
    .link { display: block; text-align: center; margin-top: 20px; color: #94a3b8; text-decoration: none; font-size: 0.9rem; }
    .alert { background: rgba(239, 68, 68, 0.2); color: #fca5a5; padding: 12px; border-radius: 8px; margin-bottom: 20px; text-align: center; border: 1px solid rgba(239, 68, 68, 0.5); }
    .nav { display: flex; justify-content: space-between; align-items: center; padding: 15px 20px; border-bottom: 1px solid #334155; background: #0f172a; position: sticky; top: 0; z-index: 100; width: 100%; }
    .brand { font-size: 1.2rem; font-weight: 800; color: #f8fafc; text-decoration: none; display: flex; align-items: center; gap: 10px; }
    .container { padding: 20px; width: 100%; max-width: 800px; margin: 0 auto; }
    .grid { display: grid; grid-template-columns: 1fr; gap: 15px; padding: 15px; width: 100%; }
    @media (min-width: 768px) { .grid { grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); } }
    .card { background: #1e293b; border-radius: 16px; padding: 20px; border: 1px solid #334155; position: relative; width: 100%; }
    .row { display: flex; align-items: center; gap: 15px; }
    .avatar { width: 50px; height: 50px; border-radius: 50%; object-fit: cover; border: 2px solid #334155; flex-shrink: 0; }
    .avatar.online { border-color: #10b981; }
    .status { font-size: 0.75rem; font-weight: 600; padding: 4px 10px; border-radius: 20px; background: #334155; display: inline-block; margin-top: 5px; }
    .status.online { background: rgba(16, 185, 129, 0.2); color: #10b981; }
    .menu-icon { font-size: 1.4rem; color: #94a3b8; margin-left: 20px; }
    .spacer { height: 80px; }
</style>
<link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap" rel="stylesheet">
"""

PAGE_SETUP = """<!DOCTYPE html><html><head>""" + STYLE + """</head><body>
<div class="auth-container"><div class="auth-card">
    <div class="title">?? Initial Setup</div>
    <p style="text-align:center; color:#94a3b8; margin-bottom:25px; font-size:0.9rem;">Create your admin account.</p>
    <form action="/do_setup" method="post">
        <input type="text" name="username" class="input" placeholder="Choose Username" required>
        <input type="password" name="password" class="input" placeholder="Choose Password" required>
        <button class="btn">Create Account</button>
    </form>
</div></div></body></html>"""

PAGE_LOGIN = """<!DOCTYPE html><html><head>""" + STYLE + """</head><body>
<div class="auth-container"><div class="auth-card">
    <div class="title">?? Login</div>
    {% if error %}<div class="alert">{{ error }}</div>{% endif %}
    <form action="/do_login" method="post">
        <input type="text" name="username" class="input" placeholder="Username" required>
        <input type="password" name="password" class="input" placeholder="Password" required>
        <button class="btn">Sign In</button>
    </form>
    <a href="/forgot_password" class="link">Forgot Password?</a>
</div></div></body></html>"""

PAGE_FORGOT = """<!DOCTYPE html><html><head>""" + STYLE + """</head><body>
<div class="auth-container"><div class="auth-card">
    <div class="title">?? Recovery</div>
    <p style="text-align:center; color:#94a3b8; font-size:0.9rem;">Enter the Recovery Key shown in your Termux console.</p>
    <form action="/do_reset" method="post">
        <input type="text" name="recovery_key" class="input" placeholder="Recovery Key" required>
        <input type="password" name="new_password" class="input" placeholder="New Password" required>
        <button class="btn" style="background:#10b981">Reset Password</button>
    </form>
    <a href="/login" class="link">Back to Login</a>
</div></div></body></html>"""

HTML_HEADER = """<!DOCTYPE html><html><head>""" + STYLE + """<script src="https://cdn.jsdelivr.net/npm/chart.js"></script></head><body>
<div class="nav">
    <a href="/" class="brand"><i class="fas fa-radar"></i> ProTracker</a>
    <div>
        <a href="/profile" class="menu-icon"><i class="fas fa-user-circle"></i></a>
        <a href="/logout" class="menu-icon"><i class="fas fa-sign-out-alt"></i></a>
    </div>
</div>"""

PAGE_DASHBOARD = """
<div class="grid">
    {% for t in targets %}
    <a href="/target/{{ t.user_id }}" style="text-decoration:none; color:inherit;">
        <div class="card">
            <div class="row">
                <img src="/static/profile_pics/{{ t.pic_path if t.pic_path else 'default.png' }}" class="avatar {{ 'online' if t.current_status == 'online' else '' }}">
                <div>
                    <div style="font-weight:700; font-size:1rem;">{{ t.display_name }}</div>
                    <div class="status {{ 'online' if t.current_status == 'online' else '' }}">{{ t.current_status.upper() }}</div>
                </div>
            </div>
            <div style="margin-top:15px; display:flex; justify-content:space-between; font-size:0.8rem; color:#94a3b8;">
                <span>Last Seen</span><span style="color:white">{{ t.last_seen }}</span>
            </div>
        </div>
    </a>
    {% else %}<div style="text-align:center; padding:40px; color:#94a3b8; grid-column: 1/-1;">No targets added.</div>{% endfor %}
</div>
<div class="spacer"></div>
<a href="/settings" style="position:fixed; bottom:25px; right:25px; background:#3b82f6; width:55px; height:55px; border-radius:50%; display:flex; align-items:center; justify-content:center; font-size:1.5rem; color:white; text-decoration:none; box-shadow: 0 4px 15px rgba(59, 130, 246, 0.4);"><i class="fas fa-plus"></i></a>
</body></html>
"""

PAGE_DETAIL = """
<div class="container">
    <a href="/" style="color:#94a3b8; text-decoration:none;">&larr; Back to Dashboard</a>
    <div class="card" style="margin-top:20px; display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:10px;">
        <div class="row">
            <img src="/static/profile_pics/{{ target.pic_path if target.pic_path else 'default.png' }}" class="avatar">
            <div><h2 style="margin:0; font-size:1.2rem;">{{ target.display_name }}</h2><div style="color:#94a3b8; font-size:0.9rem;">@{{ target.username }}</div></div>
        </div>
        <div>
             <a href="/export/{{ target.user_id }}" class="btn" style="width:auto; display:inline-block; padding:8px 12px; background:#10b981; margin-right:5px; margin-top:0;"><i class="fas fa-download"></i></a>
             <a href="/delete/{{ target.user_id }}" class="btn" style="width:auto; display:inline-block; padding:8px 12px; background:#ef4444; margin-top:0;" onclick="return confirm('Delete?')"><i class="fas fa-trash"></i></a>
        </div>
    </div>
    <div class="grid" style="grid-template-columns: 1fr 1fr; margin-top:0; padding:20px 0;">
        <div class="card"><h3>AI Insight</h3><p style="color:#3b82f6; font-size:0.9rem;">{{ insight }}</p></div>
        <div class="card"><h3>Status</h3><p style="font-size:0.9rem;">{{ target.current_status.upper() }} <br><span style="color:#94a3b8; font-size:0.8rem;">{{ target.last_seen }}</span></p></div>
    </div>
    <div class="card"><h3>Hourly Activity</h3><canvas id="userChart" style="max-height:200px; width:100%"></canvas></div>
    <script>
        new Chart(document.getElementById('userChart').getContext('2d'), {
            type: 'bar',
            data: { labels: Array.from({length:24},(_,i)=>i+':00'), datasets: [{ label: 'Sessions', data: {{ chart_data }}, backgroundColor: '#3b82f6', borderRadius: 4 }] },
            options: { responsive: true, maintainAspectRatio: false, scales: { y: { beginAtZero: true, grid: { color: '#334155' } }, x: { grid: { display: false } } } }
        });
    </script>
</div></body></html>
"""

PAGE_SETTINGS = """
<div class="container">
    <a href="/" style="display:block; margin-bottom:20px; color:#94a3b8; text-decoration:none;">&larr; Back</a>
    <div class="card">
        <h3>Add New Target</h3>
        <form action="/add_target" method="post">
            <label style="display:block; margin-bottom:5px; font-size:0.9rem; color:#94a3b8;">Username, ID, or Phone (+91...)</label>
            <input type="text" name="target_input" class="input" placeholder="e.g. +919876543210 or rohit45" required>
            <label style="display:block; margin-bottom:5px; font-size:0.9rem; color:#94a3b8;">Display Name (Optional)</label>
            <input type="text" name="display_name" class="input" placeholder="e.g. Best Friend">
            <button class="btn">Start Tracking</button>
        </form>
    </div>
</div></body></html>
"""

PAGE_PROFILE = """
<div class="container">
    <a href="/" style="display:block; margin-bottom:20px; color:#94a3b8; text-decoration:none;">&larr; Back</a>
    <div class="card">
        <h3>Admin Profile</h3>
        <form action="/update_profile" method="post">
            <label style="color:#94a3b8; font-size:0.9rem;">Username</label>
            <input type="text" name="username" value="{{ cfg.admin_username }}" class="input">
            <label style="color:#94a3b8; font-size:0.9rem;">New Password</label>
            <input type="password" name="password" class="input" placeholder="Leave empty to keep current">
            <hr style="border-color:#334155; margin:20px 0;">
            <label style="color:#94a3b8; font-size:0.9rem;">API ID</label>
            <input type="text" name="api_id" value="{{ cfg.api_id }}" class="input">
            <label style="color:#94a3b8; font-size:0.9rem;">Phone</label>
            <input type="text" name="phone" value="{{ cfg.phone }}" class="input">
            <button class="btn" style="background:#334155;">Save Changes</button>
        </form>
        <div style="margin-top:20px; padding:15px; background:rgba(16,185,129,0.1); border:1px solid #10b981; border-radius:12px; color:#10b981; font-size:0.9rem; word-break: break-all;">
            <b>Recovery Key:</b><br>{{ cfg.recovery_key }}
        </div>
    </div>
</div></body></html>
"""

# --- ROUTES ---
@app.route('/setup')
async def setup():
    if cfg['is_setup_done']: return redirect('/login')
    return PAGE_SETUP

@app.route('/do_setup', methods=['POST'])
async def do_setup():
    global cfg
    form = await request.form
    new_cfg = cfg.copy()
    new_cfg['admin_username'] = form['username']
    new_cfg['admin_password'] = form['password']
    new_cfg['is_setup_done'] = True
    save_config(new_cfg)
    cfg = new_cfg
    return redirect('/login')

@app.route('/login')
async def login():
    return await render_template_string(PAGE_LOGIN, error=request.args.get('error'))

@app.route('/do_login', methods=['POST'])
async def do_login():
    form = await request.form
    if form['username'] == cfg['admin_username'] and form['password'] == cfg['admin_password']:
        session['user'] = cfg['admin_username']
        return redirect('/')
    return redirect(url_for('login', error="Invalid Credentials"))

@app.route('/logout')
async def logout():
    session.clear()
    return redirect('/login')

@app.route('/forgot_password')
async def forgot_password(): return PAGE_FORGOT

@app.route('/do_reset', methods=['POST'])
async def do_reset():
    global cfg
    form = await request.form
    if form['recovery_key'] == cfg['recovery_key']:
        new_cfg = cfg.copy()
        new_cfg['admin_password'] = form['new_password']
        save_config(new_cfg)
        cfg = new_cfg
        return redirect(url_for('login', error="Password Reset Successful!"))
    return redirect(url_for('login', error="Invalid Recovery Key"))

@app.route('/')
async def home():
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute('SELECT * FROM targets') as c: rows = await c.fetchall()
        return await render_template_string(HTML_HEADER + PAGE_DASHBOARD, targets=[dict(r) for r in rows])
    except: return "Error loading dashboard"

@app.route('/target/<int:uid>')
async def target_detail(uid):
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute('SELECT * FROM targets WHERE user_id = ?', (uid,)) as c: target = await c.fetchone()
            async with db.execute('SELECT * FROM sessions WHERE user_id = ? ORDER BY id DESC', (uid,)) as c: sessions = await c.fetchall()
        if not target: return redirect('/')
        return await render_template_string(HTML_HEADER + PAGE_DETAIL, target=dict(target), sessions=[dict(s) for s in sessions], chart_data=await get_hourly_data(uid), insight=await get_ai_insight(uid))
    except: return redirect('/')

@app.route('/settings')
async def settings(): return await render_template_string(HTML_HEADER + PAGE_SETTINGS)

@app.route('/profile')
async def profile(): return await render_template_string(HTML_HEADER + PAGE_PROFILE, cfg=cfg)

@app.route('/update_profile', methods=['POST'])
async def update_profile():
    global cfg
    form = await request.form
    c = cfg.copy()
    c['admin_username'] = form['username']
    c['api_id'] = int(form['api_id'])
    c['phone'] = form['phone']
    if form['password']: c['admin_password'] = form['password']
    if c['phone'] != cfg['phone'] and os.path.exists('session_pro.session'): os.remove('session_pro.session')
    save_config(c)
    cfg = c
    os.execv(sys.executable, ['python'] + sys.argv)

@app.route('/add_target', methods=['POST'])
async def add_target():
    form = await request.form
    inp = form['target_input']
    name = form.get('display_name') or inp
    try:
        # Check if phone number
        if inp.startswith('+'):
            contact = InputPhoneContact(client_id=0, phone=inp, first_name=name, last_name="")
            result = await client(ImportContactsRequest([contact]))
            if result.users:
                e = result.users[0]
            else:
                return redirect('/') # Failed to find phone
        else:
            # Username or ID
            e = await client.get_entity(int(inp) if inp.isdigit() else inp)
            
        pic = await download_pic(e)
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute('INSERT OR IGNORE INTO targets (user_id, username, display_name, current_status, last_seen, pic_path) VALUES (?, ?, ?, ?, ?, ?)', 
                             (e.id, getattr(e,'username',''), name, 'checking...', 'New', pic))
            await db.commit()
    except Exception as e:
        print(f"Error adding: {e}")
    return redirect('/')

@app.route('/delete/<int:uid>')
async def delete_target(uid):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute('DELETE FROM targets WHERE user_id = ?', (uid,))
        await db.commit()
    return redirect('/')

@app.route('/export/<int:uid>')
async def export_csv(uid):
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute('SELECT * FROM sessions WHERE user_id = ? ORDER BY id DESC', (uid,)) as c:
            rows = await c.fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Session ID', 'User ID', 'Status', 'Start Time', 'End Time', 'Duration'])
    writer.writerows(rows)
    return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename=log_{uid}.csv"})

@app.before_serving
async def startup():
    print(f"\n{'='*40}\n?? RECOVERY KEY: {cfg['recovery_key']}\n{'='*40}\n")
    await init_db()
    app.add_background_task(tracker_loop)

if __name__ == '__main__':
    # Cloud Port Config
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
