import logging
import asyncio
import os
import sys
import json
import pytz
import aiosqlite
import secrets
from datetime import datetime
from telethon import TelegramClient
from telethon.tl.types import UserStatusOnline
from quart import Quart, render_template_string, request, redirect, session

# --- CONFIGURATION ---
# 1. SET YOUR WEB PASSWORD HERE
ADMIN_PASSWORD = "admin"

# 2. TELEGRAM KEYS
API_ID = 9497762
API_HASH = "272c77bf080e4a82846b8ff3dc3df0f4"

DB_FILE = 'tracker.db'
PIC_FOLDER = 'static/profile_pics'
os.makedirs(PIC_FOLDER, exist_ok=True)
logging.basicConfig(level=logging.INFO)

# --- SETUP ---
# Random session forces manual login every restart
session_id = f"session_{secrets.token_hex(4)}"
client = TelegramClient(session_id, API_ID, API_HASH)

state = {"phone": None, "phone_hash": None, "logged_in": False}

app = Quart(__name__, static_folder='static')
app.secret_key = secrets.token_hex(16)

# --- DATABASE ---
async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute('CREATE TABLE IF NOT EXISTS targets (id INTEGER PRIMARY KEY, user_id INT UNIQUE, name TEXT, status TEXT, last_seen TEXT, pic TEXT)')
        await db.execute('CREATE TABLE IF NOT EXISTS sessions (id INTEGER PRIMARY KEY, user_id INT, status TEXT, start TEXT, end TEXT, dur TEXT)')
        await db.commit()

# --- HELPERS ---
def get_time(): return datetime.now(pytz.timezone('Asia/Kolkata')).strftime('%I:%M %p')

async def download_pic(user):
    try:
        p = await client.download_profile_photo(user, file=PIC_FOLDER)
        return os.path.basename(p) if p else "default.png"
    except: return "default.png"

# --- TRACKER ENGINE ---
async def tracker_loop():
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
                    
                    # Update Last Seen text
                    async with aiosqlite.connect(DB_FILE) as db:
                        await db.execute('UPDATE targets SET last_seen=? WHERE user_id=?', (now, uid))
                        await db.commit()
                except: pass
                await asyncio.sleep(1)
            await asyncio.sleep(5)
        except: await asyncio.sleep(5)

# --- SECURITY MIDDLEWARE ---
@app.before_request
def check_security():
    if request.path.startswith('/static'): return
    # 1. Web Password Check
    if 'web_user' not in session and request.path not in ['/web_login', '/do_web_login']:
        return redirect('/web_login')
    # 2. Telegram Login Check
    if 'web_user' in session and not state['logged_in']:
        if request.path not in ['/tg_login', '/send_code', '/verify_code', '/logout']:
            return redirect('/tg_login')

# --- HTML TEMPLATES ---
CSS = "body{background:#0f172a;color:#fff;font-family:sans-serif;display:flex;justify-content:center;align-items:center;height:100vh;margin:0}.box{background:#1e293b;padding:30px;border-radius:20px;border:1px solid #334155;text-align:center;width:300px}input{width:100%;padding:12px;margin:10px 0;background:#020617;border:1px solid #334155;color:white;border-radius:8px;box-sizing:border-box}.btn{width:100%;padding:12px;background:#3b82f6;color:white;border:none;border-radius:8px;font-weight:bold;cursor:pointer}"

PAGE_WEB = f"<!doctype html><meta name='viewport' content='width=device-width'><style>{CSS}</style><div class='box'><h3>🔒 Secure Access</h3><form action='/do_web_login' method='post'><input type='password' name='password' placeholder='Enter Password' required><button class='btn'>Unlock</button></form></div>"
PAGE_TG = f"<!doctype html><meta name='viewport' content='width=device-width'><style>{CSS}</style><div class='box'><h3>🚀 Connect Telegram</h3><p style='color:#94a3b8'>Enter phone to start.</p><form action='/send_code' method='post'><input name='phone' placeholder='+91...' required><button class='btn'>Get OTP</button></form></div>"
PAGE_OTP = f"<!doctype html><meta name='viewport' content='width=device-width'><style>{CSS}</style><div class='box'><h3>Verify Identity</h3><p style='color:#94a3b8'>Check your Telegram app.</p><form action='/verify_code' method='post'><input name='code' placeholder='12345' required><button class='btn'>Start Tracker</button></form></div>"
PAGE_DASH = f"<!doctype html><meta name='viewport' content='width=device-width'><style>{CSS} .row{{display:flex;justify-content:space-between;background:#334155;padding:12px;border-radius:8px;margin-bottom:10px}}</style><div class='box' style='width:90%;max-width:600px;display:block'><div style='display:flex;justify-content:space-between;margin-bottom:20px'><b>ProTracker Active</b><a href='/logout' style='color:#ef4444;text-decoration:none'>Logout</a></div><form action='/add' method='post' style='margin-bottom:20px'><input name='u' placeholder='Username or ID'><button class='btn'>Add Target</button></form>{{ROWS}}</div>"

# --- ROUTES ---
@app.route('/web_login')
async def web_login(): return PAGE_WEB

@app.route('/do_web_login', methods=['POST'])
async def do_web_login():
    if (await request.form)['password'] == ADMIN_PASSWORD:
        session['web_user'] = True
        return redirect('/tg_login')
    return redirect('/web_login')

@app.route('/tg_login')
async def tg_login(): return PAGE_TG

@app.route('/send_code', methods=['POST'])
async def send_code():
    form = await request.form
    try:
        await client.connect()
        sent = await client.send_code_request(form['phone'])
        state['phone'] = form['phone']; state['phone_hash'] = sent.phone_code_hash
        return PAGE_OTP
    except Exception as e: return f"Error: {e} <a href='/tg_login'>Retry</a>"

@app.route('/verify_code', methods=['POST'])
async def verify_code():
    form = await request.form
    try:
        await client.sign_in(state['phone'], form['code'], phone_code_hash=state['phone_hash'])
        state['logged_in'] = True
        return redirect('/')
    except Exception as e: return f"Error: {e} <a href='/tg_login'>Retry</a>"

@app.route('/logout')
async def logout():
    session.clear()
    state['logged_in'] = False
    return redirect('/web_login')

@app.route('/')
async def index():
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT * FROM targets') as c: rows = await c.fetchall()
    html_rows = "".join([f"<div class='row'><b>{r['name']}</b> <span>{r['status']}</span></div>" for r in rows])
    return render_template_string(PAGE_DASH.replace("{{ROWS}}", html_rows))

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

@app.before_serving
async def startup():
    await init_db()
    app.add_background_task(tracker_loop)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
    
