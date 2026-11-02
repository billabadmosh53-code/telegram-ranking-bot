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
        "• Add me to any group.\n"
        "• I will automatically count messages for ranking.\n"
        "• Use the inline buttons to check stats and top active users!"
    )
    bot.reply_to(message, help_text, parse_mode="Markdown")

# Group messages track karne ke liye (Original chat fight tracking)
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
            text = "🏆 **Top Active Users:**\n\n"
            for i, u in enumerate(sorted_users[:10], 1):
                medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
                text += f"{medal} {u['name']} — {u['count']} msgs\n"
        else:
            text = "No message data yet for this group!"
        bot.answer_callback_query(call.id, text, show_alert=True)
        
    elif call.data == "stats":
        if chat_id in user_scores and user_id in user_scores[chat_id]:
            user_data = user_scores[chat_id][user_id]
            text = f"📊 Your Stats:\nName: {user_data['name']}\nTotal Messages: {user_data['count']}"
        else:
            text = "📊 You haven't sent any messages tracked yet!"
        bot.answer_callback_query(call.id, text, show_alert=True)

bot.infinity_polling()
        
