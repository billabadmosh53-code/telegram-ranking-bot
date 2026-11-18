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
        CREATE TABLE IF NOT EXISTS asked_quizzes(chat_id INTEGER,category TEXT,question_hash TEXT,asked_at REAL,PRIMARY KEY(chat_id,category,question_hash));
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

# ---------------- HEALTH SERVER & 3D LUDO MINI APP HOST ----------------
LUDO3D_ROOMS = {}
LUDO3D_LOCK = threading.Lock()

LUDO3D_COLORS = [
    {'key': 'R', 'name': 'Red', 'start': 0},
    {'key': 'G', 'name': 'Green', 'start': 13},
    {'key': 'Y', 'name': 'Yellow', 'start': 26},
    {'key': 'B', 'name': 'Blue', 'start': 39},
]

LUDO3D_HTML = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>Mishra 3D Ludo King</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  * { box-sizing: border-box; user-select: none; -webkit-tap-highlight-color: transparent; }
  body {
    margin: 0; padding: 0;
    background: radial-gradient(circle at 50% 12%, #293b6b 0%, #11182c 65%, #080b14 100%);
    color: #fff; font-family: 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
    display: flex; flex-direction: column; align-items: center; min-height: 100vh; overflow-x: hidden;
  }
  .header {
    width: 100%; max-width: 440px; padding: 10px 14px;
    display: flex; justify-content: space-between; align-items: center;
    background: rgba(255,255,255,0.06); backdrop-filter: blur(10px);
    border-bottom: 1px solid rgba(255,215,0,0.22);
  }
  .title { font-weight: 900; font-size: 16px; letter-spacing: 0.6px; color: #ffd54f; text-shadow: 0 2px 10px rgba(255,213,79,0.5); }
  .header-right { display: flex; align-items: center; gap: 7px; }
  .room-badge { font-size: 11px; background: rgba(43,58,103,0.95); padding: 4px 10px; border-radius: 14px; color: #90caf9; font-weight: 700; border: 1px solid rgba(144,202,249,0.3); }
  .sound-btn { background: rgba(255,255,255,0.12); border: none; color: #fff; border-radius: 50%; width: 30px; height: 30px; font-size: 15px; cursor: pointer; display: flex; align-items: center; justify-content: center; }

  .players-bar {
    width: 100%; max-width: 440px; display: grid; grid-template-columns: repeat(2, 1fr);
    gap: 8px; padding: 8px 12px;
  }
  .p-card {
    background: linear-gradient(145deg, rgba(255,255,255,0.10), rgba(255,255,255,0.04));
    border-radius: 16px; padding: 8px 11px;
    display: flex; align-items: center; justify-content: space-between;
    border: 2px solid rgba(255,255,255,0.08); transition: all 0.28s cubic-bezier(0.22, 1, 0.36, 1);
  }
  .p-card.active {
    border-color: #ffd54f; background: linear-gradient(145deg, rgba(255,213,79,0.28), rgba(255,152,0,0.12));
    box-shadow: 0 0 20px rgba(255, 213, 79, 0.45); transform: translateY(-2px) scale(1.02);
  }
  .p-info { display: flex; align-items: center; gap: 8px; overflow: hidden; }
  .pawn-badge {
    width: 20px; height: 20px; border-radius: 50%; flex-shrink: 0;
    box-shadow: inset -3px -3px 5px rgba(0,0,0,0.45), inset 2px 2px 5px rgba(255,255,255,0.8), 0 3px 6px rgba(0,0,0,0.5);
  }
  .p-name { font-size: 12px; font-weight: 800; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 100px; }
  .p-tag { font-size: 10px; color: #ffd54f; font-weight: 800; }
  .p-score { font-size: 11px; color: #ffe082; font-weight: 800; background: rgba(0,0,0,0.3); padding: 2px 7px; border-radius: 10px; }

  .board-stage {
    width: 94vw; max-width: 416px; aspect-ratio: 1 / 1;
    margin: 4px auto; perspective: 1050px;
  }
  .board-wrap {
    width: 100%; height: 100%; padding: 11px; border-radius: 26px;
    background: linear-gradient(145deg, #9c5728, #4d240b);
    box-shadow: 0 24px 48px rgba(0,0,0,0.78), 0 5px 0 #2b1204, inset 0 2px 6px rgba(255,224,178,0.45);
    transform: rotateX(7deg);
  }
  canvas {
    width: 100%; height: 100%; display: block; border-radius: 16px;
    background: #fff; box-shadow: inset 0 0 10px rgba(0,0,0,0.35);
  }

  .controls {
    width: 100%; max-width: 440px; padding: 10px 14px 18px;
    display: flex; flex-direction: column; align-items: center; gap: 10px;
  }
  .status-msg {
    font-size: 13px; font-weight: 700; color: #fff8e1; text-align: center;
    background: rgba(255,255,255,0.1); border: 1px solid rgba(255,255,255,0.16);
    padding: 8px 14px; border-radius: 22px; width: 100%;
    box-shadow: 0 4px 14px rgba(0,0,0,0.28);
  }
  .dice-row { display: flex; align-items: center; justify-content: center; gap: 18px; width: 100%; min-height: 68px; }

  /* Real 3D Cube Dice */
  .cube-scene { width: 64px; height: 64px; perspective: 420px; cursor: pointer; }
  .cube {
    width: 100%; height: 100%; position: relative; transform-style: preserve-3d;
    transition: transform 0.55s cubic-bezier(0.2, 0.9, 0.3, 1.15);
  }
  .cube.rolling { animation: tumble3d 0.45s infinite linear; }
  @keyframes tumble3d {
    0%   { transform: rotateX(0deg) rotateY(0deg) rotateZ(0deg) scale(1.1); }
    50%  { transform: rotateX(220deg) rotateY(160deg) rotateZ(90deg) scale(1.18); }
    100% { transform: rotateX(360deg) rotateY(360deg) rotateZ(180deg) scale(1.1); }
  }
  .face {
    position: absolute; width: 64px; height: 64px; border-radius: 15px;
    background: radial-gradient(circle at 30% 30%, #ffffff, #e6eaf0);
    border: 2px solid #cfd8dc;
    box-shadow: inset 0 0 8px rgba(0,0,0,0.15);
    display: grid; grid-template-columns: repeat(3, 1fr); grid-template-rows: repeat(3, 1fr);
    padding: 8px; gap: 3px;
  }
  .pip { width: 11px; height: 11px; border-radius: 50%; background: #1e272e; align-self: center; justify-self: center; box-shadow: inset 0 1px 2px rgba(0,0,0,0.6); }
  .pip.red { background: #e53935; width: 13px; height: 13px; }
  .f1 { transform: rotateY(0deg) translateZ(32px); }
  .f6 { transform: rotateY(180deg) translateZ(32px); }
  .f2 { transform: rotateY(90deg) translateZ(32px); }
  .f5 { transform: rotateY(-90deg) translateZ(32px); }
  .f3 { transform: rotateX(90deg) translateZ(32px); }
  .f4 { transform: rotateX(-90deg) translateZ(32px); }

  .btn {
    border: none; border-radius: 18px; padding: 13px 22px; font-size: 14px; font-weight: 800;
    cursor: pointer; color: #fff; box-shadow: 0 7px 16px rgba(0,0,0,0.42); transition: transform 0.15s;
  }
  .btn:active { transform: scale(0.95); }
  .btn-join { background: linear-gradient(135deg, #00b894, #00cec9); }
  .btn-start { background: linear-gradient(135deg, #ffeaa7, #fdcb6e, #e17055); color: #1e272e; }
  .btn-roll { background: linear-gradient(135deg, #6c5ce7, #8e44ad); flex: 1; max-width: 220px; letter-spacing: 0.4px; }
</style>
</head>
<body>
  <div class="header">
    <div class="title">👑 MISHRA 3D LUDO KING</div>
    <div class="header-right">
      <div class="room-badge" id="roomLabel">Room: ...</div>
      <button class="sound-btn" id="soundBtn" onclick="toggleSound()">🔊</button>
    </div>
  </div>

  <div class="players-bar" id="playersBar"></div>

  <div class="board-stage">
    <div class="board-wrap">
      <canvas id="ludoCanvas" width="660" height="660"></canvas>
    </div>
  </div>

  <div class="controls">
    <div class="status-msg" id="statusMsg">Loading 3D Ludo Arena...</div>
    <div class="dice-row" id="actionRow"></div>
  </div>

<script>
const tg = window.Telegram && window.Telegram.WebApp;
if (tg) { tg.ready(); tg.expand(); }

const params = new URLSearchParams(window.location.search);
let roomId = params.get('room') || (tg && tg.initDataUnsafe && tg.initDataUnsafe.start_param) || 'default';
if (String(roomId).startsWith('ch_')) {
  window.location.replace('/chess3d?room=' + encodeURIComponent(roomId));
}
document.getElementById('roomLabel').innerText = 'Room #' + roomId;

// Persistent unique ID per device/session even if Telegram initDataUnsafe is unavailable
function resolveCurrentUser() {
  if (tg && tg.initDataUnsafe && tg.initDataUnsafe.user && tg.initDataUnsafe.user.id) {
    return {
      id: String(tg.initDataUnsafe.user.id),
      name: tg.initDataUnsafe.user.first_name || 'Player'
    };
  }
  let savedId = localStorage.getItem('mishra_ludo_uid');
  let savedName = localStorage.getItem('mishra_ludo_uname');
  if (!savedId) {
    savedId = 'guest_' + Date.now() + '_' + Math.floor(Math.random() * 10000);
    savedName = 'Guest_' + Math.floor(100 + Math.random() * 900);
    localStorage.setItem('mishra_ludo_uid', savedId);
    localStorage.setItem('mishra_ludo_uname', savedName);
  }
  return { id: savedId, name: savedName };
}
const user = resolveCurrentUser();

let soundOn = true;
function toggleSound() {
  soundOn = !soundOn;
  document.getElementById('soundBtn').innerText = soundOn ? '🔊' : '🔇';
}
function playTone(freq, dur, type='sine') {
  if (!soundOn) return;
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = type; osc.frequency.value = freq;
    gain.gain.setValueAtTime(0.11, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + dur);
    osc.connect(gain); gain.connect(ctx.destination);
    osc.start(); osc.stop(ctx.currentTime + dur);
  } catch (e) {}
}

const COLORS = [
  { key: 'R', name: 'Red', hex: '#ff3b5c', mid: '#ff758f', dark: '#9e0022', light: '#ffe0e6', start: 0 },
  { key: 'G', name: 'Green', hex: '#2ec4b6', mid: '#72efdd', dark: '#0b6e65', light: '#d8f8f5', start: 13 },
  { key: 'Y', name: 'Yellow', hex: '#ffbe0b', mid: '#ffe169', dark: '#b37400', light: '#fff6d6', start: 26 },
  { key: 'B', name: 'Blue', hex: '#3a86ff', mid: '#80b3ff', dark: '#0a47a9', light: '#dceaff', start: 39 }
];

const TRACK = [
  [6,1],[6,2],[6,3],[6,4],[6,5],[5,6],[4,6],[3,6],[2,6],[1,6],[0,6],[0,7],[0,8],
  [1,8],[2,8],[3,8],[4,8],[5,8],[6,9],[6,10],[6,11],[6,12],[6,13],[6,14],[7,14],[8,14],
  [8,13],[8,12],[8,11],[8,10],[8,9],[9,8],[10,8],[11,8],[12,8],[13,8],[14,8],[14,7],[14,6],
  [13,6],[12,6],[11,6],[10,6],[9,6],[8,5],[8,4],[8,3],[8,2],[8,1],[8,0],[7,0],[6,0]
];

const HOME_PATHS = {
  R: [[7,1],[7,2],[7,3],[7,4],[7,5]],
  G: [[1,7],[2,7],[3,7],[4,7],[5,7]],
  Y: [[7,13],[7,12],[7,11],[7,10],[7,9]],
  B: [[13,7],[12,7],[11,7],[10,7],[9,7]]
};

const YARDS = {
  R: [[1.6,1.6],[1.6,4.4],[4.4,1.6],[4.4,4.4]],
  G: [[1.6,10.6],[1.6,13.4],[4.4,10.6],[4.4,13.4]],
  Y: [[10.6,10.6],[10.6,13.4],[13.4,10.6],[13.4,13.4]],
  B: [[10.6,1.6],[10.6,4.4],[13.4,1.6],[13.4,4.4]]
};

const SAFES = new Set([0, 8, 13, 21, 26, 34, 39, 47]);
const CUBE_ROT = {
  1: 'rotateX(0deg) rotateY(0deg)',
  6: 'rotateX(0deg) rotateY(180deg)',
  2: 'rotateX(0deg) rotateY(-90deg)',
  5: 'rotateX(0deg) rotateY(90deg)',
  3: 'rotateX(-90deg) rotateY(0deg)',
  4: 'rotateX(90deg) rotateY(0deg)'
};

let state = null;
let animPositions = {};
let rollingAnim = false;
let actionBusy = false;
let clickableTokens = [];
let pulsePhase = 0;
let lastUiSignature = '';

const canvas = document.getElementById('ludoCanvas');
const ctx = canvas.getContext('2d');

function getCoordsForPos(pIdx, tIdx, pos) {
  const cell = 44;
  const col = COLORS[pIdx];
  if (pos === -1) {
    const [yr, yc] = YARDS[col.key][tIdx];
    return { x: yc * cell, y: yr * cell };
  } else if (pos >= 0 && pos <= 50) {
    const gIdx = (col.start + pos) % 52;
    const [tr, tc] = TRACK[gIdx];
    return { x: tc * cell + cell / 2, y: tr * cell + cell / 2 };
  } else if (pos >= 51 && pos <= 55) {
    const [hr, hc] = HOME_PATHS[col.key][pos - 51];
    return { x: hc * cell + cell / 2, y: hr * cell + cell / 2 };
  }
  return { x: 330 + (pIdx - 1.5) * 16, y: 330 };
}

function drawStar(cx, cy, rOut, rIn, fill, stroke) {
  ctx.beginPath();
  for (let i = 0; i < 10; i++) {
    const ang = (-90 + i * 36) * Math.PI / 180;
    const rad = i % 2 === 0 ? rOut : rIn;
    const x = cx + rad * Math.cos(ang);
    const y = cy + rad * Math.sin(ang);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  }
  ctx.closePath();
  ctx.fillStyle = fill; ctx.fill();
  ctx.strokeStyle = stroke; ctx.lineWidth = 1.5; ctx.stroke();
}

function drawCute3DPawn(x, y, rad, col, num, isMovable) {
  const bounce = isMovable ? Math.sin(pulsePhase) * 4 : 0;
  const py = y - bounce;

  if (isMovable) {
    const glowR = rad + 9 + Math.sin(pulsePhase) * 3;
    ctx.beginPath(); ctx.arc(x, y, glowR, 0, Math.PI * 2);
    ctx.fillStyle = 'rgba(255, 215, 0, 0.42)'; ctx.fill();
    ctx.strokeStyle = '#ffd54f'; ctx.lineWidth = 3; ctx.stroke();
  }

  // Soft ground shadow
  ctx.beginPath(); ctx.ellipse(x + 2, y + rad * 0.72, rad * 0.95, rad * 0.42, 0, 0, Math.PI * 2);
  ctx.fillStyle = 'rgba(0,0,0,0.38)'; ctx.fill();

  // 3D Pawn Skirt/Base
  ctx.beginPath(); ctx.arc(x, py + 3, rad, 0, Math.PI * 2);
  ctx.fillStyle = col.dark; ctx.fill();

  // 3D Glossy Body
  const grad = ctx.createRadialGradient(x - rad * 0.35, py - rad * 0.4, 2, x, py, rad);
  grad.addColorStop(0, '#ffffff');
  grad.addColorStop(0.28, col.mid);
  grad.addColorStop(0.78, col.hex);
  grad.addColorStop(1, col.dark);

  ctx.beginPath(); ctx.arc(x, py, rad * 0.94, 0, Math.PI * 2);
  ctx.fillStyle = grad; ctx.fill();
  ctx.strokeStyle = '#ffffff'; ctx.lineWidth = 2.2; ctx.stroke();

  // Cute glossy eye/shine highlight
  ctx.beginPath(); ctx.ellipse(x - rad * 0.32, py - rad * 0.35, rad * 0.22, rad * 0.12, -0.6, 0, Math.PI * 2);
  ctx.fillStyle = 'rgba(255,255,255,0.75)'; ctx.fill();

  // Token Number
  ctx.fillStyle = '#ffffff';
  ctx.font = 'bold 13px sans-serif';
  ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  ctx.fillText(String(num), x, py + 2);
}

function drawBoard() {
  const cell = 44;
  ctx.clearRect(0, 0, 660, 660);

  for (let r = 0; r < 15; r++) {
    for (let c = 0; c < 15; c++) {
      ctx.fillStyle = (r + c) % 2 === 0 ? '#ffffff' : '#f4f7fb';
      ctx.fillRect(c * cell, r * cell, cell, cell);
      ctx.strokeStyle = '#d5dee8';
      ctx.lineWidth = 1;
      ctx.strokeRect(c * cell, r * cell, cell, cell);
    }
  }

  const houses = [
    { r: 0, c: 0, col: COLORS[0] },
    { r: 0, c: 9, col: COLORS[1] },
    { r: 9, c: 9, col: COLORS[2] },
    { r: 9, c: 0, col: COLORS[3] }
  ];
  houses.forEach(h => {
    const x = h.c * cell, y = h.r * cell, w = 6 * cell;
    const g = ctx.createLinearGradient(x, y, x + w, y + w);
    g.addColorStop(0, h.col.mid); g.addColorStop(1, h.col.hex);
    ctx.fillStyle = g;
    ctx.fillRect(x, y, w, w);
    ctx.strokeStyle = '#263238'; ctx.lineWidth = 2; ctx.strokeRect(x, y, w, w);

    ctx.fillStyle = h.col.dark;
    ctx.beginPath(); ctx.roundRect(x + 30, y + 33, w - 56, w - 56, 26); ctx.fill();
    ctx.fillStyle = '#ffffff';
    ctx.beginPath(); ctx.roundRect(x + 28, y + 28, w - 56, w - 56, 26); ctx.fill();

    YARDS[h.col.key].forEach(([yr, yc]) => {
      const scx = yc * cell, scy = yr * cell;
      ctx.beginPath(); ctx.arc(scx, scy, 20, 0, Math.PI * 2);
      ctx.fillStyle = h.col.light; ctx.fill();
      ctx.strokeStyle = h.col.hex; ctx.lineWidth = 3.5; ctx.stroke();
    });
  });

  COLORS.forEach(col => {
    const [sr, sc] = TRACK[col.start];
    ctx.fillStyle = col.hex;
    ctx.fillRect(sc * cell, sr * cell, cell, cell);
    HOME_PATHS[col.key].forEach(([hr, hc]) => {
      ctx.fillStyle = col.hex;
      ctx.fillRect(hc * cell, hr * cell, cell, cell);
      ctx.strokeStyle = '#eceff1'; ctx.lineWidth = 1; ctx.strokeRect(hc * cell, hr * cell, cell, cell);
    });
  });

  SAFES.forEach(idx => {
    const [sr, sc] = TRACK[idx];
    const cx = sc * cell + cell / 2, cy = sr * cell + cell / 2;
    const isStart = COLORS.some(c => c.start === idx);
    drawStar(cx, cy, 14, 6.5, isStart ? '#ffffff' : '#cfd8dc', isStart ? '#37474f' : '#78909c');
  });

  const cx0 = 6 * cell, cy0 = 6 * cell, cx1 = 9 * cell, cy1 = 9 * cell, mx = 330, my = 330;
  const tris = [
    { pts: [[cx0,cy0],[cx0,cy1],[mx,my]], fill: COLORS[0].hex },
    { pts: [[cx0,cy0],[cx1,cy0],[mx,my]], fill: COLORS[1].hex },
    { pts: [[cx1,cy0],[cx1,cy1],[mx,my]], fill: COLORS[2].hex },
    { pts: [[cx0,cy1],[cx1,cy1],[mx,my]], fill: COLORS[3].hex }
  ];
  tris.forEach(t => {
    ctx.beginPath(); ctx.moveTo(t.pts[0][0], t.pts[0][1]);
    ctx.lineTo(t.pts[1][0], t.pts[1][1]); ctx.lineTo(t.pts[2][0], t.pts[2][1]);
    ctx.closePath(); ctx.fillStyle = t.fill; ctx.fill();
    ctx.strokeStyle = '#263238'; ctx.lineWidth = 1.5; ctx.stroke();
  });
  ctx.beginPath(); ctx.arc(mx, my, 19, 0, Math.PI * 2);
  ctx.fillStyle = '#ffd54f'; ctx.fill(); ctx.strokeStyle = '#5d4037'; ctx.lineWidth = 2; ctx.stroke();

  if (!state) return;

  clickableTokens = [];
  const curPlayer = state.players[state.turn_idx];
  const isMyTurn = state.started && !state.over && curPlayer && String(curPlayer.uid) === String(user.id);

  const renderList = [];
  state.players.forEach((p, pIdx) => {
    const col = COLORS[pIdx];
    p.tokens.forEach((pos, tIdx) => {
      const idKey = `${pIdx}_${tIdx}`;
      const target = getCoordsForPos(pIdx, tIdx, pos);
      if (!animPositions[idKey]) {
        animPositions[idKey] = { x: target.x, y: target.y };
      } else {
        animPositions[idKey].x += (target.x - animPositions[idKey].x) * 0.28;
        animPositions[idKey].y += (target.y - animPositions[idKey].y) * 0.28;
      }

      const canMove = isMyTurn && state.awaiting_move && String(p.uid) === String(curPlayer.uid) &&
                      ((pos === -1 && state.dice === 6) || (pos >= 0 && pos < 56 && pos + state.dice <= 56));

      renderList.push({
        pIdx, tIdx, col, pos,
        x: animPositions[idKey].x,
        y: animPositions[idKey].y,
        canMove
      });
    });
  });

  for (let i = 0; i < renderList.length; i++) {
    let sameCount = 0;
    for (let j = 0; j < i; j++) {
      if (Math.hypot(renderList[i].x - renderList[j].x, renderList[i].y - renderList[j].y) < 10) {
        sameCount++;
      }
    }
    const offs = [[0,0],[-8,-7],[8,-7],[-8,7],[8,7]];
    const [ox, oy] = offs[sameCount % offs.length];
    const item = renderList[i];
    const fx = item.x + ox, fy = item.y + oy;
    const rad = sameCount > 0 ? 12 : 16;

    if (item.canMove) {
      clickableTokens.push({ x: fx, y: fy, rad: 26, tIdx: item.tIdx });
    }
    drawCute3DPawn(fx, fy, rad, item.col, item.tIdx + 1, item.canMove);
  }
}

function animLoop() {
  pulsePhase += 0.12;
  drawBoard();
  requestAnimationFrame(animLoop);
}
requestAnimationFrame(animLoop);

canvas.addEventListener('click', (e) => {
  if (!clickableTokens.length || actionBusy) return;
  const rect = canvas.getBoundingClientRect();
  const scaleX = canvas.width / rect.width;
  const scaleY = canvas.height / rect.height;
  const cx = (e.clientX - rect.left) * scaleX;
  const cy = (e.clientY - rect.top) * scaleY;

  for (const tok of clickableTokens) {
    if (Math.hypot(cx - tok.x, cy - tok.y) <= tok.rad) {
      playTone(580, 0.14, 'triangle');
      sendAction('mv', tok.tIdx);
      break;
    }
  }
});

function build3DCubeHTML(diceVal, isRolling) {
  const rot = CUBE_ROT[diceVal || 1] || CUBE_ROT[1];
  return `
    <div class="cube ${isRolling ? 'rolling' : ''}" style="transform:${rot}">
      <div class="face f1"><div></div><div></div><div></div><div></div><div class="pip red"></div><div></div><div></div><div></div><div></div></div>
      <div class="face f2"><div class="pip"></div><div></div><div></div><div></div><div></div><div></div><div></div><div></div><div class="pip"></div></div>
      <div class="face f3"><div class="pip"></div><div></div><div></div><div></div><div class="pip"></div><div></div><div></div><div></div><div class="pip"></div></div>
      <div class="face f4"><div class="pip"></div><div></div><div class="pip"></div><div></div><div></div><div></div><div class="pip"></div><div></div><div class="pip"></div></div>
      <div class="face f5"><div class="pip"></div><div></div><div class="pip"></div><div></div><div class="pip"></div><div></div><div class="pip"></div><div></div><div class="pip"></div></div>
      <div class="face f6"><div class="pip"></div><div></div><div class="pip"></div><div class="pip"></div><div></div><div class="pip"></div><div class="pip"></div><div></div><div class="pip"></div></div>
    </div>
  `;
}

function triggerRoll() {
  if (rollingAnim || actionBusy) return;
  rollingAnim = true;
  lastUiSignature = '';
  renderUI();
  playTone(420, 0.1, 'square');
  setTimeout(() => {
    rollingAnim = false;
    sendAction('roll');
  }, 420);
}

function renderUI() {
  if (!state) return;
  const sig = JSON.stringify({
    s: state.started, o: state.over, t: state.turn_idx, d: state.dice,
    am: state.awaiting_move, log: state.last_log, r: rollingAnim, b: actionBusy,
    p: state.players.map(x => [x.uid, x.name, x.tokens])
  });
  if (sig === lastUiSignature) return;
  lastUiSignature = sig;

  const bar = document.getElementById('playersBar');
  bar.innerHTML = '';
  state.players.forEach((p, idx) => {
    const col = COLORS[idx];
    const done = p.tokens.filter(t => t === 56).length;
    const isTurn = state.started && !state.over && idx === state.turn_idx;
    const isMe = String(p.uid) === String(user.id);
    const card = document.createElement('div');
    card.className = 'p-card' + (isTurn ? ' active' : '');
    card.innerHTML = `
      <div class="p-info">
        <div class="pawn-badge" style="background:radial-gradient(circle at 30% 30%, #fff, ${col.hex}, ${col.dark})"></div>
        <div>
          <div class="p-name">${p.name} ${isMe ? '(You)' : ''}</div>
          ${isTurn ? '<div class="p-tag">🎯 TURN</div>' : ''}
        </div>
      </div>
      <div class="p-score">🏠 ${done}/4</div>
    `;
    bar.appendChild(card);
  });

  const statusEl = document.getElementById('statusMsg');
  const actionRow = document.getElementById('actionRow');
  actionRow.innerHTML = '';

  const alreadyJoined = state.players.some(p => String(p.uid) === String(user.id));

  if (!state.started) {
    statusEl.innerText = state.last_log || '2 se 4 players join karein!';
    if (!alreadyJoined && state.players.length < 4) {
      const btnJ = document.createElement('button');
      btnJ.className = 'btn btn-join';
      btnJ.innerText = '➕ Join 3D Ludo';
      btnJ.onclick = () => { playTone(520, 0.12); sendAction('join'); };
      actionRow.appendChild(btnJ);
    }
    if (alreadyJoined && state.players.length >= 2) {
      const btnS = document.createElement('button');
      btnS.className = 'btn btn-start';
      btnS.innerText = `🚀 Start Match (${state.players.length}P)`;
      btnS.onclick = () => { playTone(660, 0.18); sendAction('start'); };
      actionRow.appendChild(btnS);
    }
  } else if (state.over) {
    statusEl.innerText = `🏆 CHAMPION: ${state.winner_name} (+100 Coins)!`;
    const btnR = document.createElement('button');
    btnR.className = 'btn btn-join';
    btnR.innerText = '🔄 Play New 3D Match';
    btnR.onclick = () => sendAction('rematch');
    actionRow.appendChild(btnR);
  } else {
    const cur = state.players[state.turn_idx];
    const isMine = cur && String(cur.uid) === String(user.id);
    if (state.awaiting_move) {
      statusEl.innerText = isMine
        ? `🎲 Dice ${state.dice} aaya! Chamakti hui 3D goti par tap karein!`
        : `⏳ ${cur.name} ne 🎲 ${state.dice} roll kiya — goti chal rahe hain...`;
    } else {
      statusEl.innerText = isMine
        ? `👉 Aapki baari hai! 3D Cube Dice par tap karein!`
        : `⏳ Abhi ${cur.name} ki baari hai...`;
    }

    const cubeScene = document.createElement('div');
    cubeScene.className = 'cube-scene';
    cubeScene.innerHTML = build3DCubeHTML(state.dice || 1, rollingAnim);
    if (isMine && !state.awaiting_move && !actionBusy) {
      cubeScene.onclick = triggerRoll;
    }
    actionRow.appendChild(cubeScene);

    if (isMine && !state.awaiting_move && !actionBusy) {
      const btnRoll = document.createElement('button');
      btnRoll.className = 'btn btn-roll';
      btnRoll.innerText = '🎲 Roll 3D Dice';
      btnRoll.onclick = triggerRoll;
      actionRow.appendChild(btnRoll);
    }
  }
}

async function fetchState() {
  if (rollingAnim || actionBusy) return;
  try {
    const res = await fetch(`/api/ludo3d?room=${encodeURIComponent(roomId)}`);
    if (res.ok && !rollingAnim && !actionBusy) {
      state = await res.json();
      renderUI();
    }
  } catch (e) {}
}

async function sendAction(action, tokenIdx = -1) {
  if (actionBusy) return;
  actionBusy = true;
  lastUiSignature = '';
  renderUI();
  try {
    const res = await fetch('/api/ludo3d', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        room: roomId,
        action: action,
        uid: String(user.id),
        name: user.name || 'Player',
        token: tokenIdx
      })
    });
    if (res.ok) {
      state = await res.json();
    }
  } catch (e) {}
  actionBusy = false;
  lastUiSignature = '';
  renderUI();
}

fetchState();
setInterval(fetchState, 1200);
</script>
</body>
</html>'''

def get_or_create_3d_room(room_id):
    with LUDO3D_LOCK:
        now = time.time()
        for k in list(LUDO3D_ROOMS.keys()):
            if now - LUDO3D_ROOMS[k].get('ts', 0) > 7200:
                LUDO3D_ROOMS.pop(k, None)
        if room_id not in LUDO3D_ROOMS:
            LUDO3D_ROOMS[room_id] = {
                'room': room_id,
                'players': [],
                'started': False,
                'over': False,
                'winner_name': '',
                'turn_idx': 0,
                'dice': 0,
                'awaiting_move': False,
                'six_streak': 0,
                'last_log': 'Table ready! Join karein.',
                'ts': now
            }
        return LUDO3D_ROOMS[room_id]

def handle_3d_ludo_post(data):
    room_id = str(data.get('room', 'default'))[:24]
    action = str(data.get('action', ''))
    uid = str(data.get('uid', '')).strip()
    uname = str(data.get('name', 'Player'))[:24]
    tidx = int(data.get('token', -1))

    room = get_or_create_3d_room(room_id)
    if not uid:
        return room

    with LUDO3D_LOCK:
        room['ts'] = time.time()
        if action == 'rematch':
            room['players'] = [{'uid': uid, 'name': uname, 'tokens': [-1, -1, -1, -1]}]
            room['started'] = False
            room['over'] = False
            room['winner_name'] = ''
            room['turn_idx'] = 0
            room['dice'] = 0
            room['awaiting_move'] = False
            room['six_streak'] = 0
            room['last_log'] = f"{uname} ne naya 3D table banaya!"
            return room

        if room['over']:
            return room

        if action == 'join':
            if not room['started'] and len(room['players']) < 4 and not any(str(p['uid']) == uid for p in room['players']):
                room['players'].append({'uid': uid, 'name': uname, 'tokens': [-1, -1, -1, -1]})
                room['last_log'] = f"{uname} ne 3D Ludo join kiya!"
            return room

        if action == 'start':
            if not room['started'] and len(room['players']) >= 2 and any(str(p['uid']) == uid for p in room['players']):
                room['started'] = True
                room['turn_idx'] = 0
                room['awaiting_move'] = False
                room['six_streak'] = 0
                room['last_log'] = f"Match shuru! {room['players'][0]['name']} ki baari."
            return room

        if not room['started'] or not room['players']:
            return room

        num_p = len(room['players'])
        room['turn_idx'] = room['turn_idx'] % num_p
        cur = room['players'][room['turn_idx']]

        # Strict turn enforcement: ONLY the current turn's player can roll or move!
        if str(cur['uid']) != uid:
            return room

        if action == 'roll':
            if room['awaiting_move']:
                return room
            dval = random.randint(1, 6)
            room['dice'] = dval
            room['six_streak'] = (room['six_streak'] + 1) if dval == 6 else 0

            # Three consecutive 6s -> forfeit turn immediately to next player
            if room['six_streak'] >= 3:
                room['six_streak'] = 0
                room['awaiting_move'] = False
                room['turn_idx'] = (room['turn_idx'] + 1) % num_p
                nxt_name = room['players'][room['turn_idx']]['name']
                room['last_log'] = f"{cur['name']} ke lagatar 3 sixes aaye — turn skip! Ab {nxt_name} ki baari."
                return room

            valid = [i for i, pos in enumerate(cur['tokens']) if (pos == -1 and dval == 6) or (0 <= pos < 56 and pos + dval <= 56)]
            if not valid:
                # No valid token can move -> pass turn strictly to next player
                room['six_streak'] = 0
                room['awaiting_move'] = False
                room['turn_idx'] = (room['turn_idx'] + 1) % num_p
                nxt_name = room['players'][room['turn_idx']]['name']
                room['last_log'] = f"{cur['name']} ne {dval} roll kiya (koi chal nahi). Ab {nxt_name} ki baari!"
                return room

            # Auto-move if only 1 unique move is possible (e.g. all tokens in yard and rolled 6, or only 1 token open)
            unique_positions = {cur['tokens'][i] for i in valid}
            if len(valid) == 1 or len(unique_positions) == 1:
                tidx = valid[0]
                action = 'mv'
                room['awaiting_move'] = True
            else:
                room['awaiting_move'] = True
                room['last_log'] = f"{cur['name']} ne {dval} roll kiya! Goti chunein."
                return room

        if action == 'mv' and room['awaiting_move'] and 0 <= tidx < 4:
            dval = room['dice']
            pos = cur['tokens'][tidx]
            if not ((pos == -1 and dval == 6) or (0 <= pos < 56 and pos + dval <= 56)):
                return room

            new_pos = 0 if pos == -1 else pos + dval
            cur['tokens'][tidx] = new_pos
            room['awaiting_move'] = False
            bonus = (dval == 6)
            cut_txt = ''

            if 0 <= new_pos <= 50:
                my_start = LUDO3D_COLORS[room['turn_idx']]['start']
                g_idx = (my_start + new_pos) % 52
                if g_idx not in LUDO_SAFE_INDICES:
                    for o_idx, opp in enumerate(room['players']):
                        if str(opp['uid']) == uid:
                            continue
                        opp_start = LUDO3D_COLORS[o_idx]['start']
                        for ot_i, o_pos in enumerate(opp['tokens']):
                            if 0 <= o_pos <= 50 and ((opp_start + o_pos) % 52) == g_idx:
                                opp['tokens'][ot_i] = -1
                                bonus = True
                                cut_txt = f" 💥 {opp['name']} ki goti kaat di!"

            if new_pos == 56:
                bonus = True
                cut_txt += " 🏠 Goti Home!"

            if all(t == 56 for t in cur['tokens']):
                room['over'] = True
                room['winner_name'] = cur['name']
                room['last_log'] = f"🏆 {cur['name']} Ludo jeet gaye!"
                if uid.isdigit():
                    with DB_LOCK, connect() as c:
                        c.execute('INSERT OR IGNORE INTO wallet(user_id,coins,last_daily) VALUES(?,0,?)', (int(uid), ''))
                        c.execute('UPDATE wallet SET coins=coins+100 WHERE user_id=?', (int(uid),))
                return room

            if bonus:
                # Extra turn ONLY on rolling 6, cutting opponent, or reaching Home
                room['last_log'] = f"{cur['name']} ({dval}): Goti {tidx+1} chali.{cut_txt} Bonus turn!"
            else:
                room['six_streak'] = 0
                room['turn_idx'] = (room['turn_idx'] + 1) % num_p
                nxt_name = room['players'][room['turn_idx']]['name']
                room['last_log'] = f"{cur['name']} ({dval}): Goti {tidx+1} chali.{cut_txt} Ab {nxt_name} ki baari!"
            return room

    return room

# ---------------- ROYAL 3D CHESS MINI APP HOST (/chess3d & /api/chess3d) ----------------
CHESS3D_ROOMS = {}
CHESS3D_LOCK = threading.Lock()

CHESS3D_HTML = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>Mishra Royal 3D Chess</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/chess.js/0.10.3/chess.min.js"></script>
<style>
  * { box-sizing: border-box; user-select: none; -webkit-tap-highlight-color: transparent; }
  body {
    margin: 0; padding: 0;
    background: radial-gradient(circle at 50% 10%, #243447 0%, #101722 65%, #070a10 100%);
    color: #fff; font-family: 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
    display: flex; flex-direction: column; align-items: center; min-height: 100vh; overflow-x: hidden;
  }
  .header {
    width: 100%; max-width: 440px; padding: 10px 14px;
    display: flex; justify-content: space-between; align-items: center;
    background: rgba(255,255,255,0.06); backdrop-filter: blur(10px);
    border-bottom: 1px solid rgba(255,215,0,0.22);
  }
  .title { font-weight: 900; font-size: 16px; letter-spacing: 0.6px; color: #ffd54f; text-shadow: 0 2px 10px rgba(255,213,79,0.5); }
  .header-right { display: flex; align-items: center; gap: 7px; }
  .room-badge { font-size: 11px; background: rgba(43,58,103,0.95); padding: 4px 10px; border-radius: 14px; color: #90caf9; font-weight: 700; border: 1px solid rgba(144,202,249,0.3); }
  .sound-btn { background: rgba(255,255,255,0.12); border: none; color: #fff; border-radius: 50%; width: 30px; height: 30px; font-size: 15px; cursor: pointer; display: flex; align-items: center; justify-content: center; }

  .players-bar {
    width: 100%; max-width: 440px; display: grid; grid-template-columns: repeat(2, 1fr);
    gap: 8px; padding: 8px 12px;
  }
  .p-card {
    background: linear-gradient(145deg, rgba(255,255,255,0.10), rgba(255,255,255,0.04));
    border-radius: 16px; padding: 8px 11px;
    display: flex; align-items: center; justify-content: space-between;
    border: 2px solid rgba(255,255,255,0.08); transition: all 0.28s ease;
  }
  .p-card.active {
    border-color: #ffd54f; background: linear-gradient(145deg, rgba(255,213,79,0.28), rgba(255,152,0,0.12));
    box-shadow: 0 0 20px rgba(255, 213, 79, 0.45); transform: translateY(-2px) scale(1.02);
  }
  .p-info { display: flex; align-items: center; gap: 8px; overflow: hidden; }
  .piece-dot {
    width: 22px; height: 22px; border-radius: 50%; flex-shrink: 0; font-size: 14px;
    display: flex; align-items: center; justify-content: center; font-weight: 900;
    box-shadow: 0 3px 6px rgba(0,0,0,0.5);
  }
  .p-name { font-size: 12px; font-weight: 800; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 115px; }
  .p-tag { font-size: 10px; color: #ffd54f; font-weight: 800; }

  .board-stage {
    width: 94vw; max-width: 416px; aspect-ratio: 1 / 1;
    margin: 4px auto; perspective: 1100px;
  }
  .board-wrap {
    width: 100%; height: 100%; padding: 12px; border-radius: 26px;
    background: linear-gradient(145deg, #6d3b1e, #361909);
    box-shadow: 0 26px 50px rgba(0,0,0,0.82), 0 6px 0 #1f0d04, inset 0 2px 6px rgba(255,224,178,0.4);
    transform: rotateX(9deg);
  }
  canvas {
    width: 100%; height: 100%; display: block; border-radius: 14px;
    box-shadow: inset 0 0 12px rgba(0,0,0,0.45);
  }

  .controls {
    width: 100%; max-width: 440px; padding: 10px 14px 18px;
    display: flex; flex-direction: column; align-items: center; gap: 10px;
  }
  .status-msg {
    font-size: 13px; font-weight: 700; color: #fff8e1; text-align: center;
    background: rgba(255,255,255,0.1); border: 1px solid rgba(255,255,255,0.16);
    padding: 8px 14px; border-radius: 22px; width: 100%;
    box-shadow: 0 4px 14px rgba(0,0,0,0.28);
  }
  .btn-row { display: flex; align-items: center; justify-content: center; gap: 12px; width: 100%; flex-wrap: wrap; }
  .btn {
    border: none; border-radius: 18px; padding: 12px 20px; font-size: 14px; font-weight: 800;
    cursor: pointer; color: #fff; box-shadow: 0 7px 16px rgba(0,0,0,0.42); transition: transform 0.15s;
  }
  .btn:active { transform: scale(0.95); }
  .btn-join { background: linear-gradient(135deg, #00b894, #00cec9); }
  .btn-ai { background: linear-gradient(135deg, #fdcb6e, #e17055); color: #1e272e; }
  .btn-flip { background: linear-gradient(135deg, #6c5ce7, #8e44ad); }
</style>
</head>
<body>
  <div class="header">
    <div class="title">♟️ MISHRA ROYAL 3D CHESS</div>
    <div class="header-right">
      <div class="room-badge" id="roomLabel">Room: ...</div>
      <button class="sound-btn" id="soundBtn" onclick="toggleSound()">🔊</button>
    </div>
  </div>

  <div class="players-bar" id="playersBar"></div>

  <div class="board-stage">
    <div class="board-wrap">
      <canvas id="chessCanvas" width="640" height="640"></canvas>
    </div>
  </div>

  <div class="controls">
    <div class="status-msg" id="statusMsg">Loading 3D Chess Arena...</div>
    <div class="btn-row" id="actionRow"></div>
  </div>

<script>
const tg = window.Telegram && window.Telegram.WebApp;
if (tg) { tg.ready(); tg.expand(); }

const params = new URLSearchParams(window.location.search);
let roomId = params.get('room') || (tg && tg.initDataUnsafe && tg.initDataUnsafe.start_param) || 'default';
document.getElementById('roomLabel').innerText = 'Room #' + roomId;

function resolveCurrentUser() {
  if (tg && tg.initDataUnsafe && tg.initDataUnsafe.user && tg.initDataUnsafe.user.id) {
    return { id: String(tg.initDataUnsafe.user.id), name: tg.initDataUnsafe.user.first_name || 'Player' };
  }
  let savedId = localStorage.getItem('mishra_chess_uid');
  let savedName = localStorage.getItem('mishra_chess_uname');
  if (!savedId) {
    savedId = 'guest_' + Date.now() + '_' + Math.floor(Math.random() * 10000);
    savedName = 'Guest_' + Math.floor(100 + Math.random() * 900);
    localStorage.setItem('mishra_chess_uid', savedId);
    localStorage.setItem('mishra_chess_uname', savedName);
  }
  return { id: savedId, name: savedName };
}
const user = resolveCurrentUser();

let soundOn = true;
function toggleSound() {
  soundOn = !soundOn;
  document.getElementById('soundBtn').innerText = soundOn ? '🔊' : '🔇';
}
function playTone(freq, dur, type='sine') {
  if (!soundOn) return;
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = type; osc.frequency.value = freq;
    gain.gain.setValueAtTime(0.11, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + dur);
    osc.connect(gain); gain.connect(ctx.destination);
    osc.start(); osc.stop(ctx.currentTime + dur);
  } catch (e) {}
}

const PIECE_GLYPHS = {
  wK: '♔', wQ: '♕', wR: '♖', wB: '♗', wN: '♘', wP: '♙',
  bK: '♚', bQ: '♛', bR: '♜', bB: '♝', bN: '♞', bP: '♟'
};
const PIECE_VALS = { p: 10, n: 30, b: 30, r: 50, q: 90, k: 900 };

let state = null;
let chess = new Chess();
let selectedSq = null;
let legalTargets = [];
let flipped = false;
let actionBusy = false;
let pulsePhase = 0;

const canvas = document.getElementById('chessCanvas');
const ctx = canvas.getContext('2d');

function sqToCoords(r, c) {
  const dr = flipped ? (7 - r) : r;
  const dc = flipped ? (7 - c) : c;
  return { x: dc * 80, y: dr * 80 };
}

function coordsToSq(px, py) {
  const dc = Math.floor(px / 80);
  const dr = Math.floor(py / 80);
  if (dc < 0 || dc > 7 || dr < 0 || dr > 7) return null;
  const r = flipped ? (7 - dr) : dr;
  const c = flipped ? (7 - dc) : dc;
  const file = String.fromCharCode(97 + c);
  const rank = 8 - r;
  return file + rank;
}

function draw3DChessPiece(cx, cy, color, type, isSelected) {
  const lift = isSelected ? 6 + Math.sin(pulsePhase) * 3 : 0;
  const py = cy - lift;
  const rad = 28;

  // Ground shadow
  ctx.beginPath(); ctx.ellipse(cx + 3, cy + 22, rad * 0.85, rad * 0.36, 0, 0, Math.PI * 2);
  ctx.fillStyle = 'rgba(0,0,0,0.45)'; ctx.fill();

  // 3D Sculpted Pedestal Base
  ctx.beginPath(); ctx.arc(cx, py + 4, rad, 0, Math.PI * 2);
  ctx.fillStyle = color === 'w' ? '#b0bec5' : '#0d1117'; ctx.fill();

  const grad = ctx.createRadialGradient(cx - 10, py - 12, 3, cx, py, rad);
  if (color === 'w') {
    grad.addColorStop(0, '#ffffff');
    grad.addColorStop(0.55, '#f5f7fa');
    grad.addColorStop(1, '#cfd8dc');
  } else {
    grad.addColorStop(0, '#607d8b');
    grad.addColorStop(0.45, '#263238');
    grad.addColorStop(1, '#0a0e14');
  }
  ctx.beginPath(); ctx.arc(cx, py, rad * 0.94, 0, Math.PI * 2);
  ctx.fillStyle = grad; ctx.fill();
  ctx.strokeStyle = color === 'w' ? '#ffffff' : '#90a4ae';
  ctx.lineWidth = 2; ctx.stroke();

  // Inner gold ring for King & Queen
  if (type === 'k' || type === 'q') {
    ctx.beginPath(); ctx.arc(cx, py, rad * 0.76, 0, Math.PI * 2);
    ctx.strokeStyle = '#ffd54f'; ctx.lineWidth = 1.8; ctx.stroke();
  }

  const glyph = PIECE_GLYPHS[color + type.toUpperCase()] || '♟';
  ctx.font = 'bold 34px serif';
  ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  ctx.fillStyle = color === 'w' ? '#1e272e' : '#ffffff';
  ctx.fillText(glyph, cx, py + 2);
}

function drawBoard() {
  ctx.clearRect(0, 0, 640, 640);
  const board = chess.board();
  const lastMv = state && state.last_move ? state.last_move : null;

  for (let r = 0; r < 8; r++) {
    for (let c = 0; c < 8; c++) {
      const { x, y } = sqToCoords(r, c);
      const isLight = (r + c) % 2 === 0;
      const sqName = String.fromCharCode(97 + c) + (8 - r);

      ctx.fillStyle = isLight ? '#f3e5ab' : '#8d5524';
      ctx.fillRect(x, y, 80, 80);

      // Subtle wood/marble bevel
      ctx.strokeStyle = isLight ? 'rgba(255,255,255,0.4)' : 'rgba(0,0,0,0.2)';
      ctx.lineWidth = 1;
      ctx.strokeRect(x + 0.5, y + 0.5, 79, 79);

      if (lastMv && (lastMv.from === sqName || lastMv.to === sqName)) {
        ctx.fillStyle = 'rgba(255, 235, 59, 0.38)';
        ctx.fillRect(x, y, 80, 80);
      }
      if (selectedSq === sqName) {
        ctx.fillStyle = 'rgba(0, 230, 118, 0.42)';
        ctx.fillRect(x, y, 80, 80);
      }

      // Draw legal move indicator
      if (legalTargets.includes(sqName)) {
        const targetPiece = board[r][c];
        if (targetPiece) {
          ctx.beginPath(); ctx.arc(x + 40, y + 40, 34, 0, Math.PI * 2);
          ctx.strokeStyle = 'rgba(255, 82, 82, 0.85)'; ctx.lineWidth = 5; ctx.stroke();
        } else {
          ctx.beginPath(); ctx.arc(x + 40, y + 40, 13, 0, Math.PI * 2);
          ctx.fillStyle = 'rgba(0, 230, 118, 0.75)'; ctx.fill();
        }
      }

      const piece = board[r][c];
      if (piece) {
        draw3DChessPiece(x + 40, y + 40, piece.color, piece.type, selectedSq === sqName);
      }
    }
  }
}

function animLoop() {
  pulsePhase += 0.12;
  drawBoard();
  requestAnimationFrame(animLoop);
}
requestAnimationFrame(animLoop);

function myColor() {
  if (!state) return null;
  if (state.white && String(state.white.uid) === String(user.id)) return 'w';
  if (state.black && String(state.black.uid) === String(user.id)) return 'b';
  return null;
}

function computeBotMove() {
  const moves = chess.moves({ verbose: true });
  if (!moves.length) return null;
  let best = moves[0];
  let bestScore = -9999;
  for (const mv of moves) {
    let score = Math.random() * 4;
    if (mv.captured) score += (PIECE_VALS[mv.captured] || 10) * 10;
    if (mv.promotion) score += 80;
    chess.move(mv);
    if (chess.in_checkmate()) score += 9999;
    else if (chess.in_check()) score += 15;
    chess.undo();
    if (score > bestScore) { bestScore = score; best = mv; }
  }
  return best;
}

canvas.addEventListener('click', (e) => {
  if (!state || !state.started || state.over || actionBusy) return;
  const mine = myColor();
  if (!mine || chess.turn() !== mine) return;

  const rect = canvas.getBoundingClientRect();
  const px = (e.clientX - rect.left) * (640 / rect.width);
  const py = (e.clientY - rect.top) * (640 / rect.height);
  const sq = coordsToSq(px, py);
  if (!sq) return;

  if (selectedSq && legalTargets.includes(sq)) {
    const mvObj = chess.move({ from: selectedSq, to: sq, promotion: 'q' });
    if (mvObj) {
      selectedSq = null;
      legalTargets = [];
      playTone(mvObj.captured ? 320 : 540, 0.14, 'triangle');
      let statusTag = 'playing';
      let winnerColor = '';
      if (chess.in_checkmate()) {
        statusTag = 'checkmate';
        winnerColor = mine;
      } else if (chess.in_draw() || chess.in_stalemate() || chess.in_threefold_repetition()) {
        statusTag = 'draw';
      }
      sendMove(chess.fen(), { from: mvObj.from, to: mvObj.to }, statusTag, winnerColor);
      return;
    }
  }

  const piece = chess.get(sq);
  if (piece && piece.color === mine) {
    selectedSq = sq;
    legalTargets = chess.moves({ square: sq, verbose: true }).map(m => m.to);
    playTone(680, 0.06);
  } else {
    selectedSq = null;
    legalTargets = [];
  }
});

function renderUI() {
  if (!state) return;
  if (state.fen) chess.load(state.fen);

  const bar = document.getElementById('playersBar');
  bar.innerHTML = '';
  const slots = [
    { role: 'w', label: 'White ♔', p: state.white, bg: '#ffffff', fg: '#1e272e' },
    { role: 'b', label: 'Black ♚', p: state.black, bg: '#263238', fg: '#ffffff' }
  ];
  slots.forEach(s => {
    const isTurn = state.started && !state.over && chess.turn() === s.role;
    const card = document.createElement('div');
    card.className = 'p-card' + (isTurn ? ' active' : '');
    const pname = s.p ? s.p.name : 'Waiting...';
    card.innerHTML = `
      <div class="p-info">
        <div class="piece-dot" style="background:${s.bg};color:${s.fg}">${s.role === 'w' ? '♔' : '♚'}</div>
        <div>
          <div class="p-name">${pname}</div>
          <div class="p-tag">${isTurn ? '🎯 TURN' : s.label}</div>
        </div>
      </div>
    `;
    bar.appendChild(card);
  });

  const statusEl = document.getElementById('statusMsg');
  const actionRow = document.getElementById('actionRow');
  actionRow.innerHTML = '';
  const mine = myColor();

  if (!state.started) {
    statusEl.innerText = state.last_log || 'White aur Black players join karein!';
    if (!mine && (!state.white || !state.black)) {
      const btnJ = document.createElement('button');
      btnJ.className = 'btn btn-join';
      btnJ.innerText = !state.white ? '♔ Join as White' : '♚ Join as Black';
      btnJ.onclick = () => sendAction('join');
      actionRow.appendChild(btnJ);
    }
    if (state.white && !state.black) {
      const btnAI = document.createElement('button');
      btnAI.className = 'btn btn-ai';
      btnAI.innerText = '🤖 Play vs AI Bot';
      btnAI.onclick = () => sendAction('vs_ai');
      actionRow.appendChild(btnAI);
    }
  } else if (state.over) {
    statusEl.innerText = state.last_log || 'Match Over!';
    const btnR = document.createElement('button');
    btnR.className = 'btn btn-join';
    btnR.innerText = '🔄 New 3D Chess Match';
    btnR.onclick = () => sendAction('rematch');
    actionRow.appendChild(btnR);
  } else {
    const turnName = chess.turn() === 'w' ? state.white.name : state.black.name;
    const chk = chess.in_check() ? ' ⚠️ CHECK!' : '';
    if (mine && chess.turn() === mine) {
      statusEl.innerText = `👉 Aapki baari hai (${mine === 'w' ? 'White' : 'Black'})!${chk}`;
    } else {
      statusEl.innerText = `⏳ ${turnName} ki baari hai...${chk}`;
    }

    const btnFlip = document.createElement('button');
    btnFlip.className = 'btn btn-flip';
    btnFlip.innerText = '🔄 Flip Board';
    btnFlip.onclick = () => { flipped = !flipped; };
    actionRow.appendChild(btnFlip);

    // If vs AI and it's Black's turn, make AI move automatically
    if (state.vs_ai && chess.turn() === 'b' && !actionBusy) {
      actionBusy = true;
      setTimeout(() => {
        const aiMv = computeBotMove();
        if (aiMv) {
          chess.move(aiMv);
          let st = 'playing', wc = '';
          if (chess.in_checkmate()) { st = 'checkmate'; wc = 'b'; }
          else if (chess.in_draw() || chess.in_stalemate()) { st = 'draw'; }
          actionBusy = false;
          sendMove(chess.fen(), { from: aiMv.from, to: aiMv.to }, st, wc, true);
        } else {
          actionBusy = false;
        }
      }, 550);
    }
  }
}

async function fetchState() {
  if (actionBusy) return;
  try {
    const res = await fetch(`/api/chess3d?room=${encodeURIComponent(roomId)}`);
    if (res.ok) {
      state = await res.json();
      if (myColor() === 'b' && !state.vs_ai) flipped = true;
      renderUI();
    }
  } catch (e) {}
}

async function sendAction(action) {
  if (actionBusy) return;
  actionBusy = true;
  try {
    const res = await fetch('/api/chess3d', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ room: roomId, action, uid: String(user.id), name: user.name })
    });
    if (res.ok) state = await res.json();
  } catch (e) {}
  actionBusy = false;
  renderUI();
}

async function sendMove(fen, lastMove, statusTag, winnerColor, isAi = false) {
  if (actionBusy) return;
  actionBusy = true;
  try {
    const res = await fetch('/api/chess3d', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        room: roomId,
        action: 'move',
        uid: isAi ? 'AI_BOT' : String(user.id),
        fen,
        last_move: lastMove,
        status_tag: statusTag,
        winner_color: winnerColor
      })
    });
    if (res.ok) state = await res.json();
  } catch (e) {}
  actionBusy = false;
  renderUI();
}

fetchState();
setInterval(fetchState, 1400);
</script>
</body>
</html>'''

START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"

def get_or_create_chess_room(room_id):
    with CHESS3D_LOCK:
        now = time.time()
        for k in list(CHESS3D_ROOMS.keys()):
            if now - CHESS3D_ROOMS[k].get('ts', 0) > 7200:
                CHESS3D_ROOMS.pop(k, None)
        if room_id not in CHESS3D_ROOMS:
            CHESS3D_ROOMS[room_id] = {
                'room': room_id,
                'white': None,
                'black': None,
                'vs_ai': False,
                'started': False,
                'over': False,
                'fen': START_FEN,
                'last_move': None,
                'last_log': 'Table ready! White aur Black join karein.',
                'ts': now
            }
        return CHESS3D_ROOMS[room_id]

def handle_3d_chess_post(data):
    room_id = str(data.get('room', 'default'))[:24]
    action = str(data.get('action', ''))
    uid = str(data.get('uid', '')).strip()
    uname = str(data.get('name', 'Player'))[:24]

    room = get_or_create_chess_room(room_id)
    if not uid:
        return room

    with CHESS3D_LOCK:
        room['ts'] = time.time()
        if action == 'rematch':
            room['white'] = {'uid': uid, 'name': uname}
            room['black'] = None
            room['vs_ai'] = False
            room['started'] = False
            room['over'] = False
            room['fen'] = START_FEN
            room['last_move'] = None
            room['last_log'] = f"{uname} (White) ne naya Chess table banaya!"
            return room

        if action == 'join':
            if room['started']:
                return room
            if room['white'] is None:
                room['white'] = {'uid': uid, 'name': uname}
                room['last_log'] = f"{uname} White ♔ bane! Black ka intezar..."
            elif str(room['white']['uid']) != uid and room['black'] is None:
                room['black'] = {'uid': uid, 'name': uname}
                room['started'] = True
                room['last_log'] = f"Match shuru! {room['white']['name']} (White) ki pehli chaal."
            return room

        if action == 'vs_ai':
            if not room['started'] and room['white'] and str(room['white']['uid']) == uid:
                room['black'] = {'uid': 'AI_BOT', 'name': 'Mishra AI ♚'}
                room['vs_ai'] = True
                room['started'] = True
                room['last_log'] = f"AI Match shuru! {room['white']['name']} ki chaal."
            return room

        if action == 'move' and room['started'] and not room['over']:
            fen = str(data.get('fen', '')).strip()
            if not fen:
                return room
            room['fen'] = fen
            room['last_move'] = data.get('last_move')
            st_tag = str(data.get('status_tag', 'playing'))
            w_col = str(data.get('winner_color', ''))

            if st_tag == 'checkmate':
                room['over'] = True
                win_p = room['white'] if w_col == 'w' else room['black']
                w_name = win_p['name'] if win_p else 'Winner'
                w_uid = str(win_p['uid']) if win_p else ''
                room['last_log'] = f"🏆 CHECKMATE! {w_name} jeet gaye (+100 Coins)!"
                if w_uid.isdigit():
                    with DB_LOCK, connect() as c:
                        c.execute('INSERT OR IGNORE INTO wallet(user_id,coins,last_daily) VALUES(?,0,?)', (int(w_uid), ''))
                        c.execute('UPDATE wallet SET coins=coins+100 WHERE user_id=?', (int(w_uid),))
            elif st_tag == 'draw':
                room['over'] = True
                room['last_log'] = "🤝 Match Draw! Koi nahi jeeta."
            else:
                turn_char = fen.split()[1] if len(fen.split()) > 1 else 'w'
                nxt = room['white']['name'] if turn_char == 'w' else room['black']['name']
                room['last_log'] = f"Ab {nxt} ki baari hai."
            return room

    return room

class Health(BaseHTTPRequestHandler):
    def _send_bytes(self, status, body, ctype='text/plain; charset=utf-8'):
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def do_GET(self):
        from urllib.parse import parse_qs
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ('/', '/health', '/healthz'):
            self._send_bytes(200, b'Mishra Guard Robot V4 is running')
        elif path == '/ludo3d':
            self._send_bytes(200, LUDO3D_HTML.encode('utf-8'), 'text/html; charset=utf-8')
        elif path == '/api/ludo3d':
            qs = parse_qs(parsed.query)
            room_id = (qs.get('room') or ['default'])[0][:24]
            room = get_or_create_3d_room(room_id)
            self._send_bytes(200, json.dumps(room).encode('utf-8'), 'application/json')
        elif path == '/chess3d':
            self._send_bytes(200, CHESS3D_HTML.encode('utf-8'), 'text/html; charset=utf-8')
        elif path == '/api/chess3d':
            qs = parse_qs(parsed.query)
            room_id = (qs.get('room') or ['default'])[0][:24]
            room = get_or_create_chess_room(room_id)
            self._send_bytes(200, json.dumps(room).encode('utf-8'), 'application/json')
        else:
            self._send_bytes(404, b'Not found')

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == '/api/ludo3d':
            try:
                length = int(self.headers.get('Content-Length', '0'))
                raw = self.rfile.read(min(length, 8192)).decode('utf-8')
                data = json.loads(raw) if raw else {}
                room = handle_3d_ludo_post(data)
                self._send_bytes(200, json.dumps(room).encode('utf-8'), 'application/json')
            except Exception as e:
                self._send_bytes(400, json.dumps({'error': str(e)}).encode('utf-8'), 'application/json')
        elif parsed.path == '/api/chess3d':
            try:
                length = int(self.headers.get('Content-Length', '0'))
                raw = self.rfile.read(min(length, 8192)).decode('utf-8')
                data = json.loads(raw) if raw else {}
                room = handle_3d_chess_post(data)
                self._send_bytes(200, json.dumps(room).encode('utf-8'), 'application/json')
            except Exception as e:
                self._send_bytes(400, json.dumps({'error': str(e)}).encode('utf-8'), 'application/json')
        else:
            self._send_bytes(404, b'Not found')

    def log_message(self, *args): pass

def health_server():
    server = ThreadingHTTPServer(('0.0.0.0', PORT), Health)
    server.daemon_threads = True
    log.info('Health & 3D Ludo server listening on 0.0.0.0:%s', PORT)
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
/broadcast TEXT — 📢 Broadcast text, photo, video, sticker or GIF with buttons to all chats
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
/protect — 🛡️ interactive Protection Panel (Night Mode, Anti-NSFW, Anti-Abuse, Anti-Forward, Anti-Channel)
/nightmode on|off [start_hr end_hr] — 🌙 auto-close chat at night (default 23 to 6 IST)
/antinsfw on|off /antiabuse on|off /antiforward on|off /antichannel on|off

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
/ludo — 🎲 Royal 3D Ludo King Mini App (+100 coins)
/chess — ♟️ Royal 3D Chess Mini App 1v1 / vs AI (+100 coins)
/spy — 🕵️ Undercover Spy Game (Host-start + Turn-by-Turn Chat Clues + Voting)
/wordchain /wc — 🔤 Live Word Chain Elimination Game (+60 coins)
/handcricket /hc — 🏏 2-Player Hand Cricket group game (+50 coins)
/rps — 🪨📄✂️ Rock-Paper-Scissors 1v1 Duel (+30 coins)
/fasttype /race — ⚡ Anti-Copy Fast Typing Race (+40 coins)
/tod — 🍾 Spin-the-Bottle Truth & Dare Wheel
/ttt /tictactoe — ❌⭕ multiplayer Tic-Tac-Toe game (+30 coins)
/style TEXT — convert text into 10+ stylish fancy fonts
/cricket [on|off] — unlimited AI hard cricket quiz & auto toggle
/spiritual [on|off] — unlimited AI spiritual quiz & auto toggle

<b>Fun GIF Roleplay Actions</b>
/hug /kiss /love /bite /kill /slap /pat /cuddle /punch /tickle — reply, @username, mention or ID

<b>AI Chat & Multi-Engine HD Image Generation</b>
/ai question — smart AI answers (needs GROQ_API_KEY)
/img prompt — HD AI image (HF FLUX + AI Horde Juggernaut XL + Pollinations)

━━━━━━━━━━━━━━━━━━━━
👑 <b>Bot Owner:</b> Shivam Mishra
📢 <b>Join our channel:</b> @dreamers_adda'''

@bot.message_handler(commands=['start', 'help', 'panel'])
def help_cmd(m):
    parts = (m.text or '').split(maxsplit=1)
    if len(parts) > 1 and parts[1].strip().startswith(('gc_', 'dm_', 'ch_')):
        room_id = parts[1].strip()[:24]
        base = get_public_webapp_base()
        if base:
            if room_id.startswith('ch_'):
                kb = types.InlineKeyboardMarkup()
                kb.add(types.InlineKeyboardButton('♟️ Launch Royal 3D Chess Now', web_app=types.WebAppInfo(url=f"{base}/chess3d?room={room_id}")))
                return bot.send_message(
                    m.chat.id,
                    f"♟️ <b>ROYAL 3D CHESS ROOM READY!</b>\nRoom ID: <code>{esc(room_id)}</code>\n\nNiche button dabakar 3D Chess Board open karein:",
                    reply_markup=kb
                )
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton('🎮 Launch 3D Ludo King Now', web_app=types.WebAppInfo(url=f"{base}/ludo3d?room={room_id}")))
            return bot.send_message(
                m.chat.id,
                f"👑 <b>3D LUDO KING ROOM READY!</b>\nRoom ID: <code>{esc(room_id)}</code>\n\nNiche button dabakar 3D Board open karein:",
                reply_markup=kb
            )
    bot.send_message(m.chat.id, HELP)

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

# ---------------- ADVANCED GROUP PROTECTION (NIGHT MODE, ANTI-NSFW, ANTI-ABUSE, ANTI-FORWARD, ANTI-CHANNEL) ----------------
from datetime import datetime, timezone, timedelta

IST_TZ = timezone(timedelta(hours=5, minutes=30))

NSFW_PATTERNS = re.compile(
    r'\b(porn|xxx|xvideos|xnxx|pornhub|sex\s*video|nude|nudes|naked|boobs|pussy|dick|cock|hentai|brazzers|onlyfans|chut|lund|randi|chod|chudai|sexchat|call\s*girl)\b',
    re.I
)
ABUSE_PATTERNS = re.compile(
    r'\b(madarchod|bhenchod|behenchod|bhosdike|bsdk|chutiya|gandu|harami|kamina|kamine|saala|mc|bc|bkl|lodu|laude|gaand)\b',
    re.I
)

def is_night_time_now(chat):
    if setting(chat, 'nightmode', 'off') != 'on':
        return False
    # Default Night Mode window: 11:00 PM (23:00) to 6:00 AM (06:00) IST
    start_h = int(setting(chat, 'night_start', '23') or '23')
    end_h = int(setting(chat, 'night_end', '6') or '6')
    now_h = datetime.now(IST_TZ).hour
    if start_h > end_h:
        return now_h >= start_h or now_h < end_h
    return start_h <= now_h < end_h

def check_group_protections(m):
    chat = m.chat.id
    uid = m.from_user.id
    is_adm = admin(chat, uid)
    is_app = is_lock_approved(chat, uid)

    # 1. Night Mode Check (Admins & /approve users bypass)
    if not (is_adm or is_app) and is_night_time_now(chat):
        try:
            bot.delete_message(chat, m.message_id)
        except Exception:
            pass
        return True

    # 2. Anti-Channel / Sender Chat Check (Admins & /approve users bypass)
    if not (is_adm or is_app) and setting(chat, 'antichannel', 'off') == 'on' and getattr(m, 'sender_chat', None):
        if m.sender_chat.id != chat:
            try:
                bot.delete_message(chat, m.message_id)
            except Exception:
                pass
            return True

    # 3. Anti-Forward Check (Admins & /approve users bypass)
    if not (is_adm or is_app) and setting(chat, 'antiforward', 'off') == 'on' and (getattr(m, 'forward_date', None) or getattr(m, 'forward_origin', None)):
        try:
            bot.delete_message(chat, m.message_id)
        except Exception:
            pass
        return True

    body = (m.text or m.caption or '').strip()
    sticker_emoji = (m.sticker.emoji if (m.sticker and getattr(m.sticker, 'emoji', None)) else '')
    sticker_set = (m.sticker.set_name if (m.sticker and getattr(m.sticker, 'set_name', None)) else '')

    # 4. Anti-NSFW Check (Deletes even for Admins unless explicitly /approve'd!)
    if not is_app and setting(chat, 'antinsfw', 'off') == 'on':
        nsfw_hit = False
        if body and NSFW_PATTERNS.search(body):
            nsfw_hit = True
        elif sticker_set and NSFW_PATTERNS.search(sticker_set):
            nsfw_hit = True
        elif sticker_emoji in ('🍆', '🍑', '💦', '🔞'):
            nsfw_hit = True
        if nsfw_hit:
            try:
                bot.delete_message(chat, m.message_id)
                warn_msg = bot.send_message(
                    chat,
                    f"🔞 <a href=\"tg://user?id={uid}\">{esc(user_name(m.from_user))}</a> NSFW content is strictly prohibited in this group!"
                )
                threading.Timer(6, lambda: safe_delete(chat, warn_msg.message_id)).start()
            except Exception:
                pass
            return True

    # 5. Anti-Abuse / Bad Words Filter (Deletes even for Admins unless explicitly /approve'd!)
    if not is_app and setting(chat, 'antiabuse', 'off') == 'on' and body:
        if ABUSE_PATTERNS.search(body):
            try:
                bot.delete_message(chat, m.message_id)
                warn_msg = bot.send_message(
                    chat,
                    f"🚫 <a href=\"tg://user?id={uid}\">{esc(user_name(m.from_user))}</a> kripya group mein gaali / abusive language ka prayog na karein!"
                )
                threading.Timer(6, lambda: safe_delete(chat, warn_msg.message_id)).start()
            except Exception:
                pass
            return True

    return False

@bot.message_handler(commands=['nightmode'])
def nightmode_cmd(m):
    if not need_admin(m): return
    parts = (m.text or '').split()
    arg = parts[1].lower() if len(parts) > 1 else ''
    if arg not in ('on', 'off'):
        cur = setting(m.chat.id, 'nightmode', 'off')
        sh = setting(m.chat.id, 'night_start', '23')
        eh = setting(m.chat.id, 'night_end', '6')
        return bot.reply_to(
            m,
            f"🌙 <b>Night Mode Status:</b> <b>{cur.upper()}</b>\n"
            f"⏰ Timing: <b>{sh}:00 se {eh}:00 IST</b> (is dauran sirf admins message kar sakte hain)\n\n"
            f"Usage:\n• <code>/nightmode on</code> ya <code>/nightmode off</code>\n"
            f"• Custom time: <code>/nightmode on 23 6</code> (11 PM se 6 AM)"
        )
    set_setting(m.chat.id, 'nightmode', arg)
    if arg == 'on':
        if len(parts) >= 4 and parts[2].isdigit() and parts[3].isdigit():
            sh = max(0, min(23, int(parts[2])))
            eh = max(0, min(23, int(parts[3])))
            set_setting(m.chat.id, 'night_start', str(sh))
            set_setting(m.chat.id, 'night_end', str(eh))
        sh = setting(m.chat.id, 'night_start', '23')
        eh = setting(m.chat.id, 'night_end', '6')
        done(m, f"🌙 <b>Night Mode ENABLED!</b>\nRoz raat <b>{sh}:00 se subah {eh}:00 IST</b> tak non-admin messages automatically delete honge.")
    else:
        done(m, "☀️ <b>Night Mode DISABLED!</b> Ab sabhi members 24x7 chat kar sakte hain.")

@bot.message_handler(commands=['antinsfw', 'antiabuse', 'antiforward', 'antichannel'])
def protection_toggles_cmd(m):
    if not need_admin(m): return
    cmd = (m.text or '').split()[0].split('@')[0][1:].lower()
    parts = (m.text or '').split()
    labels = {
        'antinsfw': '🔞 Anti-NSFW Protection',
        'antiabuse': '🚫 Anti-Abuse / Bad-Word Filter',
        'antiforward': '↪️ Anti-Forward Protection',
        'antichannel': '📢 Anti-Channel (Anonymous Channel Post) Protection',
    }
    if len(parts) < 2 or parts[1].lower() not in ('on', 'off'):
        cur = setting(m.chat.id, cmd, 'off')
        return bot.reply_to(m, f"{labels[cmd]} is currently <b>{cur.upper()}</b>.\nUsage: <code>/{cmd} on</code> ya <code>/{cmd} off</code>")
    val = parts[1].lower()
    set_setting(m.chat.id, cmd, val)
    done(m, f"✅ {labels[cmd]} <b>{'enabled' if val=='on' else 'disabled'}</b>.")

@bot.message_handler(commands=['protect', 'protection', 'security'])
def protection_panel_cmd(m):
    if not need_admin(m): return
    cid = m.chat.id
    kb = types.InlineKeyboardMarkup(row_width=2)
    items = [
        ('nightmode', '🌙 Night Mode'),
        ('antinsfw', '🔞 Anti-NSFW'),
        ('antiabuse', '🚫 Anti-Abuse'),
        ('antiforward', '↪️ Anti-Forward'),
        ('antichannel', '📢 Anti-Channel'),
        ('antilink', '🔗 Anti-Link'),
        ('antispam', '🛡️ Anti-Spam'),
    ]
    lines = ["🛡️ <b>MISHRA GUARD — GROUP PROTECTION PANEL</b>\n"]
    btns = []
    for key, label in items:
        st = setting(cid, key, 'off').upper()
        icon = "🟢 ON" if st == 'ON' else "🔴 OFF"
        lines.append(f"• {label}: <b>{icon}</b>")
        btns.append(types.InlineKeyboardButton(f"{label}: {st}", callback_data=f"prot:{key}"))
    for i in range(0, len(btns), 2):
        kb.row(*btns[i:i+2])
    bot.send_message(cid, "\n".join(lines), reply_markup=kb)

@bot.callback_query_handler(func=lambda call: bool(call.data) and call.data.startswith('prot:'))
def protection_panel_callback(call):
    chat = call.message.chat.id
    if not call.from_user or not admin(chat, call.from_user.id):
        return bot.answer_callback_query(call.id, '⛔ Sirf group admins hi protection settings badal sakte hain!', show_alert=True)
    key = call.data.split(':', 1)[1]
    cur = setting(chat, key, 'off')
    nxt = 'off' if cur == 'on' else 'on'
    set_setting(chat, key, nxt)
    bot.answer_callback_query(call.id, f"✅ {key.upper()} set to {nxt.upper()}")

    items = [
        ('nightmode', '🌙 Night Mode'),
        ('antinsfw', '🔞 Anti-NSFW'),
        ('antiabuse', '🚫 Anti-Abuse'),
        ('antiforward', '↪️ Anti-Forward'),
        ('antichannel', '📢 Anti-Channel'),
        ('antilink', '🔗 Anti-Link'),
        ('antispam', '🛡️ Anti-Spam'),
    ]
    kb = types.InlineKeyboardMarkup(row_width=2)
    lines = ["🛡️ <b>MISHRA GUARD — GROUP PROTECTION PANEL</b>\n"]
    btns = []
    for k, label in items:
        st = setting(chat, k, 'off').upper()
        icon = "🟢 ON" if st == 'ON' else "🔴 OFF"
        lines.append(f"• {label}: <b>{icon}</b>")
        btns.append(types.InlineKeyboardButton(f"{label}: {st}", callback_data=f"prot:{k}"))
    for i in range(0, len(btns), 2):
        kb.row(*btns[i:i+2])
    try:
        bot.edit_message_text("\n".join(lines), chat, call.message.message_id, reply_markup=kb)
    except Exception:
        pass

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
        if cmd in ('ban', 'kick', 'mute', 'warn'):
            if admin(chat, uid):
                return bot.reply_to(m, f'⛔ User <code>{uid}</code> ek group admin hain! Admin ko {cmd} nahi kiya ja sakta (pehle /demote karein).')
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

# ---------------- MULTI-ENGINE HD AI PHOTO GENERATION (HF FLUX + AI HORDE + POLLINATIONS) ----------------
def enhance_image_prompt_with_groq(user_prompt):
    raw_key = GROQ_API_KEY or os.getenv('GROQ_API_KEY', '')
    api_key = str(raw_key).strip().strip("'").strip('"')
    if not api_key:
        return f"{user_prompt}, ultra detailed, photorealistic, 8k resolution, cinematic lighting, masterpiece, sharp focus"
    try:
        import urllib.request
        payload = json.dumps({
            'model': 'openai/gpt-oss-20b',
            'messages': [
                {
                    'role': 'system',
                    'content': (
                        "You are an expert AI image prompt engineer. Convert the user's request (even if in Hindi/Hinglish) "
                        "into a rich, vivid, photorealistic English image generation prompt (under 45 words). "
                        "Include lighting, artistic style, composition, and quality tags (8k, ultra-detailed, cinematic lighting, sharp focus, masterpiece). "
                        "Output ONLY the final English prompt, nothing else."
                    )
                },
                {'role': 'user', 'content': user_prompt}
            ],
            'max_tokens': 110,
            'temperature': 0.7
        }).encode('utf-8')
        req = urllib.request.Request(
            'https://api.groq.com/openai/v1/chat/completions',
            data=payload,
            headers={
                'Authorization': f'Bearer {api_key}',
                'Content-Type': 'application/json',
                'User-Agent': 'Mozilla/5.0'
            },
            method='POST'
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            res = json.loads(resp.read().decode('utf-8'))
        enhanced = res['choices'][0]['message']['content'].strip().strip('"').strip("'")
        if enhanced and len(enhanced) > 10:
            return enhanced
    except Exception:
        pass
    return f"{user_prompt}, ultra detailed, photorealistic, 8k resolution, cinematic lighting, masterpiece, sharp focus"

def generate_via_huggingface(enhanced_prompt):
    hf_token = (os.getenv('HF_TOKEN', '') or os.getenv('HUGGINGFACE_TOKEN', '')).strip().strip("'").strip('"')
    if not hf_token:
        return None
    import urllib.request
    hf_models = [
        "https://router.huggingface.co/hf-inference/models/black-forest-labs/FLUX.1-schnell",
        "https://router.huggingface.co/hf-inference/models/stabilityai/stable-diffusion-xl-base-1.0",
    ]
    payload = json.dumps({"inputs": enhanced_prompt}).encode('utf-8')
    for url in hf_models:
        try:
            req = urllib.request.Request(
                url,
                data=payload,
                headers={
                    'Authorization': f'Bearer {hf_token}',
                    'Content-Type': 'application/json',
                    'Accept': 'image/jpeg,image/png,*/*'
                },
                method='POST'
            )
            with urllib.request.urlopen(req, timeout=35) as resp:
                data = resp.read(15 * 1024 * 1024)
                if data and len(data) > 12000 and (data[:3] == b'\xff\xd8\xff' or data[:4] == b'\x89PNG' or data[:4] == b'RIFF'):
                    return data
        except Exception as e:
            log.warning("HF inference failed (%s): %s", url, e)
    return None

def generate_via_aihorde(enhanced_prompt):
    # 100% Free Distributed GPU Network (Juggernaut XL / AlbedoBase XL) - works with anonymous key '0000000000'
    import urllib.request
    horde_key = os.getenv('AI_HORDE_KEY', '0000000000').strip() or '0000000000'
    headers = {
        'apikey': horde_key,
        'Content-Type': 'application/json',
        'Client-Agent': 'MishraGuardRobot:4.0:telegram',
        'User-Agent': 'Mozilla/5.0'
    }
    body = json.dumps({
        "prompt": f"{enhanced_prompt} ### ugly, blurry, deformed, distorted face, extra fingers, low quality, watermark",
        "params": {
            "sampler_name": "k_euler_a",
            "cfg_scale": 7.0,
            "width": 768,
            "height": 768,
            "steps": 22,
            "n": 1
        },
        "nsfw": False,
        "censor_nsfw": True,
        "models": ["Juggernaut XL", "AlbedoBase XL (SDXL)", "Deliberate", "stable_diffusion"],
        "r2": True
    }).encode('utf-8')
    try:
        req = urllib.request.Request("https://aihorde.net/api/v2/generate/async", data=body, headers=headers, method='POST')
        with urllib.request.urlopen(req, timeout=15) as resp:
            job = json.loads(resp.read().decode('utf-8'))
        job_id = job.get('id')
        if not job_id:
            return None

        # Poll up to 28 seconds for GPU worker completion
        for _ in range(14):
            time.sleep(2)
            chk_req = urllib.request.Request(f"https://aihorde.net/api/v2/generate/check/{job_id}", headers=headers)
            with urllib.request.urlopen(chk_req, timeout=10) as chk_resp:
                chk = json.loads(chk_resp.read().decode('utf-8'))
            if chk.get('done'):
                st_req = urllib.request.Request(f"https://aihorde.net/api/v2/generate/status/{job_id}", headers=headers)
                with urllib.request.urlopen(st_req, timeout=12) as st_resp:
                    st = json.loads(st_resp.read().decode('utf-8'))
                gens = st.get('generations') or []
                if gens and gens[0].get('img'):
                    img_ref = gens[0]['img']
                    if img_ref.startswith('http'):
                        with urllib.request.urlopen(img_ref, timeout=20) as iresp:
                            return iresp.read(15 * 1024 * 1024)
                    else:
                        import base64
                        return base64.b64decode(img_ref)
                break
            if not chk.get('is_possible', True):
                break
    except Exception as e:
        log.warning("AI Horde generation skipped/failed: %s", e)
    return None

def fetch_ai_image_bytes(enhanced_prompt):
    # Engine 1: Hugging Face FLUX.1-schnell (if HF_TOKEN set)
    img_data = generate_via_huggingface(enhanced_prompt)
    if img_data:
        return img_data, "FLUX.1 HD"

    # Engine 2: AI Horde Free GPU Network (Juggernaut XL / AlbedoBase XL - no key required)
    img_data = generate_via_aihorde(enhanced_prompt)
    if img_data:
        return img_data, "Juggernaut XL"

    # Engine 3: Fast Pollinations FLUX / Turbo fallback (with safe fallback URL parameters)
    import urllib.request, urllib.parse
    clean_prompt = re.sub(r'(nsfw|nude|naked|sex|porn|xxx)', 'beautiful art', enhanced_prompt, flags=I)
    encoded = urllib.parse.quote(clean_prompt)
    seed = random.randint(1000, 99999999)
    ua_list = [
        f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/{random.randint(120,134)}.0.0.0 Safari/537.36",
        f"Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Chrome/{random.randint(120,134)}.0.0.0 Mobile Safari/537.36",
    ]
    endpoints = [
        f"https://image.pollinations.ai/prompt/{encoded}?model=flux&width=1024&height=1024&seed={seed}&nologo=true",
        f"https://image.pollinations.ai/prompt/{encoded}?model=turbo&width=1024&height=1024&seed={seed+1}&nologo=true",
        f"https://image.pollinations.ai/prompt/{encoded}?width=1024&height=1024&seed={seed+2}&nologo=true",
    ]
    for url in endpoints:
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': random.choice(ua_list),
                'Accept': 'image/avif,image/webp,image/apng,image/*,*/*;q=0.8',
            })
            with urllib.request.urlopen(req, timeout=25) as resp:
                ctype = (resp.headers.get('Content-Type') or '').lower()
                data = resp.read(15 * 1024 * 1024)
                if data and len(data) > 15000 and ('image' in ctype or data[:3] == b'\xff\xd8\xff' or data[:4] == b'\x89PNG'):
                    return data, "Flux Studio"
        except Exception:
            continue
    return None, None
def ai_generate_image(m):
    p = (m.text or '').split(maxsplit=1)
    if len(p) < 2 or not p[1].strip():
        return bot.reply_to(m, 'Usage: <code>/img Radha Krishna in Vrindavan glowing moonlight</code>')
    raw_prompt = p[1].strip()[:400]
    status = bot.reply_to(m, '🎨 <b>HD AI Photo generate ho rahi hai...</b> thoda intezar karein.')
    try:
        enhanced = enhance_image_prompt_with_groq(raw_prompt)
        img_bytes, engine_name = fetch_ai_image_bytes(enhanced)
        if not img_bytes:
            raise RuntimeError('All image engines busy')
        bio = io.BytesIO(img_bytes)
        bio.name = 'mishra_hd_art.jpg'
        bot.send_photo(
            m.chat.id,
            bio,
            caption=f"🎨 <b>HD AI Art ({engine_name})</b>\n<i>Prompt:</i> {esc(raw_prompt[:250])}",
            reply_to_message_id=m.message_id
        )
        safe_delete(m.chat.id, status.message_id)
    except Exception:
        log.exception('AI image generation failed')
        try:
            bot.edit_message_text('❌ Sabhi AI image servers abhi busy hain, 10 second baad dobara <code>/img</code> try karein.', m.chat.id, status.message_id)
        except Exception:
            pass

# ---------------- INTERACTIVE GIF ROLEPLAY ACTIONS (/hug, /kiss, /love, /bite, /kill, etc.) ----------------
ACTION_SPECS = {
    'hug': {
        'waifu': 'hug',
        'nekos': 'hug',
        'otaku': 'hug',
        'actress_queries': [
            'bollywood actress warm hug gif',
            'shraddha kapoor alia bhatt hug gif',
            'korean actress drama hug cute gif',
            'kdrama romantic warm hug gif'
        ],
        'templates': [
            "🤗 {sender} ne {target} ko ek pyara sa warm hug diya! ❤️",
            "🫂 {sender} ne {target} ko gale laga liya! ✨"
        ]
    },
    'kiss': {
        'waifu': 'kiss',
        'nekos': 'kiss',
        'otaku': 'kiss',
        'actress_queries': [
            'bollywood actress flying kiss cute gif',
            'deepika padukone alia bhatt flying kiss gif',
            'korean actress cute kiss heart gif',
            'iu bae suzy flying kiss gif'
        ],
        'templates': [
            "💋 {sender} ne {target} ko pyar se kiss kiya! 💕",
            "😘 {sender} sent a sweet flying kiss to {target}! 💖"
        ]
    },
    'love': {
        'waifu': 'blush',
        'nekos': 'blush',
        'otaku': 'love',
        'actress_queries': [
            'bollywood actress heart love blush gif',
            'rashmika mandanna shraddha kapoor heart gif',
            'korean actress finger heart love gif',
            'kdrama actress cute love blush gif'
        ],
        'templates': [
            "❤️ {sender} ko {target} se beinteha pyar ho gaya hai! 💘",
            "💞 {sender} is sending pure love & hearts to {target}! 🥰"
        ]
    },
    'romance': {
        'waifu': 'blush',
        'nekos': 'hug',
        'otaku': 'love',
        'actress_queries': [
            'bollywood romantic couple rain dance gif',
            'korean drama romantic couple cute gif',
            'indian actress romantic eye contact gif'
        ],
        'templates': [
            "🌹 {sender} aur {target} ka romantic moment chal raha hai! ✨❤️"
        ]
    },
    'bite': {
        'waifu': 'bite',
        'nekos': 'bite',
        'otaku': 'bite',
        'actress_queries': [
            'korean actress cute playful bite lip gif',
            'bollywood actress cute naughty expression gif',
            'kdrama girl cute playful bite gif'
        ],
        'templates': [
            "🧛 {sender} ne {target} ko pyar se kaat (bite) liya! 😜",
            "🦷 Ouch! {sender} bit {target}! 😂"
        ]
    },
    'kill': {
        'waifu': 'kill',
        'nekos': 'shoot',
        'otaku': 'kill',
        'actress_queries': [
            'bollywood actress angry glare gun action gif',
            'korean actress badass action fight scene gif',
            'indian actress dramatic killer look gif'
        ],
        'templates': [
            "⚔️ {sender} ne {target} ka kaam tamam kar diya! 💀😂",
            "💥 {sender} defeated {target} in an epic battle! 🏆"
        ]
    },
    'slap': {
        'waifu': 'slap',
        'nekos': 'slap',
        'otaku': 'slap',
        'actress_queries': [
            'bollywood actress funny slap drama gif',
            'korean drama funny slap reaction gif',
            'indian serial funny slap gif'
        ],
        'templates': [
            "👋 {sender} ne {target} ko zor ka thappad lagaya! 😱😂"
        ]
    },
    'pat': {
        'waifu': 'pat',
        'nekos': 'pat',
        'otaku': 'pat',
        'actress_queries': [
            'korean actress cute head pat smile gif',
            'bollywood actress cute smile blessing gif'
        ],
        'templates': [
            "🥰 {sender} ne {target} ke sar par pyar se haath fera! ✨"
        ]
    },
    'cuddle': {
        'waifu': 'cuddle',
        'nekos': 'cuddle',
        'otaku': 'cuddle',
        'actress_queries': [
            'korean drama cute cuddle cozy gif',
            'bollywood romantic hug cuddle gif'
        ],
        'templates': [
            "🧸 {sender} is cuddling tightly with {target}! 💕"
        ]
    },
    'punch': {
        'waifu': 'bonk',
        'nekos': 'punch',
        'otaku': 'punch',
        'actress_queries': [
            'korean actress funny punch fight gif',
            'bollywood action punch funny gif'
        ],
        'templates': [
            "🥊 {sender} ne {target} ko ek solid punch mara! 💫"
        ]
    },
    'tickle': {
        'waifu': 'tickle',
        'nekos': 'tickle',
        'otaku': 'tickle',
        'actress_queries': [
            'bollywood actress laughing cute gif',
            'korean actress cute laugh giggle gif'
        ],
        'templates': [
            "🤣 {sender} ne {target} ko gudgudi (tickle) karke hasa-hasa kar lotpot kar diya! 😂"
        ]
    }
}

def resolve_action_target(m):
    if m.reply_to_message and m.reply_to_message.from_user:
        tu = m.reply_to_message.from_user
        remember_user(m.chat.id, tu)
        return tu.id, user_name(tu)

    for ent in (m.entities or []):
        if ent.type == 'text_mention' and ent.user:
            remember_user(m.chat.id, ent.user)
            return ent.user.id, user_name(ent.user)

    parts = (m.text or '').split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        return None, None

    token = parts[1].strip().split()[0].rstrip(',;!')
    if token.isdigit():
        uid = int(token)
        with DB_LOCK, connect() as c:
            r = c.execute('SELECT name FROM global_users WHERE user_id=?', (uid,)).fetchone()
        return uid, (r['name'] if r and r['name'] else f"User {uid}")

    if token.startswith('@'):
        uname = token.lstrip('@').lower()
        with DB_LOCK, connect() as c:
            r = c.execute('SELECT user_id, name FROM global_users WHERE lower(replace(username, "@", ""))=? LIMIT 1', (uname,)).fetchone()
            if not r:
                r = c.execute('SELECT user_id, name FROM stats WHERE lower(replace(username, "@", ""))=? ORDER BY last_active DESC LIMIT 1', (uname,)).fetchone()
        if r:
            return r['user_id'], (r['name'] or token)
        return None, token

    return None, parts[1].strip()[:40]

def download_gif_to_buffer(url):
    import urllib.request
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36',
        'Accept': 'video/mp4,image/gif,image/webp,*/*'
    })
    with urllib.request.urlopen(req, timeout=12) as resp:
        data = resp.read(12 * 1024 * 1024)
    if not data or len(data) < 2500:
        return None
    buf = io.BytesIO(data)
    low = url.lower()
    if '.mp4' in low or data[4:8] == b'ftyp':
        buf.name = 'action.mp4'
    else:
        buf.name = 'action.gif'
    return buf

def fetch_actress_gif_candidates(query):
    import urllib.request, urllib.parse
    found = []
    q_enc = urllib.parse.quote(query)

    # Source A: Tenor v2 Search API (public client key) - rich Indian & Korean actress GIFs
    try:
        t2_url = f"https://tenor.googleapis.com/v2/search?q={q_enc}&key=AIzaSyD-public-demo-key&client_key=mishra_bot&limit=12&media_filter=mp4,tinygif"
        req = urllib.request.Request(t2_url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=7) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        for res in (data.get('results') or []):
            mf = res.get('media_formats') or {}
            u = (mf.get('mp4') or {}).get('url') or (mf.get('tinygif') or {}).get('url')
            if u:
                found.append(u)
    except Exception:
        pass

    # Source B: Giphy Public Search Endpoint
    if not found:
        try:
            g_url = f"https://api.giphy.com/v1/gifs/search?api_key=dc6zaTOxFJmzC&q={q_enc}&limit=15&rating=pg-13"
            req = urllib.request.Request(g_url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=7) as resp:
                data = json.loads(resp.read().decode('utf-8'))
            for it in (data.get('data') or []):
                imgs = it.get('images') or {}
                u = (imgs.get('downsized_small') or {}).get('mp4') or (imgs.get('original') or {}).get('mp4') or (imgs.get('downsized') or {}).get('url')
                if u:
                    found.append(u)
        except Exception:
            pass

    # Source C: Direct Tenor Web Page Scraper (zero API key required, always works for Bollywood/Korean actress GIFs!)
    if not found:
        try:
            slug = urllib.parse.quote(query.replace(' ', '-'))
            web_url = f"https://tenor.com/search/{slug}-gifs"
            req = urllib.request.Request(web_url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36',
                'Accept-Language': 'en-US,en;q=0.9'
            })
            with urllib.request.urlopen(req, timeout=8) as resp:
                html = resp.read().decode('utf-8', errors='ignore')
            # Extract media.tenor.com .mp4 or .gif URLs directly from page JSON/HTML
            matches = re.findall(r'https://media\.tenor\.com/[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+\.(?:mp4|gif)', html)
            if matches:
                unique_m = list(dict.fromkeys(matches))
                found.extend(unique_m[:12])
        except Exception:
            pass

    random.shuffle(found)
    return found

def fetch_anime_gif_candidates(spec):
    import urllib.request
    anime_urls = []
    try:
        req = urllib.request.Request(f"https://nekos.best/api/v2/{spec['nekos']}?amount=3", headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=7) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        for item in (data.get('results') or []):
            if item.get('url'):
                anime_urls.append(item['url'])
    except Exception:
        pass

    try:
        req = urllib.request.Request(f"https://api.waifu.pics/sfw/{spec['waifu']}", headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=7) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        if data.get('url'):
            anime_urls.append(data['url'])
    except Exception:
        pass

    try:
        req = urllib.request.Request(f"https://api.otakugifs.xyz/gif?reaction={spec['otaku']}", headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=7) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        if data.get('url'):
            anime_urls.append(data['url'])
    except Exception:
        pass

    random.shuffle(anime_urls)
    return anime_urls

def fetch_action_gif_urls(action_key):
    spec = ACTION_SPECS[action_key]
    query = random.choice(spec['actress_queries'])
    actress_urls = fetch_actress_gif_candidates(query)
    anime_urls = fetch_anime_gif_candidates(spec)

    # 60% priority for Indian/Korean actress GIFs + 40% Anime GIFs across ALL action commands!
    if random.random() < 0.60 and actress_urls:
        return actress_urls + anime_urls
    return anime_urls + actress_urls

@bot.message_handler(commands=['hug', 'kiss', 'love', 'romance', 'bite', 'kill', 'slap', 'pat', 'cuddle', 'punch', 'tickle'])
def roleplay_action_cmd(m):
    if not m.from_user:
        return
    cmd = (m.text or '').split()[0].split('@')[0][1:].lower()
    if cmd not in ACTION_SPECS:
        return

    t_uid, t_name = resolve_action_target(m)
    if not t_uid and not t_name:
        return bot.reply_to(
            m,
            f"Usage: Kisi ke message par reply karke <code>/{cmd}</code> likhein, ya <code>/{cmd} @username</code> / User ID dein!"
        )

    sender_link = f'<a href="tg://user?id={m.from_user.id}">{esc(user_name(m.from_user))}</a>'
    if t_uid:
        target_link = f'<a href="tg://user?id={t_uid}">{esc(t_name)}</a>'
    else:
        target_link = f'<b>{esc(t_name)}</b>'

    template = random.choice(ACTION_SPECS[cmd]['templates'])
    caption = template.format(sender=sender_link, target=target_link)
    reply_mid = m.reply_to_message.message_id if m.reply_to_message else m.message_id

    gif_urls = fetch_action_gif_urls(cmd)
    for g_url in gif_urls[:5]:
        try:
            gif_buf = download_gif_to_buffer(g_url)
            if gif_buf:
                bot.send_animation(
                    m.chat.id,
                    gif_buf,
                    caption=caption,
                    reply_to_message_id=reply_mid
                )
                return
        except Exception as e:
            log.warning("Failed uploading GIF %s: %s", g_url, e)
            continue

    bot.send_message(m.chat.id, caption, reply_to_message_id=reply_mid)

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

# ---------------- SMART CHATBOT (OFF BY DEFAULT, CONTINUOUS LEARNING + TEXT/STICKER/GIF REPLY) ----------------
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
    # Learns from group reply chains silently even when chatbot is OFF so its vocabulary keeps growing
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
            c.execute('DELETE FROM chatbot_memory WHERE id NOT IN (SELECT id FROM chatbot_memory ORDER BY id DESC LIMIT 4000)')

def get_chatbot_response(query_text, strict_match_only=False):
    clean = re.sub(r'\s+', ' ', query_text.lower()).strip()
    with DB_LOCK, connect() as c:
        rows = c.execute('SELECT reply_type,reply_content FROM chatbot_memory WHERE trigger_text=? ORDER BY RANDOM() LIMIT 1', (clean,)).fetchall()
        if not rows and len(clean) >= 3:
            stripped = BOT_NAME_RE.sub('', clean).strip(' ,.!?')
            if stripped:
                rows = c.execute('SELECT reply_type,reply_content FROM chatbot_memory WHERE trigger_text=? ORDER BY RANDOM() LIMIT 1', (stripped,)).fetchall()
                if not rows and len(stripped) >= 4:
                    rows = c.execute('SELECT reply_type,reply_content FROM chatbot_memory WHERE trigger_text LIKE ? ORDER BY RANDOM() LIMIT 1', (f'%{stripped}%',)).fetchall()
        if not rows and not strict_match_only:
            if random.random() < 0.40:
                rows = c.execute('SELECT reply_type,reply_content FROM chatbot_memory ORDER BY RANDOM() LIMIT 1').fetchall()
    if rows:
        return rows[0]['reply_type'], rows[0]['reply_content']
    if strict_match_only:
        return None, None
    return 'text', random.choice(DEFAULT_CHATBOT_REPLIES)

def send_chatbot_reply(m, query_text, strict_match_only=False):
    r_type, r_content = get_chatbot_response(query_text, strict_match_only=strict_match_only)
    if not r_type or not r_content:
        return False
    try:
        if r_type == 'sticker':
            bot.send_sticker(m.chat.id, r_content, reply_to_message_id=m.message_id)
        elif r_type == 'animation':
            bot.send_animation(m.chat.id, r_content, reply_to_message_id=m.message_id)
        else:
            bot.reply_to(m, esc(r_content))
        return True
    except Exception:
        if not strict_match_only:
            bot.reply_to(m, random.choice(DEFAULT_CHATBOT_REPLIES))
        return False

@bot.message_handler(commands=['chatbot'])
def chatbot_toggle(m):
    if not need_admin(m): return
    p = (m.text or '').split()
    if len(p) < 2 or p[1].lower() not in ('on', 'off'):
        cur = setting(m.chat.id, 'chatbot', 'off')
        return bot.reply_to(
            m,
            f'🤖 Chatbot is currently <b>{cur.upper()}</b> in this group.\n'
            f'Usage: <code>/chatbot on</code> ya <code>/chatbot off</code>\n'
            f'<i>(Note: Chatbot har group mein default OFF rehta hai, /chatbot on karne par hi reply dega aur group messages se seekhta rahega.)</i>'
        )
    set_setting(m.chat.id, 'chatbot', p[1].lower())
    bot.reply_to(m, f'🤖 Chatbot <b>{"enabled" if p[1].lower()=="on" else "disabled"}</b> for this group.')

# ---------------- SECRET WHISPER SYSTEM (INLINE + BUTTONS + 3 MODES) ----------------
def save_whisper(sender_id, sender_name, mode, target_key, secret_text):
    wid = uuid.uuid4().hex[:10]
    with DB_LOCK, connect() as c:
        c.execute('INSERT INTO whispers VALUES(?,?,?,?,?,?)',
                  (wid, sender_id, sender_name, f'{mode}:{target_key}', secret_text[:190], time.time()))
        c.execute('DELETE FROM whispers WHERE created_at < ?', (time.time() - 7*86400,))
    return wid

# ---------------- BROADCAST SYSTEM (/broadcast or /bc) WITH MEDIA & INLINE BUTTONS ----------------
BROADCAST_STATE = {} # admin_uid -> state dict
BROADCAST_LOCK = threading.Lock()

def parse_inline_buttons(text_lines):
    kb = types.InlineKeyboardMarkup(row_width=2)
    has_btn = False
    for line in text_lines:
        if '|' in line:
            parts = [p.strip() for p in line.split('|')]
            if len(parts) >= 2:
                btn_name, btn_url = parts[0], parts[1]
                if btn_url.startswith(('http://', 'https://', 't.me/')):
                    kb.add(types.InlineKeyboardButton(btn_name, url=btn_url))
                    has_btn = True
    return kb if has_btn else None

@bot.message_handler(commands=['broadcast', 'bc'])
@bot.message_handler(commands=['broadcast', 'bc'])
def broadcast_cmd(m):
    if not m.from_user: return
    chat = m.chat.id
    uid = m.from_user.id

    # Restrict broadcast strictly to Bot Private Chat (DM) so group admins cannot spam groups from GC
    if group(m):
        return bot.reply_to(m, '⛔ <b>Broadcast Restriction:</b> /broadcast command sirf Bot ke DM (Private Chat) mein use ki ja sakti hai, group chats mein nahi!')

    p = (m.text or '').split(maxsplit=1)
    if len(p) < 2 and not m.reply_to_message:
        return bot.reply_to(
            m,
            "📢 <b>MISHRA ROBOT BROADCAST SYSTEM (DM ONLY)</b>\n\n"
            "Usage:\n"
            "• <code>/broadcast Your message text here</code>\n"
            "• Or reply to any <b>Photo, Video, Sticker, or GIF</b> with <code>/broadcast [caption or buttons]</code>\n\n"
            "🔘 <b>Inline Buttons Jodne ka Tareeqa:</b>\n"
            "Aap message ke aakhir mein buttons aise add kar sakte hain:\n"
            "<code>Button Name | https://t.me/...</code>"
        )

    raw_content = p[1].strip() if len(p) >= 2 else ''
    reply_r = m.reply_to_message
    if not raw_content and reply_r:
        raw_content = (reply_r.caption or reply_r.text or '').strip()
    elif reply_r and (reply_r.caption or reply_r.text):
        raw_content = raw_content + "\n" + (reply_r.caption or reply_r.text or '').strip()

    btn_lines = []
    main_lines = []
    for line in raw_content.splitlines():
        if '|' in line and ('http://' in line or 'https://' in line or 't.me/' in line):
            btn_lines.append(line)
        else:
            main_lines.append(line)

    msg_body = "\n".join(main_lines).strip()
    kb = parse_inline_buttons(btn_lines)

    with DB_LOCK, connect() as c:
        chats = c.execute('SELECT DISTINCT chat_id FROM stats').fetchall()
    chat_ids = [r['chat_id'] for r in chats]
    if not chat_ids:
        return bot.reply_to(m, 'No active chats found in database to broadcast.')

    status_msg = bot.reply_to(
        m,
        f"📢 <b>Broadcast Ready!</b>\n"
        f"🎯 Target chats: <b>{len(chat_ids)}</b>\n\n"
        f"Niche buttons dabakar test karein ya turant bhejein:",
        reply_markup=types.InlineKeyboardMarkup(row_width=2).add(
            types.InlineKeyboardButton('🧪 Test Send (To You)', callback_data=f'bc_test:{uid}'),
            types.InlineKeyboardButton('🚀 Send to All Chats', callback_data=f'bc_send:{uid}')
        )
    )

    media_info = {'type': 'text', 'file_id': '', 'text': msg_body, 'kb': kb, 'chats': chat_ids}
    if reply_r:
        if reply_r.photo:
            media_info['type'] = 'photo'
            media_info['file_id'] = reply_r.photo[-1].file_id
        elif reply_r.video:
            media_info['type'] = 'video'
            media_info['file_id'] = reply_r.video.file_id
        elif reply_r.animation:
            media_info['type'] = 'animation'
            media_info['file_id'] = reply_r.animation.file_id
        elif reply_r.sticker:
            media_info['type'] = 'sticker'
            media_info['file_id'] = reply_r.sticker.file_id

    with BROADCAST_LOCK:
        BROADCAST_STATE[uid] = media_info

@bot.callback_query_handler(func=lambda call: bool(call.data) and (call.data.startswith('bc_test:') or call.data.startswith('bc_send:')))
def broadcast_callback(call):
    parts = call.data.split(':')
    action = parts[0]
    admin_uid = int(parts[1])
    if call.from_user.id != admin_uid:
        return bot.answer_callback_query(call.id, '⛔ Yeh broadcast aapne initiate nahi kiya hai!', show_alert=True)

    with BROADCAST_LOCK:
        info = BROADCAST_STATE.get(admin_uid)
    if not info:
        return bot.answer_callback_query(call.id, '⚠️ Broadcast session expire ho gaya. Dobara /broadcast bhejein.', show_alert=True)

    if action == 'bc_test':
        bot.answer_callback_query(call.id, '🧪 Sending test message to you...')
        try:
            mtype = info['type']
            fid = info['file_id']
            txt = info['text']
            kb = info['kb']
            if mtype == 'photo' and fid:
                bot.send_photo(call.from_user.id, fid, caption=f"<b>[TEST BROADCAST]</b>\n\n{txt}" if txt else "[TEST BROADCAST]", reply_markup=kb)
            elif mtype == 'video' and fid:
                bot.send_video(call.from_user.id, fid, caption=f"<b>[TEST BROADCAST]</b>\n\n{txt}" if txt else "[TEST BROADCAST]", reply_markup=kb)
            elif mtype == 'animation' and fid:
                bot.send_animation(call.from_user.id, fid, caption=f"<b>[TEST BROADCAST]</b>\n\n{txt}" if txt else "[TEST BROADCAST]", reply_markup=kb)
            elif mtype == 'sticker' and fid:
                bot.send_sticker(call.from_user.id, fid)
                bot.send_message(call.from_user.id, f"<b>[TEST BROADCAST]</b>\n\n{txt}" if txt else "<b>[TEST BROADCAST]</b>", reply_markup=kb)
            else:
                bot.send_message(call.from_user.id, f"<b>[TEST BROADCAST]</b>\n\n{txt}" if txt else "<b>[TEST BROADCAST]</b>", reply_markup=kb)
        except Exception as e:
            bot.send_message(call.from_user.id, f"❌ Test send failed: {esc(e)}")
        return

    if action == 'bc_send':
        bot.answer_callback_query(call.id, '🚀 Broadcast started in background!')
        try:
            bot.edit_message_text('🚀 <b>Broadcast in progress...</b> chats ko bheja ja raha hai.', call.message.chat.id, call.message.message_id)
        except Exception:
            pass

        def run_broadcast():
            success, failed = 0, 0
            mtype = info['type']
            fid = info['file_id']
            txt = info['text']
            kb = info['kb']
            for cid in info['chats']:
                try:
                    if mtype == 'photo' and fid:
                        bot.send_photo(cid, fid, caption=txt if txt else None, reply_markup=kb)
                    elif mtype == 'video' and fid:
                        bot.send_video(cid, fid, caption=txt if txt else None, reply_markup=kb)
                    elif mtype == 'animation' and fid:
                        bot.send_animation(cid, fid, caption=txt if txt else None, reply_markup=kb)
                    elif mtype == 'sticker' and fid:
                        bot.send_sticker(cid, fid)
                        bot.send_message(cid, txt if txt else "📢 <b>Announcement</b>", reply_markup=kb)
                    else:
                        bot.send_message(cid, txt if txt else "📢 <b>Announcement</b>", reply_markup=kb)
                    success += 1
                    time.sleep(0.08)
                except Exception:
                    failed += 1
            try:
                bot.send_message(call.message.chat.id, f"✅ <b>Broadcast Complete!</b>\nSuccessful: <b>{success}</b> chats\nFailed: <b>{failed}</b> chats")
            except Exception:
                pass

        threading.Thread(target=run_broadcast, daemon=True).start()

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

# ---------------- HARD CRICKET & SPIRITUAL QUIZ GAME (AI-FIRST + 70+ UNIQUE QUESTIONS + ZERO REPEAT) ----------------
HARD_QUIZ_BANK = [
    # --- 50+ HARD CRICKET QUESTIONS ---
    ("🏏 HARD CRICKET QUIZ", "Test cricket itihas mein ek hi match mein 10 wickets (all 10 in an innings) lene wale pehle Bharatiya aur duniya ke doosre gendbaaz kaun hain?", "Anil Kumble", ["anil kumble", "kumble"]),
    ("🏏 HARD CRICKET QUIZ", "1983 ODI World Cup final mein India ke khilaf sabse zyada runs kis West Indies ballebaj ne banaye the?", "Viv Richards", ["viv richards", "vivian richards", "sir viv richards", "richards"]),
    ("🏏 HARD CRICKET QUIZ", "Test cricket mein India ke liye pehla triple century (300+) lagane wale ballebaj kaun hain?", "Virender Sehwag", ["virender sehwag", "sehwag"]),
    ("🏏 HARD CRICKET QUIZ", "ODI cricket mein pehli double century (200*) kis Bharatiya khiladi ne banayi thi?", "Sachin Tendulkar", ["sachin tendulkar", "sachin"]),
    ("🏏 HARD CRICKET QUIZ", "IPL itihas mein sabse pehli hat-trick kis gendbaaz ne li thi (2008 season mein)?", "Lakshmipathy Balaji", ["lakshmipathy balaji", "balaji", "l balaji"]),
    ("🏏 HARD CRICKET QUIZ", "Test cricket ke itihas mein 400* runs ka world record kis West Indies legend ke naam hai?", "Brian Lara", ["brian lara", "lara"]),
    ("🏏 HARD CRICKET QUIZ", "2007 T20 World Cup final mein India ke liye aakhri over kis gendbaaz ne fenka tha?", "Joginder Sharma", ["joginder sharma", "joginder"]),
    ("🏏 HARD CRICKET QUIZ", "Test cricket mein sabse zyada 800 wickets lene wale Sri Lankan off-spinner kaun hain?", "Muttiah Muralitharan", ["muttiah muralitharan", "muralitharan", "murali"]),
    ("🏏 HARD CRICKET QUIZ", "2011 ODI World Cup quarter-final mein Australia ke khilaf Yuvraj Singh ke saath match-winning partnership kisne ki thi?", "Suresh Raina", ["suresh raina", "raina"]),
    ("🏏 HARD CRICKET QUIZ", "International cricket mein 6 balls par 6 sixes lagane wale duniya ke pehle ballebaj kaun the (2007 WC)?", "Herschelle Gibbs", ["herschelle gibbs", "gibbs"]),
    ("🏏 HARD CRICKET QUIZ", "Test cricket mein lagatar 4 Test matches mein 4 double centuries lagane wale ekmatra ballebaj kaun hain?", "Virat Kohli", ["virat kohli", "kohli", "virat"]),
    ("🏏 HARD CRICKET QUIZ", "IPL ke ek hi season (2016) mein 973 runs banane ka all-time record kis khiladi ke naam hai?", "Virat Kohli", ["virat kohli", "kohli", "virat"]),
    ("🏏 HARD CRICKET QUIZ", "International T20 cricket mein sabse tez century (35 balls) lagane wale Bharatiya ballebaj kaun hain?", "Rohit Sharma", ["rohit sharma", "rohit"]),
    ("🏏 HARD CRICKET QUIZ", "India ke liye Test cricket mein hat-trick lene wale sabse pehle gendbaaz kaun the (2001 Kolkata Test)?", "Harbhajan Singh", ["harbhajan singh", "harbhajan", "bhajji"]),
    ("🏏 HARD CRICKET QUIZ", "World Cup itihas mein sabse tez double century (128 balls) kis West Indies khiladi ne lagayi thi?", "Chris Gayle", ["chris gayle", "gayle"]),
    ("🏏 HARD CRICKET QUIZ", "ODI cricket mein sabse tez 10,000 runs pure karne wale duniya ke pehle ballebaj kaun hain?", "Virat Kohli", ["virat kohli", "kohli", "virat"]),
    ("🏏 HARD CRICKET QUIZ", "Test cricket mein sabse zyada 15,921 runs banane wale duniya ke ekmatra ballebaj kaun hain?", "Sachin Tendulkar", ["sachin tendulkar", "sachin"]),
    ("🏏 HARD CRICKET QUIZ", "2019 ODI World Cup final ke Super Over mein England ke liye kisne batting ki thi? (Kisi ek ka naam likhein)", "Ben Stokes / Jos Buttler", ["ben stokes", "stokes", "jos buttler", "buttler"]),
    ("🏏 HARD CRICKET QUIZ", "IPL itihas mein 200+ wickets lene wale sabse pehle gendbaaz kaun bane the?", "Yuzvendra Chahal", ["yuzvendra chahal", "chahal"]),
    ("🏏 HARD CRICKET QUIZ", "Test match ki dono innings mein century lagane wale pehle Bharatiya wicketkeeper kaun hain?", "Rishabh Pant", ["rishabh pant", "pant"]),
    ("🏏 HARD CRICKET QUIZ", "1983 World Cup mein Zimbabwe ke khilaf 175* runs ki aitihasik pari kisne kheli thi?", "Kapil Dev", ["kapil dev", "kapil"]),
    ("🏏 HARD CRICKET QUIZ", "International cricket mein sabse tez gend (161.3 km/h) fenkne wale Pakistani pacer kaun hain?", "Shoaib Akhtar", ["shoaib akhtar", "shoaib"]),
    ("🏏 HARD CRICKET QUIZ", "Test cricket mein sabse zyada 134 catches (non-wicketkeeper) pakadne wale Bharatiya khiladi kaun hain?", "Rahul Dravid", ["rahul dravid", "dravid"]),
    ("🏏 HARD CRICKET QUIZ", "T20 International mein hat-trick lene wale pehle Bharatiya purush gendbaaz kaun hain?", "Deepak Chahar", ["deepak chahar", "chahar"]),
    ("🏏 HARD CRICKET QUIZ", "2023 ODI World Cup mein sabse zyada 24 wickets lene wale gendbaaz kaun the?", "Mohammed Shami", ["mohammed shami", "shami"]),
    ("🏏 HARD CRICKET QUIZ", "Test cricket mein sabse pehla kachra (duck) par out hone wale khiladi ya sabse zyada duck kiske naam hai? (Israr / Courtney Walsh)", "Courtney Walsh", ["courtney walsh", "walsh"]),
    ("🏏 HARD CRICKET QUIZ", "1975 ke pehle World Cup final mein Man of the Match kis khiladi ko mila tha?", "Clive Lloyd", ["clive lloyd", "lloyd"]),
    ("🏏 HARD CRICKET QUIZ", "Test cricket ke ek hi din mein sabse zyada runs banane wale ballebaj Don Bradman ne kis team ke khilaf banaye the?", "England", ["england"]),
    ("🏏 HARD CRICKET QUIZ", "IPL 2008 ke inaugural match mein kis ballebaj ne pehla century banaya tha?", "Brendon McCullum", ["brendon mccullum", "mccullum"]),
    ("🏏 HARD CRICKET QUIZ", "Wankhede Stadium (Mumbai) mein 2011 World Cup final kis team ne jeeta tha?", "India", ["india"]),
    ("🏏 HARD CRICKET QUIZ", "International cricket mein 500+ international wickets aur 5000+ runs banane wale all-rounder kaun hain?", "Kapil Dev", ["kapil dev"]),
    ("🏏 HARD CRICKET QUIZ", "Test cricket mein sabse zyada 132 test matches khelne wale captain kaun hain?", "Graeme Smith", ["graeme smith", "smith"]),
    ("🏏 HARD CRICKET QUIZ", "Women ODI World Cup sabse zyada baar kis desh ne jeeta hai?", "Australia", ["australia"]),
    ("🏏 HARD CRICKET QUIZ", "T20 World Cup 2024 kis desh ne jeeta tha?", "India", ["india"]),
    ("🏏 HARD CRICKET QUIZ", "IPL trophy sabse zyada baar kis team ne jeeta hai (Jointly Mumbai & CSK)?", "MI aur CSK", ["mi aur csk", "mumbai indians", "csk", "chennai super kings", "mumbai", "chennai"]),

    # --- 50+ SPIRITUAL KNOWLEDGE QUESTIONS ---
    ("🕉️ SPIRITUAL KNOWLEDGE QUIZ", "Radha Rani ke pita ji ka kya naam tha jo Barsana ke mukhiya the?", "Vrishabhanu", ["vrishabhanu", "vrishbhanu", "bhanu"]),
    ("🕉️ SPIRITUAL KNOWLEDGE QUIZ", "Bhagwan Shri Krishna ki bansuri (flute) ka kya naam tha jiski dhun par Radha Rani aur Gopiyan mugdh ho jaati thi?", "Mahananda", ["mahananda", "murali", "bansuri"]),
    ("🕉️ SPIRITUAL KNOWLEDGE QUIZ", "Radha Rani ki mata ji ka kya naam tha?", "Kirtida", ["kirtida", "keertida"]),
    ("🕉️ SPIRITUAL KNOWLEDGE QUIZ", "Vrindavan mein jis pavitra sthan par Bhagwan Shri Krishna aur Radha Rani ne Maharaas rachaya tha, us van ka kya naam hai?", "Nidhivan", ["nidhivan", "nidhi van"]),
    ("🕉️ SPIRITUAL KNOWLEDGE QUIZ", "Puri (Odisha) mein sthit Jagannath Mandir mein Bhagwan Jagannath ke saath unke bade bhai aur behen kaun virajman hain?", "Balabhadra aur Subhadra", ["balabhadra", "subhadra", "balram", "balabhadra aur subhadra"]),
    ("🕉️ SPIRITUAL KNOWLEDGE QUIZ", "Bhagwan Jagannath ji ki vishav-prasiddh Rath Yatra har saal kis Hindu mahine mein nikali jaati hai?", "Ashadha", ["ashadha", "ashadh", "asadha"]),
    ("🕉️ SPIRITUAL KNOWLEDGE QUIZ", "Jagannath Puri ke rath 'Nandighosha' (Bhagwan Jagannath ji ke rath) mein kul kitne pahiye (wheels) hote hain?", "16", ["16", "sixteen", "solah"]),
    ("🕉️ SPIRITUAL KNOWLEDGE QUIZ", "Mata Sita ke pita Raja Janak kis rajya ke raja the?", "Mithila (Videha)", ["mithila", "videha", "janakpur"]),
    ("🕉️ SPIRITUAL KNOWLEDGE QUIZ", "Ramayana mein Mata Sita ka haran karne ke baad Ravan ne unhe Lanka ke kis vatika mein rakha tha?", "Ashok Vatika", ["ashok vatika", "ashoka vatika"]),
    ("🕉️ SPIRITUAL KNOWLEDGE QUIZ", "Shri Ram ke vanvas jaane par unki khadau (paduka) rakh kar 14 varsh tak Ayodhya ka rajya kisne sambhala tha?", "Bharat", ["bharat", "bharata"]),
    ("🕉️ SPIRITUAL KNOWLEDGE QUIZ", "Ramayana mein Lanka jaane ke liye samudra par pul (Ram Setu) banane wale do vanar bhai kaun the?", "Nal aur Neel", ["nal", "neel", "nal neel", "nal aur neel"]),
    ("🕉️ SPIRITUAL KNOWLEDGE QUIZ", "Bhagwan Shiv ke dhanush ka kya naam tha jise Shri Ram ne Sita Swayamvar mein toda tha?", "Pinaka", ["pinaka", "pinak"]),
    ("🕉️ SPIRITUAL KNOWLEDGE QUIZ", "Mata Parvati ke pita ka kya naam tha jo parvatraj the?", "Himavan", ["himavan", "himavat", "himalaya"]),
    ("🕉️ SPIRITUAL KNOWLEDGE QUIZ", "Bhagwan Mahadev aur Mata Parvati ke bade putra (Devtaon ke Senapati) ka kya naam hai?", "Kartikeya", ["kartikeya", "murugan", "skanda", "subrahmanya"]),
    ("🕉️ SPIRITUAL KNOWLEDGE QUIZ", "Bhagwan Shiv ke 12 Jyotirlingon mein se pratham (pehla) Jyotirling kaunsa hai?", "Somnath", ["somnath"]),
    ("🕉️ SPIRITUAL KNOWLEDGE QUIZ", "Bhagwan Mahadev ke gale mein lipte hue nagraj ka kya naam hai?", "Vasuki", ["vasuki"]),
    ("🕉️ SPIRITUAL KNOWLEDGE QUIZ", "Shrimad Bhagavad Gita mein kul kitne adhyay (chapters) hain?", "18", ["18", "eighteen", "atharah"]),
    ("🕉️ SPIRITUAL KNOWLEDGE QUIZ", "Mahabharata ke yuddh mein Bhagwan Shri Krishna ke shankh (conch) ka kya naam tha?", "Panchajanya", ["panchajanya", "panchjanya"]),
    ("🕉️ SPIRITUAL KNOWLEDGE QUIZ", "Arjun ke dhanush ka kya naam tha jo unhe Agni Dev se prapt hua tha?", "Gandiva", ["gandiva", "gandiv"]),
    ("🕉️ SPIRITUAL KNOWLEDGE QUIZ", "Mahabharata ki rachna kis Maharishi ne ki thi (jise Shri Ganesh ji ne likha tha)?", "Ved Vyas", ["ved vyas", "vyas", "maharishi ved vyas"]),
    ("🕉️ SPIRITUAL KNOWLEDGE QUIZ", "Radha Rani ka janm kis pavitra gaon mein hua tha?", "Rawal", ["rawal", "raval"]),
    ("🕉️ SPIRITUAL KNOWLEDGE QUIZ", "Bhagwan Jagannath ji ke bade bhai Balabhadra ji ke rath ka kya naam hai?", "Taladhwaja", ["taladhwaja", "taladhwaj"]),
    ("🕉️ SPIRITUAL KNOWLEDGE QUIZ", "Devi Subhadra ji ke rath ka kya naam hai jo Jagannath Rath Yatra mein chalta hai?", "Darpadalana", ["darpadalana", "devadalana", "padmadhwaja"]),
    ("🕉️ SPIRITUAL KNOWLEDGE QUIZ", "Mata Sita ki chhoti behen ka kya naam tha jinka vivah Lakshman ji se hua tha?", "Urmila", ["urmila"]),
    ("🕉️ SPIRITUAL KNOWLEDGE QUIZ", "Shri Ram ke param bhakt Hanuman ji ki mata ka kya naam tha?", "Anjani", ["anjani", "anjana"]),
    ("🕉️ SPIRITUAL KNOWLEDGE QUIZ", "Bhagwan Shiv ke trishul ka kya naam hai?", "Pinaka / Vijaya", ["vijaya", "pinaka", "trishul"]),
    ("🕉️ SPIRITUAL KNOWLEDGE QUIZ", "Mahabharata mein Bhishma Pitamah ke bachpan ka kya naam tha?", "Devavrata", ["devavrata", "devavrat"]),
    ("🕉️ SPIRITUAL KNOWLEDGE QUIZ", "Ganga nadi ko swarg se prithvi par laane ke liye kis raja ne kathor tapasya ki thi?", "Bhagirath", ["bhagirath", "raja bhagirath"]),
    ("🕉️ SPIRITUAL KNOWLEDGE QUIZ", "Govardhan Parvat ko Bhagwan Shri Krishna ne apni kis ungli par uthaya tha?", "Kanistha (Chhoti ungli)", ["kanistha", "little finger", "chhoti", "kanistha ungli"]),
    ("🕉️ SPIRITUAL KNOWLEDGE QUIZ", "Kedarnath Jyotirling Bharat ke kis rajya mein sthit hai?", "Uttarakhand", ["uttarakhand"]),
    ("🕉️ SPIRITUAL KNOWLEDGE QUIZ", "Bhagwan Shri Krishna ke guru ka kya naam tha jinke ashram mein unhone shiksha li thi?", "Sandipani", ["sandipani", "sandipani muni"]),
    ("🕉️ SPIRITUAL KNOWLEDGE QUIZ", "Ramayana ke rachayita Maharishi ka kya naam hai?", "Valmiki", ["valmiki", "maharishi valmiki"]),
    ("🕉️ SPIRITUAL KNOWLEDGE QUIZ", "Lakshman ji ki patni Urmila ke alawa Raja Janak ki anya do putriyon (Bharat aur Shatrughna ki patniyon) mein se kisi ek ka naam likhein?", "Mandavi / Shrutakirti", ["mandavi", "shrutakirti"]),
    ("🕉️ SPIRITUAL KNOWLEDGE QUIZ", "Kashi Vishwanath Jyotirling kis pavitra nadi ke tat par sthit hai?", "Ganga", ["ganga", "ganges"]),
    ("🕉️ SPIRITUAL KNOWLEDGE QUIZ", "Mahabharata mein Karn ke kavach aur kundal kis devta ne dan mein maange the?", "Indra", ["indra", "devraj indra"]),
    ("🕉️ SPIRITUAL KNOWLEDGE QUIZ", "Bhagwan Shri Krishna ka janm Mathura ke kis karagar (jail) mein hua tha?", "Kans Karagar", ["kans", "kamsa", "mathura"]),
    ("🕉️ SPIRITUAL KNOWLEDGE QUIZ", "Vrindavan mein Banke Bihari Mandir ki sthapna kis mahan sangeetkar sant ne ki thi?", "Swami Haridas", ["haridas", "swami haridas"]),
    ("🕉️ SPIRITUAL KNOWLEDGE QUIZ", "Bhagwan Vishnu ke kis avatar ne Raja Bali se teen pag bhoomi dan mein maangi thi?", "Vamana", ["vamana", "vaman"]),
    ("🕉️ SPIRITUAL KNOWLEDGE QUIZ", "Ramayana mein Hanuman ji ne Lanka jaane ke liye kis parvat se chhalang lagayi thi?", "Mahendragiri", ["mahendragiri", "mahendra"]),
    ("🕉️ SPIRITUAL KNOWLEDGE QUIZ", "Mahabharata ka yuddh Haryana ke kis pavitra kshetra mein lada gaya tha?", "Kurukshetra", ["kurukshetra"]),
    ("🕉️ SPIRITUAL KNOWLEDGE QUIZ", "Bhagwan Shiv ke vahan Nandi ke pita ka kya naam tha?", "Shilada", ["shilada", "shilad"]),
    ("🕉️ SPIRITUAL KNOWLEDGE QUIZ", "Puri Jagannath Mandir mein bhog (Mahaprasad) kis bartan mein pakaya jata hai?", "Mitti ke bartan", ["mitti", "clay", "earthen", "mitti ke bartan"]),
    ("🕉️ SPIRITUAL KNOWLEDGE QUIZ", "Radha Ashtami ka pavan parv kis mahine ke Shukla Paksha ki Ashtami ko manaya jata hai?", "Bhadrapada", ["bhadrapada", "bhado", "bhadra"]),
    ("🕉️ SPIRITUAL KNOWLEDGE QUIZ", "Shri Ram ke vansh ka kya naam tha jisme unka janm hua tha?", "Ikshvaku (Suryavansh)", ["ikshvaku", "suryavansh", "raghuvansh"]),
    ("🕉️ SPIRITUAL KNOWLEDGE QUIZ", "Mahabharata mein Yudhishthira ke shankh ka kya naam tha?", "Anantavijaya", ["anantavijaya", "anantvijay"]),
    ("🕉️ SPIRITUAL KNOWLEDGE QUIZ", "Bhagwan Hanuman ji ke putra ka kya naam tha jinka janm Makardhwaj ke alawa ya samudri praani se hua tha?", "Makardhwaj", ["makardhwaj"]),
    ("🕉️ SPIRITUAL KNOWLEDGE QUIZ", "Samudra Manthan ke samay sabse pehle kaunsa vish (poison) nikla tha jise Bhagwan Shiv ne apne gale mein dharan kiya tha?", "Halahala (Vish)", ["halahala", "vish", "halahal", "kalkoot"]),
    ("🕉️ SPIRITUAL KNOWLEDGE QUIZ", "Bhagwan Ganesha ke vahan Mooshak (chuha) ka aakar pehle kya tha ya kis shrap se chuha bane the?", "Gajamukha / Durvasa shrap", ["gajamukha", "durvasa"]),
]
ACTIVE_CRICKET = {}   # chat_id -> {'category': str, 'question': str, 'name': str, 'answers': list, 'asked_at': float}
ASKED_QUIZ_HASHES = defaultdict(set)
CRICKET_LOCK = threading.Lock()

def generate_ai_quiz_question(category_filter, asked_set):
    raw_key = GROQ_API_KEY or os.getenv('GROQ_API_KEY', '')
    api_key = str(raw_key).strip().strip("'").strip('"')
    if not api_key:
        return None
    try:
        import urllib.request
        cat_label = "🏏 HARD CRICKET QUIZ" if category_filter == 'cricket' else "🕉️ SPIRITUAL KNOWLEDGE QUIZ"
        recent_avoid = list(asked_set)[-12:]
        avoid_str = "; ".join(recent_avoid) if recent_avoid else "none"
        if category_filter == 'cricket':
            sub_topic = random.choice([
                "World Cup records", "Test cricket history", "IPL records",
                "Indian cricket legends", "Famous cricket stadiums", "Bowling hat-tricks"
            ])
            topic_prompt = (
                f"Generate 1 unique, hard cricket trivia question about {sub_topic} in Hinglish (Hindi written in English script) "
                f"with a 1 or 2 word exact answer (a cricketer name, stadium, or number). "
                f"Do NOT repeat any of these questions: [{avoid_str}]. "
                f"Return ONLY valid JSON: {{\"question\": \"...\", \"answer\": \"...\", \"aliases\": [\"...\"]}}"
            )
        else:
            sub_topic = random.choice([
                "Radha Krishna leela, Barsana, Gokul & Vrindavan",
                "Shri Ram, Mata Sita, Lakshman & Ramayana",
                "Bhagwan Mahadev, Mata Parvati, Kailash & 12 Jyotirlingas",
                "Bhagwan Jagannath Puri, Balabhadra, Subhadra & Rath Yatra",
                "Mahabharata heroes, Kurukshetra & Shrimad Bhagavad Gita",
                "Bhakt Hanuman Ji, Sundarkand & Panchmukhi avatar",
                "Bhagwan Vishnu ke Dashavatar (Narasimha, Vamana, Parashurama, Kalki)",
                "Maa Durga, Navadurga, Shakti Peethas & Maa Lakshmi/Saraswati",
                "Vedas, Upanishads, Puranas & Great Rishis (Valmiki, Vyas, Vishwamitra)"
            ])
            topic_prompt = (
                f"Generate 1 unique, interesting Hindu spiritual trivia question specifically about {sub_topic} "
                f"in Hinglish (Hindi written in English script) with a 1 or 2 word exact answer. "
                f"Do NOT repeat any of these questions: [{avoid_str}]. "
                f"Return ONLY valid JSON: {{\"question\": \"...\", \"answer\": \"...\", \"aliases\": [\"...\"]}}"
            )

        # Try available Groq models in order
        for mod_name in ['openai/gpt-oss-20b', 'llama-3.3-70b-versatile', 'llama-3.1-8b-instant']:
            try:
                payload = json.dumps({
                    'model': mod_name,
                    'messages': [
                        {'role': 'system', 'content': 'You output strictly valid JSON only. No markdown, no extra text.'},
                        {'role': 'user', 'content': topic_prompt}
                    ],
                    'max_tokens': 240,
                    'temperature': 0.95
                }).encode('utf-8')
                req = urllib.request.Request(
                    'https://api.groq.com/openai/v1/chat/completions',
                    data=payload,
                    headers={
                        'Authorization': f'Bearer {api_key}',
                        'Content-Type': 'application/json',
                        'User-Agent': 'Mozilla/5.0'
                    },
                    method='POST'
                )
                with urllib.request.urlopen(req, timeout=12) as resp:
                    res = json.loads(resp.read().decode('utf-8'))
                raw_txt = res['choices'][0]['message']['content'].strip()
                raw_txt = re.sub(r'^```(?:json)?|```$', '', raw_txt, flags=re.M).strip()
                obj = json.loads(raw_txt)
                q_txt = str(obj.get('question', '')).strip()
                ans_txt = str(obj.get('answer', '')).strip()
                aliases = [str(a).strip().lower() for a in obj.get('aliases', []) if str(a).strip()]
                if ans_txt.lower() not in aliases:
                    aliases.append(ans_txt.lower())
                if q_txt and ans_txt and q_txt not in asked_set:
                    return (cat_label, q_txt, ans_txt, aliases)
            except Exception:
                continue
    except Exception:
        pass
    return None

def post_cricket_question(chat_id, reply_to_id=None, category_filter=None):
    if category_filter not in ('cricket', 'spiritual'):
        category_filter = random.choice(['cricket', 'spiritual'])

    with DB_LOCK, connect() as c:
        rows = c.execute('SELECT question_hash FROM asked_quizzes WHERE chat_id=? AND category=?', (chat_id, category_filter)).fetchall()
        asked_db = {r['question_hash'] for r in rows}

    with CRICKET_LOCK:
        asked = asked_db | ASKED_QUIZ_HASHES[chat_id]
        pool = [q for q in HARD_QUIZ_BANK if (category_filter == 'cricket' and 'CRICKET' in q[0]) or (category_filter == 'spiritual' and 'SPIRITUAL' in q[0])]
        unasked_pool = [q for q in pool if q[1] not in asked]

    chosen = None
    # Always generate brand-new AI questions first (95% priority) so every single question is fresh!
    if random.random() < 0.95 or not unasked_pool:
        ai_q = generate_ai_quiz_question(category_filter, asked)
        if ai_q:
            chosen = ai_q

    if chosen is None:
        if not unasked_pool:
            with DB_LOCK, connect() as c:
                c.execute('DELETE FROM asked_quizzes WHERE chat_id=? AND category=?', (chat_id, category_filter))
            with CRICKET_LOCK:
                for q in pool:
                    ASKED_QUIZ_HASHES[chat_id].discard(q[1])
            unasked_pool = list(pool)
        chosen = random.choice(unasked_pool)

    category, question, display_ans, answers = chosen
    with DB_LOCK, connect() as c:
        c.execute('INSERT OR IGNORE INTO asked_quizzes(chat_id,category,question_hash,asked_at) VALUES(?,?,?,?)',
                  (chat_id, category_filter, question, time.time()))
    with CRICKET_LOCK:
        ASKED_QUIZ_HASHES[chat_id].add(question)
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

# ---------------- 3D LUDO KING MINI APP LAUNCHER (/ludo) ----------------
LUDO_SAFE_INDICES = {0, 8, 13, 21, 26, 34, 39, 47}

def get_public_webapp_base():
    return (os.getenv('WEBAPP_URL', '') or os.getenv('RENDER_EXTERNAL_URL', '')).strip().rstrip('/')

@bot.message_handler(commands=['ludo'])
def ludo_cmd(m):
    base = get_public_webapp_base()
    if not base:
        return bot.reply_to(
            m,
            "⚠️ <b>3D Ludo Mini App URL set nahi hai!</b>\n"
            "Render Environment mein <code>WEBAPP_URL</code> add karein."
        )

    bot_uname = ''
    try:
        bot_uname = bot.get_me().username
    except Exception:
        pass

    if not group(m):
        room_id = f"dm_{m.from_user.id}"
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton('🎮 Open 3D Ludo King', web_app=types.WebAppInfo(url=f"{base}/ludo3d?room={room_id}")))
        return bot.reply_to(
            m,
            "👑 <b>MISHRA 3D LUDO KING</b> 👑\n"
            "Niche button dabakar 3D Ludo Board open karein:",
            reply_markup=kb
        )

    room_id = f"gc_{abs(m.chat.id)}_{uuid.uuid4().hex[:4]}"
    kb = types.InlineKeyboardMarkup(row_width=1)
    if bot_uname:
        kb.add(types.InlineKeyboardButton('🎮 Play 3D Ludo King (2-4 Players)', url=f"https://t.me/{bot_uname}?startapp={room_id}"))
    else:
        kb.add(types.InlineKeyboardButton('🎮 Play 3D Ludo King (2-4 Players)', url=f"{base}/ludo3d?room={room_id}"))

    bot.send_message(
        m.chat.id,
        f"👑 <b>MISHRA 3D LUDO KING ARENA</b> 👑\n\n"
        f"🎲 <b>Room ID:</b> <code>{room_id}</code>\n"
        f"✨ <i>Full 3D Board, 3D Cube Dice, smooth goti animation & real sound effects!</i>\n"
        f"🏆 Winner ko milenge <b>+100 Coins</b>!\n\n"
        f"👇 <i>Sabhi players niche button dabakar ek hi 3D room mein join karein:</i>",
        reply_markup=kb
    )

# ---------------- GROUP GAME 1: HAND CRICKET (/handcricket & /hc) ----------------
HC_GAMES = {}
HC_LOCK = threading.Lock()

def hc_keyboard(gid, game):
    kb = types.InlineKeyboardMarkup(row_width=3)
    if game['over']:
        return kb
    if game['p2_id'] is None:
        kb.add(types.InlineKeyboardButton('🏏 Join Match as Player 2', callback_data=f'hc:{gid}:join'))
        return kb

    btns = [types.InlineKeyboardButton(f"{n}️⃣", callback_data=f'hc:{gid}:pick:{n}') for n in range(1, 7)]
    kb.row(btns[0], btns[1], btns[2])
    kb.row(btns[3], btns[4], btns[5])
    return kb

def hc_status_text(game):
    p1 = f"<a href=\"tg://user?id={game['p1_id']}\">{esc(game['p1_name'])}</a>"
    p2 = f"<a href=\"tg://user?id={game['p2_id']}\">{esc(game['p2_name'])}</a>" if game['p2_id'] else "<i>Waiting for Player 2...</i>"

    if game['p2_id'] is None:
        return (
            f"🏏 <b>MISHRA HAND CRICKET (1v1)</b> 🏏\n\n"
            f"👤 Player 1: {p1}\n"
            f"👤 Player 2: {p2}\n\n"
            f"👇 <i>Koi bhi friend niche button dabakar match join karein!</i>"
        )

    bat_name = p1 if game['batter'] == 1 else p2
    bowl_name = p2 if game['batter'] == 1 else p1
    p1_picked = "✅" if game['p1_choice'] is not None else "⏳"
    p2_picked = "✅" if game['p2_choice'] is not None else "⏳"

    lines = [
        f"🏏 <b>HAND CRICKET — INNINGS {game['innings']}/2</b> 🏏",
        f"👤 {p1}: <b>{game['p1_score']}</b> runs {p1_picked}",
        f"👤 {p2}: <b>{game['p2_score']}</b> runs {p2_picked}",
    ]
    if game['target'] is not None:
        need = max(0, game['target'] - (game['p1_score'] if game['batter'] == 1 else game['p2_score']))
        lines.append(f"🎯 <b>Target:</b> {game['target']} runs (Jeetne ke liye <b>{need}</b> aur chahiye)")

    if game['last_ball']:
        lines.append(f"\n📢 <i>{esc(game['last_ball'])}</i>")

    if game['over']:
        lines.append(f"\n{game['result_text']}")
    else:
        lines.append(f"\n🏏 <b>Batting:</b> {bat_name} | ⚾ <b>Bowling:</b> {bowl_name}")
        lines.append("👇 <i>Dono players chupke se 1 se 6 tak apna number chunein:</i>")
    return "\n".join(lines)

@bot.message_handler(commands=['handcricket', 'hc'])
def handcricket_cmd(m):
    if not group(m):
        return bot.reply_to(m, 'Use /handcricket in a group to play!')
    gid = uuid.uuid4().hex[:8]
    p2_id, p2_name = None, None
    if m.reply_to_message and m.reply_to_message.from_user and not m.reply_to_message.from_user.is_bot:
        if m.reply_to_message.from_user.id != m.from_user.id:
            p2_id = m.reply_to_message.from_user.id
            p2_name = user_name(m.reply_to_message.from_user)

    game = {
        'chat_id': m.chat.id,
        'p1_id': m.from_user.id,
        'p1_name': user_name(m.from_user),
        'p2_id': p2_id,
        'p2_name': p2_name,
        'p1_score': 0,
        'p2_score': 0,
        'innings': 1,
        'batter': 1,
        'target': None,
        'p1_choice': None,
        'p2_choice': None,
        'last_ball': '',
        'over': False,
        'result_text': '',
        'ts': time.time()
    }
    with HC_LOCK:
        now = time.time()
        for k in list(HC_GAMES.keys()):
            if now - HC_GAMES[k]['ts'] > 3600:
                HC_GAMES.pop(k, None)
        HC_GAMES[gid] = game

    bot.send_message(m.chat.id, hc_status_text(game), reply_markup=hc_keyboard(gid, game))

@bot.callback_query_handler(func=lambda call: bool(call.data) and call.data.startswith('hc:'))
def hc_callback(call):
    parts = call.data.split(':')
    if len(parts) < 3:
        return
    gid = parts[1]
    action = parts[2]
    uid = call.from_user.id
    uname = user_name(call.from_user)

    with HC_LOCK:
        game = HC_GAMES.get(gid)
        if not game:
            return bot.answer_callback_query(call.id, '⚠️ Yeh match expire ho chuka hai. /hc se naya start karein.', show_alert=True)
        if game['over']:
            return bot.answer_callback_query(call.id, '🏁 Match khatam ho chuka hai!')

        game['ts'] = time.time()

        if action == 'join':
            if uid == game['p1_id']:
                return bot.answer_callback_query(call.id, 'Aap pehle se Player 1 hain!', show_alert=True)
            if game['p2_id'] is not None:
                return bot.answer_callback_query(call.id, 'Player 2 pehle hi join ho chuka hai!', show_alert=True)
            game['p2_id'] = uid
            game['p2_name'] = uname
            game['last_ball'] = f"{uname} ne match join kiya! 1st Innings shuru."
            bot.answer_callback_query(call.id, '🏏 Match Joined!')
            try:
                bot.edit_message_text(hc_status_text(game), call.message.chat.id, call.message.message_id, reply_markup=hc_keyboard(gid, game))
            except Exception:
                pass
            return

        if action == 'pick' and len(parts) == 4:
            if uid not in (game['p1_id'], game['p2_id']):
                return bot.answer_callback_query(call.id, '⛔ Aap is match ke player nahi hain!', show_alert=True)
            num = int(parts[3])
            if uid == game['p1_id']:
                if game['p1_choice'] is not None:
                    return bot.answer_callback_query(call.id, f"Aapne pehle hi {game['p1_choice']} chun liya hai! Doosre player ka intezar karein.")
                game['p1_choice'] = num
            else:
                if game['p2_choice'] is not None:
                    return bot.answer_callback_query(call.id, f"Aapne pehle hi {game['p2_choice']} chun liya hai! Doosre player ka intezar karein.")
                game['p2_choice'] = num

            # Wait until BOTH players have secretly picked their number
            if game['p1_choice'] is None or game['p2_choice'] is None:
                bot.answer_callback_query(call.id, f"✅ Aapne {num} lock kiya! Opponent ka intezar...")
                try:
                    bot.edit_message_text(hc_status_text(game), call.message.chat.id, call.message.message_id, reply_markup=hc_keyboard(gid, game))
                except Exception:
                    pass
                return

            c1, c2 = game['p1_choice'], game['p2_choice']
            game['p1_choice'] = None
            game['p2_choice'] = None

            bat_runs = c1 if game['batter'] == 1 else c2
            bowl_runs = c2 if game['batter'] == 1 else c1
            batter_name = game['p1_name'] if game['batter'] == 1 else game['p2_name']

            if bat_runs == bowl_runs:
                # OUT!
                if game['innings'] == 1:
                    first_score = game['p1_score'] if game['batter'] == 1 else game['p2_score']
                    game['target'] = first_score + 1
                    game['innings'] = 2
                    game['batter'] = 2 if game['batter'] == 1 else 1
                    game['last_ball'] = f"☝️ OUT! Dono ne {bat_runs} chuna! {batter_name} out. Target: {game['target']} runs!"
                else:
                    # 2nd innings batter is OUT -> 1st innings batter wins (or tie)
                    game['over'] = True
                    if game['p1_score'] > game['p2_score']:
                        win_uid, win_name = game['p1_id'], game['p1_name']
                        game['result_text'] = f"🏆 <b>WINNER:</b> <a href=\"tg://user?id={win_uid}\">{esc(win_name)}</a> (+50 Coins) 🎉"
                    elif game['p2_score'] > game['p1_score']:
                        win_uid, win_name = game['p2_id'], game['p2_name']
                        game['result_text'] = f"🏆 <b>WINNER:</b> <a href=\"tg://user?id={win_uid}\">{esc(win_name)}</a> (+50 Coins) 🎉"
                    else:
                        win_uid = None
                        game['result_text'] = "🤝 <b>MATCH TIED!</b> Dono ke barabar runs!"
                    game['last_ball'] = f"☝️ OUT! Dono ne {bat_runs} chuna! Match khatam."
                    if win_uid:
                        with DB_LOCK, connect() as c:
                            c.execute('INSERT OR IGNORE INTO wallet(user_id,coins,last_daily) VALUES(?,0,?)', (win_uid, ''))
                            c.execute('UPDATE wallet SET coins=coins+50 WHERE user_id=?', (win_uid,))
            else:
                # Runs scored!
                if game['batter'] == 1:
                    game['p1_score'] += bat_runs
                    cur_score = game['p1_score']
                else:
                    game['p2_score'] += bat_runs
                    cur_score = game['p2_score']

                shot = "SIX! 🚀" if bat_runs == 6 else ("FOUR! 🔥" if bat_runs == 4 else f"+{bat_runs} runs!")
                game['last_ball'] = f"🏏 {batter_name} ne {bat_runs} mara (Bowler: {bowl_runs}) — {shot}"

                if game['innings'] == 2 and cur_score >= game['target']:
                    game['over'] = True
                    win_uid = game['p1_id'] if game['batter'] == 1 else game['p2_id']
                    win_name = game['p1_name'] if game['batter'] == 1 else game['p2_name']
                    game['result_text'] = f"🏆 <b>TARGET CHASED! Winner:</b> <a href=\"tg://user?id={win_uid}\">{esc(win_name)}</a> (+50 Coins) 🎉"
                    with DB_LOCK, connect() as c:
                        c.execute('INSERT OR IGNORE INTO wallet(user_id,coins,last_daily) VALUES(?,0,?)', (win_uid, ''))
                        c.execute('UPDATE wallet SET coins=coins+50 WHERE user_id=?', (win_uid,))

            bot.answer_callback_query(call.id, f"Ball played: {c1} vs {c2}")
            try:
                bot.edit_message_text(hc_status_text(game), call.message.chat.id, call.message.message_id, reply_markup=hc_keyboard(gid, game))
            except Exception:
                pass

# ---------------- ROYAL 3D CHESS MINI APP LAUNCHER (/chess) ----------------
@bot.message_handler(commands=['chess', '3dchess'])
def chess_cmd(m):
    base = get_public_webapp_base()
    if not base:
        return bot.reply_to(
            m,
            "⚠️ <b>3D Chess Mini App URL set nahi hai!</b>\n"
            "Render Environment mein <code>WEBAPP_URL</code> add karein."
        )
    bot_uname = ''
    try:
        bot_uname = bot.get_me().username
    except Exception:
        pass

    if not group(m):
        room_id = f"ch_{m.from_user.id}"
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton('♟️ Open Royal 3D Chess', web_app=types.WebAppInfo(url=f"{base}/chess3d?room={room_id}")))
        return bot.reply_to(
            m,
            "♟️ <b>MISHRA ROYAL 3D CHESS</b> ♟️\n"
            "Niche button dabakar 3D Chess Board open karein:",
            reply_markup=kb
        )

    room_id = f"ch_{abs(m.chat.id)}_{uuid.uuid4().hex[:4]}"
    kb = types.InlineKeyboardMarkup(row_width=1)
    if bot_uname:
        kb.add(types.InlineKeyboardButton('♟️ Play Royal 3D Chess (1v1 / vs AI)', url=f"https://t.me/{bot_uname}?startapp={room_id}"))
    else:
        kb.add(types.InlineKeyboardButton('♟️ Play Royal 3D Chess (1v1 / vs AI)', url=f"{base}/chess3d?room={room_id}"))

    bot.send_message(
        m.chat.id,
        f"♟️ <b>MISHRA ROYAL 3D CHESS ARENA</b> ♟️\n\n"
        f"🆔 <b>Room ID:</b> <code>{room_id}</code>\n"
        f"✨ <i>Sculpted 3D Pieces, Legal Move Highlights, Check/Checkmate & vs AI Mode!</i>\n"
        f"🏆 Winner ko milenge <b>+100 Coins</b>!\n\n"
        f"👇 <i>White aur Black players niche button dabakar 3D Board join karein:</i>",
        reply_markup=kb
    )

# ---------------- GROUP GAME 2: UPGRADED UNDERCOVER SPY GAME (/spy) ----------------
SPY_WORD_PAIRS = [
    ("Chai", "Coffee"),
    ("Virat Kohli", "Rohit Sharma"),
    ("Taj Mahal", "Red Fort"),
    ("Instagram", "Snapchat"),
    ("Samosa", "Kachori"),
    ("BGMI", "Free Fire"),
    ("Goa", "Manali"),
    ("Train", "Metro"),
    ("Pizza", "Burger"),
    ("Cinema", "Netflix"),
    ("Cricket", "Badminton"),
    ("Shaadi", "Birthday"),
    ("Hospital", "School"),
    ("Vrindavan", "Ayodhya"),
    ("iPhone", "Samsung"),
    ("Panipuri", "Dahi Bhalla"),
    ("Police", "Detective"),
    ("Ganga", "Yamuna"),
    ("Arijit Singh", "Jubin Nautiyal"),
    ("Shimla", "Mussoorie"),
]

SPY_GAMES = {}
SPY_LOCK = threading.Lock()

def spy_keyboard(gid, game):
    kb = types.InlineKeyboardMarkup(row_width=2)
    if game['phase'] == 'lobby':
        if len(game['players']) < 10:
            kb.add(types.InlineKeyboardButton('🕵️ Join Spy Game', callback_data=f'spy:{gid}:join'))
        if len(game['players']) >= 3:
            kb.add(types.InlineKeyboardButton(f"🚀 Start Game (Host Only • {len(game['players'])}P)", callback_data=f'spy:{gid}:start'))
    elif game['phase'] == 'clue':
        kb.add(types.InlineKeyboardButton('👁️ Apna Secret Word Dekhein', callback_data=f'spy:{gid}:word'))
        kb.add(types.InlineKeyboardButton('⏭️ Skip Turn (Host)', callback_data=f'spy:{gid}:skip'))
    elif game['phase'] == 'vote':
        kb.add(types.InlineKeyboardButton('👁️ Apna Secret Word Dekhein', callback_data=f'spy:{gid}:word'))
        for uid, pinfo in game['players'].items():
            v_cnt = sum(1 for target_u in game['votes'].values() if target_u == uid)
            kb.add(types.InlineKeyboardButton(f"🎯 Vote: {pinfo['name'][:16]} ({v_cnt})", callback_data=f'spy:{gid}:vote:{uid}'))
    return kb

def spy_text(game):
    host_name = game['players'].get(game['host_id'], {}).get('name', 'Host')
    order = game.get('turn_order') or list(game['players'].keys())

    if game['phase'] == 'lobby':
        plist = "\n".join(
            f"  • <a href=\"tg://user?id={uid}\">{esc(p['name'])}</a>" + (" 👑 (Host)" if uid == game['host_id'] else "")
            for uid, p in game['players'].items()
        )
        return (
            f"🕵️ <b>UNDERCOVER SPY GAME</b> 🕵️\n\n"
            f"👑 <b>Host:</b> {esc(host_name)} <i>(Sirf Host hi Start kar sakta hai)</i>\n"
            f"👥 <b>Joined Players ({len(game['players'])}/10):</b>\n{plist}\n\n"
            f"📜 <b>Kaise Khelenge:</b>\n"
            f"1. Sabhi Citizens ko ek <b>Secret Word</b> milega aur 1 <b>Spy</b> ko alag word milega.\n"
            f"2. Sabhi players <b>baari-baari group chat mein 1 hint/clue likhenge</b>.\n"
            f"3. Saare clues board par aane ke baad voting se asli Spy pakda jayega!\n\n"
            f"👇 <i>Kam se kam 3 players join karein:</i>"
        )
    elif game['phase'] == 'clue':
        cur_uid = order[game['clue_idx']]
        cur_name = game['players'][cur_uid]['name']
        clue_lines = []
        for idx, uid in enumerate(order):
            pname = game['players'][uid]['name']
            if uid in game['clues']:
                clue_lines.append(f"  ✅ <b>{esc(pname)}:</b> “{esc(game['clues'][uid])}”")
            elif idx == game['clue_idx']:
                clue_lines.append(f"  👉 <b>{esc(pname)}:</b> <i>(Chat mein clue likh rahe hain...)</i>")
            else:
                clue_lines.append(f"  ⏳ {esc(pname)}: <i>Waiting...</i>")
        board_txt = "\n".join(clue_lines)
        return (
            f"🕵️ <b>SPY GAME — CLUE ROUND ({len(game['clues'])}/{len(order)})</b>\n\n"
            f"📋 <b>Live Clue Board:</b>\n{board_txt}\n\n"
            f"1️⃣ Pehle niche <b>'👁️ Apna Secret Word Dekhein'</b> dabakar apna word dekhein.\n"
            f"2️⃣ 👉 <b>Ab <a href=\"tg://user?id={cur_uid}\">{esc(cur_name)}</a> ki baari hai!</b> Group chat mein apne word ka <b>1-line hint</b> likh kar bhejein <i>(seedha word mat likhna!)</i>"
        )
    elif game['phase'] == 'vote':
        clue_lines = [
            f"  • <b>{esc(game['players'][uid]['name'])}:</b> “{esc(game['clues'].get(uid, 'No clue'))}”"
            + (" ✅ (Voted)" if uid in game['votes'] else "")
            for uid in order
        ]
        return (
            f"🗳️ <b>VOTING TIME — CLUES PADHKAR SPY PAKDEIN!</b>\n\n"
            f"📋 <b>Sabhi Players Ke Clues:</b>\n" + "\n".join(clue_lines) + "\n\n"
            f"📊 <b>Votes ({len(game['votes'])}/{len(order)}):</b>\n"
            f"👇 <i>Jis par shaq hai uske naam par vote karein:</i>"
        )
    else:
        return game['result_text']

def check_spy_clue_message(m):
    if not m.text or m.text.startswith('/'):
        return False
    chat = m.chat.id
    uid = m.from_user.id
    hint = m.text.strip()
    if not hint:
        return False

    with SPY_LOCK:
        target_gid = None
        for gid, g in SPY_GAMES.items():
            if g['chat_id'] == chat and g['phase'] == 'clue':
                order = g['turn_order']
                if order and order[g['clue_idx']] == uid:
                    target_gid = gid
                    break
        if not target_gid:
            return False

        game = SPY_GAMES[target_gid]
        # Prevent player from accidentally typing the exact secret word in chat!
        low_hint = hint.lower()
        if game['citizen_word'].lower() in low_hint or game['spy_word'].lower() in low_hint:
            try:
                bot.delete_message(chat, m.message_id)
            except Exception:
                pass
            bot.send_message(chat, f"⚠️ <a href=\"tg://user?id={uid}\">{esc(user_name(m.from_user))}</a> seedha secret word mat likhein! Sirf <b>hint/clue</b> likhein.")
            return True

        game['clues'][uid] = hint[:70]
        game['ts'] = time.time()
        game['clue_idx'] += 1

        if game['clue_idx'] >= len(game['turn_order']):
            game['phase'] = 'vote'

        msg_id = game.get('msg_id')
        txt = spy_text(game)
        kb = spy_keyboard(target_gid, game)

    if msg_id:
        try:
            bot.edit_message_text(txt, chat, msg_id, reply_markup=kb)
            return True
        except Exception:
            pass
    sent = bot.send_message(chat, txt, reply_markup=kb)
    with SPY_LOCK:
        if target_gid in SPY_GAMES:
            SPY_GAMES[target_gid]['msg_id'] = sent.message_id
    return True

@bot.message_handler(commands=['spy', 'undercover'])
def spy_cmd(m):
    if not group(m):
        return bot.reply_to(m, 'Use /spy in a group with 3+ friends!')
    gid = uuid.uuid4().hex[:8]
    game = {
        'chat_id': m.chat.id,
        'host_id': m.from_user.id,
        'msg_id': None,
        'phase': 'lobby',
        'players': {m.from_user.id: {'name': user_name(m.from_user)}},
        'turn_order': [],
        'clue_idx': 0,
        'clues': {},
        'spy_id': None,
        'citizen_word': '',
        'spy_word': '',
        'votes': {},
        'result_text': '',
        'ts': time.time()
    }
    with SPY_LOCK:
        now = time.time()
        for k in list(SPY_GAMES.keys()):
            if now - SPY_GAMES[k]['ts'] > 3600:
                SPY_GAMES.pop(k, None)
        SPY_GAMES[gid] = game

    sent = bot.send_message(m.chat.id, spy_text(game), reply_markup=spy_keyboard(gid, game))
    with SPY_LOCK:
        if gid in SPY_GAMES:
            SPY_GAMES[gid]['msg_id'] = sent.message_id

@bot.callback_query_handler(func=lambda call: bool(call.data) and call.data.startswith('spy:'))
def spy_callback(call):
    parts = call.data.split(':')
    if len(parts) < 3:
        return
    gid = parts[1]
    action = parts[2]
    uid = call.from_user.id
    uname = user_name(call.from_user)

    with SPY_LOCK:
        game = SPY_GAMES.get(gid)
        if not game:
            return bot.answer_callback_query(call.id, '⚠️ Yeh Spy game expire ho chuka hai. /spy se naya start karein.', show_alert=True)
        if game['phase'] == 'over':
            return bot.answer_callback_query(call.id, '🏁 Yeh game khatam ho chuka hai!')

        game['ts'] = time.time()
        game['msg_id'] = call.message.message_id

        if action == 'join':
            if game['phase'] != 'lobby':
                return bot.answer_callback_query(call.id, 'Game pehle hi start ho chuka hai!', show_alert=True)
            if uid in game['players']:
                return bot.answer_callback_query(call.id, 'Aap pehle se join hain!', show_alert=True)
            if len(game['players']) >= 10:
                return bot.answer_callback_query(call.id, 'Max 10 players allowed!', show_alert=True)
            game['players'][uid] = {'name': uname}
            bot.answer_callback_query(call.id, '🕵️ Aap Spy Game mein join ho gaye!')
            try:
                bot.edit_message_text(spy_text(game), call.message.chat.id, call.message.message_id, reply_markup=spy_keyboard(gid, game))
            except Exception:
                pass
            return

        if action == 'start':
            if game['phase'] != 'lobby':
                return bot.answer_callback_query(call.id, 'Already started!')
            # STRICT HOST-ONLY START LOCK
            if uid != game['host_id']:
                host_n = game['players'].get(game['host_id'], {}).get('name', 'Host')
                return bot.answer_callback_query(call.id, f"⛔ Sirf Game Host ({host_n}) hi game start kar sakte hain!", show_alert=True)
            if len(game['players']) < 3:
                return bot.answer_callback_query(call.id, 'Kam se kam 3 players chahiye!', show_alert=True)

            c_word, s_word = random.choice(SPY_WORD_PAIRS)
            if random.random() < 0.5:
                c_word, s_word = s_word, c_word
            game['citizen_word'] = c_word
            game['spy_word'] = s_word
            order = list(game['players'].keys())
            random.shuffle(order)
            game['turn_order'] = order
            game['clue_idx'] = 0
            game['clues'] = {}
            game['spy_id'] = random.choice(order)
            game['phase'] = 'clue'
            bot.answer_callback_query(call.id, '🕵️ Game Started! Sab apna word dekhein aur clue dein.')
            try:
                bot.edit_message_text(spy_text(game), call.message.chat.id, call.message.message_id, reply_markup=spy_keyboard(gid, game))
            except Exception:
                pass
            return

        if action == 'skip':
            if game['phase'] != 'clue':
                return bot.answer_callback_query(call.id, 'Abhi clue phase nahi hai!')
            if uid != game['host_id']:
                return bot.answer_callback_query(call.id, '⛔ Sirf Host hi inactive player ki turn skip kar sakte hain!', show_alert=True)
            cur_u = game['turn_order'][game['clue_idx']]
            game['clues'][cur_u] = '(Skipped)'
            game['clue_idx'] += 1
            if game['clue_idx'] >= len(game['turn_order']):
                game['phase'] = 'vote'
            bot.answer_callback_query(call.id, '⏭️ Turn skipped!')
            try:
                bot.edit_message_text(spy_text(game), call.message.chat.id, call.message.message_id, reply_markup=spy_keyboard(gid, game))
            except Exception:
                pass
            return

        if action == 'word':
            if uid not in game['players']:
                return bot.answer_callback_query(call.id, '⛔ Aap is game mein nahi hain!', show_alert=True)
            if uid == game['spy_id']:
                return bot.answer_callback_query(
                    call.id,
                    f"🕵️ AAP SPY (UNDERCOVER) HAIN!\nAapka Word: {game['spy_word']}\n(Sabke clues padh kar samajhdari se hint dein!)",
                    show_alert=True
                )
            return bot.answer_callback_query(
                call.id,
                f"👤 AAP CITIZEN HAIN!\nSecret Word: {game['citizen_word']}\n(Aisa clue dein jo sirf Citizens samjhein!)",
                show_alert=True
            )

        if action == 'vote' and len(parts) == 4:
            if game['phase'] != 'vote':
                return bot.answer_callback_query(call.id, 'Abhi voting phase nahi hai!')
            if uid not in game['players']:
                return bot.answer_callback_query(call.id, '⛔ Aap is game ke player nahi hain!', show_alert=True)
            target_uid = int(parts[3])
            if target_uid == uid:
                return bot.answer_callback_query(call.id, 'Khud ko vote nahi de sakte!', show_alert=True)
            game['votes'][uid] = target_uid

            if len(game['votes']) >= len(game['players']):
                tally = defaultdict(int)
                for voted_u in game['votes'].values():
                    tally[voted_u] += 1
                top_voted_uid = max(tally.items(), key=lambda x: x[1])[0]
                spy_uid = game['spy_id']
                spy_name = game['players'][spy_uid]['name']
                voted_name = game['players'][top_voted_uid]['name']
                game['phase'] = 'over'

                if top_voted_uid == spy_uid:
                    with DB_LOCK, connect() as c:
                        for p_u in game['players']:
                            if p_u != spy_uid:
                                c.execute('INSERT OR IGNORE INTO wallet(user_id,coins,last_daily) VALUES(?,0,?)', (p_u, ''))
                                c.execute('UPDATE wallet SET coins=coins+50 WHERE user_id=?', (p_u,))
                    game['result_text'] = (
                        f"🎉 <b>CITIZENS JEET GAYE! SPY PAKDA GAYA!</b> 🎉\n\n"
                        f"🕵️ Asli Spy tha: <a href=\"tg://user?id={spy_uid}\">{esc(spy_name)}</a>\n"
                        f"📌 Citizen Word: <b>{esc(game['citizen_word'])}</b> | Spy Word: <b>{esc(game['spy_word'])}</b>\n"
                        f"🪙 Sabhi Citizens ko <b>+50 Coins</b> mile!"
                    )
                else:
                    with DB_LOCK, connect() as c:
                        c.execute('INSERT OR IGNORE INTO wallet(user_id,coins,last_daily) VALUES(?,0,?)', (spy_uid, ''))
                        c.execute('UPDATE wallet SET coins=coins+80 WHERE user_id=?', (spy_uid,))
                    game['result_text'] = (
                        f"😈 <b>SPY JEET GAYA! BACH NIKLA!</b> 😈\n\n"
                        f"❌ Sabne <a href=\"tg://user?id={top_voted_uid}\">{esc(voted_name)}</a> ko vote kiya, jabki...\n"
                        f"🕵️ Asli Spy tha: <a href=\"tg://user?id={spy_uid}\">{esc(spy_name)}</a>!\n"
                        f"📌 Citizen Word: <b>{esc(game['citizen_word'])}</b> | Spy Word: <b>{esc(game['spy_word'])}</b>\n"
                        f"🪙 Spy ko mile <b>+80 Coins</b>!"
                    )

            bot.answer_callback_query(call.id, '🗳️ Vote recorded!')
            try:
                bot.edit_message_text(spy_text(game), call.message.chat.id, call.message.message_id, reply_markup=spy_keyboard(gid, game))
            except Exception:
                pass

# ---------------- GROUP GAME 3: WORD CHAIN (/wordchain & /wc) ----------------
WC_GAMES = {}
WC_LOCK = threading.Lock()
WC_STARTERS = ["APPLE", "TIGER", "INDIA", "MUSIC", "OCEAN", "PLANET", "ROCKET", "FRIEND", "DRAGON", "CASTLE"]

def wc_keyboard(gid, game):
    kb = types.InlineKeyboardMarkup(row_width=2)
    if game['phase'] == 'lobby':
        if len(game['players']) < 12:
            kb.add(types.InlineKeyboardButton('🔤 Join Word Chain', callback_data=f'wc:{gid}:join'))
        if len(game['players']) >= 2:
            kb.add(types.InlineKeyboardButton(f"🚀 Start (Host • {len(game['players'])}P)", callback_data=f'wc:{gid}:start'))
    elif game['phase'] == 'play':
        kb.add(types.InlineKeyboardButton('⏱️ Time Out / Eliminate Turn (Host)', callback_data=f'wc:{gid}:timeout'))
    return kb

def wc_text(game):
    if game['phase'] == 'lobby':
        plist = "\n".join(
            f"  • <a href=\"tg://user?id={uid}\">{esc(name)}</a>" + (" 👑" if uid == game['host_id'] else "")
            for uid, name in game['players'].items()
        )
        return (
            f"🔤 <b>WORD CHAIN ELIMINATION</b> 🔤\n\n"
            f"👥 <b>Joined Players ({len(game['players'])}):</b>\n{plist}\n\n"
            f"📜 <b>Rules:</b> Pichhle word ke <b>aakhri letter</b> se naya English word (min 3 letters, no repeat) chat mein likhein!\n"
            f"👇 <i>2+ players join karein aur Host Start dabayein:</i>"
        )
    elif game['phase'] == 'play':
        cur_uid = game['alive'][game['turn_idx']]
        cur_name = game['players'][cur_uid]
        req_char = game['current_word'][-1].upper()
        alive_str = ", ".join(esc(game['players'][u]) for u in game['alive'])
        return (
            f"🔤 <b>WORD CHAIN LIVE!</b>\n\n"
            f"📌 Current Word: <b>{esc(game['current_word'])}</b>\n"
            f"🎯 Required Starting Letter: <b>[ {req_char} ]</b>\n"
            f"🏃 Alive ({len(game['alive'])}): {alive_str}\n\n"
            f"👉 <b>Ab <a href=\"tg://user?id={cur_uid}\">{esc(cur_name)}</a> ki baari hai!</b>\n"
            f"Chat mein <b>{req_char}</b> se shuru hone wala word likhein:"
        )
    return game['result_text']

def check_wordchain_message(m):
    if not m.text or m.text.startswith('/'):
        return False
    chat = m.chat.id
    uid = m.from_user.id
    word = re.sub(r'[^A-Za-z]', '', m.text.strip()).upper()
    if len(word) < 3:
        return False

    with WC_LOCK:
        target_gid = None
        for gid, g in WC_GAMES.items():
            if g['chat_id'] == chat and g['phase'] == 'play':
                if g['alive'] and g['alive'][g['turn_idx']] == uid:
                    target_gid = gid
                    break
        if not target_gid:
            return False

        game = WC_GAMES[target_gid]
        req_char = game['current_word'][-1].upper()
        if not word.startswith(req_char):
            bot.reply_to(m, f"❌ Word <b>{req_char}</b> se shuru hona chahiye!")
            return True
        if word in game['used']:
            bot.reply_to(m, f"⚠️ <b>{word}</b> pehle hi use ho chuka hai! Koi aur word likhein.")
            return True

        game['used'].add(word)
        game['current_word'] = word
        game['turn_idx'] = (game['turn_idx'] + 1) % len(game['alive'])
        game['ts'] = time.time()
        msg_id = game.get('msg_id')
        txt = wc_text(game)
        kb = wc_keyboard(target_gid, game)

    if msg_id:
        try:
            bot.edit_message_text(txt, chat, msg_id, reply_markup=kb)
            return True
        except Exception:
            pass
    sent = bot.send_message(chat, txt, reply_markup=kb)
    with WC_LOCK:
        if target_gid in WC_GAMES:
            WC_GAMES[target_gid]['msg_id'] = sent.message_id
    return True

@bot.message_handler(commands=['wordchain', 'wc'])
def wordchain_cmd(m):
    if not group(m):
        return bot.reply_to(m, 'Use /wordchain in a group!')
    gid = uuid.uuid4().hex[:8]
    game = {
        'chat_id': m.chat.id,
        'host_id': m.from_user.id,
        'msg_id': None,
        'phase': 'lobby',
        'players': {m.from_user.id: user_name(m.from_user)},
        'alive': [],
        'turn_idx': 0,
        'current_word': random.choice(WC_STARTERS),
        'used': set(),
        'result_text': '',
        'ts': time.time()
    }
    with WC_LOCK:
        WC_GAMES[gid] = game
    sent = bot.send_message(m.chat.id, wc_text(game), reply_markup=wc_keyboard(gid, game))
    with WC_LOCK:
        WC_GAMES[gid]['msg_id'] = sent.message_id

@bot.callback_query_handler(func=lambda call: bool(call.data) and call.data.startswith('wc:'))
def wc_callback(call):
    parts = call.data.split(':')
    if len(parts) < 3:
        return
    gid, action = parts[1], parts[2]
    uid = call.from_user.id
    uname = user_name(call.from_user)

    with WC_LOCK:
        game = WC_GAMES.get(gid)
        if not game:
            return bot.answer_callback_query(call.id, '⚠️ Game expire ho chuka hai.', show_alert=True)
        game['msg_id'] = call.message.message_id
        if action == 'join':
            if game['phase'] != 'lobby':
                return bot.answer_callback_query(call.id, 'Game pehle hi shuru ho chuka hai!')
            if uid in game['players']:
                return bot.answer_callback_query(call.id, 'Aap pehle se join hain!')
            game['players'][uid] = uname
            bot.answer_callback_query(call.id, '✅ Joined Word Chain!')
        elif action == 'start':
            if uid != game['host_id']:
                return bot.answer_callback_query(call.id, '⛔ Sirf Host hi game start kar sakte hain!', show_alert=True)
            if len(game['players']) < 2:
                return bot.answer_callback_query(call.id, 'Kam se kam 2 players chahiye!', show_alert=True)
            game['alive'] = list(game['players'].keys())
            random.shuffle(game['alive'])
            game['used'].add(game['current_word'])
            game['phase'] = 'play'
            bot.answer_callback_query(call.id, '🔤 Word Chain Started!')
        elif action == 'timeout':
            if game['phase'] != 'play':
                return
            if uid != game['host_id']:
                return bot.answer_callback_query(call.id, '⛔ Sirf Host hi eliminate kar sakte hain!', show_alert=True)
            out_u = game['alive'].pop(game['turn_idx'])
            if len(game['alive']) == 1:
                win_u = game['alive'][0]
                win_n = game['players'][win_u]
                game['phase'] = 'over'
                game['result_text'] = f"🏆 <b>WORD CHAIN CHAMPION:</b> <a href=\"tg://user?id={win_u}\">{esc(win_n)}</a> (+60 Coins) 🎉"
                with DB_LOCK, connect() as c:
                    c.execute('INSERT OR IGNORE INTO wallet(user_id,coins,last_daily) VALUES(?,0,?)', (win_u, ''))
                    c.execute('UPDATE wallet SET coins=coins+60 WHERE user_id=?', (win_u,))
            else:
                game['turn_idx'] = game['turn_idx'] % len(game['alive'])
            bot.answer_callback_query(call.id, f"❌ {game['players'][out_u]} eliminated!")

        try:
            bot.edit_message_text(wc_text(game), call.message.chat.id, call.message.message_id, reply_markup=wc_keyboard(gid, game))
        except Exception:
            pass

# ---------------- GROUP GAME 4: ROCK-PAPER-SCISSORS (/rps) ----------------
RPS_GAMES = {}
RPS_LOCK = threading.Lock()
RPS_ICONS = {'r': '🪨 Stone', 'p': '📄 Paper', 's': '✂️ Scissors'}

def rps_keyboard(gid, game):
    kb = types.InlineKeyboardMarkup(row_width=3)
    if game['over']:
        return kb
    if game['p2_id'] is None:
        kb.add(types.InlineKeyboardButton('🎮 Join RPS Duel', callback_data=f'rps:{gid}:join'))
        return kb
    kb.row(
        types.InlineKeyboardButton('🪨 Stone', callback_data=f'rps:{gid}:pick:r'),
        types.InlineKeyboardButton('📄 Paper', callback_data=f'rps:{gid}:pick:p'),
        types.InlineKeyboardButton('✂️ Scissors', callback_data=f'rps:{gid}:pick:s'),
    )
    return kb

def rps_text(game):
    p1 = f"<a href=\"tg://user?id={game['p1_id']}\">{esc(game['p1_name'])}</a>"
    p2 = f"<a href=\"tg://user?id={game['p2_id']}\">{esc(game['p2_name'])}</a>" if game['p2_id'] else "<i>Waiting for Player 2...</i>"
    if game['p2_id'] is None:
        return f"🪨📄✂️ <b>ROCK-PAPER-SCISSORS DUEL</b>\n\n👤 Player 1: {p1}\n👤 Player 2: {p2}\n\n👇 <i>Join karne ke liye button dabayein:</i>"
    if not game['over']:
        s1 = "✅ Ready" if game['c1'] else "⏳ Choosing..."
        s2 = "✅ Ready" if game['c2'] else "⏳ Choosing..."
        return f"🪨📄✂️ <b>ROCK-PAPER-SCISSORS</b>\n\n👤 {p1}: {s1}\n👤 {p2}: {s2}\n\n👇 <i>Dono players chupke se apna button dabayein:</i>"
    return game['result_text']

@bot.message_handler(commands=['rps'])
def rps_cmd(m):
    if not group(m):
        return bot.reply_to(m, 'Use /rps in a group!')
    gid = uuid.uuid4().hex[:8]
    p2_id, p2_name = None, None
    if m.reply_to_message and m.reply_to_message.from_user and not m.reply_to_message.from_user.is_bot:
        if m.reply_to_message.from_user.id != m.from_user.id:
            p2_id = m.reply_to_message.from_user.id
            p2_name = user_name(m.reply_to_message.from_user)
    game = {
        'p1_id': m.from_user.id, 'p1_name': user_name(m.from_user),
        'p2_id': p2_id, 'p2_name': p2_name,
        'c1': None, 'c2': None, 'over': False, 'result_text': ''
    }
    with RPS_LOCK:
        RPS_GAMES[gid] = game
    bot.send_message(m.chat.id, rps_text(game), reply_markup=rps_keyboard(gid, game))

@bot.callback_query_handler(func=lambda call: bool(call.data) and call.data.startswith('rps:'))
def rps_callback(call):
    parts = call.data.split(':')
    gid, action = parts[1], parts[2]
    uid = call.from_user.id
    uname = user_name(call.from_user)

    with RPS_LOCK:
        game = RPS_GAMES.get(gid)
        if not game or game['over']:
            return bot.answer_callback_query(call.id, 'Game khatam ya expire ho gaya.')
        if action == 'join':
            if uid == game['p1_id'] or game['p2_id'] is not None:
                return bot.answer_callback_query(call.id, 'Join nahi kar sakte!')
            game['p2_id'] = uid
            game['p2_name'] = uname
            bot.answer_callback_query(call.id, '✅ Joined!')
        elif action == 'pick' and len(parts) == 4:
            if uid not in (game['p1_id'], game['p2_id']):
                return bot.answer_callback_query(call.id, '⛔ Aap player nahi hain!', show_alert=True)
            ch = parts[3]
            if uid == game['p1_id']:
                game['c1'] = ch
            else:
                game['c2'] = ch
            bot.answer_callback_query(call.id, f"✅ {RPS_ICONS[ch]} locked!")

            if game['c1'] and game['c2']:
                game['over'] = True
                c1, c2 = game['c1'], game['c2']
                p1 = f"<a href=\"tg://user?id={game['p1_id']}\">{esc(game['p1_name'])}</a>"
                p2 = f"<a href=\"tg://user?id={game['p2_id']}\">{esc(game['p2_name'])}</a>"
                header = f"🪨📄✂️ <b>RPS RESULT</b>\n\n{p1}: {RPS_ICONS[c1]}\n{p2}: {RPS_ICONS[c2]}\n\n"
                if c1 == c2:
                    game['result_text'] = header + "🤝 <b>MATCH DRAW!</b>"
                elif (c1 == 'r' and c2 == 's') or (c1 == 'p' and c2 == 'r') or (c1 == 's' and c2 == 'p'):
                    game['result_text'] = header + f"🏆 <b>Winner:</b> {p1} (+30 Coins) 🎉"
                    with DB_LOCK, connect() as c:
                        c.execute('INSERT OR IGNORE INTO wallet(user_id,coins,last_daily) VALUES(?,0,?)', (game['p1_id'], ''))
                        c.execute('UPDATE wallet SET coins=coins+30 WHERE user_id=?', (game['p1_id'],))
                else:
                    game['result_text'] = header + f"🏆 <b>Winner:</b> {p2} (+30 Coins) 🎉"
                    with DB_LOCK, connect() as c:
                        c.execute('INSERT OR IGNORE INTO wallet(user_id,coins,last_daily) VALUES(?,0,?)', (game['p2_id'], ''))
                        c.execute('UPDATE wallet SET coins=coins+30 WHERE user_id=?', (game['p2_id'],))

        try:
            bot.edit_message_text(rps_text(game), call.message.chat.id, call.message.message_id, reply_markup=rps_keyboard(gid, game))
        except Exception:
            pass

# ---------------- GROUP GAME 5: FAST TYPING RACE (/fasttype & /race) ----------------
RACE_SENTENCES = [
    "Mishra Robot sabse tez aur smart telegram bot hai",
    "Radhe Krishna ki kripa se sabhi kaam safal hote hain",
    "Practice makes a man perfect in every situation",
    "Chandu ke chacha ne chandu ki chachi ko chatni chatayi",
    "Success comes to those who work hard every single day",
    "Vrindavan ki galiyon mein radha naam ki goonj hai"
]
ACTIVE_RACES = {}
RACE_LOCK = threading.Lock()

@bot.message_handler(commands=['fasttype', 'race'])
def fasttype_cmd(m):
    if not group(m):
        return bot.reply_to(m, 'Use /fasttype in a group!')
    target = random.choice(RACE_SENTENCES)
    # Insert zero-width spaces so copy-paste doesn't match cleanly
    anti_copy = "\u200b".join(list(target))
    with RACE_LOCK:
        ACTIVE_RACES[m.chat.id] = {'target': target.lower(), 'orig': target, 'ts': time.time()}
    bot.send_message(
        m.chat.id,
        f"⚡ <b>FAST TYPING RACE!</b> ⚡\n\n"
        f"Jo sabse pehle bina spelling mistake ke niche likha sentence type karega use <b>+40 Coins</b> milenge:\n\n"
        f"📝 <code>{esc(anti_copy)}</code>"
    )

def check_fasttype_message(m):
    if not m.text or m.text.startswith('/'):
        return False
    chat = m.chat.id
    guess = m.text.replace('\u200b', '').strip().lower()
    with RACE_LOCK:
        race = ACTIVE_RACES.get(chat)
        if not race or guess != race['target']:
            return False
        ACTIVE_RACES.pop(chat, None)
        elapsed = round(time.time() - race['ts'], 2)

    uid = m.from_user.id
    with DB_LOCK, connect() as c:
        c.execute('INSERT OR IGNORE INTO wallet(user_id,coins,last_daily) VALUES(?,0,?)', (uid, ''))
        c.execute('UPDATE wallet SET coins=coins+40 WHERE user_id=?', (uid,))
    bot.reply_to(
        m,
        f"🏆 <b>TYPING RACE WINNER!</b>\n"
        f"⚡ Champion: <a href=\"tg://user?id={uid}\">{esc(user_name(m.from_user))}</a>\n"
        f"⏱️ Time: <b>{elapsed} seconds</b>\n"
        f"🪙 Reward: <b>+40 Coins</b>!"
    )
    return True

# ---------------- GROUP GAME 6: SPIN-THE-BOTTLE TRUTH & DARE WHEEL (/tod) ----------------
TOD_WHEELS = {}
TOD_LOCK = threading.Lock()

def tod_keyboard(gid, wheel):
    kb = types.InlineKeyboardMarkup(row_width=2)
    if wheel['chosen_uid'] is None:
        kb.add(types.InlineKeyboardButton('🍾 Join Bottle Circle', callback_data=f'tod:{gid}:join'))
        if len(wheel['players']) >= 2:
            kb.add(types.InlineKeyboardButton(f"🔄 Spin Bottle ({len(wheel['players'])}P)", callback_data=f'tod:{gid}:spin'))
    else:
        kb.row(
            types.InlineKeyboardButton('😇 Truth', callback_data=f'tod:{gid}:truth'),
            types.InlineKeyboardButton('🔥 Dare', callback_data=f'tod:{gid}:dare')
        )
        kb.add(types.InlineKeyboardButton('🔄 Spin Again', callback_data=f'tod:{gid}:spin'))
    return kb

def tod_text(wheel):
    plist = ", ".join(f"<a href=\"tg://user?id={u}\">{esc(n)}</a>" for u, n in wheel['players'].items())
    if wheel['chosen_uid'] is None:
        return (
            f"🍾 <b>SPIN THE BOTTLE — TRUTH OR DARE WHEEL</b> 🍾\n\n"
            f"👥 <b>Players ({len(wheel['players'])}):</b> {plist}\n\n"
            f"👇 <i>Circle join karein aur Spin Bottle dabayein:</i>"
        )
    cu = wheel['chosen_uid']
    cn = wheel['players'].get(cu, 'Player')
    if not wheel['task']:
        return (
            f"🍾 <b>BOTTLE RUK GAYI HAI!</b> 🍾\n\n"
            f"🎯 Target: <a href=\"tg://user?id={cu}\">{esc(cn)}</a>\n"
            f"👇 <i>Niche <b>Truth</b> ya <b>Dare</b> chunein:</i>"
        )
    return (
        f"🎲 <b>{wheel['kind']} FOR <a href=\"tg://user?id={cu}\">{esc(cn)}</a>:</b>\n\n"
        f"👉 <b>{esc(wheel['task'])}</b>"
    )

@bot.message_handler(commands=['tod', 'spin'])
def tod_cmd(m):
    if not group(m):
        return bot.reply_to(m, 'Use /tod in a group!')
    gid = uuid.uuid4().hex[:8]
    wheel = {'players': {m.from_user.id: user_name(m.from_user)}, 'chosen_uid': None, 'kind': '', 'task': ''}
    with TOD_LOCK:
        TOD_WHEELS[gid] = wheel
    bot.send_message(m.chat.id, tod_text(wheel), reply_markup=tod_keyboard(gid, wheel))

@bot.callback_query_handler(func=lambda call: bool(call.data) and call.data.startswith('tod:'))
def tod_callback(call):
    parts = call.data.split(':')
    gid, action = parts[1], parts[2]
    uid = call.from_user.id
    uname = user_name(call.from_user)

    with TOD_LOCK:
        wheel = TOD_WHEELS.get(gid)
        if not wheel:
            return bot.answer_callback_query(call.id, '⚠️ Wheel expire ho gaya. /tod se naya banayein.')
        if action == 'join':
            wheel['players'][uid] = uname
            bot.answer_callback_query(call.id, '🍾 Circle joined!')
        elif action == 'spin':
            if len(wheel['players']) < 2:
                return bot.answer_callback_query(call.id, 'Kam se kam 2 players chahiye!', show_alert=True)
            wheel['chosen_uid'] = random.choice(list(wheel['players'].keys()))
            wheel['kind'] = ''
            wheel['task'] = ''
            bot.answer_callback_query(call.id, '🍾 Bottle spinned!')
        elif action in ('truth', 'dare'):
            if uid != wheel['chosen_uid']:
                return bot.answer_callback_query(call.id, '⛔ Bottle aap par nahi ruki hai!', show_alert=True)
            wheel['kind'] = '😇 TRUTH' if action == 'truth' else '🔥 DARE'
            wheel['task'] = random.choice(TRUTHS if action == 'truth' else DARES)
            bot.answer_callback_query(call.id, f"{wheel['kind']} selected!")

        try:
            bot.edit_message_text(tod_text(wheel), call.message.chat.id, call.message.message_id, reply_markup=tod_keyboard(gid, wheel))
        except Exception:
            pass

# ---------------- GROUP MESSAGE MONITOR (LAST HANDLER) ----------------
@bot.message_handler(func=lambda m: True, content_types=['text','sticker','animation','photo','video','document','audio','voice','video_note','contact','location','venue','poll','dice'])
def monitor(m):
    if not group(m) or not m.from_user or m.from_user.is_bot: return
    chat, uid = m.chat.id, m.from_user.id
    record(m)

    # 1. Rose-bot style auto-delete locks & Group Protection Suite (Night Mode, Anti-NSFW, Anti-Abuse, etc.)
    if check_and_enforce_lock(m):
        return
    if check_group_protections(m):
        return

    body = (m.text or m.caption or '').strip()

    # 2. Check active group mini-game answers (Quiz, Spy Clues, Word Chain, Typing Race)
    if check_cricket_answer(m):
        return
    if check_spy_clue_message(m):
        return
    if check_wordchain_message(m):
        return
    if check_fasttype_message(m):
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

    # 4. Chatbot trigger: OFF by default per group; when ON in this group, replies using learned group text/stickers/GIFs
    if setting(chat, 'chatbot', 'off') == 'on' and not body.startswith('/'):
        replied_to_bot = False
        try:
            if m.reply_to_message and m.reply_to_message.from_user and m.reply_to_message.from_user.id == bot.get_me().id:
                replied_to_bot = True
        except Exception:
            pass
        if (body and BOT_NAME_RE.search(body)) or replied_to_bot:
            send_chatbot_reply(m, body or 'hi', strict_match_only=False)
        elif body and len(body) <= 60:
            # Also reply naturally when someone says a phrase the bot has learned from group chats!
            send_chatbot_reply(m, body, strict_match_only=True)

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
