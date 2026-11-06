"""MishraRobot V2.0 - Telegram group bot.
Required: BOT_TOKEN. Optional: OPENAI_API_KEY, OPENAI_MODEL, PORT, DATABASE_PATH.
Run exactly one polling instance per Telegram bot token.
"""
import os, io, re, time, random, sqlite3, threading, logging
from datetime import date
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import telebot
from telebot import types
from PIL import Image, ImageDraw, ImageFont

TOKEN = os.getenv('BOT_TOKEN', '').strip()
if not TOKEN:
    raise RuntimeError('BOT_TOKEN environment variable is missing')
PORT = int(os.getenv('PORT', '10000'))
DB_PATH = os.getenv('DATABASE_PATH', 'mishrarobot.db')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', '').strip()
OPENAI_MODEL = os.getenv('OPENAI_MODEL', 'gpt-4o-mini')
logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO').upper(), format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('MishraRobot')
bot = telebot.TeleBot(TOKEN, parse_mode='HTML', threaded=True)
DB_LOCK = threading.RLock()
FLOOD = defaultdict(deque)
URL_RE = re.compile(r'(https?://|www\.|t\.me/|telegram\.me/)', re.I)
MAX_PURGE = 100

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
        CREATE TABLE IF NOT EXISTS afk(chat_id INTEGER,user_id INTEGER,reason TEXT,since REAL,PRIMARY KEY(chat_id,user_id));
        CREATE TABLE IF NOT EXISTS ships(chat_id INTEGER,day TEXT,user1 INTEGER,user2 INTEGER,name1 TEXT,name2 TEXT,pct INTEGER,PRIMARY KEY(chat_id,day));
        ''')

def setting(chat, key, default='off'):
    with DB_LOCK, connect() as c:
        r = c.execute('SELECT value FROM settings WHERE chat_id=? AND key=?',(chat,key)).fetchone()
    return r['value'] if r else default

def set_setting(chat,key,value):
    with DB_LOCK, connect() as c:
        c.execute('INSERT INTO settings VALUES(?,?,?) ON CONFLICT(chat_id,key) DO UPDATE SET value=excluded.value',(chat,key,str(value)))

def esc(s):
    return str(s or '').replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')

def group(m): return m.chat.type in ('group','supergroup')
def admin(chat,uid):
    try: return bot.get_chat_member(chat,uid).status in ('creator','administrator')
    except Exception: return False

def need_admin(m):
    if not admin(m.chat.id,m.from_user.id):
        bot.reply_to(m,'⛔ Only group admins can use this command.'); return False
    return True

def log_action(chat,text):
    if setting(chat,'adminlog') == 'on':
        try: bot.send_message(chat,'🧾 <b>Admin log</b>\n'+text)
        except Exception: log.exception('Admin log failed')

def done(m,text):
    bot.reply_to(m,text); log_action(m.chat.id,text)

def user_name(u): return u.first_name or u.username or str(u.id)

def record(m):
    u=m.from_user
    if not u or u.is_bot: return
    with DB_LOCK, connect() as c:
        c.execute('''INSERT INTO stats VALUES(?,?,?,?,1,datetime('now'))
        ON CONFLICT(chat_id,user_id) DO UPDATE SET name=excluded.name,username=excluded.username,count=stats.count+1,last_active=excluded.last_active''',
        (m.chat.id,u.id,user_name(u),u.username))

def warn_count(chat,uid):
    with DB_LOCK,connect() as c:
        r=c.execute('SELECT count FROM warnings WHERE chat_id=? AND user_id=?',(chat,uid)).fetchone()
    return r['count'] if r else 0

def set_warn(chat,uid,n):
    with DB_LOCK,connect() as c:
        c.execute('INSERT INTO warnings VALUES(?,?,?) ON CONFLICT(chat_id,user_id) DO UPDATE SET count=excluded.count',(chat,uid,max(0,n)))

def target_id(m):
    if m.reply_to_message and m.reply_to_message.from_user: return m.reply_to_message.from_user.id
    p=(m.text or '').split()
    if len(p)<2: return None
    if p[1].isdigit(): return int(p[1])
    if p[1].startswith('@'):
        with DB_LOCK,connect() as c:
            r=c.execute('SELECT user_id FROM stats WHERE chat_id=? AND lower(username)=?',(m.chat.id,p[1][1:].lower())).fetchone()
        return r['user_id'] if r else None
    return None

def filters(chat):
    with DB_LOCK,connect() as c: return c.execute('SELECT keyword,response FROM filters WHERE chat_id=?',(chat,)).fetchall()

# ---------------- HEALTH SERVER ----------------
class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        body=b'MishraRobot is running'
        self.send_response(200); self.send_header('Content-Type','text/plain'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_HEAD(self): self.send_response(200); self.end_headers()
    def log_message(self,*args): pass

def health_server():
    ThreadingHTTPServer(('0.0.0.0',PORT),Health).serve_forever()

# ---------------- HELP ----------------
HELP='''🌸 <b>MISHRA ROBOT V2.0</b>

<b>Stats & Fun</b>
/top /ranking — leaderboard
/stats /mystats — your stats
/profile — profile image
/ship — daily couple
/challenge — random challenge
/afk [reason] /back — AFK

<b>Moderation (admins)</b>
/ban /unban /kick /mute /unmute
/warn /unwarn (3 warnings = ban)
/pin /unpin /del /purge
/lock /unlock
/antispam on|off /antilink on|off /adminlog on|off

<b>Group settings (admins)</b>
/filter WORD response /filters /stopfilter WORD
/setwelcome TEXT /welcome on|off
/setgoodbye TEXT /goodbye on|off

<b>Media & mentions</b>
/mmf TOP ; BOTTOM — reply to photo/static sticker
/kang — reply to sticker
@admin [reason] / @all [message] (admins only for @all)

<b>Optional AI</b>
/ai question — requires OPENAI_API_KEY'''
@bot.message_handler(commands=['start','help','panel'])
def help_cmd(m): bot.send_message(m.chat.id,HELP)

# ---------------- STATS & FUN ----------------
@bot.message_handler(commands=['top','ranking'])
def top(m):
    with DB_LOCK,connect() as c: rows=c.execute('SELECT name,count FROM stats WHERE chat_id=? ORDER BY count DESC LIMIT 10',(m.chat.id,)).fetchall()
    if not rows: return bot.reply_to(m,'No ranking data yet. Start chatting!')
    lines=['🏆 <b>Group leaderboard</b>']
    for i,r in enumerate(rows,1): lines.append(f"{('🥇','🥈','🥉')[i-1] if i<4 else str(i)+'.'} {esc(r['name'])} — <b>{r['count']}</b>")
    bot.send_message(m.chat.id,'\n'.join(lines))

@bot.message_handler(commands=['stats','mystats'])
def stats(m):
    with DB_LOCK,connect() as c: r=c.execute('SELECT count FROM stats WHERE chat_id=? AND user_id=?',(m.chat.id,m.from_user.id)).fetchone()
    bot.reply_to(m,f"📊 Messages: <b>{r['count'] if r else 0}</b>\n⚠️ Warnings: <b>{warn_count(m.chat.id,m.from_user.id)}/3</b>")

@bot.message_handler(commands=['profile'])
def profile(m):
    with DB_LOCK,connect() as c: r=c.execute('SELECT count FROM stats WHERE chat_id=? AND user_id=?',(m.chat.id,m.from_user.id)).fetchone()
    img=Image.new('RGB',(800,400),(28,35,58)); d=ImageDraw.Draw(img)
    try: big=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',42); mid=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',28)
    except OSError: big=mid=ImageFont.load_default()
    d.rounded_rectangle((20,20,780,380),radius=28,fill=(49,61,91),outline=(170,185,220),width=4)
    d.text((55,60),'MISHRA ROBOT PROFILE',font=mid,fill='white'); d.text((55,130),user_name(m.from_user)[:24],font=big,fill=(255,220,130))
    d.text((55,235),f"Messages: {r['count'] if r else 0}",font=mid,fill='white'); d.text((55,290),f"Warnings: {warn_count(m.chat.id,m.from_user.id)}/3",font=mid,fill='white')
    out=io.BytesIO(); out.name='profile.png'; img.save(out,'PNG'); out.seek(0); bot.send_photo(m.chat.id,out,reply_to_message_id=m.message_id)

@bot.message_handler(commands=['ship'])
def ship(m):
    today=date.today().isoformat()
    with DB_LOCK,connect() as c:
        old=c.execute('SELECT * FROM ships WHERE chat_id=? AND day=?',(m.chat.id,today)).fetchone()
        users=c.execute('SELECT user_id,name FROM stats WHERE chat_id=? ORDER BY count DESC LIMIT 200',(m.chat.id,)).fetchall()
    if old: return bot.send_message(m.chat.id,f"💖 Today's couple: <b>{esc(old['name1'])} ❤️ {esc(old['name2'])}</b>\nCompatibility: {old['pct']}%")
    users=[u for u in users if u['user_id']!=bot.get_me().id]
    if len(users)<2: return bot.reply_to(m,'Need at least two active members.')
    a,b=random.sample(users,2); pct=random.randint(30,100)
    with DB_LOCK,connect() as c: c.execute('INSERT INTO ships VALUES(?,?,?,?,?,?,?)',(m.chat.id,today,a['user_id'],b['user_id'],a['name'],b['name'],pct))
    bot.send_message(m.chat.id,f"💘 Today's match: <b>{esc(a['name'])} ❤️ {esc(b['name'])}</b>\nCompatibility: <b>{pct}%</b>\n<code>{'█'*(pct//10)}{'░'*(10-pct//10)}</code>")

@bot.message_handler(commands=['challenge'])
def challenge(m): bot.reply_to(m,'🎯 Challenge: '+random.choice(['Share a funny sticker!','Tell one interesting fact.','Compliment another member.','Share your favourite song.','Tell a clean joke!']))

# ---------------- AFK ----------------
@bot.message_handler(commands=['afk'])
def afk(m):
    reason=m.text.split(maxsplit=1)[1] if len(m.text.split(maxsplit=1))>1 else 'AFK'
    with DB_LOCK,connect() as c: c.execute('INSERT INTO afk VALUES(?,?,?,?) ON CONFLICT(chat_id,user_id) DO UPDATE SET reason=excluded.reason,since=excluded.since',(m.chat.id,m.from_user.id,reason[:250],time.time()))
    bot.reply_to(m,f"💤 {esc(user_name(m.from_user))} is AFK: {esc(reason[:250])}")
@bot.message_handler(commands=['back'])
def back(m):
    with DB_LOCK,connect() as c: cur=c.execute('DELETE FROM afk WHERE chat_id=? AND user_id=?',(m.chat.id,m.from_user.id))
    bot.reply_to(m,'👋 Welcome back!' if cur.rowcount else 'You were not marked AFK.')

# ---------------- SETTINGS / FILTERS ----------------
def toggle(m,key,label):
    if not need_admin(m): return
    p=(m.text or '').split()
    if len(p)<2 or p[1].lower() not in ('on','off'): return bot.reply_to(m,f'Usage: /{p[0].split("@")[0][1:]} on|off')
    set_setting(m.chat.id,key,p[1].lower()); bot.reply_to(m,f'✅ {label} {"enabled" if p[1].lower()=="on" else "disabled"}.')
@bot.message_handler(commands=['antispam'])
def anti(m): toggle(m,'antispam','Anti-spam')
@bot.message_handler(commands=['antilink'])
def links(m): toggle(m,'antilink','Anti-link')
@bot.message_handler(commands=['adminlog'])
def adminlog(m): toggle(m,'adminlog','Admin logs')
@bot.message_handler(commands=['welcome','goodbye'])
def welcome_toggle(m):
    key=m.text.split()[0].split('@')[0][1:]; toggle(m,key,key.capitalize())
@bot.message_handler(commands=['setwelcome','setgoodbye'])
def set_welcome(m):
    if not need_admin(m): return
    p=m.text.split(maxsplit=1)
    if len(p)<2: return bot.reply_to(m,'Provide a message. Use {name} and {group} placeholders.')
    key='welcome_text' if p[0].split('@')[0]=='/setwelcome' else 'goodbye_text'; set_setting(m.chat.id,key,p[1][:1000]); bot.reply_to(m,'✅ Group message saved.')
@bot.message_handler(content_types=['new_chat_members'])
def new_members(m):
    if setting(m.chat.id,'welcome','on')!='on': return
    template=setting(m.chat.id,'welcome_text','Welcome {name} to {group}! 🎉')
    for u in m.new_chat_members:
        if u.id!=bot.get_me().id: bot.send_message(m.chat.id,template.replace('{name}',esc(user_name(u))).replace('{group}',esc(m.chat.title or 'the group')))
@bot.message_handler(content_types=['left_chat_member'])
def left_member(m):
    if setting(m.chat.id,'goodbye')!='on' or not m.left_chat_member: return
    template=setting(m.chat.id,'goodbye_text','Goodbye {name}! 👋'); bot.send_message(m.chat.id,template.replace('{name}',esc(user_name(m.left_chat_member))))
@bot.message_handler(commands=['filter'])
def add_filter(m):
    if not need_admin(m): return
    p=m.text.split(maxsplit=2)
    if len(p)<3: return bot.reply_to(m,'Usage: /filter WORD response text')
    with DB_LOCK,connect() as c: c.execute('INSERT INTO filters VALUES(?,?,?) ON CONFLICT(chat_id,keyword) DO UPDATE SET response=excluded.response',(m.chat.id,p[1].lower()[:80],p[2][:1000]))
    bot.reply_to(m,'✅ Filter saved.')
@bot.message_handler(commands=['filters'])
def show_filters(m):
    rows=filters(m.chat.id); bot.send_message(m.chat.id,'📁 Filters:\n'+'\n'.join('• <code>'+esc(r['keyword'])+'</code>' for r in rows[:100]) if rows else 'No filters configured.')
@bot.message_handler(commands=['stopfilter'])
def remove_filter(m):
    if not need_admin(m): return
    p=m.text.split(maxsplit=1)
    if len(p)<2: return bot.reply_to(m,'Usage: /stopfilter WORD')
    with DB_LOCK,connect() as c: cur=c.execute('DELETE FROM filters WHERE chat_id=? AND keyword=?',(m.chat.id,p[1].lower()))
    bot.reply_to(m,'✅ Filter removed.' if cur.rowcount else 'Filter not found.')

# ---------------- LOCK / DELETE / PURGE ----------------
@bot.message_handler(commands=['lock','unlock'])
def lock(m):
    if not need_admin(m): return
    cmd=m.text.split()[0].split('@')[0][1:]
    try:
        if cmd=='lock': perm=types.ChatPermissions(can_send_messages=False)
        else: perm=types.ChatPermissions(can_send_messages=True,can_send_audios=True,can_send_documents=True,can_send_photos=True,can_send_videos=True,can_send_video_notes=True,can_send_voice_notes=True,can_send_polls=True,can_send_other_messages=True,can_add_web_page_previews=True)
        bot.set_chat_permissions(m.chat.id,perm); done(m,'🔒 Group locked.' if cmd=='lock' else '🔓 Group unlocked.')
    except Exception as e: bot.reply_to(m,'Permission update failed: '+esc(e))
@bot.message_handler(commands=['del','delete'])
def delete(m):
    if not need_admin(m): return
    if not m.reply_to_message: return bot.reply_to(m,'Reply to a message to delete it.')
    try: bot.delete_message(m.chat.id,m.reply_to_message.message_id); bot.delete_message(m.chat.id,m.message_id); log_action(m.chat.id,'Message deleted.')
    except Exception as e: bot.reply_to(m,'Delete failed: '+esc(e))
@bot.message_handler(commands=['purge'])
def purge(m):
    if not need_admin(m): return
    if not m.reply_to_message: return bot.reply_to(m,'Reply to the first message to purge.')
    start,end=m.reply_to_message.message_id,m.message_id
    if end-start>MAX_PURGE: return bot.reply_to(m,f'Limit is {MAX_PURGE} messages per purge.')
    count=0
    for mid in range(start,end+1):
        try: bot.delete_message(m.chat.id,mid); count+=1
        except Exception: pass
    status=bot.send_message(m.chat.id,f'🧹 Deleted {count} messages.'); threading.Timer(4,lambda: safe_delete(m.chat.id,status.message_id)).start(); log_action(m.chat.id,f'Purge deleted {count} messages.')
def safe_delete(chat,mid):
    try: bot.delete_message(chat,mid)
    except Exception: pass

# ---------------- MODERATION ----------------
@bot.message_handler(commands=['ban','unban','kick','mute','unmute','warn','unwarn','pin','unpin'])
def moderation(m):
    if not need_admin(m): return
    cmd=m.text.split()[0].split('@')[0][1:]; chat=m.chat.id
    if cmd in ('pin','unpin'):
        try:
            if cmd=='pin':
                if not m.reply_to_message: return bot.reply_to(m,'Reply to a message to pin it.')
                bot.pin_chat_message(chat,m.reply_to_message.message_id); done(m,'📌 Message pinned.')
            else: bot.unpin_chat_message(chat); done(m,'📌 Latest message unpinned.')
        except Exception as e: bot.reply_to(m,'Pin action failed: '+esc(e))
        return
    uid=target_id(m)
    if not uid: return bot.reply_to(m,'Reply to a user or provide a known @username/user ID.')
    if uid in (m.from_user.id,bot.get_me().id): return bot.reply_to(m,'Cannot apply this action to yourself or the bot.')
    try:
        if cmd=='ban': bot.ban_chat_member(chat,uid); done(m,f'🔨 User <code>{uid}</code> banned.')
        elif cmd=='unban': bot.unban_chat_member(chat,uid,only_if_banned=True); done(m,f'🔓 User <code>{uid}</code> unbanned.')
        elif cmd=='kick': bot.ban_chat_member(chat,uid); bot.unban_chat_member(chat,uid); done(m,f'👢 User <code>{uid}</code> kicked.')
        elif cmd=='mute': bot.restrict_chat_member(chat,uid,permissions=types.ChatPermissions(can_send_messages=False)); done(m,f'🔇 User <code>{uid}</code> muted.')
        elif cmd=='unmute': bot.restrict_chat_member(chat,uid,permissions=types.ChatPermissions(can_send_messages=True,can_send_audios=True,can_send_documents=True,can_send_photos=True,can_send_videos=True,can_send_video_notes=True,can_send_voice_notes=True,can_send_polls=True,can_send_other_messages=True,can_add_web_page_previews=True)); done(m,f'🔊 User <code>{uid}</code> unmuted.')
        elif cmd=='warn':
            n=warn_count(chat,uid)+1; set_warn(chat,uid,n)
            if n>=3: bot.ban_chat_member(chat,uid); set_warn(chat,uid,0); done(m,f'⚠️ User <code>{uid}</code> reached 3 warnings and was banned.')
            else: done(m,f'⚠️ Warning issued: {n}/3.')
        elif cmd=='unwarn':
            n=max(0,warn_count(chat,uid)-1); set_warn(chat,uid,n); done(m,f'✅ Warning removed. Current warnings: {n}/3.')
    except Exception as e: bot.reply_to(m,'Moderation action failed: '+esc(e))

# ---------------- MMF / KANG ----------------
def meme_font(size):
    for p in ['/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf','DejaVuSans-Bold.ttf','arialbd.ttf']:
        try: return ImageFont.truetype(p,size)
        except OSError: pass
    return ImageFont.load_default()
def wrapped(draw,text,font,maxw):
    words=text.upper().split(); lines=[]; line=''
    for word in words:
        candidate=(line+' '+word).strip()
        if draw.textbbox((0,0),candidate,font=font)[2]<=maxw: line=candidate
        else:
            if line: lines.append(line)
            line=word
    if line: lines.append(line)
    return lines or ['']
def draw_caption(draw,text,width,y,font):
    lines=wrapped(draw,text,font,width-24); lh=draw.textbbox((0,0),'Ag',font=font)[3]+8
    for line in lines:
        box=draw.textbbox((0,0),line,font=font); x=max(8,(width-(box[2]-box[0]))/2)
        draw.text((x,y),line,font=font,fill='white',stroke_width=max(3,font.size//14),stroke_fill='black'); y+=lh
@bot.message_handler(commands=['mmf'])
def mmf(m):
    r=m.reply_to_message; p=m.text.split(maxsplit=1)
    if not r or not (r.photo or r.sticker): return bot.reply_to(m,'Reply to a photo/static sticker: /mmf TOP ; BOTTOM')
    if len(p)<2: return bot.reply_to(m,'Add text: /mmf TOP TEXT ; BOTTOM TEXT')
    if r.sticker and (r.sticker.is_animated or r.sticker.is_video): return bot.reply_to(m,'Animated/video stickers are not supported by /mmf.')
    try:
        fid=r.photo[-1].file_id if r.photo else r.sticker.file_id; f=bot.get_file(fid); raw=bot.download_file(f.file_path)
        img=Image.open(io.BytesIO(raw)).convert('RGB'); w,h=img.size; d=ImageDraw.Draw(img); parts=p[1].split(';',1); top=parts[0].strip(); bottom=parts[1].strip() if len(parts)>1 else ''
        font=meme_font(max(20,min(h//8,w//8,90)))
        if top: draw_caption(d,top,w,10,font)
        if bottom:
            n=len(wrapped(d,bottom,font,w-24)); y=max(8,h-n*(d.textbbox((0,0),'Ag',font=font)[3]+8)-16); draw_caption(d,bottom,w,y,font)
        out=io.BytesIO(); out.name='mishra_meme.jpg'; img.save(out,'JPEG',quality=92); out.seek(0); bot.send_photo(m.chat.id,out,reply_to_message_id=m.message_id)
    except Exception as e: log.exception('MMF error'); bot.reply_to(m,'MMF failed: '+esc(e))
@bot.message_handler(commands=['kang'])
def kang(m):
    r=m.reply_to_message
    if not r or not r.sticker: return bot.reply_to(m,'Reply to a sticker with /kang.')
    try:
        f=bot.get_file(r.sticker.file_id); raw=bot.download_file(f.file_path); out=io.BytesIO(raw)
        if r.sticker.is_animated: out.name=r.sticker.file_unique_id+'.tgs'; bot.send_document(m.chat.id,out,caption='Animated sticker file')
        elif r.sticker.is_video: out.name=r.sticker.file_unique_id+'.webm'; bot.send_document(m.chat.id,out,caption='Video sticker file')
        else: out.name='kanged.webp'; bot.send_sticker(m.chat.id,out)
    except Exception as e: bot.reply_to(m,'Kang failed: '+esc(e))

# ---------------- ADMIN TAGS ----------------
@bot.message_handler(func=lambda m: bool(m.text) and m.text.strip().lower().startswith('@admin'))
def tag_admins(m):
    try:
        tags=[f'<a href="tg://user?id={a.user.id}">{esc(a.user.first_name)}</a>' for a in bot.get_chat_administrators(m.chat.id) if not a.user.is_bot]
        reason=m.text.strip()[6:].strip(); text='🚨 <b>Attention admins!</b> '+' '.join(tags)
        if reason: text+='\nReason: '+esc(reason)
        bot.reply_to(m,text)
    except Exception as e: bot.reply_to(m,'Could not tag admins: '+esc(e))
@bot.message_handler(func=lambda m: bool(m.text) and m.text.strip().lower().startswith('@all'))
def tag_all(m):
    if not admin(m.chat.id,m.from_user.id): return bot.reply_to(m,'⛔ Only admins can use @all.')
    with DB_LOCK,connect() as c: users=c.execute('SELECT user_id,name FROM stats WHERE chat_id=? ORDER BY count DESC LIMIT 300',(m.chat.id,)).fetchall()
    if not users: return bot.reply_to(m,'No tracked members yet. Members must send messages first.')
    mentions=[f'<a href="tg://user?id={u["user_id"]}">{esc(u["name"])}</a>' for u in users if u['user_id']!=bot.get_me().id]
    reason=m.text.strip()[4:].strip()
    for i in range(0,len(mentions),30):
        header='📢 <b>Attention everyone!</b>\n' if i==0 else ''
        extra=esc(reason)+'\n' if i==0 and reason else ''
        bot.send_message(m.chat.id,header+extra+' '.join(mentions[i:i+30]),disable_web_page_preview=True)

# ---------------- OPTIONAL AI ----------------
@bot.message_handler(commands=['ai'])
def ai(m):
    p=m.text.split(maxsplit=1)
    if len(p)<2: return bot.reply_to(m,'Usage: /ai ask a question')
    if not OPENAI_API_KEY: return bot.reply_to(m,'AI is optional. Add OPENAI_API_KEY to Render Environment to enable /ai.')
    try:
        from openai import OpenAI
        client=OpenAI(api_key=OPENAI_API_KEY)
        result=client.chat.completions.create(model=OPENAI_MODEL,messages=[{'role':'system','content':'You are MishraRobot, a helpful and safe Telegram group assistant. Be concise.'},{'role':'user','content':p[1][:3000]}],max_tokens=500)
        bot.reply_to(m,esc(result.choices[0].message.content or 'No answer generated.')[:3500])
    except Exception as e: log.exception('AI error'); bot.reply_to(m,'AI request failed. Check OPENAI_API_KEY and logs: '+esc(e)[:250])

# ---------------- GROUP MESSAGE MONITOR ----------------
@bot.message_handler(func=lambda m: True, content_types=['text','sticker','animation','photo','video','document','audio','voice','video_note','contact','location','venue','poll','dice'])
def monitor(m):
    if not group(m) or not m.from_user or m.from_user.is_bot: return
    chat,uid=m.chat.id,m.from_user.id; record(m); body=(m.text or m.caption or '').strip()
    if setting(chat,'antispam')=='on' and not admin(chat,uid):
        now=time.time(); q=FLOOD[(chat,uid)]; q.append(now)
        while q and now-q[0]>6: q.popleft()
        if len(q)>6:
            try: bot.delete_message(chat,m.message_id)
            except Exception: pass
            if len(q)==7:
                try: bot.restrict_chat_member(chat,uid,permissions=types.ChatPermissions(can_send_messages=False),until_date=int(now+30)); bot.send_message(chat,f'🛡 {esc(user_name(m.from_user))} muted 30 seconds for flooding.')
                except Exception: pass
            return
    if body and setting(chat,'antilink')=='on' and URL_RE.search(body) and not admin(chat,uid):
        try: bot.delete_message(chat,m.message_id); bot.send_message(chat,'🔗 Links are not allowed in this group.')
        except Exception: pass
        return
    if body:
        for row in filters(chat):
            if row['keyword'] in body.lower():
                try: bot.reply_to(m,row['response'])
                except Exception: log.exception('Filter reply failed')
                break
    with DB_LOCK,connect() as c: away=c.execute('SELECT reason,since FROM afk WHERE chat_id=? AND user_id=?',(chat,uid)).fetchone()
    if away:
        with DB_LOCK,connect() as c: c.execute('DELETE FROM afk WHERE chat_id=? AND user_id=?',(chat,uid))
        bot.reply_to(m,f'👋 Welcome back {esc(user_name(m.from_user))}! Away for {int(time.time()-away["since"])} seconds.')
    if m.reply_to_message and m.reply_to_message.from_user:
        target=m.reply_to_message.from_user
        with DB_LOCK,connect() as c: a=c.execute('SELECT reason FROM afk WHERE chat_id=? AND user_id=?',(chat,target.id)).fetchone()
        if a: bot.reply_to(m,f'💤 {esc(user_name(target))} is AFK: {esc(a["reason"])}')
    for ent in (m.entities or []):
        if ent.type=='text_mention' and ent.user:
            with DB_LOCK,connect() as c: a=c.execute('SELECT reason FROM afk WHERE chat_id=? AND user_id=?',(chat,ent.user.id)).fetchone()
            if a: bot.reply_to(m,'💤 This user is AFK: '+esc(a['reason']))

# ---------------- STARTUP ----------------
def main():
    init_db()
    threading.Thread(target=health_server,daemon=True,name='health-server').start()
    log.info('MishraRobot V2.0 started; health server on port %s',PORT)
    # Polling is intentionally a single process; Render instance count must be 1.
    while True:
        try:
            bot.infinity_polling(skip_pending=True,timeout=30,long_polling_timeout=25)
        except Exception:
            log.exception('Polling stopped; retrying in 5 seconds'); time.sleep(5)

if __name__=='__main__':
    main()
