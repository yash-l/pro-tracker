import os, sys, asyncio, secrets, logging, io, csv

# --- 1. AUTO-INSTALLER ---
try:
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from telethon.tl.types import UserStatusOnline
    from quart import Quart, render_template_string, request, redirect, session, send_file, Response
    import aiosqlite
    import python_socks
    import hypercorn.asyncio
    import pytz # Added check for pytz
except ImportError:
    print("📦 Installing libraries... Please wait.")
    # Added pytz to installation list
    os.system("pip install telethon quart aiosqlite python-socks hypercorn pytz")
    os.execv(sys.executable, ['python'] + sys.argv)

from datetime import datetime

# --- 2. CONFIGURATION ---
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
        await db.execute('CREATE TABLE IF NOT EXISTS targets (id INTEGER PRIMARY KEY, user_id INT UNIQUE, name TEXT, status TEXT, last_seen TEXT, pic TEXT)')
        await db.execute('CREATE TABLE IF NOT EXISTS sessions (id INTEGER PRIMARY KEY, user_id INT, status TEXT, start TEXT, end TEXT, dur TEXT)')
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
    # Attempt Auto-Login
    saved_key = await load_session_key()
    if saved_key:
        print("🔄 Found saved session! Logging in...")
        try:
            state['client'] = TelegramClient(StringSession(saved_key), API_ID, API_HASH)
            await state['client'].connect()
            if await state['client'].is_user_authorized():
                state['logged_in'] = True
                print("✅ Auto-Login Successful!")
        except Exception as e:
            print(f"❌ Auto-Login Failed: {e}")

    # Wait for login
    while not state['logged_in']: await asyncio.sleep(2)
    
    memory = {}
    print("🚀 Tracker is RUNNING")
    
    while True:
        try:
            # Re-check connection
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
                        # DB Update
                        async with aiosqlite.connect(DB_FILE) as db:
                            await db.execute('UPDATE targets SET status=?, last_seen=? WHERE user_id=?', (stat, now, uid))
                            if stat == 'online':
                                await db.execute('INSERT INTO sessions (user_id, status, start) VALUES (?,?,?)', (uid, 'ONLINE', now))
                                # 🔔 SEND ALERT TO SAVED MESSAGES
                                try: await state['client'].send_message('me', f"🟢 <b>{name}</b> came ONLINE at {now}", parse_mode='html')
                                except: pass
                            elif memory.get(uid) == 'online':
                                await db.execute('UPDATE sessions SET end=? WHERE user_id=? AND end IS NULL', (now, uid))
                            await db.commit()
                        memory[uid] = stat
                    
                    # Heartbeat Update
                    async with aiosqlite.connect(DB_FILE) as db:
                        await db.execute('UPDATE targets SET last_seen=? WHERE user_id=?', (now, uid))
                        await db.commit()
                except: pass
                await asyncio.sleep(1)
            await asyncio.sleep(4)
        except: await asyncio.sleep(5)

# --- 7. UI & ROUTES ---
TAILWIND = '<script src="https://cdn.tailwindcss.com"></script><style>body { background-color: #0f172a; font-family: "Inter", sans-serif; }</style>'

P_LOGIN = f"""
<!DOCTYPE html>
<html class="dark">
<head><meta name="viewport" content="width=device-width, initial-scale=1">{TAILWIND}</head>
<body class="flex items-center justify-center min-h-screen text-white">
    <div class="bg-slate-800 p-8 rounded-2xl shadow-2xl w-full max-w-sm border border-slate-700">
        <h2 class="text-2xl font-bold text-center mb-6 text-cyan-400">⚡ ProTracker V17</h2>
        <form action="/send_otp" method="post" class="space-y-4">
            <div>
                <label class="block text-sm text-slate-400 mb-1">Phone Number</label>
                <input name="ph" placeholder="+91..." class="w-full bg-slate-900 border border-slate-600 rounded-lg p-3 focus:outline-none focus:border-cyan-500 transition" required>
            </div>
            <button class="w-full bg-gradient-to-r from-cyan-500 to-blue-600 p-3 rounded-lg font-bold hover:opacity-90 transition">Get Code</button>
        </form>
    </div>
</body></html>
"""

P_CODE = f"""
<!DOCTYPE html>
<html class="dark">
<head><meta name="viewport" content="width=device-width, initial-scale=1">{TAILWIND}</head>
<body class="flex items-center justify-center min-h-screen text-white">
    <div class="bg-slate-800 p-8 rounded-2xl shadow-2xl w-full max-w-sm border border-slate-700">
        <h2 class="text-xl font-bold text-center mb-2">🔐 Verify</h2>
        <p class="text-slate-400 text-center text-sm mb-6">Check your Telegram App for the code</p>
        <form action="/verify_otp" method="post" class="space-y-4">
            <input name="code" placeholder="12345" class="w-full bg-slate-900 border border-slate-600 rounded-lg p-3 text-center tracking-widest text-lg focus:outline-none focus:border-green-500" required>
            <button class="w-full bg-green-600 p-3 rounded-lg font-bold hover:bg-green-500 transition">Start Tracking</button>
        </form>
    </div>
</body></html>
"""

P_DASH = f"""
<!DOCTYPE html>
<html class="dark">
<head><meta name="viewport" content="width=device-width, initial-scale=1">{TAILWIND}</head>
<body class="text-slate-200 pb-20">
    <nav class="sticky top-0 z-50 bg-slate-900/80 backdrop-blur-md border-b border-slate-800 px-4 py-4 flex justify-between items-center">
        <div class="font-bold text-xl tracking-tight bg-gradient-to-r from-cyan-400 to-purple-500 bg-clip-text text-transparent">ProTracker V17</div>
        <a href="/logout" class="text-xs font-semibold text-red-400 hover:text-red-300">LOGOUT</a>
    </nav>

    <div class="max-w-2xl mx-auto p-4 space-y-6">
        <div class="bg-slate-800 rounded-xl p-4 border border-slate-700 shadow-lg">
            <form action="/add" method="post" class="flex gap-2">
                <input name="u" placeholder="Username / ID" class="flex-1 bg-slate-900 border border-slate-600 rounded-lg p-3 text-sm focus:outline-none focus:border-cyan-500">
                <button class="bg-cyan-600 px-6 rounded-lg font-bold hover:bg-cyan-500 transition">+</button>
            </form>
        </div>

        <div class="grid gap-4">{{ROWS}}</div>
        
        <div class="text-center pt-8">
            <a href="/download_db" class="text-sm text-slate-500 hover:text-white transition underline">Download All Logs (CSV)</a>
        </div>
    </div>
</body></html>
"""

@app.before_request
def guard():
    if request.path.startswith('/static'): return
    if not state['logged_in']:
        if request.path not in ['/tg_login', '/send_otp', '/verify_otp']: return redirect('/tg_login')

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
    try:
        await state['client'].sign_in(state['phone'], (await request.form)['code'], phone_code_hash=state['phone_hash'])
        await save_session_key(StringSession.save(state['client'].session))
        state['logged_in'] = True
        return redirect('/')
    except Exception as e: return f"Error: {e} <a href='/tg_login'>Retry</a>"

@app.route('/')
async def index():
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT * FROM targets') as c: rows = await c.fetchall()
    
    html = ""
    for r in rows:
        status_color = "text-green-400" if r['status'] == 'online' else "text-slate-500"
        border_color = "border-green-500/50" if r['status'] == 'online' else "border-slate-700"
        bg_pulse = "bg-green-500/10" if r['status'] == 'online' else "bg-slate-800"
        
        html += f"""
        <div class="{bg_pulse} p-4 rounded-xl border {border_color} flex items-center justify-between transition hover:scale-[1.01]">
            <div class="flex items-center gap-4">
                <img src="/static/profile_pics/{r['pic']}" class="w-12 h-12 rounded-full object-cover ring-2 ring-slate-700">
                <div>
                    <div class="font-bold text-lg text-white">{r['name']}</div>
                    <div class="text-xs text-slate-400 font-mono">Last Seen: {r['last_seen']}</div>
                </div>
            </div>
            <div class="text-right">
                <div class="font-bold text-xs uppercase tracking-wider {status_color}">{r['status']}</div>
            </div>
        </div>
        """
    return render_template_string(P_DASH.replace("{{ROWS}}", html if html else "<div class='text-center text-slate-500 py-10'>No targets tracking</div>"))

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

@app.route('/download_db')
async def download_db():
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute('SELECT * FROM sessions ORDER BY id DESC') as c: rows = await c.fetchall()
    
    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow(['ID', 'User ID', 'Status', 'Start Time', 'End Time', 'Duration'])
    cw.writerows(rows)
    return Response(si.getvalue(), mimetype='text/csv', headers={"Content-Disposition": "attachment; filename=tracker_logs.csv"})

@app.route('/logout')
async def logout():
    session.clear()
    state['logged_in'] = False
    state['client'] = None
    await delete_session_key() 
    return redirect('/tg_login')

@app.before_serving
async def startup():
    await init_db()
    app.add_background_task(tracker_loop)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
        
