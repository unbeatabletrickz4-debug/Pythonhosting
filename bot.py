import os
import logging
import asyncio
import subprocess
import signal
import sys
import psutil
import json
import threading
from flask import Flask, request
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler, 
    MessageHandler, filters, ConversationHandler, CallbackQueryHandler
)

# --- CONFIGURATION ---
TOKEN = os.environ.get("TOKEN") 
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0")) 
# Render automatically provides this URL. If testing locally, use http://localhost:8080
BASE_URL = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:8080")

UPLOAD_DIR = "scripts"
if not os.path.exists(UPLOAD_DIR):
    os.makedirs(UPLOAD_DIR)

USERS_FILE = "allowed_users.json"

# Global State
running_processes = {} # {filename: subprocess_object}

# Logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- WEB SERVER (FLASK) ---
app = Flask(__name__)

@app.route('/')
def home():
    return "🤖 Python Host Bot is Alive!", 200

@app.route('/status')
def script_status():
    """
    Check if a specific script is running.
    Usage: /status?script=filename.py
    Returns 200 if running, 404 if not.
    """
    script_name = request.args.get('script')
    if not script_name:
        return "Please specify a script: /status?script=filename.py", 400
    
    if script_name in running_processes and running_processes[script_name].poll() is None:
        return f"✅ {script_name} is running.", 200
    else:
        return f"❌ {script_name} is stopped.", 404

def run_flask():
    # Render expects port 10000 or 8080
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

# --- USER MANAGEMENT ---

def get_allowed_users():
    if not os.path.exists(USERS_FILE): return []
    try:
        with open(USERS_FILE, 'r') as f: return json.load(f)
    except: return []

def save_allowed_user(user_id):
    users = get_allowed_users()
    if user_id not in users:
        users.append(user_id)
        with open(USERS_FILE, 'w') as f: json.dump(users, f)
        return True
    return False

def remove_allowed_user(user_id):
    users = get_allowed_users()
    if user_id in users:
        users.remove(user_id)
        with open(USERS_FILE, 'w') as f: json.dump(users, f)
        return True
    return False

# --- KEYBOARDS ---
def main_menu_keyboard():
    return ReplyKeyboardMarkup([
        ["📤 Upload & Run", "📂 My Files"],
        ["⚙️ Manage Running", "📊 Server Stats"],
        ["🆘 Help"]
    ], resize_keyboard=True)

def extras_keyboard():
    return ReplyKeyboardMarkup([
        ["➕ Add requirements.txt", "➕ Add .env"],
        ["🚀 RUN NOW", "🔙 Cancel"]
    ], resize_keyboard=True)

# --- SECURITY ---
def restricted(func):
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if user_id != ADMIN_ID and user_id not in get_allowed_users():
            await update.message.reply_text("⛔ **Access Denied.**")
            return
        return await func(update, context, *args, **kwargs)
    return wrapped

def super_admin_only(func):
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if update.effective_user.id != ADMIN_ID:
            await update.message.reply_text("⛔ **Super Admin Only.**")
            return
        return await func(update, context, *args, **kwargs)
    return wrapped

# --- ADMIN HANDLERS ---
@super_admin_only
async def add_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return await update.message.reply_text("Usage: `/add 12345`", parse_mode="Markdown")
    try:
        uid = int(context.args[0])
        if save_allowed_user(uid): await update.message.reply_text(f"✅ Added `{uid}`", parse_mode="Markdown")
        else: await update.message.reply_text("⚠️ Already exists.")
    except: await update.message.reply_text("❌ Invalid ID.")

@super_admin_only
async def remove_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return await update.message.reply_text("Usage: `/remove 12345`", parse_mode="Markdown")
    try:
        uid = int(context.args[0])
        if remove_allowed_user(uid): await update.message.reply_text(f"🗑️ Removed `{uid}`", parse_mode="Markdown")
        else: await update.message.reply_text("⚠️ Not found.")
    except: await update.message.reply_text("❌ Invalid ID.")

# --- HELPERS ---
def smart_fix_requirements(req_path):
    try:
        with open(req_path, 'r') as f: lines = f.readlines()
        clean = []
        for line in lines:
            line = line.strip()
            if not line: continue
            if line.lower().startswith("pip install"):
                clean.extend(line[11:].strip().split())
            else:
                clean.append(line)
        with open(req_path, 'w') as f: f.write('\n'.join(clean))
        return True
    except: return False

async def install_requirements(req_path, update):
    msg = await update.message.reply_text("⏳ **Installing requirements...**")
    smart_fix_requirements(req_path)
    try:
        proc = await asyncio.create_subprocess_exec("pip", "install", "-r", req_path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, stderr = await proc.communicate()
        if proc.returncode == 0: await msg.edit_text("✅ **Installed!**")
        else: await msg.edit_text(f"❌ Failed:\n```\n{stderr.decode()[-1000:]}\n```", parse_mode="Markdown")
    except Exception as e: await msg.edit_text(f"❌ Error: {e}")

# --- BOT HANDLERS ---
@restricted
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 **Welcome to PythonHost!**", reply_markup=main_menu_keyboard())

# UPLOAD CONVERSATION
WAIT_PY, WAIT_EXTRAS = range(2)

@restricted
async def upload_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📤 Send your **.py** file.", reply_markup=ReplyKeyboardMarkup([['🔙 Cancel']], resize_keyboard=True))
    return WAIT_PY

async def receive_py(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file = await update.message.document.get_file()
    fname = update.message.document.file_name
    if not fname.endswith(".py"): return await update.message.reply_text("❌ Needs .py file.")
    path = os.path.join(UPLOAD_DIR, fname)
    await file.download_to_drive(path)
    context.user_data['py_file'], context.user_data['py_path'] = fname, path
    await update.message.reply_text(f"✅ **{fname}** saved.", reply_markup=extras_keyboard())
    return WAIT_EXTRAS

async def receive_extras(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text
    if txt == "🚀 RUN NOW": return await execute_script(update, context)
    elif txt == "🔙 Cancel": 
        await update.message.reply_text("Cancelled.", reply_markup=main_menu_keyboard())
        return ConversationHandler.END
    elif "requirements" in txt:
        await update.message.reply_text("📂 Send `requirements.txt`.")
        context.user_data['wait'] = 'req'
    elif ".env" in txt:
        await update.message.reply_text("🔒 Send `.env`.")
        context.user_data['wait'] = 'env'
    return WAIT_EXTRAS

async def receive_extra_files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wait = context.user_data.get('wait')
    if not wait: return WAIT_EXTRAS
    file = await update.message.document.get_file()
    fname = update.message.document.file_name
    py_name = context.user_data['py_file']
    
    if wait == 'req' and fname.endswith('.txt'):
        path = os.path.join(UPLOAD_DIR, f"{py_name}_req.txt")
        await file.download_to_drive(path)
        await install_requirements(path, update)
    elif wait == 'env' and fname.endswith('.env'):
        path = os.path.join(UPLOAD_DIR, f"{py_name}.env")
        await file.download_to_drive(path)
        await update.message.reply_text("✅ Env saved.")
    
    context.user_data['wait'] = None
    await update.message.reply_text("Next?", reply_markup=extras_keyboard())
    return WAIT_EXTRAS

async def execute_script(update: Update, context: ContextTypes.DEFAULT_TYPE):
    fname = context.user_data.get('py_file', context.user_data.get('target_file'))
    if fname in running_processes and running_processes[fname].poll() is None:
        await update.message.reply_text("⚠️ Already running!", reply_markup=main_menu_keyboard())
        return ConversationHandler.END
    
    env_path = os.path.join(UPLOAD_DIR, f"{fname}.env")
    custom_env = os.environ.copy()
    if os.path.exists(env_path):
        with open(env_path) as f:
            for l in f:
                if '=' in l and not l.strip().startswith('#'):
                    k,v = l.strip().split('=', 1)
                    custom_env[k] = v.strip()

    log_file = open(os.path.join(UPLOAD_DIR, f"{fname}.log"), "w")
    try:
        # Start Process
        proc = subprocess.Popen(["python", "-u", fname], env=custom_env, stdout=log_file, stderr=subprocess.STDOUT, cwd=UPLOAD_DIR, preexec_fn=os.setsid)
        running_processes[fname] = proc
        await update.message.reply_text(f"🚀 **Started {fname}** (PID: {proc.pid})")
        
        await asyncio.sleep(3)
        if proc.poll() is not None:
            log_file.close()
            with open(os.path.join(UPLOAD_DIR, f"{fname}.log")) as f: log = f.read()[-2000:]
            await update.message.reply_text(f"❌ **Crashed:**\n`{log}`", parse_mode="Markdown", reply_markup=main_menu_keyboard())
        else:
            # Generate Uptime URL
            uptime_url = f"{BASE_URL}/status?script={fname}"
            await update.message.reply_text(f"🟢 **Running!**\n\n🔗 **Uptime URL:**\n`{uptime_url}`", parse_mode="Markdown", reply_markup=main_menu_keyboard())
    except Exception as e: await update.message.reply_text(f"❌ Error: {e}", reply_markup=main_menu_keyboard())
    return ConversationHandler.END

# --- MANAGEMENT ---
@restricted
async def list_files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    files = [f for f in os.listdir(UPLOAD_DIR) if f.endswith('.py')]
    if not files: return await update.message.reply_text("📂 Empty.")
    
    keyboard = []
    for f in files:
        status = "🟢" if f in running_processes and running_processes[f].poll() is None else "🔴"
        keyboard.append([InlineKeyboardButton(f"{status} {f}", callback_data=f"manage_{f}")])
    await update.message.reply_text("📂 **Your Files:**", reply_markup=InlineKeyboardMarkup(keyboard))

async def file_action_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID and query.from_user.id not in get_allowed_users(): return

    data = query.data
    if data.startswith("manage_"):
        fname = data.split("manage_")[1]
        is_running = fname in running_processes and running_processes[fname].poll() is None
        
        text = f"📄 `{fname}`\nStatus: {'🟢 Running' if is_running else '🔴 Stopped'}"
        buttons = []
        if is_running:
            buttons.append([InlineKeyboardButton("🛑 Stop", callback_data=f"stop_{fname}")])
            # ADDING THE UPTIME BUTTON HERE
            buttons.append([InlineKeyboardButton("🔗 Keep Alive URL", callback_data=f"url_{fname}")])
        else:
            buttons.append([InlineKeyboardButton("🚀 Run", callback_data=f"run_{fname}")])
        
        buttons.append([InlineKeyboardButton("📜 Logs", callback_data=f"log_{fname}")])
        buttons.append([InlineKeyboardButton("🗑️ Delete", callback_data=f"del_{fname}")])
        buttons.append([InlineKeyboardButton("🔙 Back", callback_data="back_list")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")

    elif data.startswith("stop_"):
        fname = data.split("stop_")[1]
        if fname in running_processes:
            os.killpg(os.getpgid(running_processes[fname].pid), signal.SIGTERM)
            running_processes[fname].wait()
            await query.edit_message_text(f"🛑 Stopped `{fname}`", parse_mode="Markdown")
    
    elif data.startswith("url_"):
        fname = data.split("url_")[1]
        url = f"{BASE_URL}/status?script={fname}"
        await query.message.reply_text(
            f"🔗 **UptimeRobot URL for {fname}:**\n\n`{url}`\n\n"
            "_Use this URL in UptimeRobot (HTTP Monitor). If the script stops, this URL returns Error 404._",
            parse_mode="Markdown"
        )

    elif data.startswith("run_"):
        fname = data.split("run_")[1]
        context.user_data['target_file'] = fname
        await query.delete_message() 
        await execute_script(update.callback_query, context)

    elif data.startswith("del_"):
        fname = data.split("del_")[1]
        path = os.path.join(UPLOAD_DIR, fname)
        if os.path.exists(path): os.remove(path)
        for ext in ['.env', '_req.txt', '.log']:
             extra = os.path.join(UPLOAD_DIR, fname + ext if ext != '_req.txt' else f"{fname}_req.txt")
             if os.path.exists(extra): os.remove(extra)
        await query.edit_message_text(f"🗑️ Deleted `{fname}`.", parse_mode="Markdown")

    elif data.startswith("log_"):
        fname = data.split("log_")[1]
        log_path = os.path.join(UPLOAD_DIR, f"{fname}.log")
        if os.path.exists(log_path): await context.bot.send_document(chat_id=update.effective_chat.id, document=open(log_path, 'rb'))
        else: await query.message.reply_text("❌ No logs.")

    elif data == "back_list": await list_files(update, context)

@restricted
async def server_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cpu = psutil.cpu_percent()
    ram = psutil.virtual_memory().percent
    count = sum(1 for p in running_processes.values() if p.poll() is None)
    await update.message.reply_text(f"📊 **Stats**\nCPU: {cpu}%\nRAM: {ram}%\nRunning: {count}", parse_mode="Markdown")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚫 Cancelled.", reply_markup=main_menu_keyboard())
    return ConversationHandler.END

if __name__ == '__main__':
    # Start Flask in a background thread
    t = threading.Thread(target=run_flask)
    t.daemon = True
    t.start()
    
    app_bot = ApplicationBuilder().token(TOKEN).build()
    
    conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📤 Upload & Run$"), upload_start)],
        states={
            WAIT_PY: [MessageHandler(filters.Document.FileExtension("py"), receive_py)],
            WAIT_EXTRAS: [MessageHandler(filters.Regex("^(🚀 RUN NOW|🔙 Cancel|➕)"), receive_extras), MessageHandler(filters.Document.ALL, receive_extra_files)]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    
    app_bot.add_handler(CommandHandler('add', add_user))
    app_bot.add_handler(CommandHandler('remove', remove_user))
    app_bot.add_handler(conv)
    app_bot.add_handler(MessageHandler(filters.Regex("^📂 My Files$|Manage Running"), list_files))
    app_bot.add_handler(MessageHandler(filters.Regex("^📊 Server Stats$"), server_stats))
    app_bot.add_handler(CallbackQueryHandler(file_action_handler))
    app_bot.add_handler(CommandHandler('start', start))

    print("Bot is up and running!")
    app_bot.run_polling()
