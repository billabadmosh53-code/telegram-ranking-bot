import os
import threading
import time
import random
import io
from http.server import HTTPServer, BaseHTTPRequestHandler
import telebot
from telebot import types
from PIL import Image, ImageDraw, ImageFont

TOKEN = "9270651831:AAF1rFK0q3DF_d61B9HmcDwCbj1i0FbQ5DM"
bot = telebot.TeleBot(TOKEN)

# Render health check server
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is active!")
    def log_message(self, format, *args):
        return

def run_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    server.serve_forever()

user_scores = {}
afk_users = {}

@bot.message_handler(commands=['start'])
def send_welcome(message):
    markup = types.InlineKeyboardMarkup()
    btn1 = types.InlineKeyboardButton("📊 Your Stats", callback_data="stats")
    btn2 = types.InlineKeyboardButton("🏆 Top Ranking", callback_data="top")
    markup.add(btn1, btn2)
    bot.send_message(message.chat.id, "👋 Welcome! Add me to a group to track rankings, ship members, make meme stickers, and kang stickers!", reply_markup=markup)

@bot.message_handler(commands=['help'])
def send_help(message):
    help_text = (
        "🤖 **Bot Help Menu:**\n\n"
        "• Add me to any group to start counting messages.\n"
        "• Use `/ranking` or `/top` to see the global leaderboard.\n"
        "• Use `/mystats` to check your personal stats.\n"
        "• Use `/afk [reason]` to go AFK.\n"
        "• Use `/ship` to find a match in the group!\n"
        "• Reply to photo/sticker with `/mmf text` (top text) or `/mmf ; text` (bottom text).\n"
        "• Reply to a sticker/video/GIF with `/kang` to steal and resend it!\n"
        "• Use the inline buttons anytime!"
    )
    bot.send_message(message.chat.id, help_text, parse_mode="Markdown")

@bot.message_handler(commands=['afk'])
def set_afk(message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    user_name = message.from_user.first_name
    username = message.from_user.username
    
    if chat_id not in afk_users:
        afk_users[chat_id] = {}
        
    if user_id in afk_users[chat_id]:
        bot.send_message(chat_id, f"Hey {user_name}, you are already AFK.")
        return

    text_parts = message.text.split(maxsplit=1) if message.text else []
    reason = text_parts[1] if len(text_parts) > 1 else "Away from keyboard"
        
    afk_users[chat_id][user_id] = {
        'name': user_name,
        'username': username.lower() if username else None,
        'reason': reason,
        'time': time.time()
    }
    
    bot.send_message(chat_id, f"✅ **AFK Status Enabled** for {user_name}.\n📝 Reason: {reason}", parse_mode="Markdown")

@bot.message_handler(commands=['ship'])
def ship_users(message):
    chat_id = message.chat.id
    if chat_id not in user_scores or len(user_scores[chat_id]) < 2:
        bot.send_message(chat_id, "⚠️ Group mein shipping ke liye kam se kam 2 active users hone chahiye!")
        return
        
    users_list = list(user_scores[chat_id].values())
    user1 = random.choice(users_list)
    user2 = random.choice(users_list)
    
    attempts = 0
    while user1['name'] == user2['name'] and len(users_list) > 1 and attempts < 10:
        user2 = random.choice(users_list)
        attempts += 1

    percentage = random.randint(15, 100)
    if percentage > 85:
        remark = "💖 Perfect Match! Shaadi ki taiyari karo!"
    elif percentage > 60:
        remark = "💞 Achhi Jodi hai!"
    elif percentage > 40:
        remark = "🤝 Friends with benefits type vibe!"
    else:
        remark = "💔 Door hi raho toh behtar hai!"

    ship_text = (
        f"💘 **Matchmaking / Shipping Result:**\n\n"
        f"✨ {user1['name']}  ❤️  {user2['name']}\n"
        f"📊 Compatibility: **{percentage}%**\n"
        f"💬 {remark}"
    )
    bot.send_message(chat_id, ship_text, parse_mode="Markdown")

@bot.message_handler(commands=['mmf'])
def mmf_command(message):
    chat_id = message.chat.id
    if not message.reply_to_message:
        bot.send_message(chat_id, "⚠️ Kripya kisi photo ya static sticker par reply karke `/mmf text` ya `/mmf ; text` likhein!")
        return
    
    replied = message.reply_to_message
    file_id = None
    
    if replied.photo:
        file_id = replied.photo[-1].file_id
    elif replied.sticker and not replied.sticker.is_animated and not replied.sticker.is_video:
        file_id = replied.sticker.file_id
    else:
        bot.send_message(chat_id, "⚠️ Yeh animated sticker hai ya supported media nahi hai! Kripya photo ya static sticker par reply karein.")
        return

    full_text = message.text.split(maxsplit=1) if message.text else []
    raw_text = full_text[1] if len(full_text) > 1 else "MEME"
    
    # Check if user wants text at bottom using semicolon ";"
    is_bottom = False
    if raw_text.startswith(';'):
        is_bottom = True
        meme_text = raw_text.lstrip(';').strip()
        if not meme_text:
            meme_text = "MEME"
    else:
        meme_text = raw_text

    try:
        file_info = bot.get_file(file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        
        image = Image.open(io.BytesIO(downloaded_file))
        if image.mode
    
