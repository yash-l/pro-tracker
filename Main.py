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
from telethon import TelegramClient, errors
from telethon.tl.types import UserStatusOnline
import python_socks
from quart import Quart, render_template_string, request, redirect, Response, session, url_for

# --- CONFIGURATION ---
DB_FILE = 'tracker.db'
PIC_FOLDER = 'static/profile_pics'

# Hardcoded API Keys (Since we need them before config loads)
API_ID = 9497762
API_HASH = "272c77bf080e4a82846b8ff3dc3df0f4"

DEFAULT_CONFIG = {
    "chat_id": 1184218529,
    "timezone": "Asia/Kolkata",
    "admin_username": "admin",
    "admin_password": "password",
    "secret_key": secrets.token_hex(16),
    "is_setup_done": True # We assume setup is done for cloud simplicity
}

os.makedirs(PIC_FOLDER, exist_ok=True)
logging.basicConfig(level=logging.INFO)

# --- GLOBAL STATE ---
# We use a global variable to store the phone hash during login flow
login_state = {
    "phone": None,
    "phone_code_hash": None,
    "is_authorized": False
}

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

# --- APP SETUP ---
app = Quart(__name__, static_folder='static')
# Use a static secret key if possible, or generate one
app.secret_key = DEFAULT_CONFIG['secret_key'] 

# Fix cookies for Cloud
app.config['SESSION_COOKIE_SECURE'] = False
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# Initialize Client (Without starting it yet)
client = TelegramClient('anon_session', API_ID, API_HASH)

# --- HELPERS ---
def get_tz(): return pytz.timezone(DEFAULT_CONFIG['timezone'])
def fmt_time(dt): 
    if not dt: return "-"
    if dt.tzinfo is None: dt = dt.replace(tzinfo=pytz.utc)
    return dt.astimezone(get_tz()).strftime('%I:%M %p')

async def download_pic(user_entity):
    try:
        path = await client.download_profile_photo(user_entity, file=PIC_FOLDER)
        return os.path.basename(path) if path else "default.png"
    except: return "default.png"

# --- TRACKER LOGIC ---
async def update_target(uid, status, last):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute('UPDATE targets SET current_status = ?, last_seen = ? WHERE user_id = ?', (status, last, uid))
        await db.commit()

async def log_session(uid, status, time):
    async with aiosqlite.connect(DB_FILE) as db:
        c = await db.execute('SELECT id, start_time FROM sessions WHERE user_id = ? AND end_time IS NULL ORDER BY id DESC LIMIT 1', (uid,))
        last = await c.fetchone()
        if last: await db.execute('UPDATE sessions SET end_time = ? WHERE id = ?', (time, last[0]))
        await db.execute('INSERT INTO sessions (user_id, status, start_time) VALUES (?, ?, ?)', (uid, status, time))
        await db.commit()

async def tracker_loop():
    # Wait until user logs in via Web
    while not login_state['is_authorized']:
        await asyncio.sleep(2)
        try:
            if await client.is_user_authorized():
                login_state['is_authorized'] = True
                logging.info("User Authorized! Starting Tracker...")
        except: pass

    # Main Loop
    memory = {}
    while True:
        try:
            async with aiosqlite.connect(DB_FILE) as db:
                async with db.execute('SELECT user_id, display_name FROM targets') as c: targets = await c.fetchall()
            
            if not targets: await asyncio.sleep(5); continue

            for (uid, name) in targets:
                try:
                    u = await client.get_entity(uid)
                    status = 'online' if isinstance(u.status, UserStatusOnline) else 'offline'
                    now = fmt_time(datetime.now(pytz.utc))
                    
                    if status != memory.get(uid):
                        await update_target(uid, status, now)
                        if status == 'online': 
                            await log_session(uid, 'ONLINE', now)
                        elif memory.get(uid) == 'online':
                            # Close session
                            async with aiosqlite.connect(DB_FILE) as db:
                                await db.execute('UPDATE sessions SET end_time = ? WHERE user_id = ? AND end_time IS NULL', (now, uid))
                                await db.commit()
                        memory[uid] = status
                    
                    # Always update "Last Seen" text
                    await update_target(uid, status, now)
                except Exception as e: logging.error(f"Err {uid}: {e}")
                await asyncio.sleep(1)
            await asyncio.sleep(5)
        except Exception as e:
            logging.error(f"Loop Err: {e}")
            await asyncio.sleep(5)

# --- HTML TEMPLATES ---
STYLE = """<meta name='viewport' content='width=device-width,initial-scale=1'><style>body{background:#0f172a;color:white;font-family:sans-serif;display:flex;justify-content:center;align-items:center;height:100vh;margin:0}.card{background:#1e293b;padding:30px;border-radius:20px;width:100%;max-width:350px;border:1px solid #334155;text-align:center}input{width:100%;padding:12px;margin:10px 0;background:#020617;border:1px solid #334155;color:white;border-radius:8px;box-sizing:border-box}.btn{width:100%;padding:12px;background:#3b82f6;color:white;border:none;border-radius:8px;font-weight:bold;cursor:pointer}</style>"""

PAGE_TG_PHONE = f"{STYLE}<div class='card'><h3>Telegram Login</h3><p>Enter your phone number to start the tracker.</p><form action='/tg_send' method='post'><input name='phone' placeholder='+91...' required><button class='btn'>Send Code</button></form></div>"
PAGE_TG_CODE = f"{STYLE}<div class='card'><h3>Enter OTP</h3><p>Check your Telegram App.</p><form action='/tg_verify' method='post'><input name='code' placeholder='12345' required><button class='btn'>Verify</button></form></div>"
PAGE_DASH = f"{STYLE}<div class='card'><h3>Running...</h3><p>Tracker is active.</p><a href='/targets' class='btn' style='display:block;text-decoration:none'>View Targets</a></div>"

# --- ROUTES ---
@app.route('/')
async def home():
    if not login_state['is_authorized']:
        # Double check if session file already exists and works
        if await client.is_user_authorized():
            login_state['is_authorized'] = True
            return redirect('/targets')
        return redirect('/tg_login')
    return redirect('/targets')

@app.route('/tg_login')
async def tg_login(): return PAGE_TG_PHONE

@app.route('/tg_send', methods=['POST'])
async def tg_send():
    form = await request.form
    phone = form['phone']
    try:
        await client.connect()
        sent = await client.send_code_request(phone)
        login_state['phone'] = phone
        login_state['phone_code_hash'] = sent.phone_code_hash
        return render_template_string(PAGE_TG_CODE)
    except Exception as e:
        return f"Error: {e} <a href='/tg_login'>Try Again</a>"

@app.route('/tg_verify', methods=['POST'])
async def tg_verify():
    form = await request.form
    code = form['code']
    try:
        await client.sign_in(login_state['phone'], code, phone_code_hash=login_state['phone_code_hash'])
        login_state['is_authorized'] = True
        return redirect('/targets')
    except Exception as e:
        return f"Error: {e} <a href='/tg_login'>Try Again</a>"

@app.route('/targets')
async def targets_view():
    if not login_state['is_authorized']: return redirect('/')
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute('SELECT * FROM targets') as c: rows = await c.fetchall()
    
    # Simple Dashboard HTML
    rows_html = "".join([f"<div style='background:#334155;padding:15px;margin:10px 0;border-radius:10px;display:flex;justify-content:space-between'><b>{r['display_name']}</b> <span>{r['current_status']}</span></div>" for r in rows])
    
    return f"{STYLE}<div style='padding:20px;width:100%;max-width:600px;display:block' class=''><div style='display:flex;justify-content:space-between'><h2>ProTracker</h2><a href='/add_page' style='color:#3b82f6'>+ Add</a></div>{rows_html}</div>"

@app.route('/add_page')
async def add_page():
    return f"{STYLE}<div class='card'><h3>Add Target</h3><form action='/add' method='post'><input name='u' placeholder='Username/ID/Phone'><button class='btn'>Track</button></form></div>"

@app.route('/add', methods=['POST'])
async def add():
    f = await request.form
    try:
        e = await client.get_entity(int(f['u']) if f['u'].isdigit() else f['u'])
        pic = await download_pic(e)
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute('INSERT OR IGNORE INTO targets (user_id, username, display_name, current_status, last_seen, pic_path) VALUES (?, ?, ?, ?, ?, ?)', (e.id, getattr(e,'username',''), f['u'], 'new', 'now', pic))
            await db.commit()
    except Exception as e: return f"Error: {e}"
    return redirect('/targets')

@app.before_serving
async def startup():
    await init_db()
    await client.connect() # Ensure client is connected so we can login
    app.add_background_task(tracker_loop)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
  
