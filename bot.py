"""Mishra Guard Robot V4 - Telegram group management bot.
Required env: BOT_TOKEN. Optional: PORT, DATABASE_PATH, GROQ_API_KEY, GROQ_MODEL.
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
GROQ_API_KEY = os.getenv('GROQ_API_KEY', '').strip().strip("'").strip('"')
GROQ_MODEL = os.getenv('GROQ_MODEL', 'llama-3.1-8b-instant').strip().strip("'").strip('"')
logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO').upper(), format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('MishraRobot')
bot = telebot.TeleBot(TOKEN, parse_mode='HTML', threaded=True, num_threads=8)
DB_LOCK = threading.RLock()
FLOOD = defaultdict(deque)
AFK_NOTICE = {}  # (chat_id, afk_user_id, viewer_id) -> last notification time
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
    except Exception:
        pass


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
        CREATE TABLE IF NOT EXISTS chatbot_memory(id INTEGER PRIMARY KEY AUTOINCREMENT,trigger_text TEXT,reply_type TEXT,reply_content TEXT);
        CREATE INDEX IF NOT EXISTS idx_chatbot_trigger ON chatbot_memory(trigger_text);
        CREATE TABLE IF NOT EXISTS whispers(whisper_id TEXT PRIMARY KEY,sender_id INTEGER,sender_name TEXT,target_key TEXT,secret_text TEXT,created_at REAL);
        ''')
        # Migrate previously saved group AFK statuses and member records.
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
        # Safe migrations for databases created by earlier MishraRobot versions.
        cols = {r['name'] for r in c.execute('PRAGMA table_info(filters)').fetchall()}
        if 'response_type' not in cols:
            c.execute("ALTER TABLE filters ADD COLUMN response_type TEXT DEFAULT 'text'")
        if 'file_id' not in cols:
            c.execute("ALTER TABLE filters ADD COLUMN file_id TEXT DEFAULT ''")
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
        bot.reply_to(m, 'This command works in groups only.'); return False
    if not m.from_user or not admin(m.chat.id, m.from_user.id):
        bot.reply_to(m, '⛔ Only group admins can use this command.'); return False
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

# Resolve target from reply, numeric ID, a tracked @username, or a Telegram mention entity.
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
        if ent.type == 'text_mention' and ent.user:
            return ent.user.id
    # Accept both /info @username and /info username. Search global memory
    # first, then older per-group records as a compatibility fallback.
    if token.startswith('@') or re.fullmatch(r'[A-Za-z0-9_]{5,32}', token):
        username = token.lstrip('@').lower()
        with DB_LOCK, connect() as c:
            r = c.execute(
                'SELECT user_id FROM global_users WHERE lower(replace(username, "@", ""))=? LIMIT 1',
                (username,)
            ).fetchone()
            if not r:
                r = c.execute(
                    'SELECT user_id FROM stats WHERE lower(replace(username, "@", ""))=? '
                    'ORDER BY last_active DESC LIMIT 1',
                    (username,)
                ).fetchone()
        return r['user_id'] if r else None
    return None

def target_usage(m):
    return bot.reply_to(m, 'Reply to the user, or use a known @username or numeric Telegram user ID. A username must have been seen in this group before.')

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

# ---------------- HELP ----------------
HELP = '''🤖 <b>MISHRA GUARD ROBOT V4</b>

<b>Stats & Fun</b>
/top /ranking — overall, weekly & daily leaderboard
/stats /mystats — your stats and warnings
/profile — profile card
/level — XP, level & group XP leaderboard
/coins /daily — wallet and daily reward
/download URL — Instagram Reels & Pinterest Pins
/ship — daily couple
/challenge — random challenge
/afk [reason] /back — AFK status

<b>Admin moderation</b>
/promote /demote — promote with 4 permission levels; reply, @username or ID
/ban /unban /kick /mute /unmute
/warn /unwarn — 3 warnings = auto-ban
/pin /unpin /del /delete /purge

<b>Complete lock controls</b>
/lock [all|media|sticker|photo|video|gif|document|audio|voice|poll|webpreview]
/unlock [all|media|sticker|photo|video|gif|document|audio|voice|poll|webpreview]
/approve /unapprove /approved — lock whitelist

<b>Filters & protection</b>
/filter WORD response — text filter; reply to sticker/GIF with /filter WORD
/filters — list filters
/stopfilter WORD — remove filter
/antispam on|off — choose threshold/action; /antispam approve|unapprove (reply/ID)
 /antilink on|off /adminlog on|off

<b>Welcome & Goodbye (Text + Sticker/GIF/Photo/Video/Audio)</b>
/setwelcome [TEXT] — reply to sticker/GIF/photo/video/audio or type text
/setgoodbye [TEXT] — reply to sticker/GIF/photo/video/audio or type text
/welcome on|off /goodbye on|off

<b>Media & mentions</b>
/mmf TOP ; BOTTOM — reply to photo/static sticker
/kang — static sticker to photo; animated/video behavior retained
@admin [reason] / @all [message] (admin only for @all)

<b>New Features & Games</b>
/chatbot on|off — smart auto-reply (text, sticker & GIF) when called
/whisper — secret whisper button (1. Specific User, 2. 1st Seen, 3. For All)
/ttt /tictactoe — ❌⭕ multiplayer Tic-Tac-Toe game (+30 coins)
/ludo — 🎲 4-Player Ludo board game with visual board (+100 coins)
/style TEXT — convert text into 10+ stylish fancy fonts
/cricket [on|off] — hard cricket quiz & auto toggle
/spiritual [on|off] — spiritual quiz (Radha Krishna, Ram Sita, Mahadev, Jagannath ji) & auto toggle

<b>Optional AI</b>
/ai question — needs GROQ_API_KEY

━━━━━━━━━━━━━━━━━━━━
👑 <b>Bot Owner:</b> Shivam Mishra
📢 <b>Join our channel:</b> @dreamers_adda'''

@bot.message_handler(commands=['start', 'help', 'panel'])
def help_cmd(m): bot.send_message(m.chat.id, HELP)

# ---------------- STATS & FUN ----------------
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
        else:
            return None
    if not rows:
        return f'🏆 <b>{label} Ranking</b>\nNo ranking data for this period yet.'
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
        if text is None:
            return bot.answer_callback_query(call.id, 'Unknown ranking period.')
        bot.answer_callback_query(call.id)
        try:
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=ranking_keyboard())
        except Exception as edit_error:
            if 'message is not modified' not in str(edit_error).lower():
                raise
    except Exception:
        log.exception('Ranking callback failed')
        try: bot.answer_callback_query(call.id, 'Could not load ranking.')
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
    if not group(m):
        return bot.reply_to(m, 'Use /info in a group.')
    uid = target_id(m) if (m.reply_to_message or len((m.text or '').split()) > 1) else (m.from_user.id if m.from_user else None)
    if not uid and not (m.reply_to_message or len((m.text or '').split()) > 1):
        return bot.reply_to(m, 'Could not identify that member. Reply to them or use /info @username or /info USER_ID.')
    if not uid:
        return bot.reply_to(m, 'Username nahi mila. /info @username ya /info USER_ID use karein. Username wala member pehle Mishra Robot ke kisi group mein active hona chahiye.')
    with DB_LOCK, connect() as c:
        row = c.execute('SELECT * FROM global_users WHERE user_id=?', (uid,)).fetchone()
        if not row:
            old = c.execute('SELECT name,username FROM stats WHERE user_id=? ORDER BY last_active DESC LIMIT 1', (uid,)).fetchone()
            if old:
                row = {'name': old['name'], 'username': old['username'], 'last_seen': 0}
    if not row:
        return bot.reply_to(m, 'No saved details for this member yet. The bot must have seen them in a group first.')
    seen = time.strftime('%d %b %Y, %I:%M %p', time.localtime(row['last_seen'] or 0)) if row['last_seen'] else 'Unknown'
    username = '@' + row['username'] if row['username'] else 'Not set'
    full_name = row['name'] or 'Unknown'
    bot.reply_to(m, f'👤 <b>Member Info</b>\nName: {esc(full_name)}\nUsername: {esc(username)}\nUser ID: <code>{uid}</code>\nLast seen by bot: <b>{esc(seen)}</b>')

TRUTHS = [
    'What is your most embarrassing moment?',
    'What is one secret talent you have?',
    'Who was your first crush?',
    'What is the funniest lie you have ever told?',
    'What is one thing you are afraid to admit?',
    'What is your biggest silly habit?',
    'If you could change one decision, what would it be?',
    'What is the most childish thing you still do?',
    'Who in this group makes you laugh the most?',
    'What is something you have never told anyone here?'
]
DARES = [
    'Send your last used emoji.',
    'Talk only in emojis for your next 3 messages.',
    'Give a genuine compliment to someone in this group.',
    'Share a funny sticker.',
    'Write your name backwards.',
    'Tell a clean joke in the group.',
    'Use a dramatic movie dialogue in your next message.',
    'Let the group choose a nickname for you for 10 minutes.',
    'Send a voice note saying “Mishra Robot is the best!”',
    'Describe your day using exactly 5 words.'
]

@bot.message_handler(commands=['truth', 'dare', 'truthdare'])
def truth_dare(m):
    cmd = (m.text or '').split()[0].split('@')[0].lower()
    if cmd == '/truth':
        kind, prompt = 'TRUTH', random.choice(TRUTHS)
    elif cmd == '/dare':
        kind, prompt = 'DARE', random.choice(DARES)
    else:
        kind = random.choice(['TRUTH', 'DARE'])
        prompt = random.choice(TRUTHS if kind == 'TRUTH' else DARES)
    bot.reply_to(m, f'🎲 <b>{kind} TIME!</b>\n{esc(prompt)}')

@bot.message_handler(commands=['ship'])
def ship(m):
    if not group(m): return bot.reply_to(m, 'Use this command in a group.')
    today = date.today().isoformat()
    with DB_LOCK, connect() as c:
        old = c.execute('SELECT * FROM ships WHERE chat_id=? AND day=?', (m.chat.id, today)).fetchone()
        users = c.execute('SELECT user_id,name FROM stats WHERE chat_id=? ORDER BY count DESC LIMIT 200', (m.chat.id,)).fetchall()
    if old:
        mention1 = f"<a href=\"tg://user?id={old['user1']}\">{esc(old['name1'])}</a>"
        mention2 = f"<a href=\"tg://user?id={old['user2']}\">{esc(old['name2'])}</a>"
        return bot.send_message(m.chat.id, f"💖 Today's couple: {mention1} ❤️ {mention2}\nCompatibility: {old['pct']}%")
    users = [u for u in users if u['user_id'] != bot.get_me().id]
    if len(users) < 2: return bot.reply_to(m, 'Need at least two active members.')
    a, b = random.sample(users, 2); pct = random.randint(30, 100)
    with DB_LOCK, connect() as c:
        c.execute('INSERT INTO ships VALUES(?,?,?,?,?,?,?)', (m.chat.id, today, a['user_id'], b['user_id'], a['name'], b['name'], pct))
    mention1 = f"<a href=\"tg://user?id={a['user_id']}\">{esc(a['name'])}</a>"
    mention2 = f"<a href=\"tg://user?id={b['user_id']}\">{esc(b['name'])}</a>"
    bot.send_message(m.chat.id, f"💘 Today's match: {mention1} ❤️ {mention2}\nCompatibility: <b>{pct}%</b>\n<code>{'█'*(pct//10)}{'░'*(10-pct//10)}</code>")

@bot.message_handler(commands=['challenge'])
def challenge(m): bot.reply_to(m, '🎯 Challenge: ' + random.choice(['Share a funny sticker!', 'Tell one interesting fact.', 'Compliment another member.', 'Share your favourite song.', 'Tell a clean joke!']))

# ---------------- AFK (improved) ----------------
def duration_text(seconds):
    seconds = max(0, int(seconds)); days, seconds = divmod(seconds, 86400); hours, seconds = divmod(seconds, 3600); minutes, seconds = divmod(seconds, 60)
    parts = []
    if days: parts.append(f'{days}d')
    if hours: parts.append(f'{hours}h')
    if minutes: parts.append(f'{minutes}m')
    if not parts: parts.append(f'{seconds}s')
    return ' '.join(parts)

@bot.message_handler(commands=['afk'])
def afk(m):
    if not m.from_user: return
    raw = (m.text or '').split(maxsplit=1)
    reason = raw[1].strip() if len(raw) > 1 else 'AFK'
    media_type, media_file_id = '', ''
    reply = m.reply_to_message
    if reply:
        if reply.sticker:
            media_type, media_file_id = 'sticker', reply.sticker.file_id
        elif reply.animation:
            media_type, media_file_id = 'animation', reply.animation.file_id
    reason = reason[:250]
    now = time.time()
    with DB_LOCK, connect() as c:
        c.execute('''INSERT INTO global_afk(user_id,reason,since,media_type,media_file_id) VALUES(?,?,?,?,?)
          ON CONFLICT(user_id) DO UPDATE SET reason=excluded.reason,since=excluded.since,
          media_type=excluded.media_type,media_file_id=excluded.media_file_id''',
          (m.from_user.id, reason, now, media_type, media_file_id))
        c.execute('DELETE FROM afk WHERE user_id=?', (m.from_user.id,))
    bot.reply_to(m, f'💤 <a href="tg://user?id={m.from_user.id}">{esc(user_name(m.from_user))}</a> is now AFK.\nReason: {esc(reason)}' + ('\nSaved sticker/GIF attached.' if media_file_id else ''))

@bot.message_handler(commands=['back'])
def back(m):
    if not m.from_user: return
    with DB_LOCK, connect() as c:
        old = c.execute('SELECT since FROM global_afk WHERE user_id=?', (m.from_user.id,)).fetchone()
        c.execute('DELETE FROM global_afk WHERE user_id=?', (m.from_user.id,))
        c.execute('DELETE FROM afk WHERE user_id=?', (m.from_user.id,))
    if old: bot.reply_to(m, f'👋 Welcome back, {esc(user_name(m.from_user))}! You were away for <b>{duration_text(time.time()-old["since"])}</b>.')
    else: bot.reply_to(m, 'You were not marked AFK.')

# ---------------- SETTINGS / FILTERS ----------------
def toggle(m, key, label):
    if not need_admin(m): return
    p = (m.text or '').split()
    if len(p) < 2 or p[1].lower() not in ('on', 'off'):
        return bot.reply_to(m, f'Usage: /{p[0].split("@")[0][1:]} on|off')
    set_setting(m.chat.id, key, p[1].lower())
    bot.reply_to(m, f'✅ {label} {"enabled" if p[1].lower()=="on" else "disabled"}.')

def antispam_keyboard(chat):
    kb = types.InlineKeyboardMarkup(row_width=3)
    kb.row(*[types.InlineKeyboardButton(f'{n} msgs', callback_data=f'as:threshold:{n}') for n in (3,5,7)])
    kb.row(*[types.InlineKeyboardButton(f'{n} msgs', callback_data=f'as:threshold:{n}') for n in (10,15,20)])
    kb.row(*[types.InlineKeyboardButton(label, callback_data=f'as:action:{value}') for label,value in
             (('🔇 Mute','mute'),('🔨 Ban','ban'))])
    kb.row(*[types.InlineKeyboardButton(label, callback_data=f'as:action:{value}') for label,value in
             (('👢 Kick','kick'),('⚠️ Warn','warn'))])
    return kb

def is_antispam_approved(chat, uid):
    with DB_LOCK, connect() as c:
        return c.execute('SELECT 1 FROM antispam_approved WHERE chat_id=? AND user_id=?', (chat,uid)).fetchone() is not None

@bot.message_handler(commands=['antispam'])
def anti(m):
    if not need_admin(m): return
    parts = (m.text or '').split()
    arg = parts[1].lower() if len(parts) > 1 else ''
    if arg in ('approve','unapprove'):
        uid = target_id(m)
        if not uid and len(parts) >= 3 and parts[2].isdigit():
            uid = int(parts[2])
        if not uid: return bot.reply_to(m, 'Reply to a member or use: /antispam approve USER_ID (or /antispam unapprove USER_ID).')
        with DB_LOCK, connect() as c:
            if arg == 'approve':
                c.execute('INSERT OR IGNORE INTO antispam_approved(chat_id,user_id) VALUES(?,?)', (m.chat.id,uid))
            else:
                c.execute('DELETE FROM antispam_approved WHERE chat_id=? AND user_id=?', (m.chat.id,uid))
        return bot.reply_to(m, f'✅ User <code>{uid}</code> '+('approved' if arg == 'approve' else 'removed from anti-spam whitelist')+'.')
    if arg not in ('on','off'):
        return bot.reply_to(m, 'Usage: /antispam on|off|approve|unapprove (approve/unapprove by reply or ID)')
    set_setting(m.chat.id, 'antispam', arg)
    if arg == 'off':
        return bot.reply_to(m, '🛡️ Anti-spam disabled.')
    if not setting(m.chat.id, 'antispam_threshold', ''): set_setting(m.chat.id, 'antispam_threshold', '7')
    if not setting(m.chat.id, 'antispam_action', ''): set_setting(m.chat.id, 'antispam_action', 'mute')
    bot.send_message(m.chat.id, '🛡️ <b>Anti-spam enabled.</b>\nChoose the message threshold and action:', reply_markup=antispam_keyboard(m.chat.id))

@bot.callback_query_handler(func=lambda call: bool(call.data) and call.data.startswith('as:'))
def antispam_callback(call):
    try:
        chat = call.message.chat.id
        if not call.from_user or not can_promote(chat, call.from_user.id):
            return bot.answer_callback_query(call.id, 'Only group admins can change anti-spam settings.')
        _, kind, value = call.data.split(':', 2)
        if kind == 'threshold' and value in {'3','5','7','10','15','20'}:
            set_setting(chat, 'antispam_threshold', value)
            message = f'✅ Anti-spam threshold set to {value} messages.'
        elif kind == 'action' and value in {'mute','ban','kick','warn'}:
            set_setting(chat, 'antispam_action', value)
            message = f'✅ Anti-spam action set to {value.upper()}.'
        else:
            return bot.answer_callback_query(call.id, 'Invalid setting.')
        bot.answer_callback_query(call.id, message)
        bot.edit_message_text('🛡️ <b>Anti-spam settings</b>\n'
            f'Threshold: <b>{esc(setting(chat, "antispam_threshold", "7"))} messages</b>\n'
            f'Action: <b>{esc(setting(chat, "antispam_action", "mute")).upper()}</b>\n'
            'Use /antispam approve (reply/ID) to whitelist a member.',
            chat, call.message.message_id, reply_markup=antispam_keyboard(chat))
    except Exception:
        log.exception('Anti-spam callback failed')
        try: bot.answer_callback_query(call.id, 'Could not update anti-spam setting.')
        except Exception: pass

@bot.message_handler(commands=['antilink'])
def links(m): toggle(m, 'antilink', 'Anti-link')
@bot.message_handler(commands=['adminlog'])
def adminlog(m): toggle(m, 'adminlog', 'Admin logs')
@bot.message_handler(commands=['welcome', 'goodbye'])
def welcome_toggle(m):
    key = m.text.split()[0].split('@')[0][1:]; toggle(m, key, key.capitalize())

@bot.message_handler(commands=['setwelcome', 'setgoodbye'])
def set_welcome(m):
    if not need_admin(m): return
    cmd = (m.text or '').split()[0].split('@')[0].lower()
    is_wel = (cmd == '/setwelcome')
    prefix = 'welcome' if is_wel else 'goodbye'
    p = (m.text or '').split(maxsplit=1)
    extra_text = p[1].strip()[:1000] if len(p) > 1 else ''
    reply = m.reply_to_message

    media_type = 'text'
    file_id = ''
    caption_text = extra_text

    if reply:
        if reply.sticker:
            media_type = 'sticker'
            file_id = reply.sticker.file_id
            caption_text = ''
        elif reply.animation:
            media_type = 'animation'
            file_id = reply.animation.file_id
            caption_text = extra_text or (reply.caption or '').strip()[:1000]
        elif reply.photo:
            media_type = 'photo'
            file_id = reply.photo[-1].file_id
            caption_text = extra_text or (reply.caption or '').strip()[:1000]
        elif reply.video:
            media_type = 'video'
            file_id = reply.video.file_id
            caption_text = extra_text or (reply.caption or '').strip()[:1000]
        elif reply.audio:
            media_type = 'audio'
            file_id = reply.audio.file_id
            caption_text = extra_text or (reply.caption or '').strip()[:1000]
        elif reply.voice:
            media_type = 'voice'
            file_id = reply.voice.file_id
            caption_text = extra_text or (reply.caption or '').strip()[:1000]
        elif reply.text:
            media_type = 'text'
            caption_text = extra_text or reply.text.strip()[:1000]

    if media_type == 'text' and not caption_text:
        return bot.reply_to(
            m,
            f'Usage:\n• Type text: <code>{cmd} Welcome {{name}} to {{group}}!</code>\n'
            f'• Or reply to any <b>Sticker, GIF, Photo, Video, or Audio</b> with <code>{cmd} [optional caption]</code>\n'
            f'Placeholders: <code>{{name}}</code>, <code>{{group}}</code>, <code>{{username}}</code>, <code>{{id}}</code>'
        )

    set_setting(m.chat.id, f'{prefix}_type', media_type)
    set_setting(m.chat.id, f'{prefix}_file_id', file_id)
    set_setting(m.chat.id, f'{prefix}_text', caption_text)
    bot.reply_to(m, f'✅ {prefix.capitalize()} saved! (Format: <b>{media_type.upper()}</b>)')

def render_group_template(chat, u, template):
    if not template:
        return ''
    return (template
            .replace('{name}', esc(user_name(u)))
            .replace('{group}', esc(chat.title or 'the group'))
            .replace('{username}', esc('@' + u.username if u.username else user_name(u)))
            .replace('{id}', str(u.id)))

def send_greeting_media(chat, u, prefix, default_text):
    m_type = setting(chat.id, f'{prefix}_type', 'text')
    f_id = setting(chat.id, f'{prefix}_file_id', '')
    raw_txt = setting(chat.id, f'{prefix}_text', default_text)
    rendered = render_group_template(chat, u, raw_txt) if raw_txt else ''
    try:
        if m_type == 'sticker' and f_id:
            bot.send_sticker(chat.id, f_id)
            if rendered:
                bot.send_message(chat.id, rendered)
        elif m_type == 'animation' and f_id:
            bot.send_animation(chat.id, f_id, caption=rendered or None)
        elif m_type == 'photo' and f_id:
            bot.send_photo(chat.id, f_id, caption=rendered or None)
        elif m_type == 'video' and f_id:
            bot.send_video(chat.id, f_id, caption=rendered or None)
        elif m_type == 'audio' and f_id:
            bot.send_audio(chat.id, f_id, caption=rendered or None)
        elif m_type == 'voice' and f_id:
            bot.send_voice(chat.id, f_id, caption=rendered or None)
        else:
            bot.send_message(chat.id, rendered or render_group_template(chat, u, default_text))
    except Exception:
        log.exception('Greeting media send failed, falling back to text')
        bot.send_message(chat.id, rendered or render_group_template(chat, u, default_text))

@bot.message_handler(content_types=['new_chat_members'])
def new_members(m):
    if setting(m.chat.id, 'welcome', 'on') != 'on': return
    for u in m.new_chat_members:
        remember_user(m.chat.id, u)
        if u.id != bot.get_me().id:
            send_greeting_media(m.chat, u, 'welcome', 'Welcome {name} to {group}! 🎉')

@bot.message_handler(content_types=['left_chat_member'])
def left_member(m):
    if setting(m.chat.id, 'goodbye') != 'on' or not m.left_chat_member: return
    send_greeting_media(m.chat, m.left_chat_member, 'goodbye', 'Goodbye {name}! 👋')

@bot.message_handler(commands=['filter'])
def add_filter(m):
    if not need_admin(m): return
    p = (m.text or '').split(maxsplit=2)
    if len(p) < 2: return bot.reply_to(m, 'Usage: reply to a sticker/GIF with /filter KEYWORD, or /filter KEYWORD response')
    keyword = p[1].strip().lower()[:80]
    if not keyword: return bot.reply_to(m, 'Keyword cannot be empty.')
    reply = m.reply_to_message
    if reply and reply.sticker:
        response_type, file_id = 'sticker', reply.sticker.file_id
        response = ''
    elif reply and reply.animation:
        response_type, file_id = 'animation', reply.animation.file_id
        response = ''
    elif len(p) >= 3:
        response_type, file_id, response = 'text', '', p[2].strip()[:1000]
    else:
        return bot.reply_to(m, 'Reply to a sticker/GIF, or provide text: /filter KEYWORD response')
    with DB_LOCK, connect() as c:
        c.execute('''INSERT INTO filters(chat_id,keyword,response,response_type,file_id) VALUES(?,?,?,?,?)
            ON CONFLICT(chat_id,keyword) DO UPDATE SET response=excluded.response,response_type=excluded.response_type,file_id=excluded.file_id''',
            (m.chat.id, keyword, response, response_type, file_id))
    bot.reply_to(m, f'✅ {response_type.capitalize()} filter saved for <code>{esc(keyword)}</code>.')

@bot.message_handler(commands=['filters'])
def show_filters(m):
    rows = filters(m.chat.id)
    if not rows: return bot.reply_to(m, 'No filters configured in this group.')
    lines = ['📁 <b>Active filters</b>']
    for r in rows[:80]:
        kind = r['response_type'] or 'text'
        preview = r['response'][:100] if kind == 'text' else f'[{kind} response]'
        lines.append(f'• <code>{esc(r["keyword"])}</code> → {esc(preview)}')
    bot.send_message(m.chat.id, '\n'.join(lines))

@bot.message_handler(commands=['stopfilter'])
def remove_filter(m):
    if not need_admin(m): return
    p = (m.text or '').split(maxsplit=1)
    if len(p) < 2: return bot.reply_to(m, 'Usage: /stopfilter KEYWORD')
    key = p[1].strip().lower()
    with DB_LOCK, connect() as c:
        cur = c.execute('DELETE FROM filters WHERE chat_id=? AND keyword=?', (m.chat.id, key))
    bot.reply_to(m, '✅ Filter removed.' if cur.rowcount else 'Filter not found.')

# ---------------- COMPLETE LOCK / UNLOCK (ROSE-BOT AUTO-DELETE STYLE) ----------------
LOCK_TYPES = {'all','media','sticker','photo','video','gif','document','audio','voice','poll','link','forward'}

def get_chat_locks(chat):
    raw = setting(chat, 'chat_locks', '')
    if raw:
        try:
            return set(json.loads(raw))
        except Exception:
            pass
    return set()

def set_chat_locks(chat, locks_set):
    set_setting(chat, 'chat_locks', json.dumps(sorted(list(locks_set))))

def is_lock_approved(chat, uid):
    with DB_LOCK, connect() as c:
        return c.execute('SELECT 1 FROM lock_approved WHERE chat_id=? AND user_id=?', (chat, uid)).fetchone() is not None

def check_and_enforce_lock(m):
    chat = m.chat.id
    uid = m.from_user.id
    if admin(chat, uid) or is_lock_approved(chat, uid):
        return False
    locks = get_chat_locks(chat)
    if not locks:
        return False

    should_delete = False
    if 'all' in locks:
        should_delete = True
    else:
        ct = m.content_type
        if 'media' in locks and ct in ('photo', 'video', 'animation', 'sticker', 'document', 'audio', 'voice', 'video_note'):
            should_delete = True
        elif 'sticker' in locks and ct == 'sticker':
            should_delete = True
        elif 'photo' in locks and ct == 'photo':
            should_delete = True
        elif 'video' in locks and ct in ('video', 'video_note'):
            should_delete = True
        elif 'gif' in locks and ct == 'animation':
            should_delete = True
        elif 'document' in locks and ct == 'document':
            should_delete = True
        elif 'audio' in locks and ct == 'audio':
            should_delete = True
        elif 'voice' in locks and ct in ('voice', 'video_note'):
            should_delete = True
        elif 'poll' in locks and ct == 'poll':
            should_delete = True
        elif 'forward' in locks and (getattr(m, 'forward_date', None) or getattr(m, 'forward_origin', None)):
            should_delete = True
        elif 'link' in locks:
            txt = (m.text or m.caption or '')
            has_url_entity = any(e.type in ('url', 'text_link') for e in (m.entities or m.caption_entities or []))
            if has_url_entity or URL_RE.search(txt):
                should_delete = True

    if should_delete:
        try:
            bot.delete_message(chat, m.message_id)
        except Exception:
            pass
        return True
    return False

@bot.message_handler(commands=['approve','unapprove','approved'])
def lock_approval(m):
    if not need_admin(m): return
    cmd = (m.text or '').split()[0].split('@')[0][1:].lower()
    target = target_id(m)
    if cmd == 'approved':
        with DB_LOCK, connect() as c:
            rows = c.execute('SELECT user_id FROM lock_approved WHERE chat_id=? ORDER BY created_at', (m.chat.id,)).fetchall()
        if not rows:
            return bot.reply_to(m, 'No approved members yet.')
        return bot.reply_to(m, '✅ <b>Approved members (Lock Bypass):</b>\n' + '\n'.join(f'• <a href="tg://user?id={r["user_id"]}"><code>{r["user_id"]}</code></a>' for r in rows))
    if not target:
        return bot.reply_to(m, f'Reply to a user or pass @username / ID with /{cmd}.')
    if cmd == 'approve':
        with DB_LOCK, connect() as c:
            c.execute('INSERT OR REPLACE INTO lock_approved VALUES(?,?,?,?)', (m.chat.id, target, m.from_user.id, time.time()))
        return bot.reply_to(m, f'✅ User <code>{target}</code> approved! Inke messages group locks hone par bhi delete nahi honge.')
    with DB_LOCK, connect() as c:
        c.execute('DELETE FROM lock_approved WHERE chat_id=? AND user_id=?', (m.chat.id, target))
    return bot.reply_to(m, f'❎ Removed lock approval for <code>{target}</code>.')

@bot.message_handler(commands=['lock', 'unlock', 'locks'])
def lock(m):
    if not need_admin(m): return
    parts = (m.text or '').split()
    cmd = parts[0].split('@')[0][1:].lower()
    if cmd == 'locks':
        locks = get_chat_locks(m.chat.id)
        if not locks:
            return bot.reply_to(m, '🔓 No active locks in this group.')
        return bot.reply_to(m, '🔒 <b>Active Locks (Auto-Delete):</b>\n' + ', '.join(f'<code>{esc(x)}</code>' for x in sorted(locks)))

    mode = parts[1].lower() if len(parts) > 1 else 'all'
    if mode not in LOCK_TYPES:
        return bot.reply_to(m, 'Available lock types: <code>all, media, sticker, photo, video, gif, document, audio, voice, poll, link, forward</code>')

    locks = get_chat_locks(m.chat.id)
    if cmd == 'lock':
        locks.add(mode)
        set_chat_locks(m.chat.id, locks)
        done(m, f'🔒 Locked <b>{esc(mode)}</b>! Non-approved members ke {esc(mode)} messages turant delete honge.')
    else:
        if mode == 'all':
            locks.clear()
        else:
            locks.discard(mode)
        set_chat_locks(m.chat.id, locks)
        done(m, f'🔓 Unlocked <b>{esc(mode)}</b>.')

# ---------------- DELETE / PURGE ----------------
def safe_delete(chat, mid):
    try: bot.delete_message(chat, mid)
    except Exception: pass

@bot.message_handler(commands=['del', 'delete'])
def delete(m):
    if not need_admin(m): return
    if not m.reply_to_message: return bot.reply_to(m, 'Reply to the message you want to delete.')
    try:
        bot.delete_message(m.chat.id, m.reply_to_message.message_id)
        safe_delete(m.chat.id, m.message_id)
        log_action(m.chat.id, 'Message deleted.')
    except Exception as e: bot.reply_to(m, 'Delete failed: ' + esc(e))

@bot.message_handler(commands=['purge'])
def purge(m):
    if not need_admin(m): return
    if not m.reply_to_message: return bot.reply_to(m, 'Reply to the FIRST message in the range, then send /purge.')
    start, end = m.reply_to_message.message_id, m.message_id
    if start >= end: return bot.reply_to(m, 'The replied message must be older than /purge.')
    if end - start > MAX_PURGE: return bot.reply_to(m, f'Limit is {MAX_PURGE} message IDs per purge. Run multiple purges for larger ranges.')
    count = 0
    message_ids = list(range(start, end + 1))
    batch_delete = getattr(bot, 'delete_messages', None)
    if callable(batch_delete):
        for offset in range(0, len(message_ids), 100):
            batch = message_ids[offset:offset + 100]
            try:
                result = batch_delete(m.chat.id, batch)
                count += sum(bool(x) for x in result) if isinstance(result, (list, tuple)) else len(batch)
            except Exception:
                for mid in batch:
                    try:
                        bot.delete_message(m.chat.id, mid); count += 1
                    except Exception:
                        pass
    else:
        for mid in message_ids:
            try: bot.delete_message(m.chat.id, mid); count += 1
            except Exception: pass
    status = bot.send_message(m.chat.id, f'🧹 Deleted {count} messages.')
    threading.Timer(5, lambda: safe_delete(m.chat.id, status.message_id)).start()
    log_action(m.chat.id, f'Purge attempted range {start}-{end}; deleted {count}.')

# ---------------- MODERATION ----------------
@bot.message_handler(commands=['promote', 'demote', 'ban', 'unban', 'kick', 'mute', 'unmute', 'warn', 'unwarn', 'pin', 'unpin'])
def moderation(m):
    if not need_admin(m): return
    cmd = m.text.split()[0].split('@')[0][1:].lower(); chat = m.chat.id
    if cmd in ('pin', 'unpin'):
        try:
            if cmd == 'pin':
                if not m.reply_to_message: return bot.reply_to(m, 'Reply to a message to pin it.')
                bot.pin_chat_message(chat, m.reply_to_message.message_id); done(m, '📌 Message pinned.')
            else: bot.unpin_chat_message(chat); done(m, '📌 Latest message unpinned.')
        except Exception as e: bot.reply_to(m, 'Pin action failed: ' + esc(e))
        return
    uid = target_id(m)
    if not uid: return target_usage(m)
    if uid in (m.from_user.id, bot.get_me().id): return bot.reply_to(m, 'Cannot apply this action to yourself or the bot.')
    try:
        if cmd in ('promote', 'demote'):
            if not can_promote(chat, m.from_user.id):
                return bot.reply_to(m, '⛔ Aapke paas naye admin add/remove karne ka right (Add Admins permission) nahi hai.')
        if cmd == 'promote':
            kb = types.InlineKeyboardMarkup(row_width=1)
            kb.add(
                types.InlineKeyboardButton('1️⃣ Basic Admin (current rights)', callback_data=f'pr:{uid}:basic'),
                types.InlineKeyboardButton('2️⃣ Full Admin (all rights)', callback_data=f'pr:{uid}:full'),
                types.InlineKeyboardButton('3️⃣ Restricted Admin (no ban/add-admin/change-info)', callback_data=f'pr:{uid}:restricted'),
                types.InlineKeyboardButton('4️⃣ No extra rights Admin', callback_data=f'pr:{uid}:limited')
            )
            return bot.reply_to(m, f'Choose admin permission level for <code>{uid}</code>:', reply_markup=kb)
        elif cmd == 'demote':
            ok = bot.promote_chat_member(chat, uid, can_manage_chat=False, can_delete_messages=False, can_manage_video_chats=False, can_restrict_members=False, can_promote_members=False, can_change_info=False, can_invite_users=False, can_pin_messages=False, can_manage_topics=False)
            if not ok: return bot.reply_to(m, 'Demote failed. The bot needs permission to promote members.')
            done(m, f'⬇️ User <code>{uid}</code> demoted.')
        elif cmd == 'ban': bot.ban_chat_member(chat, uid); done(m, f'🔨 User <code>{uid}</code> banned.')
        elif cmd == 'unban': bot.unban_chat_member(chat, uid, only_if_banned=True); done(m, f'🔓 User <code>{uid}</code> unbanned.')
        elif cmd == 'kick': bot.ban_chat_member(chat, uid); bot.unban_chat_member(chat, uid); done(m, f'👢 User <code>{uid}</code> kicked.')
        elif cmd == 'mute': bot.restrict_chat_member(chat, uid, permissions=types.ChatPermissions(can_send_messages=False)); done(m, f'🔇 User <code>{uid}</code> muted.')
        elif cmd == 'unmute':
            bot.restrict_chat_member(chat, uid, permissions=types.ChatPermissions(can_send_messages=True, can_send_audios=True, can_send_documents=True, can_send_photos=True, can_send_videos=True, can_send_video_notes=True, can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True, can_add_web_page_previews=True)); done(m, f'🔊 User <code>{uid}</code> unmuted.')
        elif cmd == 'warn':
            n = warn_count(chat, uid) + 1; set_warn(chat, uid, n)
            if n >= 3:
                bot.ban_chat_member(chat, uid); set_warn(chat, uid, 0); done(m, f'⚠️ User <code>{uid}</code> reached 3 warnings and was banned.')
            else: done(m, f'⚠️ Warning issued: {n}/3.')
        elif cmd == 'unwarn':
            n = max(0, warn_count(chat, uid)-1); set_warn(chat, uid, n); done(m, f'✅ Warning removed. Current warnings: {n}/3.')
    except Exception as e:
        log.exception('%s failed for user %s', cmd, uid)
        bot.reply_to(m, f'{cmd.capitalize()} failed: {esc(e)}')

@bot.callback_query_handler(func=lambda call: bool(call.data) and call.data.startswith('pr:'))
def promote_callback(call):
    try:
        _, raw_uid, level = call.data.split(':', 2)
        uid = int(raw_uid); chat = call.message.chat.id
        if not call.from_user or not admin(chat, call.from_user.id):
            return bot.answer_callback_query(call.id, 'Only group admins can promote members.')
        if level == 'basic':
            rights = dict(can_manage_chat=True, can_delete_messages=True, can_manage_video_chats=True,
                can_restrict_members=True, can_promote_members=False, can_change_info=True,
                can_invite_users=True, can_pin_messages=True, can_manage_topics=True)
        elif level == 'full':
            rights = dict(can_manage_chat=True, can_post_messages=True, can_edit_messages=True,
                can_delete_messages=True, can_manage_video_chats=True, can_restrict_members=True,
                can_promote_members=True, can_change_info=True, can_invite_users=True,
                can_pin_messages=True, can_manage_topics=True, can_post_stories=True,
                can_edit_stories=True, can_delete_stories=True)
        elif level == 'restricted':
            rights = dict(can_manage_chat=True, can_post_messages=True, can_edit_messages=True,
                can_delete_messages=True, can_manage_video_chats=True, can_restrict_members=False,
                can_promote_members=False, can_change_info=False, can_invite_users=True,
                can_pin_messages=True, can_manage_topics=True, can_post_stories=True,
                can_edit_stories=True, can_delete_stories=True)
        elif level == 'limited':
            rights = dict(can_manage_chat=True, can_post_messages=False, can_edit_messages=False,
                can_delete_messages=False, can_manage_video_chats=False, can_restrict_members=False,
                can_promote_members=False, can_change_info=False, can_invite_users=False,
                can_pin_messages=False, can_manage_topics=False, can_post_stories=False,
                can_edit_stories=False, can_delete_stories=False)
        else:
            return bot.answer_callback_query(call.id, 'Unknown permission level.')
        ok = bot.promote_chat_member(chat, uid, **rights)
        if not ok:
            return bot.answer_callback_query(call.id, 'Promotion failed. Check bot promote rights.')
        labels = {'basic':'Basic Admin','full':'Full Admin','restricted':'Restricted Admin','limited':'No-extra-rights Admin'}
        bot.answer_callback_query(call.id, f'{labels[level]} applied.')
        try: bot.edit_message_reply_markup(chat, call.message.message_id, reply_markup=None)
        except Exception: pass
        bot.send_message(chat, f'⬆️ User <code>{uid}</code> promoted: <b>{labels[level]}</b>.')
    except Exception as e:
        log.exception('Promote callback failed')
        try: bot.answer_callback_query(call.id, 'Promotion failed. Check bot admin rights and Telegram API support.')
        except Exception: pass

# ---------------- MMF / KANG ----------------
def meme_font(size):
    for p in ['/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', 'DejaVuSans-Bold.ttf', 'arialbd.ttf']:
        try: return ImageFont.truetype(p, size)
        except OSError: pass
    return ImageFont.load_default()

def wrapped(draw, text, font, maxw):
    words = text.upper().split(); lines = []; line = ''
    for word in words:
        candidate = (line + ' ' + word).strip()
        if draw.textbbox((0,0), candidate, font=font)[2] <= maxw: line = candidate
        else:
            if line: lines.append(line)
            line = word
    if line: lines.append(line)
    return lines or ['']

def draw_caption(draw, text, width, y, font):
    lines = wrapped(draw, text, font, width-24); lh = draw.textbbox((0,0), 'Ag', font=font)[3] + 8
    for line in lines:
        box = draw.textbbox((0,0), line, font=font); x = max(8, (width-(box[2]-box[0]))/2)
        draw.text((x,y), line, font=font, fill='white', stroke_width=max(3,font.size//14), stroke_fill='black'); y += lh

@bot.message_handler(commands=['mmf'])
def mmf(m):
    r = m.reply_to_message; p = (m.text or '').split(maxsplit=1)
    if not r or not (r.photo or r.sticker): return bot.reply_to(m, 'Reply to a photo/static sticker: /mmf TOP ; BOTTOM')
    if len(p) < 2: return bot.reply_to(m, 'Add text: /mmf TOP TEXT ; BOTTOM TEXT')
    if r.sticker and (r.sticker.is_animated or r.sticker.is_video): return bot.reply_to(m, 'Animated/video stickers are not supported by /mmf.')
    try:
        fid = r.photo[-1].file_id if r.photo else r.sticker.file_id; f = bot.get_file(fid); raw = bot.download_file(f.file_path)
        img = Image.open(io.BytesIO(raw)).convert('RGB'); w,h = img.size; d = ImageDraw.Draw(img)
        parts = p[1].split(';',1); top_text = parts[0].strip(); bottom = parts[1].strip() if len(parts)>1 else ''
        font = meme_font(max(20,min(h//8,w//8,90)))
        if top_text: draw_caption(d, top_text, w, 10, font)
        if bottom:
            n = len(wrapped(d,bottom,font,w-24)); y = max(8,h-n*(d.textbbox((0,0),'Ag',font=font)[3]+8)-16); draw_caption(d,bottom,w,y,font)
        img.thumbnail((512, 512), getattr(Image, 'Resampling', Image).LANCZOS)
        out = io.BytesIO(); out.name = 'mishra_meme.webp'
        quality = 90
        while True:
            out.seek(0); out.truncate(0)
            img.save(out, 'WEBP', quality=quality, method=6)
            if out.tell() <= 512 * 1024 or quality <= 45: break
            quality -= 10
        out.seek(0)
        bot.send_sticker(m.chat.id, out, reply_to_message_id=m.message_id)
    except Exception as e: log.exception('MMF error'); bot.reply_to(m, 'MMF failed: ' + esc(e))

@bot.message_handler(commands=['kang'])
def kang(m):
    r = m.reply_to_message
    if not r or not r.sticker: return bot.reply_to(m, 'Reply to a sticker with /kang.')
    try:
        f = bot.get_file(r.sticker.file_id); raw = bot.download_file(f.file_path); out = io.BytesIO(raw)
        if r.sticker.is_animated: out.name = r.sticker.file_unique_id+'.tgs'; bot.send_document(m.chat.id,out,caption='Animated sticker file')
        elif r.sticker.is_video: out.name = r.sticker.file_unique_id+'.webm'; bot.send_document(m.chat.id,out,caption='Video sticker file')
        else:
            sticker_img = Image.open(io.BytesIO(raw)).convert('RGBA')
            photo = io.BytesIO(); photo.name = 'kanged.png'
            sticker_img.save(photo, 'PNG'); photo.seek(0)
            bot.send_photo(m.chat.id, photo, caption='')
    except Exception as e: bot.reply_to(m, 'Kang failed: ' + esc(e))

# ---------------- ADMIN TAGS ----------------
@bot.message_handler(func=lambda m: bool(m.text) and m.text.strip().lower().startswith('@admin'))
def tag_admins(m):
    if not group(m): return
    try:
        tags = [f'<a href="tg://user?id={a.user.id}">{esc(a.user.first_name)}</a>' for a in bot.get_chat_administrators(m.chat.id) if not a.user.is_bot]
        reason = m.text.strip()[6:].strip(); msg = '🚨 <b>Attention admins!</b> ' + ' '.join(tags)
        if reason: msg += '\nReason: ' + esc(reason)
        bot.reply_to(m, msg)
    except Exception as e: bot.reply_to(m, 'Could not tag admins: ' + esc(e))

@bot.message_handler(func=lambda m: bool(m.text) and m.text.strip().lower().startswith('@all'))
def tag_all(m):
    if not group(m): return
    if not m.from_user or not admin(m.chat.id, m.from_user.id): return bot.reply_to(m, '⛔ Only admins can use @all.')
    with DB_LOCK, connect() as c:
        users = c.execute('SELECT user_id,name FROM stats WHERE chat_id=? ORDER BY count DESC LIMIT 300', (m.chat.id,)).fetchall()
    if not users: return bot.reply_to(m, 'No tracked members yet. Members must send messages first.')
    mentions = []
    for u in users:
        if u['user_id'] != bot.get_me().id:
            u_id = u['user_id']
            u_name = esc(u['name'])
            mentions.append(f'<a href="tg://user?id={u_id}">{u_name}</a>')
    reason = m.text.strip()[4:].strip()
    for i in range(0, len(mentions), 30):
        header = '📢 <b>Attention everyone!</b>\n' if i == 0 else ''
        extra = esc(reason)+'\n' if i == 0 and reason else ''
        bot.send_message(m.chat.id, header+extra+' '.join(mentions[i:i+30]), disable_web_page_preview=True)

# ---------------- OPTIONAL AI (GROQ) ----------------
@bot.message_handler(commands=['ai'])
def ai(m):
    p = (m.text or '').split(maxsplit=1)
    if len(p) < 2:
        return bot.reply_to(m, 'Usage: /ai ask a question')
    raw_key = GROQ_API_KEY or os.getenv('GROQ_API_KEY', '')
    api_key = str(raw_key).strip().strip("'").strip('"')
    if not api_key:
        return bot.reply_to(m, 'AI is optional. Add GROQ_API_KEY to Render Environment to enable /ai.')

    query = p[1][:3000]
    user_configured_model = str(GROQ_MODEL or os.getenv('GROQ_MODEL', '')).strip().strip("'").strip('"')

    # Models list to try in order:
    # 1. User specified model
    # 2. Modern Groq production models (openai/gpt-oss-20b, openai/gpt-oss-120b)
    # 3. Llama models (llama-3.3-70b-versatile, llama-3.1-8b-instant)
    candidate_models = []
    if user_configured_model:
        candidate_models.append(user_configured_model)
    for default_mod in ['openai/gpt-oss-20b', 'llama-3.3-70b-versatile', 'llama-3.1-8b-instant', 'openai/gpt-oss-120b']:
        if default_mod not in candidate_models:
            candidate_models.append(default_mod)

    # 1. Try with official OpenAI SDK first
    try:
        from openai import OpenAI
        client = OpenAI(base_url="https://api.groq.com/openai/v1", api_key=api_key, timeout=30.0)
        for model_name in candidate_models:
            try:
                resp = client.chat.completions.create(
                    model=model_name,
                    messages=[
                        {'role': 'system', 'content': 'You are MishraRobot, a helpful and safe Telegram group assistant. Be concise.'},
                        {'role': 'user', 'content': query}
                    ],
                    max_tokens=600,
                    temperature=0.7
                )
                answer = resp.choices[0].message.content or 'No reply generated.'
                try:
                    return bot.reply_to(m, answer[:3500], parse_mode=None)
                except Exception:
                    return bot.reply_to(m, esc(answer)[:3500])
            except Exception as e_model:
                err_str = str(e_model).lower()
                # If model is not found (404), continue to next model!
                if '404' in err_str or 'model_not_found' in err_str:
                    continue
                if '401' in err_str or 'invalid_api_key' in err_str:
                    return bot.reply_to(m, '🔒 Groq API key galat hai. Render Environment mein GROQ_API_KEY check karein.')
                if '429' in err_str or 'rate_limit' in err_str:
                    return bot.reply_to(m, '⏳ Groq usage limit reached. Thodi der baad try karein.')
                raise e_model
    except Exception as e_sdk:
        log.warning("OpenAI SDK exception: %s", e_sdk)

    # 2. Direct urllib fallback
    import urllib.request, urllib.error
    for model_name in candidate_models:
        try:
            payload = json.dumps({
                'model': model_name,
                'messages': [
                    {'role': 'system', 'content': 'You are MishraRobot, a helpful and safe Telegram group assistant. Be concise.'},
                    {'role': 'user', 'content': query}
                ],
                'max_tokens': 600,
                'temperature': 0.7
            }).encode('utf-8')
            req = urllib.request.Request(
                'https://api.groq.com/openai/v1/chat/completions',
                data=payload,
                headers={
                    'Authorization': f'Bearer {api_key}',
                    'Content-Type': 'application/json',
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)',
                    'Accept': 'application/json',
                },
                method='POST'
            )
            with urllib.request.urlopen(req, timeout=30) as response:
                result = json.loads(response.read().decode('utf-8'))
            answer = result.get('choices', [{}])[0].get('message', {}).get('content') or 'No answer generated.'
            try:
                return bot.reply_to(m, answer[:3500], parse_mode=None)
            except Exception:
                return bot.reply_to(m, esc(answer)[:3500])
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue
            if e.code in (401, 403):
                return bot.reply_to(m, '🔒 Groq API key galat hai ya access nahi hai. Render mein GROQ_API_KEY check karein.')
            if e.code == 429:
                return bot.reply_to(m, '⏳ Groq usage limit reached. Thodi der baad try karein.')
            return bot.reply_to(m, f'❌ Groq AI request failed (HTTP {e.code}).')
        except Exception:
            continue

    bot.reply_to(m, '❌ Groq AI par koi working model nahi mila. Render mein GROQ_MODEL variable ko delete/clear kar dein taaki bot auto-select kare.')

# ---------------- XP / LEVELS ----------------
def xp_level(points):
    points = max(0, int(points))
    level = 1
    remaining = points
    while remaining >= level * 100:
        remaining -= level * 100
        level += 1
    return level, remaining, level * 100

@bot.message_handler(commands=['level', 'levels'])
def level_cmd(m):
    if not m.from_user:
        return
    if not group(m):
        return bot.reply_to(m, 'Use /level in a group.')
    with DB_LOCK, connect() as c:
        row = c.execute('SELECT xp FROM xp_stats WHERE chat_id=? AND user_id=?',
                        (m.chat.id, m.from_user.id)).fetchone()
        top = c.execute('SELECT user_id,xp FROM xp_stats WHERE chat_id=? ORDER BY xp DESC LIMIT 10',
                        (m.chat.id,)).fetchall()
    points = row['xp'] if row else 0
    level, progress, needed = xp_level(points)
    lines = [
        f'⭐ <b>{esc(user_name(m.from_user))}</b> — Level <b>{level}</b>',
        f'XP: <b>{points}</b> · Next level: <b>{progress}/{needed}</b>',
        '',
        '<b>🏆 Group XP leaderboard</b>'
    ]
    for rank, item in enumerate(top, 1):
        lines.append(f'{rank}. <a href="tg://user?id={item["user_id"]}">Member</a> — {item["xp"]} XP')
    bot.reply_to(m, '\n'.join(lines))

# ---------------- SIMPLE COINS / DAILY REWARDS ----------------
@bot.message_handler(commands=['coins','balance'])
def coins(m):
    if not m.from_user: return
    with DB_LOCK, connect() as c:
        c.execute('INSERT OR IGNORE INTO wallet(user_id,coins,last_daily) VALUES(?,0,?)',(m.from_user.id,''))
        row=c.execute('SELECT coins FROM wallet WHERE user_id=?',(m.from_user.id,)).fetchone()
    bot.reply_to(m,f'🪙 {esc(user_name(m.from_user))}, your balance is <b>{row["coins"]}</b> coins.')

@bot.message_handler(commands=['daily'])
def daily_reward(m):
    if not m.from_user: return
    today=date.today().isoformat()
    with DB_LOCK, connect() as c:
        c.execute('INSERT OR IGNORE INTO wallet(user_id,coins,last_daily) VALUES(?,0,?)',(m.from_user.id,''))
        row=c.execute('SELECT coins,last_daily FROM wallet WHERE user_id=?',(m.from_user.id,)).fetchone()
        if row['last_daily']==today:
            return bot.reply_to(m,'🎁 You have already claimed today’s reward. Come back tomorrow!')
        reward=random.randint(20,50)
        c.execute('UPDATE wallet SET coins=coins+?,last_daily=? WHERE user_id=?',(reward,today,m.from_user.id))
    bot.reply_to(m,f'🎁 Daily reward: <b>+{reward} coins</b>! Use /coins to check your balance.')

# ---------------- INSTAGRAM & PINTEREST VIDEO DOWNLOADER ----------------
PENDING_DL = {}
PENDING_LOCK = threading.Lock()

def store_dl_url(url, platform):
    with PENDING_LOCK:
        now = time.time()
        for key in list(PENDING_DL):
            if now - PENDING_DL[key].get('ts', 0) > 3600:
                PENDING_DL.pop(key, None)
        token = uuid.uuid4().hex[:8]
        PENDING_DL[token] = {'url': url, 'platform': platform, 'ts': now}
        return token

def get_dl_url(token):
    with PENDING_LOCK:
        return PENDING_DL.get(token)

INSTA_VIDEO_RE = re.compile(r'https?://(?:www\.|m\.)?instagram\.com/(?:reel|tv|p)/[A-Za-z0-9_-]+(?:/)?(?:\?[^\s]*)?', re.I)
PINTEREST_VIDEO_RE = re.compile(r'https?://(?:(?:www|[a-z]{2})\.)?(?:pinterest\.com/pin/|pin\.it/)[A-Za-z0-9_/-]+(?:\?[^\s]*)?', re.I)

def make_cookie_header(cookie_text):
    if not cookie_text:
        return ''
    cookies = []
    for line in cookie_text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        parts = line.split('\t')
        if len(parts) >= 7:
            cookies.append(f"{parts[5]}={parts[6]}")
        elif '=' in line and len(parts) == 1:
            cookies.append(line)
    return "; ".join(cookies)

def get_pinterest_video_stream(url):
    import urllib.request, re
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode('utf-8', errors='ignore')
            v_match = re.search(r'<meta\s+(?:property|name)="og:video(?::secure_url)?"\s+content="([^"]+)"', html, re.I) or \
                      re.search(r'<meta\s+content="([^"]+)"\s+(?:property|name)="og:video(?::secure_url)?"', html, re.I)
            if v_match:
                return v_match.group(1).replace('&amp;', '&')
    except Exception as e:
        log.warning("Pinterest direct extraction failed: %s", e)
    return None

def get_instagram_video_stream(url):
    import urllib.request
    m = re.search(r'instagram\.com/(?:reel|p|tv)/([^/?#&]+)', url)
    if not m:
        return None
    shortcode = m.group(1)

    # 1. Cookie-authenticated fetch (if user provided INSTAGRAM_COOKIES)
    cookie_str = make_cookie_header(COOKIES_DATA)
    if cookie_str:
        try:
            req = urllib.request.Request(f'https://www.instagram.com/p/{shortcode}/?__a=1&__d=dis', headers={
                'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15',
                'Cookie': cookie_str,
                'X-IG-App-ID': '936619743392459',
                'Accept': 'application/json'
            })
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                items = data.get('items', [])
                if items and items[0].get('video_versions'):
                    return items[0]['video_versions'][0]['url']
                media = data.get('graphql', {}).get('shortcode_media', {})
                if media and media.get('is_video') and media.get('video_url'):
                    return media['video_url']
        except Exception as e:
            log.warning("Instagram cookie API fetch failed: %s", e)

    # 2. Mirror fallback (bypasses datacenter IP rate limits when cookies aren't set)
    mirrors = [
        f'https://kkinstagram.com/reel/{shortcode}/',
        f'https://ddinstagram.com/reel/{shortcode}/',
        f'https://instagramez.com/reel/{shortcode}/',
        f'https://kkinstagram.com/p/{shortcode}/',
        f'https://ddinstagram.com/p/{shortcode}/'
    ]
    headers_bot = {
        'User-Agent': 'TelegramBot (like TwitterBot)',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'
    }
    for m_url in mirrors:
        try:
            req = urllib.request.Request(m_url, headers=headers_bot)
            with urllib.request.urlopen(req, timeout=12) as resp:
                ct = resp.headers.get('Content-Type', '').lower()
                if 'video' in ct or 'mp4' in ct or resp.geturl().endswith('.mp4'):
                    return resp.geturl()
                html = resp.read().decode('utf-8', errors='ignore')
                v_match = re.search(r'<meta\s+(?:property|name)="og:video(?::secure_url)?"\s+content="([^"]+)"', html, re.I) or \
                          re.search(r'<meta\s+content="([^"]+)"\s+(?:property|name)="og:video(?::secure_url)?"', html, re.I) or \
                          re.search(r'<meta\s+name="twitter:player:stream"\s+content="([^"]+)"', html, re.I) or \
                          re.search(r'src="(https?://[^"]+\.mp4[^"]*)"', html, re.I)
                if v_match:
                    v_url = v_match.group(1).replace('&amp;', '&').replace('\\u0026', '&')
                    if v_url.startswith('http'):
                        return v_url
        except Exception:
            continue

    return None

def send_download_button(chat_id, message_id, url, platform):
    token = store_dl_url(url, platform)
    title = 'Instagram Reel' if platform == 'instagram' else 'Pinterest Video'
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton('📥 Download Video', callback_data=f'dlv:{token}'))
    bot.send_message(chat_id, f'🎬 <b>{title} link mil gaya!</b>\nDownload ke liye button dabayein:',
                     reply_to_message_id=message_id, reply_markup=kb)

def download_video_thread(chat_id, status_mid, reply_to_mid, url, platform):
    temp_dir = None
    try:
        # 1. Fast direct stream for Pinterest
        if platform == 'pinterest':
            p_stream = get_pinterest_video_stream(url)
            if p_stream:
                import urllib.request
                req = urllib.request.Request(p_stream, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    buf = io.BytesIO(resp.read(48 * 1024 * 1024))
                buf.name = 'pinterest_video.mp4'
                bot.send_video(chat_id, buf, caption='📥 <b>Pinterest Video</b>', reply_to_message_id=reply_to_mid, timeout=120)
                try: bot.delete_message(chat_id, status_mid)
                except Exception: pass
                return

        # 2. Fast direct stream for Instagram (Cookies first, then Mirrors)
        if platform == 'instagram':
            ig_stream = get_instagram_video_stream(url)
            if ig_stream:
                import urllib.request
                req = urllib.request.Request(ig_stream, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    buf = io.BytesIO(resp.read(48 * 1024 * 1024))
                buf.name = 'instagram_video.mp4'
                bot.send_video(chat_id, buf, caption='📥 <b>Instagram Reel</b>', reply_to_message_id=reply_to_mid, timeout=120)
                try: bot.delete_message(chat_id, status_mid)
                except Exception: pass
                return

        # 3. yt-dlp Extractor Fallback
        import yt_dlp
        temp_dir = tempfile.mkdtemp(prefix='mishra_dl_')
        opts = {
            'outtmpl': os.path.join(temp_dir, '%(id)s.%(ext)s'),
            'format': 'bestvideo+bestaudio/best',
            'noplaylist': True,
            'socket_timeout': 25,
            'retries': 2,
            'fragment_retries': 2,
            'quiet': True,
            'no_warnings': True,
            'restrictfilenames': True,
        }

        target_url = url
        if platform == 'instagram':
            if COOKIE_FILE_PATH and os.path.isfile(COOKIE_FILE_PATH):
                opts['cookiefile'] = COOKIE_FILE_PATH
            else:
                # Route through mirror if no cookies are configured so Render IP isn't 429 blocked
                target_url = re.sub(r'https?://(?:www\.|m\.)?instagram\.com', 'https://kkinstagram.com', url, flags=re.I)

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(target_url, download=True)
        except Exception as first_err:
            # If authenticated yt-dlp failed, retry via mirror without cookies
            if platform == 'instagram' and 'cookiefile' in opts:
                opts.pop('cookiefile', None)
                mirror_url = re.sub(r'https?://(?:www\.|m\.)?instagram\.com', 'https://kkinstagram.com', url, flags=re.I)
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(mirror_url, download=True)
            else:
                raise first_err

        files = [os.path.join(temp_dir, name) for name in os.listdir(temp_dir)
                 if os.path.isfile(os.path.join(temp_dir, name)) and not name.endswith(('.part', '.ytdl'))]
        if not files:
            raise RuntimeError('Is link se downloadable media nahi mila. Link public ho aur platform access allow karta ho.')
        path = max(files, key=os.path.getsize)
        size = os.path.getsize(path)
        if size <= 0:
            raise RuntimeError('Downloaded file khaali hai.')
        if size > 49 * 1024 * 1024:
            raise RuntimeError('File 49 MB se badi hai; Telegram upload limit ke andar wali quality chunein.')
        ext = os.path.splitext(path)[1].lower()
        title = (info.get('title') if isinstance(info, dict) else None) or 'Downloaded media'
        caption = f'📥 <b>{esc(title)[:700]}</b>'
        with open(path, 'rb') as media:
            if ext in ('.mp4', '.m4v', '.mov', '.webm'):
                bot.send_video(chat_id, media, caption=caption, reply_to_message_id=reply_to_mid, timeout=120)
            else:
                bot.send_document(chat_id, media, caption=caption, reply_to_message_id=reply_to_mid, timeout=120)
        try:
            bot.delete_message(chat_id, status_mid)
        except Exception:
            pass
    except Exception as e:
        log.exception('Download failed for %s', platform)
        detail = str(e)
        low = detail.lower()
        if '429' in low or 'too many requests' in low:
            detail = 'Platform ne requests limit kar di hain (HTTP 429). Render mein INSTAGRAM_COOKIES set karein ya kuch der baad try karein.'
        elif 'unable to download webpage' in low or 'name or service not known' in low or 'temporary failure in name resolution' in low:
            detail = 'Website/DNS abhi reachable nahi hai. Kuch der baad dobara try karein.'
        elif 'login' in low or 'private' in low or 'cookies' in low:
            detail = 'Ye media login/private ho sakta hai. Sirf public link ya valid cookies use karein.'
        elif 'yt_dlp' in low or 'no module named' in low:
            detail = 'Downloader dependency missing hai. requirements.txt mein yt-dlp install hona chahiye.'
        elif 'requested format is not available' in low:
            detail = 'Is link ke liye downloadable video format nahi mila. Link public hona chahiye.'
        err = f'❌ <b>Download Error:</b>\n{esc(detail)[:350]}'
        try:
            bot.edit_message_text(err, chat_id, status_mid)
        except Exception:
            try: bot.send_message(chat_id, err, reply_to_message_id=reply_to_mid)
            except Exception: log.exception('Could not send download error message')
    finally:
        if temp_dir:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)

@bot.callback_query_handler(func=lambda call: bool(call.data) and call.data.startswith('dlv:'))
def download_button_callback(call):
    token = call.data.split(':', 1)[1]
    item = get_dl_url(token)
    if not item:
        return bot.answer_callback_query(call.id, '⚠️ Button expire ho gaya. Link dobara bhejein.', show_alert=True)
    bot.answer_callback_query(call.id, '⏳ Download shuru ho raha hai...')
    try:
        bot.edit_message_text('⬇️ Downloading… thoda intezar karein.', call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    threading.Thread(target=download_video_thread,
                     args=(call.message.chat.id, call.message.message_id, call.message.message_id, item['url'], item['platform']),
                     daemon=True).start()


@bot.message_handler(commands=['download', 'dl'])
def download_media(m):
    parts = (m.text or '').split(maxsplit=1)
    if len(parts) < 2:
        return bot.reply_to(m, 'Usage: /download URL')
    url = parts[1].strip().rstrip('.,!?)')
    if INSTA_VIDEO_RE.fullmatch(url):
        send_download_button(m.chat.id, m.message_id, url, 'instagram')
    elif PINTEREST_VIDEO_RE.fullmatch(url):
        send_download_button(m.chat.id, m.message_id, url, 'pinterest')
    else:
        bot.reply_to(m, 'ℹ️ Is build mein Instagram Reels aur Pinterest Pins supported hain. Private/login-protected media download nahi hoga.')


@bot.message_handler(func=lambda m: not group(m) and bool(m.text), content_types=['text'])
def pm_auto_download(m):
    body = (m.text or '').strip()
    insta = INSTA_VIDEO_RE.search(body)
    pin = PINTEREST_VIDEO_RE.search(body)
    if insta:
        send_download_button(m.chat.id, m.message_id, insta.group(0).rstrip('.,!?)'), 'instagram')
    elif pin:
        send_download_button(m.chat.id, m.message_id, pin.group(0).rstrip('.,!?)'), 'pinterest')

# ---------------- SMART CHATBOT (TEXT, STICKER & GIF) ----------------
BOT_NAME_RE = re.compile(r'\b(mishra\s*robot|mishra\s*bot|mishra|robot)\b', re.I)

DEFAULT_CHATBOT_REPLIES = [
    "Haan ji, bolo! Mishra Robot hazir hai 😎",
    "Kya haal hai? Kaise madad kar sakta hoon? ✨",
    "Bolo bolo, main sun raha hoon 👀",
    "Ji hukum! Mishra Robot aapki seva mein hai 🤖",
    "Aaj group mein bada sannata hai, kuch masti karte hain! 🎉",
    "Main hamesha active hoon! Batao kya kaam hai? ⚡",
    "Arre waah, yaad kiya mujhe? Batao kya baat hai 😄",
    "Shant raho warna mute kar dunga 😜 Mazak kar raha hoon, bolo!",
]

def learn_chatbot_data(m):
    if not m.reply_to_message:
        return
    orig_txt = (m.reply_to_message.text or m.reply_to_message.caption or '').strip().lower()
    if not orig_txt or orig_txt.startswith('/') or len(orig_txt) > 80:
        return
    r_type, r_content = None, None
    if m.text and not m.text.startswith('/') and len(m.text.strip()) <= 200:
        r_type, r_content = 'text', m.text.strip()
    elif m.sticker:
        r_type, r_content = 'sticker', m.sticker.file_id
    elif m.animation:
        r_type, r_content = 'animation', m.animation.file_id
    if r_type and r_content:
        with DB_LOCK, connect() as c:
            c.execute('INSERT INTO chatbot_memory(trigger_text,reply_type,reply_content) VALUES(?,?,?)',
                      (orig_txt, r_type, r_content))
            c.execute('DELETE FROM chatbot_memory WHERE id NOT IN (SELECT id FROM chatbot_memory ORDER BY id DESC LIMIT 3000)')

def get_chatbot_response(query_text):
    clean = re.sub(r'\s+', ' ', query_text.lower()).strip()
    with DB_LOCK, connect() as c:
        rows = c.execute('SELECT reply_type,reply_content FROM chatbot_memory WHERE trigger_text=? ORDER BY RANDOM() LIMIT 1', (clean,)).fetchall()
        if not rows and len(clean) >= 4:
            stripped = BOT_NAME_RE.sub('', clean).strip(' ,.!?')
            if stripped:
                rows = c.execute('SELECT reply_type,reply_content FROM chatbot_memory WHERE trigger_text LIKE ? ORDER BY RANDOM() LIMIT 1', (f'%{stripped}%',)).fetchall()
        if not rows:
            if random.random() < 0.35:
                rows = c.execute('SELECT reply_type,reply_content FROM chatbot_memory ORDER BY RANDOM() LIMIT 1').fetchall()
    if rows:
        return rows[0]['reply_type'], rows[0]['reply_content']
    return 'text', random.choice(DEFAULT_CHATBOT_REPLIES)

def send_chatbot_reply(m, query_text):
    r_type, r_content = get_chatbot_response(query_text)
    try:
        if r_type == 'sticker':
            bot.send_sticker(m.chat.id, r_content, reply_to_message_id=m.message_id)
        elif r_type == 'animation':
            bot.send_animation(m.chat.id, r_content, reply_to_message_id=m.message_id)
        else:
            bot.reply_to(m, esc(r_content))
    except Exception:
        bot.reply_to(m, random.choice(DEFAULT_CHATBOT_REPLIES))

@bot.message_handler(commands=['chatbot'])
def chatbot_toggle(m):
    if not need_admin(m): return
    p = (m.text or '').split()
    if len(p) < 2 or p[1].lower() not in ('on', 'off'):
        cur = setting(m.chat.id, 'chatbot', 'on')
        return bot.reply_to(m, f'🤖 Chatbot is currently <b>{cur.upper()}</b>.\nUsage: <code>/chatbot on</code> or <code>/chatbot off</code>')
    set_setting(m.chat.id, 'chatbot', p[1].lower())
    bot.reply_to(m, f'🤖 Chatbot <b>{"enabled" if p[1].lower()=="on" else "disabled"}</b>.')

# ---------------- SECRET WHISPER SYSTEM (INLINE + BUTTONS + 3 MODES) ----------------
def save_whisper(sender_id, sender_name, mode, target_key, secret_text):
    wid = uuid.uuid4().hex[:10]
    with DB_LOCK, connect() as c:
        c.execute('INSERT INTO whispers VALUES(?,?,?,?,?,?)',
                  (wid, sender_id, sender_name, f'{mode}:{target_key}', secret_text[:190], time.time()))
        c.execute('DELETE FROM whispers WHERE created_at < ?', (time.time() - 7*86400,))
    return wid

@bot.message_handler(commands=['whisper'])
def whisper_cmd(m):
    if group(m):
        safe_delete(m.chat.id, m.message_id)

    raw = (m.text or '').split(maxsplit=1)
    prefill = raw[1].strip() if len(raw) > 1 else ''
    if not prefill and m.reply_to_message and m.reply_to_message.from_user:
        tu = m.reply_to_message.from_user
        if not tu.is_bot:
            prefill = f'@{tu.username} ' if tu.username else f'{tu.id} '

    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton('🤫 Send Secret Whisper (Private Input)', switch_inline_query_current_chat=prefill)
    )
    msg_text = (
        "🤫 <b>Secret Whisper System</b>\n"
        "Group mein bina message dikhaye whisper bhejne ke liye niche button dabayein:\n\n"
        "• <b>1. Specific User:</b> <code>@username aapka message</code> (ya User ID)\n"
        "• <b>2. 1st Person Seen:</b> <code>aapka message</code> likh kar <i>'1st Person Seen'</i> chunein\n"
        "• <b>3. For All:</b> <code>aapka message</code> likh kar <i>'For All'</i> chunein\n\n"
        "<i>(Note: Inline mode ke liye @BotFather mein /setinline ON hona chahiye)</i>"
    )
    sent = bot.send_message(m.chat.id, msg_text, reply_markup=kb)
    if group(m):
        threading.Timer(25, lambda: safe_delete(m.chat.id, sent.message_id)).start()

@bot.inline_handler(lambda query: True)
def inline_whisper_handler(iq):
    q = (iq.query or '').strip()
    sender_id = iq.from_user.id
    sender_name = user_name(iq.from_user)

    if not q:
        r_help = types.InlineQueryResultArticle(
            id='help_wh',
            title='🤫 Type your secret whisper...',
            description='Format: @username secret message  OR  just type secret message',
            input_message_content=types.InputTextMessageContent(
                '🤫 Use <code>/whisper</code> to send a secret message!',
                parse_mode='HTML'
            )
        )
        return bot.answer_inline_query(iq.id, [r_help], cache_time=1, is_personal=True)

    parts = q.split(maxsplit=1)
    first_tok = parts[0].strip()
    has_target = (len(parts) >= 2) and (first_tok.startswith('@') or first_tok.isdigit())

    results = []

    # Option 1: Specific User (if @username or numeric ID provided as first word)
    if has_target:
        target_label = first_tok
        secret_msg = parts[1].strip()[:190]
        target_key = f'id:{first_tok}' if first_tok.isdigit() else f'user:{first_tok.lstrip("@").lower()}'
        wid1 = save_whisper(sender_id, sender_name, 'target', target_key, secret_msg)
        kb1 = types.InlineKeyboardMarkup()
        kb1.add(types.InlineKeyboardButton('🔐 Show Whisper', callback_data=f'wh:{wid1}'))
        results.append(types.InlineQueryResultArticle(
            id=f't_{wid1}',
            title=f'1️⃣ Whisper to {target_label} (Only Target)',
            description=f'Sirf {target_label} hi yeh secret message dekh payega',
            input_message_content=types.InputTextMessageContent(
                f'🤫 <b>Secret Whisper</b> from <a href="tg://user?id={sender_id}">{esc(sender_name)}</a> to <b>{esc(target_label)}</b>!\n'
                f'<i>🔒 Sirf {esc(target_label)} hi is button par click karke padh sakte hain.</i>',
                parse_mode='HTML'
            ),
            reply_markup=kb1
        ))

    # Option 2: 1st Person Who Sees It
    full_secret = q[:190]
    wid2 = save_whisper(sender_id, sender_name, 'first', '', full_secret)
    kb2 = types.InlineKeyboardMarkup()
    kb2.add(types.InlineKeyboardButton('👁️ Show Whisper (1st Person)', callback_data=f'wh:{wid2}'))
    results.append(types.InlineQueryResultArticle(
        id=f'f_{wid2}',
        title='2️⃣ Whisper for 1st Person Seen',
        description='Jo sabse pehle click karega sirf wahi dekh payega!',
        input_message_content=types.InputTextMessageContent(
            f'🤫 <b>1st-Seen Secret Whisper</b> from <a href="tg://user?id={sender_id}">{esc(sender_name)}</a>!\n'
            f'<i>👁️ Jo member sabse pehle button dabayega, sirf wahi ise padh payega!</i>',
            parse_mode='HTML'
        ),
        reply_markup=kb2
    ))

    # Option 3: For All
    wid3 = save_whisper(sender_id, sender_name, 'all', '', full_secret)
    kb3 = types.InlineKeyboardMarkup()
    kb3.add(types.InlineKeyboardButton('🌍 Show Whisper (For All)', callback_data=f'wh:{wid3}'))
    results.append(types.InlineQueryResultArticle(
        id=f'a_{wid3}',
        title='3️⃣ Whisper For All (Public Pop-up)',
        description='Group ka koi bhi member click karke dekh sakta hai',
        input_message_content=types.InputTextMessageContent(
            f'📢 <b>Mystery Whisper</b> from <a href="tg://user?id={sender_id}">{esc(sender_name)}</a> for <b>Everyone</b>!\n'
            f'<i>🌍 Niche button par tap karke message dekhein:</i>',
            parse_mode='HTML'
        ),
        reply_markup=kb3
    ))

    try:
        bot.answer_inline_query(iq.id, results, cache_time=1, is_personal=True)
    except Exception:
        log.exception('Inline whisper error')

@bot.callback_query_handler(func=lambda call: bool(call.data) and call.data.startswith('wh:'))
def whisper_callback(call):
    wid = call.data.split(':', 1)[1]
    with DB_LOCK, connect() as c:
        row = c.execute('SELECT * FROM whispers WHERE whisper_id=?', (wid,)).fetchone()
        if not row:
            return bot.answer_callback_query(call.id, '⚠️ Yeh whisper expire ho chuka hai.', show_alert=True)

        uid = call.from_user.id
        uname = (call.from_user.username or '').lower()
        raw_tkey = row['target_key'] or ''
        mode, _, tval = raw_tkey.partition(':')

        if mode not in ('target', 'first', 'all'):
            mode = 'target'
            tval = raw_tkey

        if mode == 'all':
            return bot.answer_callback_query(call.id, f"🤫 {row['secret_text']}", show_alert=True)

        if mode == 'first':
            if uid == row['sender_id']:
                return bot.answer_callback_query(call.id, f"🤫 (Your Whisper): {row['secret_text']}", show_alert=True)
            if not tval:
                c.execute('UPDATE whispers SET target_key=? WHERE whisper_id=?', (f'first:{uid}', wid))
                return bot.answer_callback_query(call.id, f"🎉 You are 1st! Whisper:\n{row['secret_text']}", show_alert=True)
            elif tval == str(uid):
                return bot.answer_callback_query(call.id, f"🤫 {row['secret_text']}", show_alert=True)
            else:
                return bot.answer_callback_query(call.id, '⛔ Kisi aur ne pehle hi yeh whisper dekh liya hai!', show_alert=True)

        allowed = (uid == row['sender_id'])
        if tval.startswith('id:') and str(uid) == tval[3:]:
            allowed = True
        elif tval.startswith('user:') and uname and uname == tval[5:]:
            allowed = True

        if not allowed:
            return bot.answer_callback_query(call.id, '⛔ Yeh secret whisper aapke liye nahi hai!', show_alert=True)
        bot.answer_callback_query(call.id, f"🤫 {row['secret_text']}", show_alert=True)

# ---------------- TIC-TAC-TOE GAME (❌⭕) ----------------
TTT_GAMES = {}
TTT_LOCK = threading.Lock()
WIN_LINES = [
    (0, 1, 2), (3, 4, 5), (6, 7, 8),
    (0, 3, 6), (1, 4, 7), (2, 5, 8),
    (0, 4, 8), (2, 4, 6)
]

def ttt_check_winner(board):
    for a, b, c in WIN_LINES:
        if board[a] != ' ' and board[a] == board[b] == board[c]:
            return board[a]
    if ' ' not in board:
        return 'DRAW'
    return None

def ttt_keyboard(gid, game):
    kb = types.InlineKeyboardMarkup(row_width=3)
    board = game['board']
    btns = []
    for i in range(9):
        sym = '❌' if board[i] == 'X' else ('⭕' if board[i] == 'O' else '⬜')
        btns.append(types.InlineKeyboardButton(sym, callback_data=f'ttt:{gid}:{i}'))
    kb.row(btns[0], btns[1], btns[2])
    kb.row(btns[3], btns[4], btns[5])
    kb.row(btns[6], btns[7], btns[8])
    if game['o_id'] is None and not game['over']:
        kb.add(types.InlineKeyboardButton('🎮 Join as ⭕ (Player 2)', callback_data=f'ttt:{gid}:join'))
    return kb

def ttt_status_text(game):
    p1 = f"<a href=\"tg://user?id={game['x_id']}\">{esc(game['x_name'])}</a>"
    p2 = f"<a href=\"tg://user?id={game['o_id']}\">{esc(game['o_name'])}</a>" if game['o_id'] else "<i>Waiting for Player 2...</i>"
    header = f"🎮 <b>TIC-TAC-TOE (❌ vs ⭕)</b>\n❌ Player 1: {p1}\n⭕ Player 2: {p2}\n\n"
    if game['winner'] == 'X':
        return header + f"🏆 <b>Winner:</b> {p1} (❌) — <b>+30 Coins!</b> 🎉"
    elif game['winner'] == 'O':
        return header + f"🏆 <b>Winner:</b> {p2} (⭕) — <b>+30 Coins!</b> 🎉"
    elif game['winner'] == 'DRAW':
        return header + "🤝 <b>Match Draw!</b> Koi nahi jeeta."
    turn_mention = p1 if game['turn'] == 'X' else p2
    turn_sym = '❌' if game['turn'] == 'X' else '⭕'
    return header + f"👉 <b>Turn:</b> {turn_mention} ({turn_sym})"

@bot.message_handler(commands=['ttt', 'tictactoe'])
def start_ttt(m):
    if not group(m):
        return bot.reply_to(m, 'Use /ttt in a group to play with friends!')
    gid = uuid.uuid4().hex[:8]
    o_id, o_name = None, None
    if m.reply_to_message and m.reply_to_message.from_user and not m.reply_to_message.from_user.is_bot:
        if m.reply_to_message.from_user.id != m.from_user.id:
            o_id = m.reply_to_message.from_user.id
            o_name = user_name(m.reply_to_message.from_user)

    game = {
        'chat_id': m.chat.id,
        'x_id': m.from_user.id,
        'x_name': user_name(m.from_user),
        'o_id': o_id,
        'o_name': o_name,
        'board': [' '] * 9,
        'turn': 'X',
        'over': False,
        'winner': None,
        'ts': time.time()
    }
    with TTT_LOCK:
        now = time.time()
        for k in list(TTT_GAMES.keys()):
            if now - TTT_GAMES[k]['ts'] > 3600:
                TTT_GAMES.pop(k, None)
        TTT_GAMES[gid] = game

    bot.send_message(m.chat.id, ttt_status_text(game), reply_markup=ttt_keyboard(gid, game))

@bot.callback_query_handler(func=lambda call: bool(call.data) and call.data.startswith('ttt:'))
def ttt_callback(call):
    parts = call.data.split(':')
    if len(parts) != 3:
        return
    _, gid, action = parts
    uid = call.from_user.id
    uname = user_name(call.from_user)

    with TTT_LOCK:
        game = TTT_GAMES.get(gid)
        if not game:
            return bot.answer_callback_query(call.id, '⚠️ Yeh game expire ho chuka hai. /ttt se naya start karein.', show_alert=True)
        if game['over']:
            return bot.answer_callback_query(call.id, '🏁 Yeh game khatam ho chuka hai!')

        if action == 'join':
            if uid == game['x_id']:
                return bot.answer_callback_query(call.id, 'Aap pehle se ❌ Player 1 hain!', show_alert=True)
            if game['o_id'] is not None:
                return bot.answer_callback_query(call.id, 'Player 2 pehle hi join ho chuka hai!', show_alert=True)
            game['o_id'] = uid
            game['o_name'] = uname
            game['ts'] = time.time()
            bot.answer_callback_query(call.id, '✅ Aap ⭕ Player 2 ban gaye!')
            try:
                bot.edit_message_text(ttt_status_text(game), call.message.chat.id, call.message.message_id, reply_markup=ttt_keyboard(gid, game))
            except Exception:
                pass
            return

        if game['o_id'] is None and uid != game['x_id'] and game['turn'] == 'O':
            game['o_id'] = uid
            game['o_name'] = uname

        if game['turn'] == 'X' and uid != game['x_id']:
            return bot.answer_callback_query(call.id, '⛔ Abhi ❌ Player 1 ki baari hai!', show_alert=True)
        if game['turn'] == 'O':
            if game['o_id'] is None:
                return bot.answer_callback_query(call.id, 'Pehle kisi ko Player 2 join karne dein!', show_alert=True)
            if uid != game['o_id']:
                return bot.answer_callback_query(call.id, '⛔ Abhi ⭕ Player 2 ki baari hai!', show_alert=True)

        idx = int(action)
        if game['board'][idx] != ' ':
            return bot.answer_callback_query(call.id, '⚠️ Yeh box pehle se bhara hua hai!')

        game['board'][idx] = game['turn']
        game['ts'] = time.time()
        res = ttt_check_winner(game['board'])
        if res:
            game['over'] = True
            game['winner'] = res
            if res in ('X', 'O'):
                win_uid = game['x_id'] if res == 'X' else game['o_id']
                with DB_LOCK, connect() as c:
                    c.execute('INSERT OR IGNORE INTO wallet(user_id,coins,last_daily) VALUES(?,0,?)', (win_uid, ''))
                    c.execute('UPDATE wallet SET coins=coins+30 WHERE user_id=?', (win_uid,))
        else:
            game['turn'] = 'O' if game['turn'] == 'X' else 'X'

        new_text = ttt_status_text(game)
        new_kb = ttt_keyboard(gid, game)

    bot.answer_callback_query(call.id)
    try:
        bot.edit_message_text(new_text, call.message.chat.id, call.message.message_id, reply_markup=new_kb)
    except Exception:
        pass

# ---------------- STYLISH TEXT GENERATOR ----------------
ASCII_NORMAL = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
FONT_MAPS = {
    "Bold Serif": "𝐀𝐁𝐂𝐃𝐄𝐅𝐆𝐇𝐈𝐉𝐊𝐋𝐌𝐍𝐎𝐏𝐐𝐑𝐒𝐓𝐔𝐕𝐖𝐗𝐘𝐙𝐚𝐛𝐜𝐝𝐞𝐟𝐠𝐡𝐢𝐣𝐤𝐥𝐦𝐧𝐨𝐩𝐪𝐫𝐬𝐭𝐮𝐯𝐰𝐱𝐲𝐳𝟎𝟏𝟐𝟑𝟒𝟓𝟔𝟕𝟖𝟗",
    "Italic Bold": "𝘼𝘽𝘾𝘿𝙀𝙁𝙂𝙃𝙄𝙅𝙆𝙇𝙈𝙉𝙊𝙋𝙌𝙍𝙎𝙏𝙐𝙑𝙒𝙓𝙔𝙕𝙖𝙗𝙘𝙙𝙚𝙛𝙜𝙝𝙞𝙟𝙠𝙡𝙢𝙣𝙤𝙥𝙦𝙧𝙨𝙩𝙪𝙫𝙬𝙭𝙮𝙯0123456789",
    "Script": "𝒜ℬ𝒞𝒟ℰℱ𝒢ℋℐ𝒥𝒦ℒℳ𝒩𝒪𝒫𝒬ℛ𝒮𝒯𝒰𝒱𝒲𝒳𝒴𝒵𝒶𝒷𝒸𝒹ℯ𝒻ℊ𝒽𝒾𝒿𝓀𝓁𝓂𝓃ℴ𝓅𝓆𝓇𝓈𝓉𝓊𝓋𝓌𝓍𝓎𝓏0123456789",
    "Bold Script": "𝓐𝓑𝓒𝓓𝓔𝓕𝓖𝓗𝓘𝓙𝓚𝓛𝓜𝓝𝓞𝓟𝓠𝓡𝓢𝓣𝓤𝓥𝓦𝓧𝓨𝓩𝓪𝓫𝓬𝓭𝓮𝓯𝓰𝓱𝓲𝓳𝓴𝓵𝓶𝓷𝓸𝓹𝓺𝓻𝓼𝓽𝓾𝓿𝔀𝔁𝔂𝔃0123456789",
    "Gothic": "𝔄𝔅ℭ𝔇𝔈𝔉𝔊ℌℑ𝔍𝔎𝔏𝔐𝔑𝔒𝔓𝔔ℜ𝔖𝔗𝔘𝔙𝔚𝔛𝔜ℨ𝔞𝔟𝔠𝔡𝔢𝔣𝔤𝔥𝔦𝔧𝔨𝔩𝔪𝔫𝔬𝔭𝔮𝔯𝔰𝔱𝔲𝔳𝔴𝔵𝔶𝔷0123456789",
    "Bold Gothic": "𝕬𝕭𝕮𝕯𝕰𝕱𝕲𝕳𝕴𝕵𝕶𝕷𝕸𝕹𝕺𝕻𝕼𝕽𝕾𝕿𝖀𝖁𝖂𝖃𝖄𝖅𝖆𝖇𝖈𝖉𝖊𝖋𝖌𝖍𝖎𝖏𝖐𝖑𝖒𝖓𝖔𝖕𝖖𝖗𝖘𝖙𝖚𝖛𝖜𝖝𝖞𝖟0123456789",
    "Double Struck": "𝔸𝔹ℂ𝔻𝔼𝔽𝔾ℍ𝕀𝕁𝕂𝕃𝕄ℕ𝕆ℙℚℝ𝕊𝕋𝕌𝕍𝕎𝕏𝕐ℤ𝕒𝕓𝕔𝕕𝕖𝕗𝕘𝕙𝕚𝕛𝕜𝕝𝕞𝕟𝕠𝕡𝕢𝕣𝕤𝕥𝕦𝕧𝕨𝕩𝕪𝕫𝟘𝟙𝟚𝟛𝟜𝟝𝟞𝟟𝟠𝟡",
    "Monospace": "𝙰𝙱𝙲𝙳𝙴𝙵𝙶𝙷𝙸𝙹𝙺𝙻𝙼𝙽𝙾𝙿𝚀𝚁𝚂𝚃𝚄𝚅𝚆𝚇𝚈𝚉𝚊𝚋𝚌𝚍𝚎𝚏𝚐𝚑𝚒𝚓𝚔𝚕𝚖𝚗𝚘𝚙𝚚𝚛𝚜𝚝𝚞𝚟𝚠𝚡𝚢𝚣𝟶𝟷𝟸𝟹𝟺𝟻𝟼𝟽𝟾𝟿",
    "Circled": "ⒶⒷⒸⒹⒺⒻⒼⒽⒾⒿⓀⓁⓂⓃⓄⓅⓆⓇⓈⓉⓊⓋⓌⓍⓎⓏⓐⓑⓒⓓⓔⓕⓖⓗⓘⓙⓚⓛⓜⓝⓞⓟⓠⓡⓢⓣⓤⓥⓦⓧⓨⓩ0①②③④⑤⑥⑦⑧⑨",
    "Small Caps": "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘQʀꜱᴛᴜᴠᴡxʏᴢᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘQʀꜱᴛᴜᴠᴡxʏᴢ0123456789",
}

def stylize_text(txt):
    results = []
    for label, mapped in FONT_MAPS.items():
        table = str.maketrans(ASCII_NORMAL, mapped)
        results.append(f"• <code>{esc(txt.translate(table))}</code>")
    results.append(f"• <code>★彡 {esc(txt)} 彡★</code>")
    results.append(f"• <code>꧁༒☬ {esc(txt)} ☬༒꧂</code>")
    return "\n".join(results)

@bot.message_handler(commands=['style', 'stylish', 'font'])
def stylish_cmd(m):
    p = (m.text or '').split(maxsplit=1)
    src = p[1].strip() if len(p) > 1 else ((m.reply_to_message.text or '').strip() if m.reply_to_message else '')
    if not src:
        return bot.reply_to(m, 'Usage: <code>/style Your Name</code> (tap any generated style to copy!)')
    bot.reply_to(m, f'✨ <b>Stylish Designs (Tap to copy):</b>\n\n{stylize_text(src[:80])}')

# ---------------- HARD CRICKET & SPIRITUAL QUIZ GAME (2-HOUR AUTO + SEPARATE ON/OFF) ----------------
HARD_QUIZ_BANK = [
    # --- HARD CRICKET QUESTIONS ---
    (
        "🏏 HARD CRICKET QUIZ",
        "Test cricket itihas mein ek hi match mein 10 wickets (all 10 in an innings) lene wale pehle Bharatiya aur duniya ke doosre gendbaaz kaun hain?",
        "Anil Kumble",
        ["anil kumble", "kumble"]
    ),
    (
        "🏏 HARD CRICKET QUIZ",
        "1983 ODI World Cup final mein India ke khilaf sabse zyada runs kis West Indies ballebaj ne banaye the?",
        "Viv Richards",
        ["viv richards", "vivian richards", "sir viv richards", "richards"]
    ),
    (
        "🏏 HARD CRICKET QUIZ",
        "Test cricket mein India ke liye pehla triple century (300+) lagane wale ballebaj kaun hain?",
        "Virender Sehwag",
        ["virender sehwag", "sehwag"]
    ),
    (
        "🏏 HARD CRICKET QUIZ",
        "ODI cricket mein pehli double century (200*) kis Bharatiya khiladi ne banayi thi?",
        "Sachin Tendulkar",
        ["sachin tendulkar", "sachin"]
    ),
    (
        "🏏 HARD CRICKET QUIZ",
        "IPL itihas mein sabse pehli hat-trick kis gendbaaz ne li thi (2008 season mein)?",
        "Lakshmipathy Balaji",
        ["lakshmipathy balaji", "balaji", "l balaji"]
    ),
    (
        "🏏 HARD CRICKET QUIZ",
        "Test cricket ke itihas mein 400* runs ka world record kis West Indies legend ke naam hai?",
        "Brian Lara",
        ["brian lara", "lara"]
    ),
    (
        "🏏 HARD CRICKET QUIZ",
        "2007 T20 World Cup final mein India ke liye aakhri over kis gendbaaz ne fenka tha?",
        "Joginder Sharma",
        ["joginder sharma", "joginder"]
    ),
    (
        "🏏 HARD CRICKET QUIZ",
        "Test cricket mein sabse zyada 800 wickets lene wale Sri Lankan off-spinner kaun hain?",
        "Muttiah Muralitharan",
        ["muttiah muralitharan", "muralitharan", "murali"]
    ),
    (
        "🏏 HARD CRICKET QUIZ",
        "2011 ODI World Cup quarter-final mein Australia ke khilaf Yuvraj Singh ke saath match-winning partnership kisne ki thi?",
        "Suresh Raina",
        ["suresh raina", "raina"]
    ),
    (
        "🏏 HARD CRICKET QUIZ",
        "International cricket mein 6 balls par 6 sixes lagane wale duniya ke pehle ballebaj kaun the (2007 WC)?",
        "Herschelle Gibbs",
        ["herschelle gibbs", "gibbs"]
    ),
    (
        "🏏 HARD CRICKET QUIZ",
        "Test cricket mein lagatar 4 Test matches mein 4 double centuries lagane wale ekmatra ballebaj kaun hain?",
        "Virat Kohli",
        ["virat kohli", "kohli", "virat"]
    ),
    (
        "🏏 HARD CRICKET QUIZ",
        "IPL ke ek hi season (2016) mein 973 runs banane ka all-time record kis khiladi ke naam hai?",
        "Virat Kohli",
        ["virat kohli", "kohli", "virat"]
    ),
    (
        "🏏 HARD CRICKET QUIZ",
        "International T20 cricket mein sabse tez century (35 balls) lagane wale Bharatiya ballebaj kaun hain?",
        "Rohit Sharma",
        ["rohit sharma", "rohit"]
    ),
    (
        "🏏 HARD CRICKET QUIZ",
        "India ke liye Test cricket mein hat-trick lene wale sabse pehle gendbaaz kaun the (2001 Kolkata Test)?",
        "Harbhajan Singh",
        ["harbhajan singh", "harbhajan", "bhajji"]
    ),
    (
        "🏏 HARD CRICKET QUIZ",
        "World Cup itihas mein sabse tez double century (128 balls) kis West Indies khiladi ne lagayi thi?",
        "Chris Gayle",
        ["chris gayle", "gayle"]
    ),

    # --- SPIRITUAL QUESTIONS (RADHA KRISHNA, RAM SITA, MAHADEV PARVATI, RAMAYANA, MAHABHARATA, JAGANNATH JI) ---
    (
        "🕉️ SPIRITUAL KNOWLEDGE QUIZ",
        "Radha Rani ke pita ji ka kya naam tha jo Barsana ke mukhiya the?",
        "Vrishabhanu",
        ["vrishabhanu", "vrishbhanu", "bhanu"]
    ),
    (
        "🕉️ SPIRITUAL KNOWLEDGE QUIZ",
        "Bhagwan Shri Krishna ki bansuri (flute) ka kya naam tha jiski dhun par Radha Rani aur Gopiyan mugdh ho jaati thi?",
        "Mahananda",
        ["mahananda", "murali", "bansuri"]
    ),
    (
        "🕉️ SPIRITUAL KNOWLEDGE QUIZ",
        "Radha Rani ki mata ji ka kya naam tha?",
        "Kirtida",
        ["kirtida", "keertida"]
    ),
    (
        "🕉️ SPIRITUAL KNOWLEDGE QUIZ",
        "Vrindavan mein jis pavitra sthan par Bhagwan Shri Krishna aur Radha Rani ne Maharaas rachaya tha, us van ka kya naam hai?",
        "Nidhivan",
        ["nidhivan", "nidhi van"]
    ),
    (
        "🕉️ SPIRITUAL KNOWLEDGE QUIZ",
        "Puri (Odisha) mein sthit Jagannath Mandir mein Bhagwan Jagannath ke saath unke bade bhai aur behen kaun virajman hain? (Kisi ek ka naam likhein)",
        "Balabhadra aur Subhadra",
        ["balabhadra", "subhadra", "balram", "balabhadra aur subhadra"]
    ),
    (
        "🕉️ SPIRITUAL KNOWLEDGE QUIZ",
        "Bhagwan Jagannath ji ki vishav-prasiddh Rath Yatra har saal kis Hindu mahine mein nikali jaati hai?",
        "Ashadha",
        ["ashadha", "ashadh", "asadha"]
    ),
    (
        "🕉️ SPIRITUAL KNOWLEDGE QUIZ",
        "Jagannath Puri ke rath 'Nandighosha' (Bhagwan Jagannath ji ke rath) mein kul kitne pahiye (wheels) hote hain?",
        "16",
        ["16", "sixteen", "solah"]
    ),
    (
        "🕉️ SPIRITUAL KNOWLEDGE QUIZ",
        "Mata Sita ke pita Raja Janak kis rajya ke raja the?",
        "Mithila (Videha)",
        ["mithila", "videha", "janakpur"]
    ),
    (
        "🕉️ SPIRITUAL KNOWLEDGE QUIZ",
        "Ramayana mein Mata Sita ka haran karne ke baad Ravan ne unhe Lanka ke kis vatika mein rakha tha?",
        "Ashok Vatika",
        ["ashok vatika", "ashoka vatika"]
    ),
    (
        "🕉️ SPIRITUAL KNOWLEDGE QUIZ",
        "Shri Ram ke vanvas jaane par unki khadau (paduka) rakh kar 14 varsh tak Ayodhya ka rajya kisne sambhala tha?",
        "Bharat",
        ["bharat", "bharata"]
    ),
    (
        "🕉️ SPIRITUAL KNOWLEDGE QUIZ",
        "Ramayana mein Lanka jaane ke liye samudra par pul (Ram Setu) banane wale do vanar bhai kaun the?",
        "Nal aur Neel",
        ["nal", "neel", "nal neel", "nal aur neel"]
    ),
    (
        "🕉️ SPIRITUAL KNOWLEDGE QUIZ",
        "Bhagwan Shiv ke dhanush ka kya naam tha jise Shri Ram ne Sita Swayamvar mein toda tha?",
        "Pinaka",
        ["pinaka", "pinak"]
    ),
    (
        "🕉️ SPIRITUAL KNOWLEDGE QUIZ",
        "Mata Parvati ke pita ka kya naam tha jo parvatraj the?",
        "Himavan",
        ["himavan", "himavat", "himalaya"]
    ),
    (
        "🕉️ SPIRITUAL KNOWLEDGE QUIZ",
        "Bhagwan Mahadev aur Mata Parvati ke bade putra (Devtaon ke Senapati) ka kya naam hai?",
        "Kartikeya",
        ["kartikeya", "murugan", "skanda", "subrahmanya"]
    ),
    (
        "🕉️ SPIRITUAL KNOWLEDGE QUIZ",
        "Bhagwan Shiv ke 12 Jyotirlingon mein se pratham (pehla) Jyotirling kaunsa hai?",
        "Somnath",
        ["somnath"]
    ),
    (
        "🕉️ SPIRITUAL KNOWLEDGE QUIZ",
        "Bhagwan Mahadev ke gale mein lipte hue nagraj ka kya naam hai?",
        "Vasuki",
        ["vasuki"]
    ),
    (
        "🕉️ SPIRITUAL KNOWLEDGE QUIZ",
        "Shrimad Bhagavad Gita mein kul kitne adhyay (chapters) hain?",
        "18",
        ["18", "eighteen", "atharah"]
    ),
    (
        "🕉️ SPIRITUAL KNOWLEDGE QUIZ",
        "Mahabharata ke yuddh mein Bhagwan Shri Krishna ke shankh (conch) ka kya naam tha?",
        "Panchajanya",
        ["panchajanya", "panchjanya"]
    ),
    (
        "🕉️ SPIRITUAL KNOWLEDGE QUIZ",
        "Arjun ke dhanush ka kya naam tha jo unhe Agni Dev se prapt hua tha?",
        "Gandiva",
        ["gandiva", "gandiv"]
    ),
    (
        "🕉️ SPIRITUAL KNOWLEDGE QUIZ",
        "Mahabharata ki rachna kis Maharishi ne ki thi (jise Shri Ganesh ji ne likha tha)?",
        "Ved Vyas",
        ["ved vyas", "vyas", "maharishi ved vyas"]
    ),
]

ACTIVE_CRICKET = {}  # chat_id -> {'category': str, 'question': str, 'name': str, 'answers': list, 'asked_at': float}
CRICKET_LOCK = threading.Lock()

def post_cricket_question(chat_id, reply_to_id=None, category_filter=None):
    pool = HARD_QUIZ_BANK
    if category_filter == 'cricket':
        pool = [q for q in HARD_QUIZ_BANK if 'CRICKET' in q[0]]
    elif category_filter == 'spiritual':
        pool = [q for q in HARD_QUIZ_BANK if 'SPIRITUAL' in q[0]]

    if not pool:
        return
    category, question, display_ans, answers = random.choice(pool)
    with CRICKET_LOCK:
        ACTIVE_CRICKET[chat_id] = {
            'category': category,
            'question': question,
            'name': display_ans,
            'answers': [a.lower() for a in answers],
            'asked_at': time.time()
        }
    text = (
        f"<b>{category}</b>\n\n"
        f"❓ <b>Sawal:</b> {esc(question)}\n\n"
        f"👇 <i>Neeche sahi jawab likhein! Pehle sahi jawab dene wale ko <b>+75 Coins</b> milenge!</i>"
    )
    try:
        bot.send_message(chat_id, text, reply_to_message_id=reply_to_id)
    except Exception:
        pass

def check_cricket_answer(m):
    if not m.text or m.text.startswith('/'):
        return False
    chat = m.chat.id
    with CRICKET_LOCK:
        q = ACTIVE_CRICKET.get(chat)
        if not q:
            return False
        guess = re.sub(r'[^a-z0-9\s]', '', m.text.lower()).strip()
        if not guess:
            return False
        matched = False
        for ans in q['answers']:
            if guess == ans or (len(ans) >= 4 and ans in guess):
                matched = True
                break
        if matched:
            winner_data = ACTIVE_CRICKET.pop(chat)
        else:
            return False

    uid = m.from_user.id
    with DB_LOCK, connect() as c:
        c.execute('INSERT OR IGNORE INTO wallet(user_id,coins,last_daily) VALUES(?,0,?)', (uid, ''))
        c.execute('UPDATE wallet SET coins=coins+75 WHERE user_id=?', (uid,))
        row = c.execute('SELECT coins FROM wallet WHERE user_id=?', (uid,)).fetchone()
    bal = row['coins'] if row else 75
    bot.reply_to(
        m,
        f"🎉 <b>BILKUL SAHI JAWAB!</b>\n"
        f"🏆 Winner: <a href=\"tg://user?id={uid}\">{esc(user_name(m.from_user))}</a>\n"
        f"✅ Sahi Uttar: <b>{esc(winner_data['name'])}</b>\n"
        f"🪙 Reward: <b>+75 Coins</b> (Total Balance: <b>{bal}</b>)"
    )
    return True

@bot.message_handler(commands=['cricket', 'guess'])
def cricket_cmd(m):
    if not group(m):
        return bot.reply_to(m, 'Use /cricket in a group!')
    parts = (m.text or '').split()
    if len(parts) > 1 and parts[1].lower() in ('on', 'off'):
        if not need_admin(m):
            return
        val = parts[1].lower()
        set_setting(m.chat.id, 'cricket_auto', val)
        return bot.reply_to(m, f'🏏 Auto Cricket Quiz (every 2 hrs) <b>{"enabled" if val=="on" else "disabled"}</b>.')
    post_cricket_question(m.chat.id, reply_to_id=m.message_id, category_filter='cricket')

@bot.message_handler(commands=['spiritual', 'gyan'])
def spiritual_cmd(m):
    if not group(m):
        return bot.reply_to(m, 'Use /spiritual in a group!')
    parts = (m.text or '').split()
    if len(parts) > 1 and parts[1].lower() in ('on', 'off'):
        if not need_admin(m):
            return
        val = parts[1].lower()
        set_setting(m.chat.id, 'spiritual_auto', val)
        return bot.reply_to(m, f'🕉️ Auto Spiritual Quiz (every 2 hrs) <b>{"enabled" if val=="on" else "disabled"}</b>.')
    post_cricket_question(m.chat.id, reply_to_id=m.message_id, category_filter='spiritual')

def hourly_cricket_loop():
    # Runs every 2 hours (7200 seconds) and checks cricket_auto and spiritual_auto separately
    while True:
        time.sleep(7200)
        try:
            with DB_LOCK, connect() as c:
                chats = c.execute('SELECT DISTINCT chat_id FROM stats WHERE chat_id < 0').fetchall()
            for r in chats:
                cid = r['chat_id']
                c_on = setting(cid, 'cricket_auto', 'on') == 'on'
                s_on = setting(cid, 'spiritual_auto', 'on') == 'on'
                if not c_on and not s_on:
                    continue
                if c_on and s_on:
                    cat = random.choice(['cricket', 'spiritual'])
                elif c_on:
                    cat = 'cricket'
                else:
                    cat = 'spiritual'
                post_cricket_question(cid, category_filter=cat)
                time.sleep(1.5)
        except Exception:
            log.exception('2-hour quiz loop error')

# ---------------- VISUAL 4-PLAYER LUDO GAME (/ludo) ----------------
LUDO_GAMES = {}
LUDO_LOCK = threading.Lock()

LUDO_COLORS = [
    {'key': 'R', 'name': 'Red', 'emoji': '🔴', 'rgb': (220, 48, 48), 'light': (255, 210, 210), 'start': 0},
    {'key': 'G', 'name': 'Green', 'emoji': '🟢', 'rgb': (38, 175, 72), 'light': (205, 248, 215), 'start': 13},
    {'key': 'Y', 'name': 'Yellow', 'emoji': '🟡', 'rgb': (235, 185, 20), 'light': (255, 245, 195), 'start': 26},
    {'key': 'B', 'name': 'Blue', 'emoji': '🔵', 'rgb': (45, 115, 235), 'light': (205, 228, 255), 'start': 39},
]

# 52-step outer track coordinates on a 15x15 Ludo grid (row, col)
LUDO_TRACK_COORDS = [
    (6,1),(6,2),(6,3),(6,4),(6,5),
    (5,6),(4,6),(3,6),(2,6),(1,6),(0,6),
    (0,7),(0,8),
    (1,8),(2,8),(3,8),(4,8),(5,8),
    (6,9),(6,10),(6,11),(6,12),(6,13),(6,14),
    (7,14),(8,14),
    (8,13),(8,12),(8,11),(8,10),(8,9),
    (9,8),(10,8),(11,8),(12,8),(13,8),(14,8),
    (14,7),(14,6),
    (13,6),(12,6),(11,6),(10,6),(9,6),
    (8,5),(8,4),(8,3),(8,2),(8,1),(8,0),
    (7,0),(6,0)
]

# Home stretch coordinates (5 steps leading to center, steps 51..55; step 56 = finished)
LUDO_HOME_COORDS = {
    'R': [(7,1),(7,2),(7,3),(7,4),(7,5)],
    'G': [(1,7),(2,7),(3,7),(4,7),(5,7)],
    'Y': [(7,13),(7,12),(7,11),(7,10),(7,9)],
    'B': [(13,7),(12,7),(11,7),(10,7),(9,7)],
}

# Yard positions for 4 tokens per color (when pos == -1)
LUDO_YARD_COORDS = {
    'R': [(2,2),(2,3),(3,2),(3,3)],
    'G': [(2,11),(2,12),(3,11),(3,12)],
    'Y': [(11,11),(11,12),(12,11),(12,12)],
    'B': [(11,2),(11,3),(12,2),(12,3)],
}

LUDO_SAFE_INDICES = {0, 8, 13, 21, 26, 34, 39, 47}

def render_ludo_board(game):
    cell = 40
    size = cell * 15
    img = Image.new('RGB', (size, size), (250, 250, 250))
    d = ImageDraw.Draw(img)

    # Draw grid cells
    for r in range(15):
        for c in range(15):
            x0, y0 = c * cell, r * cell
            d.rectangle([x0, y0, x0 + cell, y0 + cell], fill=(255, 255, 255), outline=(195, 195, 205), width=1)

    # Draw 4 colored corner yards
    yards = [
        (0, 0, LUDO_COLORS[0]),
        (0, 9, LUDO_COLORS[1]),
        (9, 9, LUDO_COLORS[2]),
        (9, 0, LUDO_COLORS[3]),
    ]
    for r0, c0, col in yards:
        x0, y0 = c0 * cell, r0 * cell
        x1, y1 = (c0 + 6) * cell, (r0 + 6) * cell
        d.rectangle([x0, y0, x1, y1], fill=col['rgb'], outline=(40, 40, 40), width=2)
        d.rounded_rectangle([x0 + 28, y0 + 28, x1 - 28, y1 - 28], radius=18, fill=(255, 255, 255), outline=(60, 60, 60), width=2)

    # Color home stretches & start squares
    for col in LUDO_COLORS:
        k = col['key']
        sr, sc = LUDO_TRACK_COORDS[col['start']]
        d.rectangle([sc * cell, sr * cell, (sc + 1) * cell, (sr + 1) * cell], fill=col['rgb'], outline=(120, 120, 120), width=1)
        for hr, hc in LUDO_HOME_COORDS[k]:
            d.rectangle([hc * cell, hr * cell, (hc + 1) * cell, (hr + 1) * cell], fill=col['light'], outline=(160, 160, 160), width=1)

    # Mark safe stars
    for idx in LUDO_SAFE_INDICES:
        sr, sc = LUDO_TRACK_COORDS[idx]
        cx, cy = sc * cell + cell // 2, sr * cell + cell // 2
        d.ellipse([cx - 6, cy - 6, cx + 6, cy + 6], outline=(110, 110, 110), width=2)

    # Draw Center 3x3 Home Triangles
    cx0, cy0 = 6 * cell, 6 * cell
    cx1, cy1 = 9 * cell, 9 * cell
    midx, midy = size // 2, size // 2
    d.polygon([(cx0, cy0), (cx0, cy1), (midx, midy)], fill=LUDO_COLORS[0]['rgb'], outline=(40, 40, 40))
    d.polygon([(cx0, cy0), (cx1, cy0), (midx, midy)], fill=LUDO_COLORS[1]['rgb'], outline=(40, 40, 40))
    d.polygon([(cx1, cy0), (cx1, cy1), (midx, midy)], fill=LUDO_COLORS[2]['rgb'], outline=(40, 40, 40))
    d.polygon([(cx0, cy1), (cx1, cy1), (midx, midy)], fill=LUDO_COLORS[3]['rgb'], outline=(40, 40, 40))

    # Draw Tokens for all joined players
    cell_occupants = defaultdict(list)
    for p in game['players']:
        ckey = p['color']['key']
        c_start = p['color']['start']
        for tidx, pos in enumerate(p['tokens']):
            if pos == -1:
                yr, yc = LUDO_YARD_COORDS[ckey][tidx]
                cell_occupants[(yr, yc)].append((p['color']['rgb'], tidx + 1))
            elif 0 <= pos <= 50:
                g_idx = (c_start + pos) % 52
                tr, tc = LUDO_TRACK_COORDS[g_idx]
                cell_occupants[(tr, tc)].append((p['color']['rgb'], tidx + 1))
            elif 51 <= pos <= 55:
                hr, hc = LUDO_HOME_COORDS[ckey][pos - 51]
                cell_occupants[(hr, hc)].append((p['color']['rgb'], tidx + 1))
            else:
                # Finished token near center
                cell_occupants[(7, 7)].append((p['color']['rgb'], tidx + 1))

    try:
        tfont = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', 15)
    except OSError:
        tfont = ImageFont.load_default()

    for (r, c), items in cell_occupants.items():
        base_x, base_y = c * cell + cell // 2, r * cell + cell // 2
        offsets = [(0, 0)] if len(items) == 1 else [(-8, -8), (8, -8), (-8, 8), (8, 8)]
        for i, (rgb, num) in enumerate(items[:4]):
            ox, oy = offsets[i]
            rad = 13 if len(items) == 1 else 9
            px, py = base_x + ox, base_y + oy
            d.ellipse([px - rad, py - rad, px + rad, py + rad], fill=rgb, outline=(255, 255, 255), width=2)
            d.ellipse([px - rad - 1, py - rad - 1, px + rad + 1, py + rad + 1], outline=(20, 20, 20), width=1)
            if len(items) == 1:
                d.text((px - 4, py - 8), str(num), font=tfont, fill=(255, 255, 255))

    out = io.BytesIO()
    out.name = 'ludo_board.png'
    img.save(out, 'PNG')
    out.seek(0)
    return out

def ludo_valid_moves(player, dice_val):
    moves = []
    for i, pos in enumerate(player['tokens']):
        if pos == -1 and dice_val == 6:
            moves.append(i)
        elif 0 <= pos < 56 and pos + dice_val <= 56:
            moves.append(i)
    return moves

def ludo_caption(game):
    lines = ["🎲 <b>MISHRA LUDO CHAMPIONSHIP</b> 🎲\n"]
    for idx, p in enumerate(game['players']):
        done_cnt = sum(1 for t in p['tokens'] if t == 56)
        out_cnt = sum(1 for t in p['tokens'] if 0 <= t < 56)
        turn_mark = " 👈 <b>(TURN)</b>" if (game['started'] and not game['over'] and idx == game['turn_idx']) else ""
        lines.append(f"{p['color']['emoji']} <a href=\"tg://user?id={p['uid']}\">{esc(p['name'])}</a> — 🏠 {done_cnt}/4 | 🎯 {out_cnt}{turn_mark}")

    if game['last_log']:
        lines.append(f"\n📝 <i>{esc(game['last_log'])}</i>")

    if not game['started']:
        lines.append("\n⏳ <i>Players join karein (Max 4). Start dabakar khel shuru karein!</i>")
    elif game['over']:
        w = game['winner']
        lines.append(f"\n🏆 <b>WINNER:</b> {w['color']['emoji']} <a href=\"tg://user?id={w['uid']}\">{esc(w['name'])}</a> (+100 Coins) 🎉")
    else:
        cur = game['players'][game['turn_idx']]
        if game['awaiting_move']:
            lines.append(f"\n🎲 <b>Dice: {game['dice']}</b> — {cur['color']['emoji']} {esc(cur['name'])}, apni goti chunein:")
        else:
            lines.append(f"\n👉 {cur['color']['emoji']} <b>{esc(cur['name'])}</b> ki baari hai — Roll Dice dabayein!")
    return "\n".join(lines)

def ludo_keyboard(gid, game):
    kb = types.InlineKeyboardMarkup(row_width=2)
    if game['over']:
        return kb
    if not game['started']:
        if len(game['players']) < 4:
            kb.add(types.InlineKeyboardButton('➕ Join Ludo Game', callback_data=f'ludo:{gid}:join'))
        if len(game['players']) >= 2:
            kb.add(types.InlineKeyboardButton('🚀 Start Match', callback_data=f'ludo:{gid}:start'))
        return kb

    cur = game['players'][game['turn_idx']]
    if not game['awaiting_move']:
        kb.add(types.InlineKeyboardButton(f"🎲 Roll Dice ({cur['color']['emoji']} {cur['name'][:12]})", callback_data=f'ludo:{gid}:roll'))
    else:
        valid = ludo_valid_moves(cur, game['dice'])
        btns = []
        for tidx in valid:
            pos = cur['tokens'][tidx]
            desc = "Open" if pos == -1 else f"Step {pos}→{pos + game['dice']}"
            btns.append(types.InlineKeyboardButton(f"{cur['color']['emoji']} Goti #{tidx+1} ({desc})", callback_data=f'ludo:{gid}:mv:{tidx}'))
        if btns:
            kb.add(*btns)
    return kb

def send_or_update_ludo(chat_id, gid, game, msg_id=None):
    board_img = render_ludo_board(game)
    cap = ludo_caption(game)
    kb = ludo_keyboard(gid, game)
    if msg_id:
        try:
            bot.edit_message_media(
                media=types.InputMediaPhoto(board_img, caption=cap, parse_mode='HTML'),
                chat_id=chat_id,
                message_id=msg_id,
                reply_markup=kb
            )
            return
        except Exception:
            pass
    bot.send_photo(chat_id, board_img, caption=cap, reply_markup=kb)

@bot.message_handler(commands=['ludo'])
def ludo_cmd(m):
    if not group(m):
        return bot.reply_to(m, 'Use /ludo in a group to play with friends!')
    gid = uuid.uuid4().hex[:8]
    game = {
        'chat_id': m.chat.id,
        'players': [{
            'uid': m.from_user.id,
            'name': user_name(m.from_user),
            'color': LUDO_COLORS[0],
            'tokens': [-1, -1, -1, -1]
        }],
        'started': False,
        'over': False,
        'winner': None,
        'turn_idx': 0,
        'dice': 0,
        'awaiting_move': False,
        'six_streak': 0,
        'last_log': f"{user_name(m.from_user)} ne Ludo table banaya!",
        'ts': time.time()
    }
    with LUDO_LOCK:
        now = time.time()
        for k in list(LUDO_GAMES.keys()):
            if now - LUDO_GAMES[k]['ts'] > 3600:
                LUDO_GAMES.pop(k, None)
        LUDO_GAMES[gid] = game

    send_or_update_ludo(m.chat.id, gid, game)

@bot.callback_query_handler(func=lambda call: bool(call.data) and call.data.startswith('ludo:'))
def ludo_callback(call):
    parts = call.data.split(':')
    if len(parts) < 3:
        return
    gid = parts[1]
    action = parts[2]
    uid = call.from_user.id
    uname = user_name(call.from_user)

    with LUDO_LOCK:
        game = LUDO_GAMES.get(gid)
        if not game:
            return bot.answer_callback_query(call.id, '⚠️ Yeh Ludo game expire ho chuka hai. /ludo se naya start karein.', show_alert=True)
        if game['over']:
            return bot.answer_callback_query(call.id, '🏁 Match khatam ho chuka hai!')

        game['ts'] = time.time()

        if action == 'join':
            if game['started']:
                return bot.answer_callback_query(call.id, 'Match pehle hi start ho chuka hai!', show_alert=True)
            if any(p['uid'] == uid for p in game['players']):
                return bot.answer_callback_query(call.id, 'Aap pehle se game mein hain!', show_alert=True)
            if len(game['players']) >= 4:
                return bot.answer_callback_query(call.id, 'Table full hai (4/4 players)!', show_alert=True)
            col = LUDO_COLORS[len(game['players'])]
            game['players'].append({
                'uid': uid,
                'name': uname,
                'color': col,
                'tokens': [-1, -1, -1, -1]
            })
            game['last_log'] = f"{uname} ({col['emoji']} {col['name']}) ne join kiya!"
            bot.answer_callback_query(call.id, f"✅ Aap {col['emoji']} {col['name']} team se join hue!")
            send_or_update_ludo(call.message.chat.id, gid, game, call.message.message_id)
            return

        if action == 'start':
            if game['started']:
                return bot.answer_callback_query(call.id, 'Match already running!')
            if not any(p['uid'] == uid for p in game['players']):
                return bot.answer_callback_query(call.id, 'Sirf joined players hi match start kar sakte hain!', show_alert=True)
            if len(game['players']) < 2:
                return bot.answer_callback_query(call.id, 'Kam se kam 2 players chahiye!', show_alert=True)
            game['started'] = True
            game['turn_idx'] = 0
            game['last_log'] = "Match shuru ho gaya! Red ki pehli baari."
            bot.answer_callback_query(call.id, '🚀 Ludo Match Started!')
            send_or_update_ludo(call.message.chat.id, gid, game, call.message.message_id)
            return

        cur = game['players'][game['turn_idx']]
        if uid != cur['uid']:
            return bot.answer_callback_query(call.id, f"⛔ Abhi {cur['color']['emoji']} {cur['name']} ki baari hai!", show_alert=True)

        if action == 'roll':
            if game['awaiting_move']:
                return bot.answer_callback_query(call.id, 'Pehle apni goti chunein!', show_alert=True)
            dval = random.randint(1, 6)
            game['dice'] = dval
            if dval == 6:
                game['six_streak'] += 1
            else:
                game['six_streak'] = 0

            if game['six_streak'] >= 3:
                game['six_streak'] = 0
                game['last_log'] = f"{cur['name']} ke lagatar teen 6 aaye — turn cancel!"
                game['turn_idx'] = (game['turn_idx'] + 1) % len(game['players'])
                bot.answer_callback_query(call.id, '🎲 Teen 6! Turn skip ho gaya!')
                send_or_update_ludo(call.message.chat.id, gid, game, call.message.message_id)
                return

            valid = ludo_valid_moves(cur, dval)
            if not valid:
                game['last_log'] = f"{cur['name']} ne 🎲 {dval} roll kiya — koi valid chal nahi."
                game['turn_idx'] = (game['turn_idx'] + 1) % len(game['players'])
                bot.answer_callback_query(call.id, f'🎲 {dval} aaya — koi move available nahi!')
                send_or_update_ludo(call.message.chat.id, gid, game, call.message.message_id)
                return

            # If only 1 valid move exists, auto-play it for fast smooth gameplay
            if len(valid) == 1:
                action = 'mv'
                parts = ['ludo', gid, 'mv', str(valid[0])]
            else:
                game['awaiting_move'] = True
                game['last_log'] = f"{cur['name']} ne 🎲 {dval} roll kiya! Goti chunein."
                bot.answer_callback_query(call.id, f'🎲 {dval} aaya! Apni goti chunein.')
                send_or_update_ludo(call.message.chat.id, gid, game, call.message.message_id)
                return

        if action == 'mv' and len(parts) == 4:
            tidx = int(parts[3])
            dval = game['dice']
            valid = ludo_valid_moves(cur, dval)
            if tidx not in valid:
                return bot.answer_callback_query(call.id, 'Invalid token move!', show_alert=True)

            old_pos = cur['tokens'][tidx]
            new_pos = 0 if old_pos == -1 else old_pos + dval
            cur['tokens'][tidx] = new_pos
            game['awaiting_move'] = False
            bonus_turn = (dval == 6)
            cut_msg = ""

            # Check capture on outer track (0..50) if not a safe star square
            if 0 <= new_pos <= 50:
                g_idx = (cur['color']['start'] + new_pos) % 52
                if g_idx not in LUDO_SAFE_INDICES:
                    for opp in game['players']:
                        if opp['uid'] == cur['uid']:
                            continue
                        for o_tidx, o_pos in enumerate(opp['tokens']):
                            if 0 <= o_pos <= 50:
                                o_gidx = (opp['color']['start'] + o_pos) % 52
                                if o_gidx == g_idx:
                                    opp['tokens'][o_tidx] = -1
                                    bonus_turn = True
                                    cut_msg = f" 💥 {opp['name']} ki goti kaat di!"

            if new_pos == 56:
                bonus_turn = True
                cut_msg += " 🏠 Goti Home pahunch gayi!"

            game['last_log'] = f"{cur['name']} (🎲 {dval}): Goti #{tidx+1} chali.{cut_msg}"

            # Check win condition (all 4 tokens at 56)
            if all(p_pos == 56 for p_pos in cur['tokens']):
                game['over'] = True
                game['winner'] = cur
                with DB_LOCK, connect() as c:
                    c.execute('INSERT OR IGNORE INTO wallet(user_id,coins,last_daily) VALUES(?,0,?)', (cur['uid'], ''))
                    c.execute('UPDATE wallet SET coins=coins+100 WHERE user_id=?', (cur['uid'],))
                bot.answer_callback_query(call.id, '🏆 MUBARAK HO! Aap Ludo Jeet Gaye!')
                send_or_update_ludo(call.message.chat.id, gid, game, call.message.message_id)
                return

            if not bonus_turn:
                game['six_streak'] = 0
                game['turn_idx'] = (game['turn_idx'] + 1) % len(game['players'])

            bot.answer_callback_query(call.id, f'🎲 {dval} moved!')
            send_or_update_ludo(call.message.chat.id, gid, game, call.message.message_id)

# ---------------- GROUP MESSAGE MONITOR (LAST HANDLER) ----------------
@bot.message_handler(func=lambda m: True, content_types=['text','sticker','animation','photo','video','document','audio','voice','video_note','contact','location','venue','poll','dice'])
def monitor(m):
    if not group(m) or not m.from_user or m.from_user.is_bot: return
    chat, uid = m.chat.id, m.from_user.id
    record(m)

    # 1. Rose-bot style auto-delete locks (Admin & /approve users bypass)
    if check_and_enforce_lock(m):
        return

    body = (m.text or m.caption or '').strip()

    # 2. Check active Cricketer Guess Game answer
    if check_cricket_answer(m):
        return

    # 3. Learn natural chat replies (text, stickers, GIFs) for chatbot
    learn_chatbot_data(m)

    if setting(chat, 'antispam') == 'on' and not admin(chat, uid) and not is_antispam_approved(chat, uid):
        now = time.time(); q = FLOOD[(chat,uid)]; q.append(now)
        while q and now-q[0] > 6: q.popleft()
        threshold = int(setting(chat, 'antispam_threshold', '7') or '7')
        if len(q) >= threshold:
            try: bot.delete_message(chat, m.message_id)
            except Exception: pass
            q.clear()
            action = setting(chat, 'antispam_action', 'mute')
            try:
                if action == 'mute':
                    bot.restrict_chat_member(chat, uid, permissions=types.ChatPermissions(can_send_messages=False), until_date=int(now+30))
                    notice = 'muted for 30 seconds'
                elif action == 'ban':
                    bot.ban_chat_member(chat, uid); notice = 'banned'
                elif action == 'kick':
                    bot.ban_chat_member(chat, uid); bot.unban_chat_member(chat, uid); notice = 'kicked'
                else:
                    n = warn_count(chat, uid) + 1; set_warn(chat, uid, n)
                    if n >= 3:
                        bot.ban_chat_member(chat, uid); set_warn(chat, uid, 0)
                        notice = 'banned after reaching 3 warnings'
                    else: notice = f'warned ({n}/3)'
                bot.send_message(chat, f'🛡️ {esc(user_name(m.from_user))} {notice} for flooding.')
            except Exception: log.exception('Antispam action failed')
            return

    # Check for Instagram Reel or Pinterest Video link in group
    insta_m = INSTA_VIDEO_RE.search(body)
    pin_m = PINTEREST_VIDEO_RE.search(body)
    if insta_m:
        send_download_button(chat, m.message_id, insta_m.group(0), 'instagram')
        return
    elif pin_m:
        send_download_button(chat, m.message_id, pin_m.group(0), 'pinterest')
        return

    # Anti-link restriction in group for other links
    if body and setting(chat, 'antilink') == 'on' and URL_RE.search(body) and not admin(chat,uid):
        try: bot.delete_message(chat,m.message_id); bot.send_message(chat,'🔗 Links are not allowed in this group.')
        except Exception: pass
        return

    # Custom word filters
    if body and not body.startswith('/'):
        for row in filters(chat):
            if row['keyword'] in body.lower():
                try:
                    kind = row['response_type'] or 'text'
                    if kind == 'sticker': bot.send_sticker(chat, row['file_id'], reply_to_message_id=m.message_id)
                    elif kind == 'animation': bot.send_animation(chat, row['file_id'], reply_to_message_id=m.message_id)
                    else: bot.reply_to(m, row['response'])
                except Exception: log.exception('Filter reply failed')
                break

    if body.startswith('/afk'):
        return

    # Remove AFK when member speaks
    with DB_LOCK, connect() as c:
        away = c.execute('SELECT reason,since,media_type,media_file_id FROM global_afk WHERE user_id=?', (uid,)).fetchone()
        if away:
            c.execute('DELETE FROM global_afk WHERE user_id=?', (uid,))
            c.execute('DELETE FROM afk WHERE user_id=?', (uid,))
    if away:
        AFK_NOTICE.pop((chat,uid), None)
        bot.reply_to(m, f'👋 Welcome back {esc(user_name(m.from_user))}! Away for <b>{duration_text(time.time()-away["since"])}</b>.')

    # Detect AFK mentions
    targets = {}
    if m.reply_to_message and m.reply_to_message.from_user:
        u = m.reply_to_message.from_user
        if u.id != uid: targets[u.id] = u
    for ent in (m.entities or m.caption_entities or []):
        if ent.type == 'text_mention' and ent.user: targets[ent.user.id] = ent.user
        elif ent.type == 'mention':
            encoded = body.encode('utf-16-le')
            start_byte, end_byte = ent.offset * 2, (ent.offset + ent.length) * 2
            username = encoded[start_byte:end_byte].decode('utf-16-le', errors='ignore').lstrip('@').lower()
            with DB_LOCK, connect() as c:
                r = c.execute('SELECT user_id,name,username FROM global_users WHERE lower(replace(username, "@", ""))=? LIMIT 1', (username,)).fetchone()
            if r:
                class CachedUser: pass
                u = CachedUser(); u.id=r['user_id']; u.first_name=r['name']; u.username=r['username']; targets[u.id]=u
    for target_id_value, target in targets.items():
        with DB_LOCK, connect() as c:
            af = c.execute('SELECT reason,since,media_type,media_file_id FROM global_afk WHERE user_id=?', (target_id_value,)).fetchone()
        if not af: continue
        key = (chat,target_id_value,uid); now = time.time()
        if now - AFK_NOTICE.get(key, 0) < 60: continue
        AFK_NOTICE[key] = now
        try:
            bot.reply_to(m, f'💤 <a href="tg://user?id={target_id_value}">{esc(user_name(target))}</a> is AFK: <b>{esc(af["reason"])}</b> · {duration_text(now-af["since"])}')
            if af['media_file_id']:
                if af['media_type'] == 'sticker': bot.send_sticker(chat, af['media_file_id'], reply_to_message_id=m.message_id)
                elif af['media_type'] == 'animation': bot.send_animation(chat, af['media_file_id'], reply_to_message_id=m.message_id)
        except Exception: log.exception('AFK notice failed')

    # 4. Chatbot trigger: when someone says "Mishra robot" or replies to bot's message
    if setting(chat, 'chatbot', 'on') == 'on' and not body.startswith('/'):
        replied_to_bot = False
        try:
            if m.reply_to_message and m.reply_to_message.from_user and m.reply_to_message.from_user.id == bot.get_me().id:
                replied_to_bot = True
        except Exception:
            pass
        if (body and BOT_NAME_RE.search(body)) or replied_to_bot:
            send_chatbot_reply(m, body or 'hi')

# ---------------- STARTUP ----------------
def main():
    init_db()
    threading.Thread(target=health_server, daemon=True, name='health-server').start()
    threading.Thread(target=hourly_cricket_loop, daemon=True, name='cricket-game').start()
    log.info('Mishra Guard Robot V4 started; health server on port %s', PORT)
    while True:
        try:
            bot.infinity_polling(skip_pending=True)
        except Exception as e:
            log.exception('Polling stopped')
            message = str(e).lower()
            if '409' in message or 'conflict' in message or 'terminated by other getupdates' in message:
                log.critical('Telegram reports another polling session for this token. Stop other Render instances/services; not retrying in a tight loop.')
                time.sleep(30)
            else:
                time.sleep(5)

if __name__ == '__main__':
    main()
