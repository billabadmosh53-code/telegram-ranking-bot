import telebot
from telebot import types

TOKEN = "9270651831:AAHu43NPpfx0EbRgAkq-6aWLia95eXpV8qM"
bot = telebot.TeleBot(TOKEN)

# Scores store karne ke liye dictionary
user_scores = {}

# /start command
@bot.message_handler(commands=['start'])
def send_welcome(message):
    markup = types.InlineKeyboardMarkup()
    btn1 = types.InlineKeyboardButton("📊 Your Stats", callback_data="stats")
    btn2 = types.InlineKeyboardButton("🏆 Top Ranking", callback_data="top")
    markup.add(btn1, btn2)
    bot.reply_to(message, "👋 Welcome! Add me to a group to track group message rankings!", reply_markup=markup)

# /help command
@bot.message_handler(commands=['help'])
def send_help(message):
    help_text = (
        "🤖 **Bot Help Menu:**\n\n"
        "• Add me to any group to start counting messages.\n"
        "• Use `/ranking` or `/top` to see the global leaderboard.\n"
        "• Use `/mystats` to check your personal stats.\n"
        "• Use the inline buttons anytime!"
    )
    bot.reply_to(message, help_text, parse_mode="Markdown")

# /ranking ya /top command (Global Leaderboard + User's own status in group)
@bot.message_handler(commands=['ranking', 'top'])
def show_ranking(message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    
    if chat_id in user_scores and user_scores[chat_id]:
        sorted_users = sorted(user_scores[chat_id].values(), key=lambda x: x['count'], reverse=True)
        text = "🏆 **Global Active Users Ranking:**\n\n"
        
        user_rank = "Not Ranked"
        user_count = 0
        
        for i, u in enumerate(sorted_users, 1):
            if i <= 10:
                medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
                text += f"{medal} {u['name']} — {u['count']} msgs\n"
            
            # User ki apni position dhoondne ke liye
            # (Yahan hum simple user_id match kar sakte hain agar dictionary key user_id hai)
        
        # User ki personal rank find karna group mein
        for i, (uid, u_data) in enumerate(sorted(user_scores[chat_id].items(), key=lambda x: x[1]['count'], reverse=True), 1):
            if uid == user_id:
                user_rank = i
                user_count = u_data['count']
                break
                
        text += f"\n-------------------\n"
        text += f"📍 **Your Standing:** #{user_rank} | {user_count} msgs"
    else:
        text = "No message data yet for this group! Pehle kuch messages bhejiye."
        
    bot.reply_to(message, text, parse_mode="Markdown")

# /mystats command
@bot.message_handler(commands=['mystats', 'stats'])
def show_mystats(message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    if chat_id in user_scores and user_id in user_scores[chat_id]:
        user_data = user_scores[chat_id][user_id]
        
        # User ki rank nikalne ke liye
        sorted_users = sorted(user_scores[chat_id].items(), key=lambda x: x[1]['count'], reverse=True)
        user_rank = "N/A"
        for i, (uid, _) in enumerate(sorted_users, 1):
            if uid == user_id:
                user_rank = i
                break

        text = f"📊 **Your Stats:**\n👤 Name: {user_data['name']}\n💬 Total Messages: {user_data['count']}\n🏆 Group Rank: #{user_rank}"
    else:
        text = "📊 You haven't sent any messages tracked yet in this group!"
    bot.reply_to(message, text, parse_mode="Markdown")

# Group messages track karne ke liye (Chat fight message counting)
@bot.message_handler(func=lambda message: True)
def track_messages(message):
    if message.chat.type in ['group', 'supergroup']:
        chat_id = message.chat.id
        user_id = message.from_user.id
        user_name = message.from_user.first_name

        if chat_id not in user_scores:
            user_scores[chat_id] = {}
        if user_id not in user_scores[chat_id]:
            user_scores[chat_id][user_id] = {'name': user_name, 'count': 0}
        
        user_scores[chat_id][user_id]['count'] += 1

# Inline buttons handle karne ke liye
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

bot.infinity_polling()
