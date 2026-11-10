"""Mishra Guard Robot V4 - Telegram group management bot.
Required env: BOT_TOKEN. Optional: PORT, DATABASE_PATH, GROQ_API_KEY, GROQ_MODEL.
Run exactly one polling instance per token. Render: one Web Service instance.
"""
import os, io, re, time, json, random, sqlite3, threading, logging, tempfile
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
AFK_NOTICE = {}  # (chat_id, afk_user_id, viewer_id) -> last notification time
URL_RE = re.compile(r'(https?://|www\.|t\.me/|telegram\.me/)', re.I)
MAX_PURGE = 5000

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
/download URL — supported public media
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

<b>Welcome</b>
/setwelcome TEXT /welcome on|off
/setgoodbye TEXT /goodbye on|off

<b>Media & mentions</b>
/mmf TOP ; BOTTOM — reply to photo/static sticker
/kang — static sticker to photo; animated/video behavior retained
@admin [reason] / @all [message] (admin only for @all)

<b>Optional AI</b>
/ai question — needs GROQ_API_KEY'''

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
        if not call.from_user or not admin(chat, call.from_user.id):
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
    p = (m.text or '').split(maxsplit=1)
    if len(p) < 2: return bot.reply_to(m, 'Provide a message. Placeholders: {name}, {group}, {username}, {id}.')
    key = 'welcome_text' if p[0].split('@')[0] == '/setwelcome' else 'goodbye_text'
    set_setting(m.chat.id, key, p[1][:1000]); bot.reply_to(m, '✅ Group message saved.')

def render_group_template(chat, u, template):
    return template.replace('{name}', esc(user_name(u))).replace('{group}', esc(chat.title or 'the group')).replace('{username}', esc('@'+u.username if u.username else user_name(u))).replace('{id}', str(u.id))

@bot.message_handler(content_types=['new_chat_members'])
def new_members(m):
    if setting(m.chat.id, 'welcome', 'on') != 'on': return
    template = setting(m.chat.id, 'welcome_text', 'Welcome {name} to {group}! 🎉')
    for u in m.new_chat_members:
        remember_user(m.chat.id, u)
        if u.id != bot.get_me().id: bot.send_message(m.chat.id, render_group_template(m.chat, u, template))

@bot.message_handler(content_types=['left_chat_member'])
def left_member(m):
    if setting(m.chat.id, 'goodbye') != 'on' or not m.left_chat_member: return
    template = setting(m.chat.id, 'goodbye_text', 'Goodbye {name}! 👋')
    bot.send_message(m.chat.id, render_group_template(m.chat, m.left_chat_member, template))

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

# ---------------- COMPLETE LOCK / UNLOCK ----------------
PERMISSION_FIELDS = ('can_send_messages','can_send_audios','can_send_documents','can_send_photos','can_send_videos','can_send_video_notes','can_send_voice_notes','can_send_polls','can_send_other_messages','can_add_web_page_previews','can_invite_users','can_pin_messages','can_change_info','can_manage_topics')
def permissions_dict(perms):
    return {k: getattr(perms, k, None) for k in PERMISSION_FIELDS if getattr(perms, k, None) is not None}

def apply_lock(m, cmd, mode):
    chat = m.chat.id
    current = bot.get_chat(chat).permissions
    if cmd == 'lock' and mode == 'all' and not setting(chat, 'saved_permissions', ''):
        set_setting(chat, 'saved_permissions', json.dumps(permissions_dict(current)))
    base = permissions_dict(current)
    if not base:
        base = {k: True for k in PERMISSION_FIELDS}
    if mode == 'all':
        for k in PERMISSION_FIELDS:
            if k in base: base[k] = False
        if 'can_send_messages' in base: base['can_send_messages'] = False
    elif mode == 'media':
        for k in ('can_send_photos','can_send_videos','can_send_documents','can_send_audios','can_send_voice_notes','can_send_video_notes','can_send_other_messages'):
            if k in base: base[k] = False
    elif mode in ('sticker','gif'):
        base['can_send_other_messages'] = False
        if mode == 'gif': base['can_send_videos'] = False
    elif mode == 'photo': base['can_send_photos'] = False
    elif mode == 'video': base['can_send_videos'] = False
    elif mode == 'document': base['can_send_documents'] = False
    elif mode == 'audio': base['can_send_audios'] = False
    elif mode == 'voice': base['can_send_voice_notes'] = False
    elif mode == 'poll': base['can_send_polls'] = False
    elif mode == 'webpreview': base['can_add_web_page_previews'] = False
    else: raise ValueError('Unknown lock type')
    bot.set_chat_permissions(chat, types.ChatPermissions(**base))

def apply_unlock(m, mode):
    chat = m.chat.id
    if mode == 'all':
        raw = setting(chat, 'saved_permissions', '')
        if raw:
            try: base = json.loads(raw)
            except Exception: base = {}
        else: base = {}
        if not base: base = {k: True for k in PERMISSION_FIELDS}
        # Restore a complete, permissive send state if the previous lock came from older code.
        for k in ('can_send_messages','can_send_audios','can_send_documents','can_send_photos','can_send_videos','can_send_video_notes','can_send_voice_notes','can_send_polls','can_send_other_messages','can_add_web_page_previews'):
            base.setdefault(k, True)
        bot.set_chat_permissions(chat, types.ChatPermissions(**base))
        with DB_LOCK, connect() as c:
            c.execute('DELETE FROM settings WHERE chat_id=? AND key=?', (chat, 'saved_permissions'))
        return
    current = permissions_dict(bot.get_chat(chat).permissions)
    if not current: current = {k: True for k in PERMISSION_FIELDS}
    if mode == 'media':
        for k in ('can_send_photos','can_send_videos','can_send_documents','can_send_audios','can_send_voice_notes','can_send_video_notes','can_send_other_messages'): current[k] = True
    elif mode in ('sticker','gif'): current['can_send_other_messages'] = True; current['can_send_videos'] = True if mode == 'gif' else current.get('can_send_videos', True)
    elif mode == 'photo': current['can_send_photos'] = True
    elif mode == 'video': current['can_send_videos'] = True
    elif mode == 'document': current['can_send_documents'] = True
    elif mode == 'audio': current['can_send_audios'] = True
    elif mode == 'voice': current['can_send_voice_notes'] = True
    elif mode == 'poll': current['can_send_polls'] = True
    elif mode == 'webpreview': current['can_add_web_page_previews'] = True
    else: raise ValueError('Unknown unlock type')
    bot.set_chat_permissions(chat, types.ChatPermissions(**current))

def is_lock_approved(chat, uid):
    with DB_LOCK, connect() as c:
        return c.execute('SELECT 1 FROM lock_approved WHERE chat_id=? AND user_id=?', (chat,uid)).fetchone() is not None

@bot.message_handler(commands=['approve','unapprove','approved'])
def lock_approval(m):
    if not need_admin(m): return
    cmd=(m.text or '').split()[0].split('@')[0][1:].lower()
    target=target_id(m)
    if cmd == 'approved':
        with DB_LOCK, connect() as c:
            rows=c.execute('SELECT user_id FROM lock_approved WHERE chat_id=? ORDER BY created_at', (m.chat.id,)).fetchall()
        if not rows: return bot.reply_to(m,'No approved members yet.')
        return bot.reply_to(m,'✅ Approved members:\n'+'\n'.join(f'<a href="tg://user?id={r["user_id"]}">{r["user_id"]}</a>' for r in rows))
    if not target: return bot.reply_to(m, f'Reply to a user with /{cmd}.')
    if cmd == 'approve':
        with DB_LOCK, connect() as c:
            c.execute('INSERT OR REPLACE INTO lock_approved VALUES(?,?,?,?)',(m.chat.id,target,m.from_user.id,time.time()))
        try:
            bot.restrict_chat_member(m.chat.id,target,permissions=types.ChatPermissions(can_send_messages=True,can_send_audios=True,can_send_documents=True,can_send_photos=True,can_send_videos=True,can_send_video_notes=True,can_send_voice_notes=True,can_send_polls=True,can_send_other_messages=True,can_add_web_page_previews=True))
        except Exception:
            log.exception('Could not lift restrictions for approved user')
        return bot.reply_to(m, f'✅ Approved <code>{target}</code> for this group.')
    with DB_LOCK, connect() as c:
        c.execute('DELETE FROM lock_approved WHERE chat_id=? AND user_id=?',(m.chat.id,target))
    try:
        defaults=bot.get_chat(m.chat.id).permissions
        bot.restrict_chat_member(m.chat.id,target,permissions=defaults or types.ChatPermissions(can_send_messages=False))
    except Exception:
        log.exception('Could not reapply group default permissions after unapprove')
    return bot.reply_to(m, f'❎ Removed approval for <code>{target}</code>. Group default permissions have been reapplied where Telegram allows.')

@bot.message_handler(commands=['lock', 'unlock'])
def lock(m):
    if not need_admin(m): return
    parts = (m.text or '').split()
    cmd = parts[0].split('@')[0][1:].lower()
    mode = parts[1].lower() if len(parts) > 1 else 'all'
    allowed = {'all','media','sticker','photo','video','gif','document','audio','voice','poll','webpreview'}
    if mode not in allowed: return bot.reply_to(m, 'Types: all, media, sticker, photo, video, gif, document, audio, voice, poll, webpreview')
    try:
        if cmd == 'lock': apply_lock(m, cmd, mode)
        else: apply_unlock(m, mode)
        done(m, f'{"🔒 Locked" if cmd=="lock" else "🔓 Unlocked"}: <b>{esc(mode)}</b>.')
    except Exception as e:
        log.exception('Lock update failed')
        bot.reply_to(m, 'Permission update failed. Check that the bot is an admin with permission to restrict members. ' + esc(e))

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
                # Fall back per message for this batch if the installed library/API wrapper rejects bulk deletion.
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
            # Telegram requires can_manage_chat for an administrator; all other rights are disabled.
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
        # Telegram static sticker: WebP, max side 512 px.
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
            # Static sticker is converted to PNG so it is delivered as a photo.
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
    mentions = [f'<a href="tg://user?id={u["user_id"]}">{esc(u["name"])}</a>' for u in users if u['user_id'] != bot.get_me().id]
    reason = m.text.strip()[4:].strip()
    for i in range(0, len(mentions), 30):
        header = '📢 <b>Attention everyone!</b>\n' if i == 0 else ''
        extra = esc(reason)+'\n' if i == 0 and reason else ''
        bot.send_message(m.chat.id, header+extra+' '.join(mentions[i:i+30]), disable_web_page_preview=True)

# ---------------- OPTIONAL AI (GROQ FREE TIER) ----------------
@bot.message_handler(commands=['ai'])
def ai(m):
    p = (m.text or '').split(maxsplit=1)
    if len(p) < 2: return bot.reply_to(m, 'Usage: /ai ask a question')
    if not GROQ_API_KEY:
        return bot.reply_to(m, 'AI is optional. Add GROQ_API_KEY to Render Environment to enable /ai.')
    try:
        import urllib.request, urllib.error
        payload = json.dumps({
            'model': GROQ_MODEL,
            'messages': [
                {'role':'system','content':'You are MishraRobot, a helpful and safe Telegram group assistant. Be concise.'},
                {'role':'user','content':p[1][:3000]}
            ],
            'max_tokens':500,
            'temperature':0.7
        }).encode('utf-8')
        req = urllib.request.Request(
            'https://api.groq.com/openai/v1/chat/completions', data=payload,
            headers={
                'Authorization': f'Bearer {GROQ_API_KEY}',
                'Content-Type': 'application/json',
                'User-Agent': 'MishraRobot/1.0 (Telegram bot; Groq API client)',
                'Accept': 'application/json',
            },
            method='POST'
        )
        with urllib.request.urlopen(req, timeout=35) as response:
            result = json.loads(response.read().decode('utf-8'))
        answer = result.get('choices',[{}])[0].get('message',{}).get('content') or 'No answer generated.'
        bot.reply_to(m, esc(answer)[:3500])
    except urllib.error.HTTPError as e:
        detail = e.read().decode('utf-8', errors='replace')[:250]
        log.warning('Groq AI HTTP error %s: %s', e.code, detail)
        if e.code == 429:
            message = 'Groq free usage limit reached. Please try again after the limit resets.'
        elif e.code in (401, 403):
            message = 'Groq API key was rejected. Check GROQ_API_KEY in Render Environment.'
        else:
            message = f'Groq AI request failed (HTTP {e.code}). Please try again later.'
        bot.reply_to(m, message)
    except Exception as e:
        log.exception('Groq AI error')
        bot.reply_to(m, 'AI request failed. Check GROQ_API_KEY, GROQ_MODEL, and Render logs: ' + esc(e)[:200])

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

# ---------------- PUBLIC MEDIA DOWNLOADER ----------------
@bot.message_handler(commands=['download','dl'])
def download_media(m):
    parts=(m.text or '').split(maxsplit=1)
    if len(parts)<2:
        return bot.reply_to(m,'Usage: /download https://public-content-url')
    url=parts[1].strip()
    if not re.match(r'^https?://',url,re.I):
        return bot.reply_to(m,'Please send a valid http/https URL.')
    status=bot.reply_to(m,'⬇️ Checking public media link…')
    temp_dir=None
    try:
        import yt_dlp
        temp_dir=tempfile.mkdtemp(prefix='mishra_dl_')
        opts={
            'outtmpl': os.path.join(temp_dir,'%(title).80s.%(ext)s'),
            'format':'best[filesize<45M][ext=mp4]/best[filesize_approx<45M]/best',
            'noplaylist':True, 'max_filesize':45*1024*1024,
            'socket_timeout':30, 'retries':1, 'extractor_retries':1,
            'fragment_retries':1, 'sleep_interval_requests':1,
            'quiet':True, 'no_warnings':True,
            'restrictfilenames':True,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info=ydl.extract_info(url,download=True)
            path=ydl.prepare_filename(info)
        if not os.path.isfile(path):
            candidates=[os.path.join(temp_dir,x) for x in os.listdir(temp_dir)]
            candidates=[x for x in candidates if os.path.isfile(x)]
            if not candidates: raise RuntimeError('No downloadable media was returned.')
            path=max(candidates,key=os.path.getsize)
        size=os.path.getsize(path)
        if size>49*1024*1024:
            raise RuntimeError('File is larger than the safe Telegram upload limit (49 MB).')
        caption='📥 '+esc(info.get('title') or 'Downloaded media')[:900]
        ext=os.path.splitext(path)[1].lower()
        with open(path,'rb') as media:
            if ext in ('.mp4','.m4v','.mov','.webm'):
                bot.send_video(m.chat.id,media,caption=caption,reply_to_message_id=m.message_id,timeout=120)
            elif ext in ('.mp3','.m4a','.aac','.ogg','.opus','.wav'):
                bot.send_audio(m.chat.id,media,caption=caption,reply_to_message_id=m.message_id,timeout=120)
            else:
                bot.send_document(m.chat.id,media,caption=caption,reply_to_message_id=m.message_id,timeout=120)
        try: bot.delete_message(m.chat.id,status.message_id)
        except Exception: pass
    except Exception as e:
        log.exception('Downloader failed')
        detail = str(e)
        if '429' in detail or 'Too Many Requests' in detail:
            message = ('⏳ Instagram/platform is temporarily rate-limiting downloads (HTTP 429). '
                       'Please wait before retrying; repeated attempts can extend the limit. '
                       'This is a platform-side restriction, not a missing Python package.')
        elif '403' in detail or 'Forbidden' in detail:
            message = ('🔒 The platform refused this request (HTTP 403). The post may require '
                       'login or may not be publicly downloadable. I cannot bypass access controls.')
        elif 'No module named' in detail and 'yt_dlp' in detail:
            message = ('⚙️ Downloader dependency yt-dlp is missing on the server. '
                       'Check requirements.txt and redeploy.')
        else:
            message = ('❌ Download failed. The site may be unsupported, private/login-restricted, '
                       'DRM-protected, unavailable, or the file may exceed the size limit. '
                       'Only download content you own or are authorized to save.\\n' + esc(detail)[:350])
        try:
            bot.edit_message_text(message, m.chat.id, status.message_id)
        except Exception:
            bot.reply_to(m, message)
    finally:
        if temp_dir:
            import shutil
            shutil.rmtree(temp_dir,ignore_errors=True)

# ---------------- GROUP MESSAGE MONITOR (LAST HANDLER) ----------------
@bot.message_handler(func=lambda m: True, content_types=['text','sticker','animation','photo','video','document','audio','voice','video_note','contact','location','venue','poll','dice'])
def monitor(m):
    if not group(m) or not m.from_user or m.from_user.is_bot: return
    chat, uid = m.chat.id, m.from_user.id
    record(m)
    body = (m.text or m.caption or '').strip()
    if setting(chat, 'antispam') == 'on' and not admin(chat, uid) and not is_antispam_approved(chat, uid):
        now = time.time(); q = FLOOD[(chat,uid)]; q.append(now)
        while q and now-q[0] > 6: q.popleft()
        threshold = int(setting(chat, 'antispam_threshold', '7') or '7')
        if len(q) >= threshold:
            try: bot.delete_message(chat, m.message_id)
            except Exception: pass
            # Clear the window so one flood burst causes one moderation action.
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
    if body and setting(chat, 'antilink') == 'on' and URL_RE.search(body) and not admin(chat,uid):
        try: bot.delete_message(chat,m.message_id); bot.send_message(chat,'🔗 Links are not allowed in this group.')
        except Exception: pass
        return
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
    # Remove AFK when the member speaks again.
    with DB_LOCK, connect() as c:
        away = c.execute('SELECT reason,since,media_type,media_file_id FROM global_afk WHERE user_id=?', (uid,)).fetchone()
        if away:
            c.execute('DELETE FROM global_afk WHERE user_id=?', (uid,))
            c.execute('DELETE FROM afk WHERE user_id=?', (uid,))
    if away:
        AFK_NOTICE.pop((chat,uid), None)
        bot.reply_to(m, f'👋 Welcome back {esc(user_name(m.from_user))}! Away for <b>{duration_text(time.time()-away["since"])}</b>.')
    # Detect AFK users from replies, text mentions, and @username entities.
    targets = {}
    if m.reply_to_message and m.reply_to_message.from_user:
        u = m.reply_to_message.from_user
        if u.id != uid: targets[u.id] = u
    for ent in (m.entities or m.caption_entities or []):
        if ent.type == 'text_mention' and ent.user: targets[ent.user.id] = ent.user
        elif ent.type == 'mention':
            username = body[ent.offset:ent.offset+ent.length].lstrip('@').lower()
            with DB_LOCK, connect() as c:
                r = c.execute('SELECT user_id,name,username FROM global_users WHERE lower(username)=?', (username)).fetchone()
            if r:
                class CachedUser: pass
                u = CachedUser(); u.id=r['user_id']; u.first_name=r['name']; u.username=r['username']; targets[u.id]=u
    for target_id_value, target in targets.items():
        with DB_LOCK, connect() as c:
            af = c.execute('SELECT reason,since,media_type,media_file_id FROM global_afk WHERE user_id=?', (target_id_value)).fetchone()
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

# ---------------- STARTUP ----------------
def main():
    init_db()
    threading.Thread(target=health_server, daemon=True, name='health-server').start()
    log.info('Mishra Guard Robot V4 started; health server on port %s', PORT)
    # One Render instance and one polling process per BOT_TOKEN only.
    while True:
        try:
            bot.infinity_polling(skip_pending=True)
        except Exception:
            log.exception('Polling stopped; retrying in 5 seconds')
            time.sleep(5)

if __name__ == '__main__':
    main()
