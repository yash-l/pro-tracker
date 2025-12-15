import os, sys, asyncio, secrets, logging, io, csv

# --- 1. LIGHTWEIGHT AUTO-INSTALLER ---
try:
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from telethon.tl.types import UserStatusOnline
    from quart import Quart, render_template_string, request, redirect, session, Response
    import aiosqlite
    import python_socks
    import hypercorn.asyncio
    import pytz
except ImportError:
    print("⚡ Optimizing & Installing libraries...")
    os.system("pip install telethon quart aiosqlite python-socks hypercorn pytz")
    os.execv(sys.executable, ['python'] + sys.argv)

from datetime import datetime

# --- 2. CONFIGURATION ---
API_ID = 9497762
API_HASH = "272c77bf080e4a82846b8ff3dc3df0f4"
DB_FILE = 'tracker.db'
PIC_FOLDER = 'static/profile_pics'
POLL_INTERVAL = 15  # Seconds between checks

os.makedirs(PIC_FOLDER, exist_ok=True)
logging.basicConfig(level=logging.ERROR) 

# --- 3. STATE ---
state = {"client": None, "phone": None, "phone_hash": None, "logged_in": False}

app = Quart(__name__, static_folder='static')
app.secret_key = secrets.token_hex(16)

# --- 4. DATABASE ---
async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute('CREATE TABLE IF NOT EXISTS targets (id INTEGER PRIMARY KEY, user_id INT UNIQUE, name TEXT, status TEXT, last_seen TEXT, pic TEXT)')
        await db.execute('CREATE TABLE IF NOT EXISTS sessions (id INTEGER PRIMARY KEY, user_id INT, status TEXT, start TEXT, end TEXT, dur TEXT)')
        await db.execute('CREATE TABLE IF NOT EXISTS app_config (key TEXT PRIMARY KEY, value TEXT)')
        await db.commit()

async def manage_session(action, key=None):
    async with aiosqlite.connect(DB_FILE) as db:
        if action == "save":
            await db.execute('INSERT OR REPLACE INTO app_config (key, value) VALUES (?, ?)', ('session_key', key))
        elif action == "load":
            async with db.execute("SELECT value FROM app_config WHERE key='session_key'") as c:
                row = await c.fetchone()
                return row[0] if row else None
        elif action == "delete":
            await db.execute("DELETE FROM app_config WHERE key='session_key'")
        await db.commit()

# --- 5. HELPERS ---
def get_time(): return datetime.now(pytz.timezone('Asia/Kolkata')).strftime('%I:%M %p')

async def download_pic(user):
    try:
        p = await state['client'].download_profile_photo(user, file=PIC_FOLDER)
        return os.path.basename(p) if p else "default.png"
    except: return "default.png"

# --- 6. OPTIMIZED TRACKER ENGINE ---
async def tracker_loop():
    # Auto-Resume Logic
    saved_key = await manage_session("load")
    if saved_key:
        try:
            state['client'] = TelegramClient(StringSession(saved_key), API_ID, API_HASH)
            await state['client'].connect()
            if await state['client'].is_user_authorized():
                state['logged_in'] = True
                print("✅ System Resumed Successfully")
        except: pass

    while not state['logged_in']: await asyncio.sleep(5)
    
    memory = {}
    print(f"🚀 Tracking Active (Interval: {POLL_INTERVAL}s)")
    
    while True:
        try:
            if not state['client'] or not await state['client'].is_user_authorized():
                state['logged_in'] = False; continue

            async with aiosqlite.connect(DB_FILE) as db:
                async with db.execute('SELECT user_id, name FROM targets') as c: targets = await c.fetchall()
            
            if not targets: await asyncio.sleep(POLL_INTERVAL); continue

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
                                try: await state['client'].send_message('me', f"🟢 <b>{name}</b> is ONLINE", parse_mode='html')
                                except: pass
                            elif memory.get(uid) == 'online':
                                await db.execute('UPDATE sessions SET end=? WHERE user_id=? AND end IS NULL', (now, uid))
                            await db.commit()
                        memory[uid] = stat
                    
                    # Heartbeat
                    async with aiosqlite.connect(DB_FILE) as db:
                        await db.execute('UPDATE targets SET last_seen=? WHERE user_id=?', (now, uid))
                        await db.commit()
                except: pass
                await asyncio.sleep(0.5) 
            
            await asyncio.sleep(POLL_INTERVAL)
        except Exception: await asyncio.sleep(POLL_INTERVAL)

# --- 7. COMPACT UI & ROUTES ---
TAILWIND = '<script src="https://cdn.tailwindcss.com"></script><style>body{background:#0f172a;font-family:sans-serif}</style>'

P_LOGIN = f"""<!DOCTYPE html><html class="dark"><head><meta name="viewport" content="width=device-width">{TAILWIND}</head><body class="flex items-center justify-center min-h-screen text-white"><div class="bg-slate-800 p-8 rounded-xl border border-slate-700 w-80"><h2 class="text-xl font-bold mb-4 text-cyan-400">⚡ Tracker V18.1</h2><form action="/send_otp" method="post" class="space-y-4"><input name="ph" placeholder="+91..." class="w-full bg-slate-900 border border-slate-600 rounded p-2 outline-none focus:border-cyan-500" required><button class="w-full bg-cyan-600 p-2 rounded font-bold hover:opacity-90">Login</button></form></div></body></html>"""

P_CODE = f"""<!DOCTYPE html><html class="dark"><head><meta name="viewport" content="width=device-width">{TAILWIND}</head><body class="flex items-center justify-center min-h-screen text-white"><div class="bg-slate-800 p-8 rounded-xl border border-slate-700 w-80"><h2 class="text-xl font-bold mb-2">🔐 Verify</h2><p class="text-sm text-slate-400 mb-4">Check Telegram App</p><form action="/verify_otp" method="post" class="space-y-4"><input name="code" placeholder="12345" class="w-full bg-slate-900 border border-slate-600 rounded p-2 text-center tracking-widest outline-none focus:border-green-500" required><button class="w-full bg-green-600 p-2 rounded font-bold">Start</button></form></div></body></html>"""

P_DASH = f"""<!DOCTYPE html><html class="dark"><head><meta name="viewport" content="width=device-width">{TAILWIND}</head><body class="text-slate-200 pb-20"><nav class="sticky top-0 z-50 bg-slate-900/90 backdrop-blur border-b border-slate-800 p-4 flex justify-between items-center"><div class="font-bold text-lg text-cyan-400">ProTracker</div><a href="/logout" class="text-xs text-red-400 font-bold">EXIT</a></nav><div class="max-w-md mx-auto p-4 space-y-4"><div class="bg-slate-800 rounded-lg p-3 border border-slate-700"><form action="/add" method="post" class="flex gap-2"><input name="u" placeholder="User/ID" class="flex-1 bg-slate-900 border border-slate-600 rounded p-2 text-sm outline-none focus:border-cyan-500"><button class="bg-cyan-600 px-4 rounded font-bold">+</button></form></div><div class="grid gap-3">{{ROWS}}</div><div class="text-center pt-6"><a href="/download_db" class="text-xs text-slate-500 underline">Export Logs</a></div></div></body></html>"""

@app.before_request
def guard():
    if request.path.startswith('/static'): return
    if not state['logged_in'] and request.path not in ['/tg_login', '/send_otp', '/verify_otp']:
        return redirect('/tg_login')

@app.route('/tg_login')
async def tg_login(): return P_LOGIN

@app.route('/send_otp', methods=['POST'])
async def send_otp():
    try:
        state['client'] = TelegramClient(StringSession(), API_ID, API_HASH)
        await state['client'].connect()
        s = await state['client'].send_code_request((await request.form)['ph'])
        state['phone_hash'] = s.phone_code_hash
        state['phone'] = (await request.form)['ph']
        return P_CODE
    except Exception as e: return f"Error: {e} <a href='/tg_login'>Back</a>"

@app.route('/verify_otp', methods=['POST'])
async def verify_otp():
    try:
        await state['client'].sign_in(state['phone'], (await request.form)['code'], phone_code_hash=state['phone_hash'])
        await manage_session("save", StringSession.save(state['client'].session))
        state['logged_in'] = True
        return redirect('/')
    except Exception as e: return f"Error: {e} <a href='/tg_login'>Back</a>"

@app.route('/')
async def index():
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM targets ORDER BY CASE WHEN status='online' THEN 0 ELSE 1 END, name ASC") as c: 
            rows = await c.fetchall()
    
    html = ""
    for r in rows:
        color = "text-green-400" if r['status'] == 'online' else "text-slate-500"
        border = "border-green-500/40" if r['status'] == 'online' else "border-slate-700"
        bg = "bg-green-500/10" if r['status'] == 'online' else "bg-slate-800"
        html += f"""<div class="{bg} p-3 rounded-lg border {border} flex items-center justify-between"><div class="flex items-center gap-3"><img src="/static/profile_pics/{r['pic']}" class="w-10 h-10 rounded-full bg-slate-900 object-cover"><div><div class="font-bold text-sm text-white">{r['name']}</div><div class="text-[10px] text-slate-400">{r['last_seen']}</div></div></div><div class="font-bold text-[10px] uppercase {color}">{r['status']}</div></div>"""
    
    # FIX: Added 'await' here
    return await render_template_string(P_DASH.replace("{{ROWS}}", html if html else "<div class='text-center text-slate-500 text-sm'>No targets</div>"))

@app.route('/add', methods=['POST'])
async def add():
    try:
        u = (await request.form)['u']
        e = await state['client'].get_entity(int(u) if u.isdigit() else u)
        pic = await download_pic(e)
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute('INSERT OR IGNORE INTO targets (user_id, name, status, last_seen, pic) VALUES (?,?,?,?,?)', (e.id, getattr(e,'username',''), '...', 'Now', pic))
            await db.commit()
    except: pass
    return redirect('/')

@app.route('/download_db')
async def download_db():
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute('SELECT * FROM sessions ORDER BY id DESC') as c: rows = await c.fetchall()
    si = io.StringIO(); cw = csv.writer(si); cw.writerows(rows)
    return Response(si.getvalue(), mimetype='text/csv', headers={"Content-Disposition": "attachment; filename=logs.csv"})

@app.route('/logout')
async def logout():
    session.clear(); state['logged_in'] = False; state['client'] = None
    await manage_session("delete")
    return redirect('/tg_login')

@app.before_serving
async def startup():
    await init_db()
    app.add_background_task(tracker_loop)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
                             
