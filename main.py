import os, sys, asyncio, secrets, logging

# --- 1. AUTO-INSTALLER ---
try:
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from telethon.tl.types import UserStatusOnline
    from quart import Quart, render_template_string, request, redirect, session
    import aiosqlite
    import python_socks
    import hypercorn.asyncio
except ImportError:
    print("📦 Installing libraries... Please wait.")
    os.system("pip install telethon quart aiosqlite python-socks hypercorn")
    os.execv(sys.executable, ['python'] + sys.argv)

from datetime import datetime
import pytz

# --- 2. CONFIGURATION ---
ADMIN_PASSWORD = "admin" 
API_ID = 9497762
API_HASH = "272c77bf080e4a82846b8ff3dc3df0f4"
DB_FILE = 'tracker.db'
PIC_FOLDER = 'static/profile_pics'

os.makedirs(PIC_FOLDER, exist_ok=True)
logging.basicConfig(level=logging.INFO)

# --- 3. STATE ---
state = {
    "client": None,
    "phone": None,
    "phone_hash": None,
    "logged_in": False
}

app = Quart(__name__, static_folder='static')
app.secret_key = secrets.token_hex(16)

# --- 4. DATABASE & SESSION STORAGE ---
async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        # Targets Table
        await db.execute('CREATE TABLE IF NOT EXISTS targets (id INTEGER PRIMARY KEY, user_id INT UNIQUE, name TEXT, status TEXT, last_seen TEXT, pic TEXT)')
        # Sessions Table (Logs)
        await db.execute('CREATE TABLE IF NOT EXISTS sessions (id INTEGER PRIMARY KEY, user_id INT, status TEXT, start TEXT, end TEXT, dur TEXT)')
        # Config Table (Stores the Session Key)
        await db.execute('CREATE TABLE IF NOT EXISTS app_config (key TEXT PRIMARY KEY, value TEXT)')
        await db.commit()

async def save_session_key(key_str):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute('INSERT OR REPLACE INTO app_config (key, value) VALUES (?, ?)', ('session_key', key_str))
        await db.commit()

async def load_session_key():
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT value FROM app_config WHERE key='session_key'") as c:
            row = await c.fetchone()
            return row[0] if row else None

async def delete_session_key():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("DELETE FROM app_config WHERE key='session_key'")
        await db.commit()

# --- 5. HELPERS ---
def get_time(): return datetime.now(pytz.timezone('Asia/Kolkata')).strftime('%I:%M %p')

async def download_pic(user):
    try:
        p = await state['client'].download_profile_photo(user, file=PIC_FOLDER)
        return os.path.basename(p) if p else "default.png"
    except: return "default.png"

# --- 6. TRACKER ENGINE ---
async def tracker_loop():
    # Attempt Auto-Login on Startup
    saved_key = await load_session_key()
    if saved_key:
        print("🔄 Found saved session! logging in...")
        try:
            state['client'] = TelegramClient(StringSession(saved_key), API_ID, API_HASH)
            await state['client'].connect()
            if await state['client'].is_user_authorized():
                state['logged_in'] = True
                print("✅ Auto-Login Successful!")
        except Exception as e:
            print(f"❌ Auto-Login Failed: {e}")

    # Wait for manual login if auto-login failed
    while not state['logged_in']: await asyncio.sleep(2)
    
    memory = {}
    print("🚀 Tracker is RUNNING")
    while True:
        try:
            if not state['client'] or not await state['client'].is_user_authorized():
                state['logged_in'] = False; continue

            async with aiosqlite.connect(DB_FILE) as db:
                async with db.execute('SELECT user_id, name FROM targets') as c: targets = await c.fetchall()
            
            if not targets: await asyncio.sleep(5); continue

            for uid, name in targets:
                try:
                    u = await state['client'].get_entity(uid)
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
                    
                    async with aiosqlite.connect(DB_FILE) as db:
                        await db.execute('UPDATE targets SET last_seen=? WHERE user_id=?', (now, uid))
                        await db.commit()
                except: pass
                await asyncio.sleep(1)
            await asyncio.sleep(4)
        except: await asyncio.sleep(5)

# --- 7. ROUTES ---
CSS = "body{background:#0f172a;color:#fff;font-family:sans-serif;display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0}.box{background:#1e293b;padding:30px;border-radius:20px;border:1px solid #334155;text-align:center;width:300px;box-shadow:0 10px 25px rgba(0,0,0,0.5)}input{width:100%;padding:12px;margin:10px 0;background:#020617;border:1px solid #334155;color:white;border-radius:8px;box-sizing:border-box}.btn{width:100%;padding:12px;background:#3b82f6;color:white;border:none;border-radius:8px;font-weight:bold;cursor:pointer}.btn:hover{background:#2563eb}"

P_PASS = f"<!doctype html><meta name='viewport' content='width=device-width'><style>{CSS}</style><div class='box'><h3>🔒 Access Control</h3><form action='/login_pass' method='post'><input type='password' name='pw' placeholder='Admin Password' required><button class='btn'>Unlock</button></form></div>"

P_LOGIN = f"<!doctype html><meta name='viewport' content='width=device-width'><style>{CSS}</style><div class='box'><h3>📱 User Login</h3><p style='color:#94a3b8'>Enter phone to start tracking.</p><form action='/send_otp' method='post'><input name='ph' placeholder='+91...' required><button class='btn'>Get Code</button></form></div>"

P_CODE = f"<!doctype html><meta name='viewport' content='width=device-width'><style>{CSS}</style><div class='box'><h3>🔑 Verify</h3><p style='color:#94a3b8'>Check Telegram App</p><form action='/verify_otp' method='post'><input name='code' placeholder='12345' required><button class='btn'>Verify & Save</button></form></div>"

P_DASH = f"<!doctype html><meta name='viewport' content='width=device-width'><style>{CSS} .row{{display:flex;justify-content:space-between;background:#334155;padding:12px;border-radius:8px;margin-bottom:10px;text-align:left}}</style><div class='box' style='width:95%;max-width:600px;display:block'><div style='display:flex;justify-content:space-between;margin-bottom:15px'><b>ProTracker</b><a href='/logout' style='color:#ef4444;text-decoration:none'>Logout</a></div><form action='/add' method='post' style='margin-bottom:20px;display:flex;gap:5px'><input name='u' placeholder='Username/ID' style='margin:0'><button class='btn' style='width:auto'>Add</button></form>{{ROWS}}</div>"

@app.before_request
def guard():
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
        return redirect('/') # Will redirect to tg_login if not logged in
    return redirect('/web_login')

@app.route('/tg_login')
async def tg_login(): return P_LOGIN

@app.route('/send_otp', methods=['POST'])
async def send_otp():
    ph = (await request.form)['ph']
    try:
        state['client'] = TelegramClient(StringSession(), API_ID, API_HASH)
        await state['client'].connect()
        s = await state['client'].send_code_request(ph)
        state['phone'] = ph; state['phone_hash'] = s.phone_code_hash
        return P_CODE
    except Exception as e: return f"Error: {e} <a href='/tg_login'>Retry</a>"

@app.route('/verify_otp', methods=['POST'])
async def verify_otp():
    code = (await request.form)['code']
    try:
        await state['client'].sign_in(state['phone'], code, phone_code_hash=state['phone_hash'])
        
        # --- AUTO-SAVE KEY ---
        key_to_save = StringSession.save(state['client'].session)
        await save_session_key(key_to_save)
        # ---------------------
        
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
        e = await state['client'].get_entity(int(f['u']) if f['u'].isdigit() else f['u'])
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
    state['client'] = None
    # OPTIONAL: Delete key from DB on logout so next user must login
    await delete_session_key() 
    return redirect('/web_login')

@app.before_serving
async def startup():
    await init_db()
    app.add_background_task(tracker_loop)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)

                              
