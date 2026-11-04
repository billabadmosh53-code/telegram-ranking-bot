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
    bot.send_message(message.chat.id, "👋 Welcome! Add me to a group to track rankings, manage admins, make meme stickers, and more!", reply_markup=markup)

@bot.message_handler(commands=['help'])
def send_help(message):
    help_text = (
        "🤖 **Bot Help Menu:**\n\n"
        "📊 **Ranking & Fun:**\n"
        "• `/ranking` or `/top` - Global leaderboard\n"
        "• `/mystats` - Check your stats\n"
        "• `/afk [reason]` - Set AFK status\n"
        "• `/ship` - Matchmaking\n"
        "• `/mmf text` or `/mmf ; text` - Meme sticker\n"
        "• `/kang` - Steal sticker/media\n\n"
        "🛡️ **Admin & Moderation Commands:**\n"
        "• `/promote` (reply) - Make user admin\n"
        "• `/demote` (reply) - Remove admin rights\n"
        "• `/ban` (reply) - Ban user from group\n"
        "• `/unban [user_id]` - Unban user\n"
        "• `/kick` (reply) - Kick/remove user\n"
        "• `/mute` (reply) - Restrict user from sending messages\n"
        "• `/unmute` (reply) - Restore messaging rights\n"
        "• `/pin` (reply) - Pin message\n"
        "• `/unpin` (reply) - Unpin message"
    )
    bot.send_message(message.chat.id, help_text, parse_mode="Markdown")

def is_user_admin(chat_id, user_id):
    try:
        member = bot.get_chat_member(chat_id, user_id)
        return member.status in ['creator', 'administrator']
    except:
        return False

@bot.message_handler(commands=['promote'])
def promote_user(message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    if not is_user_admin(chat_id, user_id):
        bot.send_message(chat_id, "⚠️ Aap is command ko use karne ke liye Admin nahi hain!")
        return
    if not message.reply_to_message:
        bot.send_message(chat_id, "⚠️ Kripya jis user ko promote karna hai, uske message par reply karke `/promote` likhein!")
        return
    target_id = message.reply_to_message.from_user.id
    target_name = message.reply_to_message.from_user.first_name
    try:
        bot.promote_chat_member(chat_id, target_id, can_manage_chat=True, can_delete_messages=True, can_invite_users=True, can_restrict_members=True)
        bot.send_message(chat_id, f"✅ **{target_name}** ko successfully Admin promote kar diya gaya hai! 👑", parse_mode="Markdown")
    except Exception as e:
        bot.send_message(chat_id, f"❌ Error: {str(e)}")

@bot.message_handler(commands=['demote'])
def demote_user(message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    if not is_user_admin(chat_id, user_id):
        bot.send_message(chat_id, "⚠️ Aap Admin nahi hain!")
        return
    if not message.reply_to_message:
        bot.send_message(chat_id, "⚠️ Kripya user ke message par reply karein!")
        return
    target_id = message.reply_to_message.from_user.id
    target_name = message.reply_to_message.from_user.first_name
    try:
        bot.promote_chat_member(chat_id, target_id, can_manage_chat=False, can_delete_messages=False, can_invite_users=False, can_restrict_members=False)
        bot.send_message(chat_id, f"📉 **{target_name}** ke admin rights wapas le liye gaye hain.", parse_mode="Markdown")
    except Exception as e:
        bot.send_message(chat_id, f"❌ Error: {str(e)}")

@bot.message_handler(commands=['ban'])
def ban_user(message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    if not is_user_admin(chat_id, user_id):
        bot.send_message(chat_id, "⚠️ Aap Admin nahi hain!")
        return
    if not message.reply_to_message:
        bot.send_message(chat_id, "⚠️ Kripya reply karein!")
        return
    target_id = message.reply_to_message.from_user.id
    target_name = message.reply_to_message.from_user.first_name
    try:
        bot.ban_chat_member(chat_id, target_id)
        bot.send_message(chat_id, f"🔨 **{target_name}** ko ban kar diya gaya hai!", parse_mode="Markdown")
    except Exception as e:
        bot.send_message(chat_id, f"❌ Error: {str(e)}")

@bot.message_handler(commands=['unban'])
def unban_user(message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    if not is_user_admin(chat_id, user_id):
        bot.send_message(chat_id, "⚠️ Aap Admin nahi hain!")
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.send_message(chat_id, "⚠️ Kripya user ID dein: `/unban [user_id]`", parse_mode="Markdown")
        return
    try:
        target_id = int(parts[1])
        bot.unban_chat_member(chat_id, target_id, only_if_banned=True)
        bot.send_message(chat_id, f"🔓 User ID `{target_id}` ko unban kar diya gaya hai.", parse_mode="Markdown")
    except Exception as e:
        bot.send_message(chat_id, f"❌ Error: {str(e)}")

@bot.message_handler(commands=['kick'])
def kick_user(message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    if not is_user_admin(chat_id, user_id):
        bot.send_message(chat_id, "⚠️ Aap Admin nahi hain!")
        return
    if not message.reply_to_message:
        bot.send_message(chat_id, "⚠️ Kripya reply karein!")
        return
    target_id = message.reply_to_message.from_user.id
    target_name = message.reply_to_message.from_user.first_name
    try:
        bot.ban_chat_member(chat_id, target_id)
        bot.unban_chat_member(chat_id, target_id)
        bot.send_message(chat_id, f"👢 **{target_name}** ko kick kar diya gaya hai!", parse_mode="Markdown")
    except Exception as e:
        bot.send_message(chat_id, f"❌ Error: {str(e)}")

@bot.message_handler(commands=['mute'])
def mute_user(message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    if not is_user_admin(chat_id, user_id):
        bot.send_message(chat_id, "⚠️ Aap Admin nahi hain!")
        return
    if not message.reply_to_message:
        bot.send_message(chat_id, "⚠️ Kripya reply karein!")
        return
    target_id = message.reply_to_message.from_user.id
    target_name = message.reply_to_message.from_user.first_name
    try:
        bot.restrict_chat_member(chat_id, target_id, can_send_messages=False)
        bot.send_message(chat_id, f"🔇 **{target_name}** ko mute kar diya gaya hai!", parse_mode="Markdown")
    except Exception as e:
        bot.send_message(chat_id, f"❌ Error: {str(e)}")

@bot.message_handler(commands=['unmute'])
def unmute_user(message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    if not is_user_admin(chat_id, user_id):
        bot.send_message(chat_id, "⚠️ Aap Admin nahi hain!")
        return
    if not message.reply_to_message:
        bot.send_message(chat_id, "⚠️ Kripya reply karein!")
        return
    target_id = message.reply_to_message.from_user.id
    target_name = message.reply_to_message.from_user.first_name
    try:
        bot.restrict_chat_member(chat_id, target_id, can_send_messages=True, can_send_media_messages=True, can_send_other_messages=True, can_add_web_page_previews=True)
        bot.send_message(chat_id, f"🔊 **{target_name}** ko unmute kar diya gaya hai!", parse_mode="Markdown")
    except Exception as e:
        bot.send_message(chat_id, f"❌ Error: {str(e)}")

@bot.message_handler(commands=['pin'])
def pin_message(message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    if not is_user_admin(chat_id, user_id):
        bot.send_message(chat_id, "⚠️ Aap Admin nahi hain!")
        return
    if not message.reply_to_message:
        bot.send_message(chat_id, "⚠️ Kripya pin karne ke liye message par reply karein!")
        return
    try:
        bot.pin_chat_message(chat_id, message.reply_to_message.message_id)
        bot.send_message(chat_id, "📌 Message successfully pin kar diya gaya hai!")
    except Exception as e:
        bot.send_message(chat_id, f"❌ Error: {str(e)}")

@bot.message_handler(commands=['unpin'])
def unpin_message(message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    if not is_user_admin(chat_id, user_id):
        bot.send_message(chat_id, "⚠️ Aap Admin nahi hain!")
        return
    try:
        bot.unpin_chat_message(chat_id)
        bot.send_message(chat_id, "📌 Message successfully unpin kar diya gaya hai!")
    except Exception as e:
        bot.send_message(chat_id, f"❌ Error: {str(e)}")

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
    afk_users[chat_id][user_id] = {'name': user_name, 'username': username.lower() if username else None, 'reason': reason, 'time': time.time()}
    bot.send_message(chat_id, f"✅ **AFK Status Enabled** for {user_name}.\n📝 Reason: {reason}", parse_mode="Markdown")

@bot.message_handler(commands=['ship'])
def ship_users(message):
    chat_id = message.chat.id
    if chat_id not in user_scores or len(user_scores[chat_id]) < 2:
        bot.send_message(chat_id, "⚠️ Group mein shipping ke liye kam se kam 2 active users hone chahiye!")
        return
    users_list = list(user_scores[chat_id].values())
    user1, user2 = random.choice(users_list), random.choice(users_list)
    attempts = 0
    while user1['name'] == user2['name'] and len(users_list) > 1 and attempts < 10:
        user2 = random.choice(users_list)
        attempts += 1
    percentage = random.randint(15, 100)
    remark = "💖 Perfect Match!" if percentage > 85 else "💞 Achhi Jodi hai!" if percentage > 60 else "🤝 Friends with benefits!" if percentage > 40 else "💔 Door hi raho!"
    bot.send_message(chat_id, f"💘 **Shipping Result:**\n✨ {user1['name']} ❤️ {user2['name']}\n📊 Compatibility: **{percentage}%**\n💬 {remark}", parse_mode="Markdown")

@bot.message_handler(commands=['mmf'])
def mmf_command(message):
    chat_id = message.chat.id
    if not message.reply_to_message:
        bot.send_message(chat_id, "⚠️ Kripya photo/sticker par reply karke `/mmf text` ya `/mmf ; text` likhein!")
        return
    replied = message.reply_to_message
    file_id = replied.photo[-1].file_id if replied.photo else replied.sticker.file_id if (replied.sticker and not replied.sticker.is_animated and not replied.sticker.is_video) else None
    if not file_id:
        bot.send_message(chat_id, "⚠️ Supported media nahi hai!")
        return
    full_text = message.text.split(maxsplit=1) if message.text else []
    raw_text = full_text[1] if len(full_text) > 1 else "MEME"
    is_bottom = raw_text.startswith(';')
    meme_text = raw_text.lstrip(';').strip() or "MEME"
    try:
        file_info = bot.get_file(file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        orig_image = Image.open(io.BytesIO(downloaded_file)).convert("RGBA")
        canvas = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
        orig_image.thumbnail((512, 400))
        w, h = orig_image.size
        canvas.paste(orig_image, ((512 - w) // 2, 60 if not is_bottom else 20), orig_image)
        draw = ImageDraw.Draw(canvas)
        try: font = ImageFont.truetype("arial.ttf", 45)
        except: font = ImageFont.load_default()
        bbox = draw.textbbox((0, 0), meme_text, font=font)
        x, y = (512 - (bbox[2] - bbox[0])) / 2, 512 - (bbox[3] - bbox[1]) - 35 if is_bottom else 10
        for adj_x in range(-3, 4):
            for adj_y in range(-3, 4):
                draw.text((x + adj_x, y + adj_y), meme_text, font=font, fill="black")
        draw.text((x, y), meme_text, font=font, fill="white")
        output = io.BytesIO()
        canvas.save(output, format="WEBP")
        output.seek(0)
        bot.send_sticker(chat_id, output)
    except Exception as e:
        bot.send_message(chat_id, f"❌ Error: {str(e)}")

@bot.message_handler(commands=['kang'])
def kang_command(message):
    chat_id = message.chat.id
    if not message.reply_to_message:
        bot.send_message(chat_id, "⚠️ Reply to sticker/media with `/kang`!")
        return
    r = message.reply_to_message
    if r.sticker: bot.send_sticker(chat_id, r.sticker.file_id)
    elif r.animation: bot.send_animation(chat_id, r.animation.file_id)
    elif r.video: bot.send_video(chat_id, r.video.file_id)
    else: bot.send_message(chat_id, "⚠️ Unsupported media!")

@bot.message_handler(commands=['ranking', 'top'])
def show_ranking(message):
    chat_id = message.chat.id
    if chat_id in user_scores and user_scores[chat_id]:
        sorted_users = sorted(user_scores[chat_id].values(), key=lambda x: x['count'], reverse=True)
        text = "🏆 **Global Ranking:**\n\n"
        for i, u in enumerate(sorted_users[:10], 1):
            text += f"{'🥇' if i==1 else '🥈' if i==2 else '🥉' if i==3 else f'{i}.'} {u['name']} — {u['count']} msgs\n"
    else:
        text = "No message data yet!"
    bot.send_message(message.chat.id, text, parse_mode="Markdown")

@bot.message_handler(commands=['mystats', 'stats'])
def show_mystats(message):
    chat_id, user_id = message.chat.id, message.from_user.id
    if chat_id in user_scores and user_id in user_scores[chat_id]:
        u = user_scores[chat_id][user_id]
        rank = sorted(user_scores[chat_id].items(), key=lambda x: x[1]['count'], reverse=True)
        user_rank = next((i for i, (uid, _) in enumerate(rank, 1) if uid == user_id), "N/A")
        text = f"📊 **Your Stats:**\n👤 Name: {u['name']}\n💬 Messages: {u['count']}\n🏆 Rank: #{user_rank}"
    else:
        text = "📊 No stats yet!"
    bot.send_message(message.chat.id, text, parse_mode="Markdown")

@bot.message_handler(func=lambda message: True, content_types=['text', 'sticker', 'animation', 'photo', 'video', 'document', 'audio', 'voice'])
def handle_all_messages(message):
    if message.chat.type in ['group', 'supergroup']:
        chat_id, user_id, user_name = message.chat.id, message.from_user.id, message.from_user.first_name
        msg_text = message.text or message.caption or ""
        if chat_id not in user_scores: user_scores[chat_id] = {}
        if user_id not in user_scores[chat_id]: user_scores[chat_id][user_id] = {'name': user_name, 'count': 0}
        if not msg_text.startswith('/'): user_scores[chat_id][user_id]['count'] += 1

if __name__ == "__main__":
    threading.Thread(target=run_server, daemon=True).start()
    # Safe loop to prevent application exit on polling errors
    while True:
        try:
            bot.infinity_polling(timeout=10, long_polling_timeout=5)
        except Exception as e:
            print(f"Polling error: {e}")
            time.sleep(3)
