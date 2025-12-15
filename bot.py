import os
import logging
import asyncio
import subprocess
import signal
import sys
import psutil
import json
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler, 
    MessageHandler, filters, ConversationHandler, CallbackQueryHandler
)
from keep_alive import keep_alive

# --- CONFIGURATION ---
TOKEN = os.environ.get("TOKEN") 
# The Super Admin (You) - Can add/remove other users
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0")) 

UPLOAD_DIR = "scripts"
if not os.path.exists(UPLOAD_DIR):
    os.makedirs(UPLOAD_DIR)

USERS_FILE = "allowed_users.json"

# Global State
running_processes = {} # {filename: subprocess_object}

# Logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Conversation States
WAIT_ACTION, WAIT_PY_UPLOAD, WAIT_EXTRAS = range(3)

# --- USER MANAGEMENT FUNCTIONS ---

def get_allowed_users():
    """Load users from JSON file."""
    if not os.path.exists(USERS_FILE):
        return []
    try:
        with open(USERS_FILE, 'r') as f:
            return json.load(f)
    except:
        return []

def save_allowed_user(user_id):
    """Add a user ID to the list."""
    users = get_allowed_users()
    if user_id not in users:
        users.append(user_id)
        with open(USERS_FILE, 'w') as f:
            json.dump(users, f)
        return True
    return False

def remove_allowed_user(user_id):
    """Remove a user ID."""
    users = get_allowed_users()
    if user_id in users:
        users.remove(user_id)
        with open(USERS_FILE, 'w') as f:
            json.dump(users, f)
        return True
    return False

# --- KEYBOARDS ---
def main_menu_keyboard():
    keyboard = [
        ["📤 Upload & Run", "📂 My Files"],
        ["⚙️ Manage Running", "📊 Server Stats"],
        ["🆘 Help"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)

def extras_keyboard():
    keyboard = [
        ["➕ Add requirements.txt", "➕ Add .env"],
        ["🚀 RUN NOW", "🔙 Cancel"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# --- SECURITY DECORATORS ---

def restricted(func):
    """Allows Super Admin AND Allowed Users."""
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        allowed_users = get_allowed_users()
        
        # Check if user is Admin OR in the allowed list
        if user_id != ADMIN_ID and user_id not in allowed_users:
            await update.message.reply_text("⛔ **Access Denied.**\nContact the owner to get access.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapped

def super_admin_only(func):
    """Allows ONLY the Super Admin (Env Variable ID)."""
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if user_id != ADMIN_ID:
            await update.message.reply_text("⛔ **Super Admin Only.**\nYou cannot manage users.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapped

# --- ADMIN PANEL HANDLERS ---

@super_admin_only
async def add_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Usage: /add 123456789"""
    if not context.args:
        await update.message.reply_text("⚠️ Usage: `/add <telegram_id>`", parse_mode="Markdown")
        return
    
    try:
        new_id = int(context.args[0])
        if save_allowed_user(new_id):
            await update.message.reply_text(f"✅ User `{new_id}` has been **added**.", parse_mode="Markdown")
        else:
            await update.message.reply_text(f"⚠️ User `{new_id}` is already in the list.", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ Invalid ID. It must be a number.")

@super_admin_only
async def remove_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Usage: /remove 123456789"""
    if not context.args:
        await update.message.reply_text("⚠️ Usage: `/remove <telegram_id>`", parse_mode="Markdown")
        return
    
    try:
        target_id = int(context.args[0])
        if remove_allowed_user(target_id):
            await update.message.reply_text(f"🗑️ User `{target_id}` has been **removed**.", parse_mode="Markdown")
        else:
            await update.message.reply_text(f"⚠️ User `{target_id}` was not found.", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ Invalid ID.")

@super_admin_only
async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users = get_allowed_users()
    if not users:
        await update.message.reply_text("👥 **Allowed Users:**\n(None)", parse_mode="Markdown")
    else:
        text = "👥 **Allowed Users:**\n" + "\n".join([f"- `{uid}`" for uid in users])
        await update.message.reply_text(text, parse_mode="Markdown")

# --- HELPER FUNCTIONS (Smart Fix & Install) ---

def smart_fix_requirements(req_path):
    try:
        with open(req_path, 'r') as f:
            lines = f.readlines()
        clean_lines = []
        for line in lines:
            line = line.strip()
            if not line: continue
            if line.lower().startswith("pip install"):
                line = line[11:].strip()
                packages = line.split()
                clean_lines.extend(packages)
            else:
                clean_lines.append(line)
        with open(req_path, 'w') as f:
            f.write('\n'.join(clean_lines))
        return True
    except Exception as e:
        print(f"Error fixing requirements: {e}")
        return False

async def install_requirements(req_path, update):
    status_msg = await update.message.reply_text("⏳ **Checking requirements...**")
    smart_fix_requirements(req_path)
    await status_msg.edit_text("⏳ **Installing libraries...**")
    try:
        process = await asyncio.create_subprocess_exec(
            "pip", "install", "-r", req_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        if process.returncode == 0:
            await status_msg.edit_text("✅ **Libraries Installed Successfully!**")
            return True
        else:
            err_text = stderr.decode()[-2000:] 
            await status_msg.edit_text(f"❌ **Installation Failed:**\n```\n{err_text}\n```", parse_mode="Markdown")
            return False
    except Exception as e:
        await status_msg.edit_text(f"❌ Error: {e}")
        return False

# --- MAIN HANDLERS ---

@restricted
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = (
        f"👋 **Hello {user.first_name}!**\n\n"
        "I am a Python Hosting Bot.\n"
        "Use the menu below to manage your scripts."
    )
    if user.id == ADMIN_ID:
        msg += "\n\n👑 **Admin Controls:**\n`/add <id>`\n`/remove <id>`\n`/users`"
        
    await update.message.reply_text(msg, reply_markup=main_menu_keyboard(), parse_mode="Markdown")

# --- CONVERSATION: UPLOAD & SETUP ---

@restricted
async def upload_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📤 **Upload your Python Script**\n"
        "Please send the `.py` file now.",
        reply_markup=ReplyKeyboardMarkup([['🔙 Cancel']], resize_keyboard=True)
    )
    return WAIT_PY_UPLOAD

async def receive_py(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file = await update.message.document.get_file()
    filename = update.message.document.file_name
    
    if not filename.endswith(".py"):
        await update.message.reply_text("❌ File must end with .py. Try again.")
        return WAIT_PY_UPLOAD

    # Save .py
    file_path = os.path.join(UPLOAD_DIR, filename)
    await file.download_to_drive(file_path)
    
    context.user_data['py_file'] = filename
    context.user_data['py_path'] = file_path
    
    await update.message.reply_text(
        f"✅ **{filename}** saved!\n\n"
        "Do you need to add extras before running?",
        reply_markup=extras_keyboard()
    )
    return WAIT_EXTRAS

async def receive_extras(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "🚀 RUN NOW":
        return await execute_script(update, context)
    elif text == "🔙 Cancel":
        await update.message.reply_text("❌ Cancelled.", reply_markup=main_menu_keyboard())
        return ConversationHandler.END
    elif text == "➕ Add requirements.txt":
        await update.message.reply_text("📂 Send `requirements.txt`.")
        context.user_data['waiting_for'] = 'req'
        return WAIT_EXTRAS
    elif text == "➕ Add .env":
        await update.message.reply_text("🔒 Send `.env` file.")
        context.user_data['waiting_for'] = 'env'
        return WAIT_EXTRAS
    return WAIT_EXTRAS

async def receive_extra_files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    waiting_for = context.user_data.get('waiting_for')
    if not waiting_for: return WAIT_EXTRAS
        
    file = await update.message.document.get_file()
    filename = update.message.document.file_name
    py_filename = context.user_data['py_file']

    if waiting_for == 'req' and filename.endswith('.txt'):
        save_path = os.path.join(UPLOAD_DIR, f"{py_filename}_req.txt")
        await file.download_to_drive(save_path)
        await install_requirements(save_path, update)
    elif waiting_for == 'env' and filename.endswith('.env'):
        save_path = os.path.join(UPLOAD_DIR, f"{py_filename}.env")
        await file.download_to_drive(save_path)
        await update.message.reply_text("✅ Environment variables saved.")

    context.user_data['waiting_for'] = None
    await update.message.reply_text("Anything else? Or click RUN.", reply_markup=extras_keyboard())
    return WAIT_EXTRAS

# --- EXECUTION LOGIC ---

async def execute_script(update: Update, context: ContextTypes.DEFAULT_TYPE):
    filename = context.user_data.get('py_file')
    if not filename: filename = context.user_data.get('target_file')
        
    env_path = os.path.join(UPLOAD_DIR, f"{filename}.env")
    
    if filename in running_processes:
        if running_processes[filename].poll() is None:
            await update.message.reply_text(f"⚠️ **{filename}** is already running!", reply_markup=main_menu_keyboard())
            return ConversationHandler.END

    custom_env = os.environ.copy()
    if os.path.exists(env_path):
        with open(env_path, 'r') as f:
            for line in f:
                if '=' in line and not line.strip().startswith('#'):
                    k, v = line.strip().split('=', 1)
                    custom_env[k] = v

    log_file = open(os.path.join(UPLOAD_DIR, f"{filename}.log"), "w")
    try:
        process = subprocess.Popen(
            ["python", "-u", filename], 
            env=custom_env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            cwd=UPLOAD_DIR, 
            preexec_fn=os.setsid 
        )
        running_processes[filename] = process

        await update.message.reply_text(f"🚀 **Starting {filename}...**\nPID: {process.pid}")

        await asyncio.sleep(3)
        if process.poll() is not None:
            log_file.close()
            with open(os.path.join(UPLOAD_DIR, f"{filename}.log"), "r") as f:
                error_log = f.read()[-3000:]
            await update.message.reply_text(f"❌ **Crashed Immediately!**\n\n`{error_log}`", parse_mode="Markdown", reply_markup=main_menu_keyboard())
        else:
            await update.message.reply_text(f"🟢 **{filename} is running.**", reply_markup=main_menu_keyboard())

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}", reply_markup=main_menu_keyboard())

    return ConversationHandler.END

# --- FILE MANAGEMENT ---

@restricted
async def list_files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    files = [f for f in os.listdir(UPLOAD_DIR) if f.endswith('.py')]
    if not files:
        await update.message.reply_text("📂 No scripts uploaded.")
        return

    keyboard = []
    text = "📂 **Hosted Files:**"
    for f in files:
        status = "🟢" if f in running_processes and running_processes[f].poll() is None else "🔴"
        keyboard.append([InlineKeyboardButton(f"{status} {f}", callback_data=f"manage_{f}")])
    
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

@restricted
async def server_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cpu = psutil.cpu_percent()
    ram = psutil.virtual_memory().percent
    active_scripts = sum(1 for p in running_processes.values() if p.poll() is None)
    
    stats = f"📊 **Stats**\nCPU: {cpu}%\nRAM: {ram}%\nRunning: {active_scripts}"
    await update.message.reply_text(stats, parse_mode="Markdown")

async def file_action_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    # Check permission for button clicks too
    if query.from_user.id != ADMIN_ID and query.from_user.id not in get_allowed_users():
        await query.message.reply_text("⛔ Access Denied")
        return

    if data.startswith("manage_"):
        filename = data.split("manage_")[1]
        is_running = filename in running_processes and running_processes[filename].poll() is None
        text = f"📄 `{filename}`\nStatus: {'🟢 Running' if is_running else '🔴 Stopped'}"
        buttons = []
        if is_running: buttons.append([InlineKeyboardButton("🛑 Stop", callback_data=f"stop_{filename}")])
        else: buttons.append([InlineKeyboardButton("🚀 Run", callback_data=f"run_{filename}")])
        buttons.append([InlineKeyboardButton("📜 Logs", callback_data=f"log_{filename}")])
        buttons.append([InlineKeyboardButton("🗑️ Delete", callback_data=f"del_{filename}")])
        buttons.append([InlineKeyboardButton("🔙 Back", callback_data="back_list")])
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")

    elif data.startswith("stop_"):
        filename = data.split("stop_")[1]
        if filename in running_processes:
            os.killpg(os.getpgid(running_processes[filename].pid), signal.SIGTERM)
            running_processes[filename].wait()
            await query.edit_message_text(f"🛑 Stopped `{filename}`", parse_mode="Markdown")

    elif data.startswith("run_"):
        filename = data.split("run_")[1]
        context.user_data['target_file'] = filename
        await query.delete_message()
        context.user_data['py_file'] = filename 
        await execute_script(update.callback_query, context)

    elif data.startswith("del_"):
        filename = data.split("del_")[1]
        path = os.path.join(UPLOAD_DIR, filename)
        if os.path.exists(path): os.remove(path)
        for ext in ['.env', '_req.txt', '.log']:
             extra = os.path.join(UPLOAD_DIR, filename + ext if ext != '_req.txt' else f"{filename}_req.txt")
             if os.path.exists(extra): os.remove(extra)
        await query.edit_message_text(f"🗑️ Deleted `{filename}`.", parse_mode="Markdown")

    elif data.startswith("log_"):
        filename = data.split("log_")[1]
        log_path = os.path.join(UPLOAD_DIR, f"{filename}.log")
        if os.path.exists(log_path):
            await context.bot.send_document(chat_id=update.effective_chat.id, document=open(log_path, 'rb'), caption=f"📜 {filename}")
        else:
            await context.bot.send_message(chat_id=update.effective_chat.id, text="❌ No log found.")

    elif data == "back_list":
        await list_files(update, context)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚫 Cancelled.", reply_markup=main_menu_keyboard())
    return ConversationHandler.END

if __name__ == '__main__':
    keep_alive()
    application = ApplicationBuilder().token(TOKEN).build()
    
    # Upload Conversation
    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📤 Upload & Run$"), upload_start)],
        states={
            WAIT_PY_UPLOAD: [MessageHandler(filters.Document.FileExtension("py"), receive_py)],
            WAIT_EXTRAS: [
                MessageHandler(filters.Regex("^(🚀 RUN NOW|🔙 Cancel|➕ Add requirements.txt|➕ Add .env)$"), receive_extras),
                MessageHandler(filters.Document.ALL, receive_extra_files) 
            ]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    
    application.add_handler(CommandHandler('add', add_user))
    application.add_handler(CommandHandler('remove', remove_user))
    application.add_handler(CommandHandler('users', list_users))
    application.add_handler(conv_handler)
    application.add_handler(MessageHandler(filters.Regex("^📂 My Files$"), list_files))
    application.add_handler(MessageHandler(filters.Regex("^⚙️ Manage Running$"), list_files))
    application.add_handler(MessageHandler(filters.Regex("^📊 Server Stats$"), server_stats))
    application.add_handler(CallbackQueryHandler(file_action_handler))
    application.add_handler(CommandHandler('start', start))

    print("Bot is up and running!")
    application.run_polling()
