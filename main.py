import os, sys, time, asyncio, json, secrets, logging

# --- 1. AUTO-INSTALLER (Runs if libraries are missing) ---
try:
    from telethon import TelegramClient, events, errors
    from telethon.tl.types import UserStatusOnline
    from quart import Quart, render_template_string, request, redirect, session
    import aiosqlite
    import python_socks
    import hypercorn.asyncio
except ImportError:
    print("⚠️ Libraries missing! Installing now... (This happens only once)")
    os.system("pip install telethon quart aiosqlite python-socks hypercorn")
    print("✅ Installed! Restarting script...")
    os.execv(sys.executable, ['python'] + sys.argv)

# --- 2. CONFIGURATION (EDIT THIS) ---
ADMIN_PASSWORD = "admin"       # Password to access the website
API_ID = 9497762               # Your Telegram API ID
API_HASH = "272c77bf080e4a82846b8ff3dc3df0f4" # Your Telegram API Hash

# --- 3. SETUP ---
# Random session name ensures 100% manual login every restart
SESSION_ID = f"session_{secrets.token_hex(4)}"
DB_FILE = 'tracker.db'
PIC_FOLDER = 'static/profile_pics'

os.makedirs(PIC_FOLDER, exist_ok=True)
logging.basicConfig(level=logging.INFO)

app = Quart(__name__, static_folder='static')
app.secret_key = secrets.token_hex(16)
client = TelegramClient(SESSION_ID, API_ID, API_HASH)

# Global State
state = {"phone": None, "phone_hash": None, "logged_in": False}

# --- 4. DATABASE ---
async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute('CREATE TABLE IF NOT EXISTS targets (id INTEGER PRIMARY KEY, user_id INT UNIQUE, name TEXT, status TEXT, last_seen TEXT, pic TEXT)')
        await db.execute('CREATE TABLE IF NOT EXISTS sessions (id INTEGER PRIMARY KEY, user_id INT, status TEXT, start TEXT, end TEXT, dur TEXT)')
        await db.commit()

# --- 5. HELPERS ---
def get_time(): 
    from datetime import datetime
    import pytz
    return datetime.now(pytz.timezone('Asia/Kolkata')).strftime('%I:%M %p')

async def download_pic(user):
    try:
        p = await client.download_profile_photo(user, file=PIC_FOLDER)
        return os.path.basename(p) if p else "default.png"
    except: return "default.png"

# --- 6. TRACKER ENGINE ---
async def tracker_loop():
    # Wait for login
    while not state['logged_in']: await asyncio.sleep(2)
    
    memory = {}
    while True:
        try:
            async with aiosqlite.connect(DB_FILE) as db:
                async with db.execute('SELECT user_id, name FROM targets') as c: targets = await c.fetchall()
            
            if not targets: await asyncio.sleep(5); continue

            for uid, name in targets:
                try:
                    u = await client.get_entity(uid)
                    stat = 'online' if isinstance(u.status, UserStatusOnline) else 'offline'
                    now = get_time()
                    
                    if stat != memory.get(uid):
                        async with aiosqlite.connect(DB_FILE) as db:
                            await db.execute('UPDATE targets SET status=?, last_seen=? WHERE user_id=?', (stat, now, uid))
                            if stat == 'online':
                                await db.execute('INSERT INTO sessions (user_id, status, start) VALUES (?,?,?)', (uid, 'ONLINE', now))
                            elif memory.get(uid) == 'online':
                                await db.execute('UPDATE sessions SET end=? WHERE user_id=? AND end IS NULL', (now, uid))
                            await db.commit()
                        memory[uid] = stat
                    
                    # Refresh timestamp
                    async with aiosqlite.connect(DB_FILE) as db:
                        await db.execute('UPDATE targets SET last_seen=? WHERE user_id=?', (now, uid))
                        await db.commit()
                except: pass
                await asyncio.sleep(1)
            await asyncio.sleep(5)
        except: await asyncio.sleep(5)

# --- 7. WEBSITE ROUTES ---
CSS = "body{background:#0f172a;color:#fff;font-family:sans-serif;display:flex;justify-content:center;align-items:center;height:100vh;margin:0}.box{background:#1e293b;padding:30px;border-radius:20px;border:1px solid #334155;text-align:center;width:300px;box-shadow:0 10px 25px rgba(0,0,0,0.5)}input{width:100%;padding:12px;margin:10px 0;background:#020617;border:1px solid #334155;color:white;border-radius:8px;box-sizing:border-box}.btn{width:100%;padding:12px;background:#3b82f6;color:white;border:none;border-radius:8px;font-weight:bold;cursor:pointer}.btn:hover{background:#2563eb}"

P_PASS = f"<!doctype html><meta name='viewport' content='width=device-width'><style>{CSS}</style><div class='box'><h3>🔒 Admin Access</h3><form action='/login_pass' method='post'><input type='password' name='pw' placeholder='Password' required><button class='btn'>Unlock</button></form></div>"
P_PHONE = f"<!doctype html><meta name='viewport' content='width=device-width'><style>{CSS}</style><div class='box'><h3>📱 Connect Telegram</h3><p style='color:#94a3b8;font-size:0.9rem'>Enter your phone number</p><form action='/send_otp' method='post'><input name='ph' placeholder='+91...' required><button class='btn'>Get Code</button></form></div>"
P_CODE = f"<!doctype html><meta name='viewport' content='width=device-width'><style>{CSS}</style><div class='box'><h3>🔑 Enter Code</h3><p style='color:#94a3b8;font-size:0.9rem'>Check your Telegram App</p><form action='/verify_otp' method='post'><input name='code' placeholder='12345' required><button class='btn'>Start Tracker</button></form></div>"
P_DASH = f"<!doctype html><meta name='viewport' content='width=device-width'><style>{CSS} .row{{display:flex;justify-content:space-between;background:#334155;padding:12px;border-radius:8px;margin-bottom:10px;text-align:left}}</style><div class='box' style='width:95%;max-width:600px;display:block'><div style='display:flex;justify-content:space-between;margin-bottom:15px'><b>ProTracker Live</b><a href='/logout' style='color:#ef4444;text-decoration:none'>Logout</a></div><form action='/add' method='post' style='margin-bottom:20px;display:flex;gap:5px'><input name='u' placeholder='Username/ID' style='margin:0'><button class='btn' style='width:auto'>Add</button></form>{{ROWS}}</div>"

@app.before_request
def security():
    if request.path.startswith('/static'): return
    if 'auth' not in session and request.path not in ['/web_login', '/login_pass']: return redirect('/web_login')
    if 'auth' in session and not state['logged_in']:
        if request.path not in ['/tg_login', '/send_otp', '/verify_otp']: return redirect('/tg_login')

@app.route('/web_login')
async def web_login(): return P_PASS

@app.route('/login_pass', methods=['POST'])
async def login_pass():
    if (await request.form)['pw'] == ADMIN_PASSWORD:
        session['auth'] = True
        return redirect('/tg_login')
    return redirect('/web_login')

@app.route('/tg_login')
async def tg_login(): return P_PHONE

@app.route('/send_otp', methods=['POST'])
async def send_otp():
    ph = (await request.form)['ph']
    try:
        await client.connect()
        s = await client.send_code_request(ph)
        state['phone'] = ph; state['phone_hash'] = s.phone_code_hash
        return P_CODE
    except Exception as e: return f"Error: {e} <a href='/tg_login'>Retry</a>"

@app.route('/verify_otp', methods=['POST'])
async def verify_otp():
    code = (await request.form)['code']
    try:
        await client.sign_in(state['phone'], code, phone_code_hash=state['phone_hash'])
        state['logged_in'] = True
        return redirect('/')
    except Exception as e: return f"Error: {e} <a href='/tg_login'>Retry</a>"

@app.route('/')
async def index():
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT * FROM targets') as c: rows = await c.fetchall()
    html = "".join([f"<div class='row'><div><b>{r['name']}</b><br><small style='color:#94a3b8'>{r['last_seen']}</small></div><span style='color:{'#4ade80' if r['status']=='online' else '#f87171'}'>{r['status'].upper()}</span></div>" for r in rows])
    return render_template_string(P_DASH.replace("{{ROWS}}", html if html else "<p style='color:#94a3b8'>No targets added</p>"))

@app.route('/add', methods=['POST'])
async def add():
    f = await request.form
    try:
        e = await client.get_entity(int(f['u']) if f['u'].isdigit() else f['u'])
        pic = await download_pic(e)
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute('INSERT OR IGNORE INTO targets (user_id, name, status, last_seen, pic) VALUES (?,?,?,?,?)', (e.id, getattr(e,'username',''), 'Checking...', 'New', pic))
            await db.commit()
    except: pass
    return redirect('/')

@app.route('/logout')
async def logout():
    session.clear()
    state['logged_in'] = False
    return redirect('/web_login')

@app.before_serving
async def startup():
    await init_db()
    app.add_background_task(tracker_loop)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
                              
