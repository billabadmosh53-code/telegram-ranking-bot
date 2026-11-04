import os
import threading
import time
import random
from http.server import HTTPServer, BaseHTTPRequestHandler
import telebot
from telebot import types

TOKEN = "9270651831:AAF1rFK0q3DF_d61B9HmcDwCbj1i0FbQ5DM"
bot = telebot.TeleBot(TOKEN)

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
    bot.send_message(message.chat.id, "👋 Welcome! Add me to a group to track group message rankings and ship members!", reply_markup=markup)

@bot.message_handler(commands=['help'])
def send_help(message):
    help_text = (
        "🤖 **Bot Help Menu:**\n\n"
        "• Add me to any group to start counting messages.\n"
        "• Use `/ranking` or `/top` to see the global leaderboard.\n"
        "• Use `/mystats` to check your personal stats.\n"
        "• Use `/afk [reason]` to go AFK.\n"
        "• Use `/ship` to find a match in the group!\n"
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

# /ship command (Naya Feature!)
@bot.message_handler(commands=['ship'])
def ship_users(message):
    chat_id = message.chat.id
    
    if chat_id not in user_scores or len(user_scores[chat_id]) < 2:
        bot.send_message(chat_id, "⚠️ Group mein shipping ke liye kam se kam 2 active users hone chahiye!")
        return
        
    # Get all active users in this group
    users_list = list(user_scores[chat_id].values())
    
    # Pick two random users
    user1 = random.choice(users_list)
    user2 = random.choice(users_list)
    
    # Make sure user1 and user2 are not the same person (if possible)
    attempts = 0
    while user1['name'] == user2['name'] and len(users_list) > 1 and attempts < 10:
        user2 = random.choice(users_list)
        attempts += 1

    # Generate a random compatibility score (0 to 100)
    percentage = random.randint(15, 100)
    
    # Choose a fun emoji/status based on percentage
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

@bot.message_handler(commands=['ranking', 'top'])
def show_ranking(message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    
    if chat_id in user_scores and user_scores[chat_id]:
        sorted_users = sorted(user_scores[chat_id].values(), key=lambda x: x['count'], reverse=True)
        text = "🏆 **Global Active Users Ranking:**\n\n"
        
        for i, u in enumerate(sorted_users[:10], 1):
            medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
            text += f"{medal} {u['name']} — {u['count']} msgs\n"
        
        user_rank = "N/A"
        user_count = 0
        for i, (uid, u_data) in enumerate(sorted(user_scores[chat_id].items(), key=lambda x: x[1]['count'], reverse=True), 1):
            if uid == user_id:
                user_rank = i
                user_count = u_data['count']
                break
                
        text += f"\n-------------------\n"
        text += f"📍 **Your Standing:** #{user_rank} | {user_count} msgs"
    else:
        text = "No message data yet for this group! Pehle kuch messages bhejiye."
        
    bot.send_message(message.chat.id, text, parse_mode="Markdown")

@bot.message_handler(commands=['mystats', 'stats'])
def show_mystats(message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    if chat_id in user_scores and user_id in user_scores[chat_id]:
        user_data = user_scores[chat_id][user_id]
        
        sorted_users = sorted(user_scores[chat_id].items(), key=lambda x: x[1]['count'], reverse=True)
        user_rank = "N/A"
        for i, (uid, _) in enumerate(sorted_users, 1):
            if uid == user_id:
                user_rank = i
                break

        text = f"📊 **Your Stats:**\n👤 Name: {user_data['name']}\n💬 Total Messages: {user_data['count']}\n🏆 Group Rank: #{user_rank}"
    else:
        text = "📊 You haven't sent any messages tracked yet in this group!"
    bot.send_message(message.chat.id, text, parse_mode="Markdown")

@bot.message_handler(func=lambda message: True, content_types=['text', 'sticker', 'animation', 'photo', 'video', 'document', 'audio', 'voice'])
def handle_all_messages(message):
    if message.chat.type in ['group', 'supergroup']:
        chat_id = message.chat.id
        user_id = message.from_user.id
        user_name = message.from_user.first_name
        msg_text = message.text or message.caption or ""

        # --- 1. Message Tracking ---
        if chat_id not in user_scores:
            user_scores[chat_id] = {}
        if user_id not in user_scores[chat_id]:
            user_scores[chat_id][user_id] = {'name': user_name, 'count': 0}
        
        if msg_text.startswith('/afk') or msg_text.startswith('/ship'):
            pass
        else:
            user_scores[chat_id][user_id]['count'] += 1

        # --- 2. AFK Welcome Back (Reset) ---
        if chat_id in afk_users and user_id in afk_users[chat_id]:
            if not msg_text.startswith('/afk'):
                afk_data = afk_users[chat_id].pop(user_id)
                afk_time_sec = int(time.time() - afk_data['time'])
                
                if afk_time_sec < 60:
                    afk_time_str = f"{afk_time_sec}s"
                elif afk_time_sec < 3600:
                    afk_time_str = f"{afk_time_sec // 60}m {afk_time_sec % 60}s"
                else:
                    afk_time_str = f"{afk_time_sec // 3600}h {afk_time_sec // 60 % 60}m"

                bot.send_message(chat_id, f"👋 Welcome back {user_name}!\nYou were AFK for: {afk_time_str}")

        # --- 3. AFK Reply for Replies ---
        if message.reply_to_message:
            replied_user_id = message.reply_to_message.from_user.id
            replied_user_name = message.reply_to_message.from_user.first_name
            if chat_id in afk_users and replied_user_id in afk_users[chat_id]:
                afk_info = afk_users[chat_id][replied_user_id]
                bot.send_message(chat_id, f"🔇 {replied_user_name} is currently **AFK**.\n📝 Reason: {afk_info['reason']}", parse_mode="Markdown")

        # --- 4. AFK Reply for Mentions ---
        if chat_id in afk_users:
            text_lower = msg_text.lower()
            notified = set()
            
            for afk_uid, afk_info in afk_users[chat_id].items():
                if afk_uid == user_id:
                    continue
                
                uname = afk_info['username']
                fname = afk_info['name'].lower()
                
                matched = False
                if uname and f"@{uname}" in text_lower:
                    matched = True
                elif fname in text_lower:
                    matched = True
                    
                if matched and afk_uid not in notified:
                    notified.add(afk_uid)
                    bot.send_message(chat_id, f"🔇 {afk_info['name']} is currently **AFK**.\n📝 Reason: {afk_info['reason']}", parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: True)
def callback_query(call):
    chat_id = call.message.chat.id
    user_id = call.from_user.id
    
    if call.data == "top":
        if chat_id in user_scores and user_scores[chat_id]:
            sorted_users = sorted(user_scores[chat_id].values(), key=lambda x: x['count'], reverse=True)
            text = "🏆 **Global Active Users Ranking:**\n\n"
            for i, u in enumerate(sorted_users[:10], 1):
                medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
                text += f"{medal} {u['name']} — {u['count']} msgs\n"
        else:
            text = "No message data yet for this group!"
        bot.answer_callback_query(call.id, text, show_alert=True)
        
    elif call.data == "stats":
        if chat_id in user_scores and user_id in user_scores[chat_id]:
            user_data = user_scores[chat_id][user_id]
            sorted_users = sorted(user_scores[chat_id].items(), key=lambda x: x[1]['count'], reverse=True)
            user_rank = "N/A"
            for i, (uid, _) in enumerate(sorted_users, 1):
                if uid == user_id:
                    user_rank = i
                    break
            text = f"Your Stats:\nName: {user_data['name']}\nMessages: {user_data['count']}\nRank: #{user_rank}"
        else:
            text = "You haven't sent any messages tracked yet!"
        bot.answer_callback_query(call.id, text, show_alert=True)

if __name__ == "__main__":
    threading.Thread(target=run_server, daemon=True).start()
    bot.infinity_polling()
    
