import os
import logging
import asyncio
import subprocess
import signal
import sys
import psutil
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler, 
    MessageHandler, filters, ConversationHandler, CallbackQueryHandler
)
from keep_alive import keep_alive

# --- CONFIGURATION ---
TOKEN = os.environ.get("TOKEN") 
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0")) 

UPLOAD_DIR = "scripts"
if not os.path.exists(UPLOAD_DIR):
    os.makedirs(UPLOAD_DIR)

# Global State
running_processes = {} # {filename: subprocess_object}

# Logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Conversation States
WAIT_ACTION, WAIT_PY_UPLOAD, WAIT_EXTRAS = range(3)

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

# --- SECURITY ---
def restricted(func):
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if user_id != ADMIN_ID:
            await update.message.reply_text("⛔ **Access Denied.**\nThis is a private hosting bot.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapped

# --- HELPER FUNCTIONS ---

def smart_fix_requirements(req_path):
    """
    Reads the requirements file and fixes common user errors:
    1. Removes 'pip install'
    2. Splits multiple packages on one line into new lines
    """
    try:
        with open(req_path, 'r') as f:
            lines = f.readlines()
        
        clean_lines = []
        for line in lines:
            line = line.strip()
            if not line: continue
            
            # Fix: User pasted 'pip install lib1 lib2'
            if line.lower().startswith("pip install"):
                line = line[11:].strip() # Remove 'pip install'
                # Assume remaining text is space-separated packages
                packages = line.split()
                clean_lines.extend(packages)
            else:
                # Keep original line (handling normal usage)
                clean_lines.append(line)
        
        # Write back cleaned file
        with open(req_path, 'w') as f:
            f.write('\n'.join(clean_lines))
        return True
    except Exception as e:
        print(f"Error fixing requirements: {e}")
        return False

async def install_requirements(req_path, update):
    status_msg = await update.message.reply_text("⏳ **Checking requirements...**")
    
    # 1. Run Smart Fix
    smart_fix_requirements(req_path)
    
    await status_msg.edit_text("⏳ **Installing libraries (this may take a minute)...**")
    
    try:
        process = await asyncio.create_subprocess_exec(
            "pip", "install", "-r", req_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        
        if process.returncode == 0:
            await status_msg.edit_text("✅ **Libraries Installed Successfully!**")
            return True
        else:
            # Send the last 2000 chars of error to avoid telegram limit
            err_text = stderr.decode()[-2000:] 
            await status_msg.edit_text(f"❌ **Installation Failed:**\n```\n{err_text}\n```", parse_mode="Markdown")
            return False
    except Exception as e:
        await status_msg.edit_text(f"❌ Error: {e}")
        return False

# --- MAIN HANDLERS ---

@restricted
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 **Welcome to PythonHost!**\n\n"
        "I can host your Python scripts 24/7 (as long as the server is alive).",
        reply_markup=main_menu_keyboard()
    )

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
        await update.message.reply_text(
            "📂 Please send your `requirements.txt` file.\n"
            "_(Tip: Just list package names, e.g., 'requests')_",
            parse_mode="Markdown"
        )
        context.user_data['waiting_for'] = 'req'
        return WAIT_EXTRAS

    elif text == "➕ Add .env":
        await update.message.reply_text("🔒 Please send your `.env` file.")
        context.user_data['waiting_for'] = 'env'
        return WAIT_EXTRAS

    return WAIT_EXTRAS

async def receive_extra_files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    waiting_for = context.user_data.get('waiting_for')
    if not waiting_for:
        return WAIT_EXTRAS
        
    file = await update.message.document.get_file()
    filename = update.message.document.file_name
    py_filename = context.user_data['py_file']

    if waiting_for == 'req' and filename.endswith('.txt'):
        save_path = os.path.join(UPLOAD_DIR, f"{py_filename}_req.txt")
        await file.download_to_drive(save_path)
        # Call installation (which now includes the Smart Fix)
        await install_requirements(save_path, update)
        
    elif waiting_for == 'env' and filename.endswith('.env'):
        save_path = os.path.join(UPLOAD_DIR, f"{py_filename}.env")
        await file.download_to_drive(save_path)
        await update.message.reply_text("✅ Environment variables saved.")

    # Reset waiting state
    context.user_data['waiting_for'] = None
    await update.message.reply_text(
        "Anything else? Or click RUN.",
        reply_markup=extras_keyboard()
    )
    return WAIT_EXTRAS

# --- EXECUTION LOGIC ---

async def execute_script(update: Update, context: ContextTypes.DEFAULT_TYPE):
    filename = context.user_data.get('py_file')
    if not filename: 
        filename = context.user_data.get('target_file')
        
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
        # NOTE: FIX FOR 'No such file' IS HERE (filename instead of py_path)
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
            await update.message.reply_text(
                f"❌ **Script Crashed Immediately!**\n\n📜 Log Preview:\n`{error_log}`", 
                parse_mode="Markdown",
                reply_markup=main_menu_keyboard()
            )
        else:
            await update.message.reply_text(f"🟢 **{filename} is stable and running.**", reply_markup=main_menu_keyboard())

    except Exception as e:
        await update.message.reply_text(f"❌ Execution Error: {e}", reply_markup=main_menu_keyboard())

    return ConversationHandler.END

# --- FILE MANAGEMENT ---

@restricted
async def list_files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    files = [f for f in os.listdir(UPLOAD_DIR) if f.endswith('.py')]
    if not files:
        await update.message.reply_text("📂 No scripts uploaded yet.")
        return

    keyboard = []
    text = "📂 **Your Files:**\nSelect a file to manage:"
    
    for f in files:
        status = "🔴"
        if f in running_processes and running_processes[f].poll() is None:
            status = "🟢"
        
        keyboard.append([InlineKeyboardButton(f"{status} {f}", callback_data=f"manage_{f}")])
    
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

@restricted
async def server_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cpu = psutil.cpu_percent()
    ram = psutil.virtual_memory().percent
    
    active_scripts = 0
    for p in running_processes.values():
        if p.poll() is None:
            active_scripts += 1

    stats = (
        f"📊 **Server Statistics**\n"
        f"──────────────────\n"
        f"🖥️ CPU Usage: {cpu}%\n"
        f"💾 RAM Usage: {ram}%\n"
        f"🏃 Running Scripts: {active_scripts}\n"
        f"📂 Total Scripts: {len([f for f in os.listdir(UPLOAD_DIR) if f.endswith('.py')])}"
    )
    await update.message.reply_text(stats, parse_mode="Markdown")

async def file_action_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data.startswith("manage_"):
        filename = data.split("manage_")[1]
        is_running = False
        if filename in running_processes and running_processes[filename].poll() is None:
            is_running = True
            
        text = f"📄 **File:** `{filename}`\nStatus: {'🟢 Running' if is_running else '🔴 Stopped'}"
        buttons = []
        if is_running:
            buttons.append([InlineKeyboardButton("🛑 Stop", callback_data=f"stop_{filename}")])
        else:
            buttons.append([InlineKeyboardButton("🚀 Run", callback_data=f"run_{filename}")])
        buttons.append([InlineKeyboardButton("📜 Get Logs", callback_data=f"log_{filename}")])
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
            await context.bot.send_document(chat_id=update.effective_chat.id, document=open(log_path, 'rb'), caption=f"📜 Log for {filename}")
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
    
    application.add_handler(conv_handler)
    application.add_handler(MessageHandler(filters.Regex("^📂 My Files$"), list_files))
    application.add_handler(MessageHandler(filters.Regex("^⚙️ Manage Running$"), list_files))
    application.add_handler(MessageHandler(filters.Regex("^📊 Server Stats$"), server_stats))
    application.add_handler(CallbackQueryHandler(file_action_handler))
    application.add_handler(CommandHandler('start', start))

    print("Bot is up and running!")
    application.run_polling()
