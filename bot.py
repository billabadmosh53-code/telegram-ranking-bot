"""Mishra Guard Robot V4 - Telegram group management bot.
Required env: BOT_TOKEN. Optional: PORT, DATABASE_PATH, GROQ_API_KEY, GROQ_MODEL, INSTAGRAM_COOKIES.
Run exactly one polling instance per token. Render: one Web Service instance.
"""
import os, io, re, time, json, random, sqlite3, threading, logging, tempfile, socket, ipaddress, uuid
from datetime import date
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import telebot
from telebot import types
from PIL import Image, ImageDraw, ImageFont

TOKEN = os.getenv('BOT_TOKEN', '').strip()
if not TOKEN:
    raise RuntimeError('BOT_TOKEN environment variable is missing; set it in Render Environment.')
PORT = int(os.getenv('PORT', '10000'))
DB_PATH = os.getenv('DATABASE_PATH', 'mishrarobot.db')
GROQ_API_KEY = os.getenv('GROQ_API_KEY', '').strip()
GROQ_MODEL = os.getenv('GROQ_MODEL', 'llama-3.3-70b-versatile').strip()
logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO').upper(), format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('MishraRobot')
bot = telebot.TeleBot(TOKEN, parse_mode='HTML', threaded=True, num_threads=8)
DB_LOCK = threading.RLock()
FLOOD = defaultdict(deque)
AFK_NOTICE = {}
URL_RE = re.compile(r'(https?://|www\.|t\.me/|telegram\.me/)', re.I)
PRIVATE_HOSTS = re.compile(r'^(localhost|127\.\d+\.\d+\.\d+|10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|172\.(1[6-9]|2[0-9]|3[0-1])\.\d+\.\d+|169\.254\.\d+\.\d+|0\.0\.0\.0|\[::1\])', re.I)
MAX_PURGE = 5000

COOKIES_DATA = os.getenv('INSTAGRAM_COOKIES', '').strip()
COOKIE_FILE_PATH = None
if COOKIES_DATA:
    try:
        cf = tempfile.NamedTemporaryFile(delete=False, mode='w', encoding='utf-8', suffix='.txt')
        cf.write(COOKIES_DATA)
        cf.close()
        COOKIE_FILE_PATH = cf.name
    except Exception as e:
        log.warning("Cookie file creation error: %s", e)

# ---------------- DATABASE ----------------
def connect():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    with DB_LOCK, connect() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS stats(chat_id INTEGER,user_id INTEGER,name TEXT,username TEXT,count INTEGER DEFAULT 0,last_active TEXT,PRIMARY KEY(chat_id,user_id));
        CREATE TABLE IF NOT EXISTS warnings(chat_id INTEGER,user_id INTEGER,count INTEGER DEFAULT 0,PRIMARY KEY(chat_id,user_id));
        CREATE TABLE IF NOT EXISTS settings(chat_id INTEGER,key TEXT,value TEXT,PRIMARY KEY(chat_id,key));
        CREATE TABLE IF NOT EXISTS filters(chat_id INTEGER,keyword TEXT,response TEXT,PRIMARY KEY(chat_id,keyword));
        CREATE TABLE IF NOT EXISTS message_events(chat_id INTEGER,user_id INTEGER,created_at REAL);
        CREATE INDEX IF NOT EXISTS idx_message_events_chat_time ON message_events(chat_id,created_at);
        CREATE TABLE IF NOT EXISTS antispam_approved(chat_id INTEGER,user_id INTEGER,PRIMARY KEY(chat_id,user_id));
        CREATE TABLE IF NOT EXISTS afk(chat_id INTEGER,user_id INTEGER,reason TEXT,since REAL,PRIMARY KEY(chat_id,user_id));
        CREATE TABLE IF NOT EXISTS ships(chat_id INTEGER,day TEXT,user1 INTEGER,user2 INTEGER,name1 TEXT,name2 TEXT,pct INTEGER,PRIMARY KEY(chat_id,day));
        CREATE TABLE IF NOT EXISTS global_afk(user_id INTEGER PRIMARY KEY,reason TEXT,since REAL,media_type TEXT DEFAULT '',media_file_id TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS lock_approved(chat_id INTEGER,user_id INTEGER,approved_by INTEGER,created_at REAL,PRIMARY KEY(chat_id,user_id));
        CREATE TABLE IF NOT EXISTS wallet(user_id INTEGER PRIMARY KEY,coins INTEGER NOT NULL DEFAULT 0,last_daily TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS global_users(user_id INTEGER PRIMARY KEY,name TEXT,username TEXT,first_name TEXT,last_name TEXT,last_seen REAL,last_chat INTEGER);
        CREATE TABLE IF NOT EXISTS xp_stats(chat_id INTEGER,user_id INTEGER,xp INTEGER NOT NULL DEFAULT 0,last_award REAL NOT NULL DEFAULT 0,PRIMARY KEY(chat_id,user_id));
        ''')
        c.execute('''INSERT INTO global_afk(user_id,reason,since)
            SELECT a.user_id,a.reason,a.since FROM afk a
            JOIN (SELECT user_id,MAX(since) AS latest FROM afk GROUP BY user_id) x
            ON a.user_id=x.user_id AND a.since=x.latest
            ON CONFLICT(user_id) DO UPDATE SET reason=excluded.reason,since=MAX(global_afk.since,excluded.since)''')
        c.execute('''INSERT INTO global_users(user_id,name,username,last_seen,last_chat)
            SELECT s.user_id,MAX(s.name),MAX(s.username),
                   MAX(COALESCE(strftime('%s',s.last_active),0)),MAX(s.chat_id)
            FROM stats s GROUP BY s.user_id
            ON CONFLICT(user_id) DO UPDATE SET
              name=COALESCE(excluded.name,global_users.name),
              username=COALESCE(excluded.username,global_users.username),
              last_seen=MAX(global_users.last_seen,excluded.last_seen),
              last_chat=COALESCE(excluded.last_chat,global_users.last_chat)''')
        cols = {r['name'] for r in c.execute('PRAGMA table_info(filters)').fetchall()}
        if 'response_type' not in cols: c.execute("ALTER TABLE filters ADD COLUMN response_type TEXT DEFAULT 'text'")
        if 'file_id' not in cols: c.execute("ALTER TABLE filters ADD COLUMN file_id TEXT DEFAULT ''")
        afk_cols = {r['name'] for r in c.execute('PRAGMA table_info(global_afk)').fetchall()}
        if 'media_type' not in afk_cols: c.execute("ALTER TABLE global_afk ADD COLUMN media_type TEXT DEFAULT ''")
        if 'media_file_id' not in afk_cols: c.execute("ALTER TABLE global_afk ADD COLUMN media_file_id TEXT DEFAULT ''")

def setting(chat, key, default='off'):
    with DB_LOCK, connect() as c:
        r = c.execute('SELECT value FROM settings WHERE chat_id=? AND key=?', (chat, key)).fetchone()
    return r['value'] if r else default

def set_setting(chat, key, value):
    with DB_LOCK, connect() as c:
        c.execute('INSERT INTO settings VALUES(?,?,?) ON CONFLICT(chat_id,key) DO UPDATE SET value=excluded.value', (chat, key, str(value)))

def esc(s):
    return str(s or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')

def group(m): return m.chat.type in ('group', 'supergroup')

def admin(chat, uid):
    try: return bot.get_chat_member(chat, uid).status in ('creator', 'administrator')
    except Exception: return False

def can_promote(chat, uid):
    try:
        m = bot.get_chat_member(chat, uid)
        if m.status == 'creator': return True
        if m.status == 'administrator': return getattr(m, 'can_promote_members', False) is True
        return False
    except Exception: return False

def need_admin(m):
    if not group(m):
        bot.reply_to(m, 'This command works in groups only.')
        return False
    if not m.from_user or not admin(m.chat.id, m.from_user.id):
        bot.reply_to(m, '⛔ Only group admins can use this command.')
        return False
    return True

def log_action(chat, text):
    if setting(chat, 'adminlog') == 'on':
        try: bot.send_message(chat, '🧾 <b>Admin log</b>\n' + text)
        except Exception: log.exception('Admin log failed')

def done(m, text):
    bot.reply_to(m, text); log_action(m.chat.id, text)

def user_name(u): return (getattr(u, 'first_name', None) or getattr(u, 'username', None) or str(u.id))

def remember_user(chat, u):
    if not u or u.is_bot: return
    with DB_LOCK, connect() as c:
        c.execute('''INSERT INTO stats VALUES(?,?,?,?,0,datetime('now'))
        ON CONFLICT(chat_id,user_id) DO UPDATE SET name=excluded.name,username=excluded.username''',
        (chat, u.id, user_name(u), u.username))
        c.execute('''INSERT INTO global_users(user_id,name,username,first_name,last_name,last_seen,last_chat)
            VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET
            name=excluded.name,username=excluded.username,first_name=excluded.first_name,
            last_name=excluded.last_name,last_seen=excluded.last_seen,last_chat=excluded.last_chat''',
            (u.id,user_name(u),u.username,getattr(u,'first_name',None),getattr(u,'last_name',None),time.time(),chat))

def record(m):
    u = m.from_user
    if not u or u.is_bot: return
    with DB_LOCK, connect() as c:
        c.execute('''INSERT INTO stats VALUES(?,?,?,?,1,datetime('now'))
        ON CONFLICT(chat_id,user_id) DO UPDATE SET name=excluded.name,username=excluded.username,count=stats.count+1,last_active=excluded.last_active''',
        (m.chat.id, u.id, user_name(u), u.username))
        c.execute('INSERT INTO message_events(chat_id,user_id,created_at) VALUES(?,?,?)', (m.chat.id, u.id, time.time()))
        c.execute('DELETE FROM message_events WHERE created_at < ?', (time.time() - 30*86400,))
        c.execute('''INSERT INTO global_users(user_id,name,username,first_name,last_name,last_seen,last_chat)
            VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET
            name=excluded.name,username=excluded.username,first_name=excluded.first_name,
            last_name=excluded.last_name,last_seen=excluded.last_seen,last_chat=excluded.last_chat''',
            (u.id,user_name(u),u.username,getattr(u,'first_name',None),getattr(u,'last_name',None),time.time(),m.chat.id))
        now = time.time()
        c.execute('INSERT OR IGNORE INTO xp_stats(chat_id,user_id,xp,last_award) VALUES(?,?,0,0)', (m.chat.id, u.id))
        c.execute('UPDATE xp_stats SET xp=xp+1,last_award=? WHERE chat_id=? AND user_id=? AND last_award<=?',
                  (now, m.chat.id, u.id, now-60))

def warn_count(chat, uid):
    with DB_LOCK, connect() as c:
        r = c.execute('SELECT count FROM warnings WHERE chat_id=? AND user_id=?', (chat, uid)).fetchone()
    return r['count'] if r else 0

def set_warn(chat, uid, n):
    with DB_LOCK, connect() as c:
        c.execute('INSERT INTO warnings VALUES(?,?,?) ON CONFLICT(chat_id,user_id) DO UPDATE SET count=excluded.count', (chat, uid, max(0, n)))

def filters(chat):
    with DB_LOCK, connect() as c:
        return c.execute('SELECT keyword,response,response_type,file_id FROM filters WHERE chat_id=? ORDER BY keyword', (chat,)).fetchall()

def target_id(m):
    if m.reply_to_message and m.reply_to_message.from_user:
        remember_user(m.chat.id, m.reply_to_message.from_user)
        return m.reply_to_message.from_user.id
    text = m.text or m.caption or ''
    parts = text.split(maxsplit=1)
    if len(parts) < 2: return None
    token = parts[1].strip().split()[0].rstrip(',;')
    if token.isdigit(): return int(token)
    for ent in (m.entities or []):
        if ent.type == 'text_mention' and ent.user: return ent.user.id
    if token.startswith('@') or re.fullmatch(r'[A-Za-z0-9_]{5,32}', token):
        username = token.lstrip('@').lower()
        with DB_LOCK, connect() as c:
            r = c.execute('SELECT user_id FROM global_users WHERE lower(replace(username, "@", ""))=? LIMIT 1', (username,)).fetchone()
            if not r:
                r = c.execute('SELECT user_id FROM stats WHERE lower(replace(username, "@", ""))=? ORDER BY last_active DESC LIMIT 1', (username,)).fetchone()
        return r['user_id'] if r else None
    return None

def target_usage(m):
    return bot.reply_to(m, 'Reply to the user, or use a known @username or numeric Telegram user ID.')

# ---------------- HEALTH SERVER ----------------
class Health(BaseHTTPRequestHandler):
    def _send(self):
        path = urlparse(self.path).path
        if path not in ('/', '/health', '/healthz'):
            body = b'Not found'; self.send_response(404)
        else:
            body = b'Mishra Guard Robot V4 is running'; self.send_response(200)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        if self.command != 'HEAD': self.wfile.write(body)
    def do_GET(self): self._send()
    def do_HEAD(self): self._send()
    def log_message(self, *args): pass

def health_server():
    server = ThreadingHTTPServer(('0.0.0.0', PORT), Health)
    server.daemon_threads = True
    log.info('Health server listening on 0.0.0.0:%s', PORT)
    server.serve_forever()

HELP = '''🤖 <b>MISHRA GUARD ROBOT V4</b>

<b>Stats & Fun</b>
/top /ranking — leaderboard
/stats /mystats — your stats and warnings
/profile — profile card
/level — XP leaderboard
/coins /daily — wallet and daily reward
/download URL — Instagram & Pinterest videos
/ship — daily couple
/afk [reason] /back — AFK status

<b>Admin moderation</b>
/promote /demote — (Owner or Admin with Add Admin permission only)
/ban /unban /kick /mute /unmute
/warn /unwarn — 3 warnings = auto-ban
/pin /unpin /del /delete /purge

<b>Complete lock controls</b>
/lock [all|media|sticker|photo|video|gif|document|audio|voice|poll|webpreview]
/unlock [all|media|sticker|photo|video|gif|document|audio|voice|poll|webpreview]
/approve /unapprove /approved — lock whitelist

<b>Filters & protection</b>
/filter WORD response /filters /stopfilter WORD
/antispam on|off /antilink on|off /adminlog on|off

<b>Optional AI</b>
/ai question — needs GROQ_API_KEY'''

@bot.message_handler(commands=['start', 'help', 'panel'])
def help_cmd(m): bot.send_message(m.chat.id, HELP)

# ---------------- STATS & LEADERBOARD ----------------
def ranking_text(chat, period):
    with DB_LOCK, connect() as c:
        if period == 'overall':
            rows = c.execute('SELECT name,count AS total FROM stats WHERE chat_id=? ORDER BY count DESC LIMIT 10', (chat,)).fetchall()
            label = 'Overall'
        elif period in ('weekly', 'daily'):
            seconds = 7*86400 if period == 'weekly' else 86400
            query = ('SELECT s.name,COUNT(e.user_id) AS total '
                     'FROM message_events e JOIN stats s ON s.chat_id=e.chat_id AND s.user_id=e.user_id '
                     'WHERE e.chat_id=? AND e.created_at>=? '
                     'GROUP BY e.user_id ORDER BY total DESC LIMIT 10')
            rows = c.execute(query, (chat, time.time()-seconds)).fetchall()
            label = 'Weekly (last 7 days)' if period == 'weekly' else 'Daily (last 24 hours)'
        else: return None
    if not rows: return f'🏆 <b>{label} Ranking</b>\nNo ranking data for this period yet.'
    lines = [f'🏆 <b>{label} Ranking</b>']
    for i, row in enumerate(rows, 1):
        medal = ('🥇', '🥈', '🥉')[i-1] if i < 4 else f'{i}.'
        lines.append(f"{medal} {esc(row['name'])} — <b>{row['total']}</b>")
    return '\n'.join(lines)

def ranking_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=3)
    kb.add(
        types.InlineKeyboardButton('🥇 Overall', callback_data='rank:overall'),
        types.InlineKeyboardButton('🥈 Weekly', callback_data='rank:weekly'),
        types.InlineKeyboardButton('🥉 Daily', callback_data='rank:daily')
    )
    return kb

@bot.message_handler(commands=['top', 'ranking'])
def top(m):
    if not group(m): return bot.reply_to(m, 'Use this command in a group.')
    bot.send_message(m.chat.id, ranking_text(m.chat.id, 'overall'), reply_markup=ranking_keyboard())

@bot.callback_query_handler(func=lambda call: bool(call.data) and call.data.startswith('rank:'))
def ranking_callback(call):
    try:
        period = call.data.split(':', 1)[1]
        text = ranking_text(call.message.chat.id, period)
        if text is None: return bot.answer_callback_query(call.id, 'Unknown period.')
        bot.answer_callback_query(call.id)
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=ranking_keyboard())
    except Exception: pass

@bot.message_handler(commands=['stats', 'mystats'])
def stats(m):
    if not m.from_user: return
    if group(m):
        with DB_LOCK, connect() as c:
            r = c.execute('SELECT count FROM stats WHERE chat_id=? AND user_id=?', (m.chat.id, m.from_user.id)).fetchone()
        wc = warn_count(m.chat.id, m.from_user.id)
    else: r, wc = None, 0
    bot.reply_to(m, f"📊 Messages: <b>{r['count'] if r else 0}</b>\n⚠️ Warnings: <b>{wc}/3</b>")

@bot.message_handler(commands=['profile'])
def profile(m):
    if not m.from_user: return
    count = 0
    if group(m):
        with DB_LOCK, connect() as c:
            r = c.execute('SELECT count FROM stats WHERE chat_id=? AND user_id=?', (m.chat.id, m.from_user.id)).fetchone()
        count = r['count'] if r else 0
    img = Image.new('RGB', (800, 400), (28, 35, 58)); d = ImageDraw.Draw(img)
    try:
        big = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', 42)
        mid = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 28)
    except OSError: big = mid = ImageFont.load_default()
    d.rounded_rectangle((20,20,780,380), radius=28, fill=(49,61,91), outline=(170,185,220), width=4)
    d.text((55,60), 'MISHRA ROBOT PROFILE', font=mid, fill='white')
    d.text((55,130), user_name(m.from_user)[:24], font=big, fill=(255,220,130))
    d.text((55,235), f'Messages: {count}', font=mid, fill='white')
    d.text((55,290), f'Warnings: {warn_count(m.chat.id,m.from_user.id) if group(m) else 0}/3', font=mid, fill='white')
    out = io.BytesIO(); out.name = 'profile.png'; img.save(out, 'PNG'); out.seek(0)
    bot.send_photo(m.chat.id, out, reply_to_message_id=m.message_id)

@bot.message_handler(commands=['info'])
def info(m):
    if not group(m): return bot.reply_to(m, 'Use /info in a group.')
    uid = target_id(m) if (m.reply_to_message or len((m.text or '').split()) > 1) else (m.from_user.id if m.from_user else None)
    if not uid: return bot.reply_to(m, 'Member nahi mila. Reply karein ya /info @username use karein.')
    with DB_LOCK, connect() as c:
        row = c.execute('SELECT * FROM global_users WHERE user_id=?', (uid,)).fetchone()
        if not row:
            old = c.execute('SELECT name,username FROM stats WHERE user_id=? ORDER BY last_active DESC LIMIT 1', (uid,)).fetchone()
            if old: row = {'name': old['name'], 'username': old['username'], 'last_seen': 0}
    if not row: return bot.reply_to(m, 'No saved details for this member yet.')
    seen = time.strftime('%d %b %Y, %I:%M %p', time.localtime(row['last_seen'] or 0)) if row['last_seen'] else 'Unknown'
    bot.reply_to(m, f"👤 <b>Member Info</b>\nName: {esc(row['name'])}\nUsername: @{esc(row['username'] or 'None')}\nUser ID: <code>{uid}</code>\nLast seen: <b>{esc(seen)}</b>")

@bot.message_handler(commands=['ship'])
def ship(m):
    if not group(m): return bot.reply_to(m, 'Use in a group.')
    today = date.today().isoformat()
    with DB_LOCK, connect() as c:
        old = c.execute('SELECT * FROM ships WHERE chat_id=? AND day=?', (m.chat.id, today)).fetchone()
        users = c.execute('SELECT user_id,name FROM stats WHERE chat_id=? ORDER BY count DESC LIMIT 200', (m.chat.id,)).fetchall()
    if old:
        return bot.send_message(m.chat.id, f"💖 Today's couple: {esc(old['name1'])} ❤️ {esc(old['name2'])}\nCompatibility: {old['pct']}%")
    users = [u for u in users if u['user_id'] != bot.get_me().id]
    if len(users) < 2: return bot.reply_to(m, 'Need at least two active members.')
    a, b = random.sample(users, 2); pct = random.randint(30, 100)
    with DB_LOCK, connect() as c:
        c.execute('INSERT INTO ships VALUES(?,?,?,?,?,?,?)', (m.chat.id, today, a['user_id'], b['user_id'], a['name'], b['name'], pct))
    bot.send_message(m.chat.id, f"💘 Match: {esc(a['name'])} ❤️ {esc(b['name'])}\nCompatibility: <b>{pct}%</b>")

# ---------------- AFK ----------------
def duration_text(seconds):
    seconds = max(0, int(seconds)); days, seconds = divmod(seconds, 86400); hours, seconds = divmod(seconds, 3600); minutes, seconds = divmod(seconds, 60)
    parts = []
    if days: parts.append(f'{days}d')
    if hours: parts.append(f'{hours}h')
    if minutes: parts.append(f'{minutes}m')
    if not parts: parts.append(f'{seconds}s')
    return ' '.join(parts)

@bot.message_h
