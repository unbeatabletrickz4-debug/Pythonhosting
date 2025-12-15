import os
import logging
import asyncio
import subprocess
import signal
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler, 
    MessageHandler, filters, ConversationHandler
)
from keep_alive import keep_alive

# --- CONFIGURATION ---
TOKEN = os.environ.get("TOKEN") # Load from Environment
ADMIN_ID = int(os.environ.get("ADMIN_ID", "123456789")) # Replace default or set in env

# Folders
UPLOAD_DIR = "scripts"
if not os.path.exists(UPLOAD_DIR):
    os.makedirs(UPLOAD_DIR)

# States for Conversation
WAIT_PY, WAIT_REQ, WAIT_ENV = range(3)

# Global dictionary to store running processes: {filename: subprocess_object}
running_processes = {}

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# --- SECURITY CHECK ---
def restricted(func):
    """Decorator to restrict usage to Admin only."""
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if user_id != ADMIN_ID:
            await update.message.reply_text("⛔ Unauthorized. You are not the admin.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapped

# --- HELPER FUNCTIONS ---
async def install_requirements(req_file):
    """Installs requirements from a txt file."""
    try:
        process = await asyncio.create_subprocess_exec(
            "pip", "install", "-r", req_file,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        await process.communicate()
        return True
    except Exception as e:
        print(f"Error installing reqs: {e}")
        return False

def load_env_file(env_path):
    """Parses a .env file into a dictionary."""
    env_vars = os.environ.copy()
    if os.path.exists(env_path):
        with open(env_path, 'r') as f:
            for line in f:
                if '=' in line and not line.startswith('#'):
                    key, value = line.strip().split('=', 1)
                    env_vars[key] = value
    return env_vars

# --- HANDLERS ---

@restricted
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 **Python Host Bot**\n\n"
        "Commands:\n"
        "/runpy - Upload and run a new script\n"
        "/bpy <filename> - Run an existing script\n"
        "/list - List hosted scripts\n"
        "/manage - Stop or delete scripts"
    )

# --- CONVERSATION: UPLOAD & RUN ---

@restricted
async def runpy_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📤 Please upload your **.py** file.")
    return WAIT_PY

async def receive_py(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file = await update.message.document.get_file()
    filename = update.message.document.file_name
    
    if not filename.endswith(".py"):
        await update.message.reply_text("❌ File must end with .py. Try again.")
        return WAIT_PY

    # Save file
    file_path = os.path.join(UPLOAD_DIR, filename)
    await file.download_to_drive(file_path)
    
    context.user_data['py_file'] = filename
    context.user_data['py_path'] = file_path
    
    await update.message.reply_text(
        f"✅ {filename} saved.\nDo you have a **requirements.txt**?",
        reply_markup=ReplyKeyboardMarkup([['Yes', 'No']], one_time_keyboard=True)
    )
    return WAIT_REQ

async def receive_req_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.lower()
    if text == 'no':
        await update.message.reply_text(
            "Okay. Do you have a **.env** file?",
            reply_markup=ReplyKeyboardMarkup([['Yes', 'No']], one_time_keyboard=True)
        )
        return WAIT_ENV
    
    await update.message.reply_text("📤 Upload your **requirements.txt** file.", reply_markup=ReplyKeyboardRemove())
    return WAIT_REQ # Stay in state to receive file

async def receive_req_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file = await update.message.document.get_file()
    filename = update.message.document.file_name
    
    req_path = os.path.join(UPLOAD_DIR, f"{context.user_data['py_file']}_req.txt")
    await file.download_to_drive(req_path)
    context.user_data['req_path'] = req_path
    
    await update.message.reply_text("Installing requirements... This might take a moment.")
    await install_requirements(req_path)
    
    await update.message.reply_text(
        "✅ Requirements installed. Do you have a **.env** file?",
        reply_markup=ReplyKeyboardMarkup([['Yes', 'No']], one_time_keyboard=True)
    )
    return WAIT_ENV

async def receive_env_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.lower()
    if text == 'no':
        # FINISH - Run the script
        return await execute_script(update, context)
    
    await update.message.reply_text("📤 Upload your **.env** file.", reply_markup=ReplyKeyboardRemove())
    return WAIT_ENV

async def receive_env_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file = await update.message.document.get_file()
    
    env_path = os.path.join(UPLOAD_DIR, f"{context.user_data['py_file']}.env")
    await file.download_to_drive(env_path)
    context.user_data['env_path'] = env_path
    
    # FINISH - Run the script
    return await execute_script(update, context)

async def execute_script(update: Update, context: ContextTypes.DEFAULT_TYPE):
    py_filename = context.user_data.get('py_file')
    py_path = os.path.join(UPLOAD_DIR, py_filename)
    env_path = os.path.join(UPLOAD_DIR, f"{py_filename}.env")
    
    # Check if already running
    if py_filename in running_processes:
        if running_processes[py_filename].poll() is None:
            await update.message.reply_text(f"⚠️ {py_filename} is already running!")
            return ConversationHandler.END

    # Prepare Environment
    custom_env = load_env_file(env_path)
    
    try:
        # Run subprocess (detached)
        log_file = open(os.path.join(UPLOAD_DIR, f"{py_filename}.log"), "w")
        process = subprocess.Popen(
            ["python", py_path],
            env=custom_env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            cwd=UPLOAD_DIR # Run inside scripts folder
        )
        
        running_processes[py_filename] = process
        
        await update.message.reply_text(
            f"🚀 **Started:** `{py_filename}`\n"
            f"PID: {process.pid}\n\n"
            "Use /manage to stop it.",
            reply_markup=ReplyKeyboardRemove(),
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to start: {e}")

    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚫 Operation cancelled.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

# --- OTHER COMMANDS ---

@restricted
async def bpy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Run an existing file: /bpy filename.py"""
    if not context.args:
        await update.message.reply_text("Usage: /bpy <filename.py>")
        return

    filename = context.args[0]
    file_path = os.path.join(UPLOAD_DIR, filename)
    
    if not os.path.exists(file_path):
        await update.message.reply_text("❌ File not found.")
        return

    # Mock user_data to reuse execute logic (simplified manual trigger)
    context.user_data['py_file'] = filename
    await execute_script(update, context)

@restricted
async def list_files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    files = [f for f in os.listdir(UPLOAD_DIR) if f.endswith('.py')]
    if not files:
        await update.message.reply_text("📂 No scripts found.")
        return
    
    status_text = "📂 **Hosted Scripts:**\n"
    for f in files:
        status = "🔴 Stopped"
        if f in running_processes and running_processes[f].poll() is None:
            status = "🟢 Running"
        status_text += f"- `{f}` : {status}\n"
        
    await update.message.reply_text(status_text, parse_mode="Markdown")

@restricted
async def manage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Usage:\n"
            "/manage stop <filename.py>\n"
            "/manage delete <filename.py>\n"
            "/manage log <filename.py>"
        )
        return

    action = context.args[0].lower()
    if len(context.args) < 2:
        await update.message.reply_text("⚠️ Please specify the filename.")
        return
    filename = context.args[1]

    if action == "stop":
        if filename in running_processes and running_processes[filename].poll() is None:
            running_processes[filename].terminate()
            await update.message.reply_text(f"🛑 Stopped {filename}")
        else:
            await update.message.reply_text(f"⚠️ {filename} is not running.")
            
    elif action == "delete":
        # Stop first if running
        if filename in running_processes and running_processes[filename].poll() is None:
            running_processes[filename].terminate()
        
        # Remove files
        try:
            path = os.path.join(UPLOAD_DIR, filename)
            if os.path.exists(path): os.remove(path)
            
            # Remove associated env/reqs/logs
            for ext in ['.env', '_req.txt', '.log']:
                extra = os.path.join(UPLOAD_DIR, filename + ext if ext != '_req.txt' else f"{filename}_req.txt")
                if os.path.exists(extra): os.remove(extra)
                
            await update.message.reply_text(f"🗑️ Deleted {filename} and associated files.")
        except Exception as e:
            await update.message.reply_text(f"❌ Error deleting: {e}")

    elif action == "log":
        log_path = os.path.join(UPLOAD_DIR, f"{filename}.log")
        if os.path.exists(log_path):
            await update.message.reply_document(document=open(log_path, 'rb'))
        else:
            await update.message.reply_text("❌ No log file found.")

# --- MAIN ---

if __name__ == '__main__':
    # Start Keep Alive for Render
    keep_alive()
    
    # Initialize Bot
    application = ApplicationBuilder().token(TOKEN).build()

    # Conversation Handler
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('runpy', runpy_start)],
        states={
            WAIT_PY: [MessageHandler(filters.Document.FileExtension("py"), receive_py)],
            WAIT_REQ: [
                MessageHandler(filters.Regex('^(Yes|yes)$'), lambda u,c: receive_req_response(u,c)),
                MessageHandler(filters.Regex('^(No|no)$'), receive_req_response),
                MessageHandler(filters.Document.FileExtension("txt"), receive_req_file)
            ],
            WAIT_ENV: [
                MessageHandler(filters.Regex('^(Yes|yes)$'), lambda u,c: receive_env_response(u,c)),
                MessageHandler(filters.Regex('^(No|no)$'), receive_env_response),
                MessageHandler(filters.Document.FileExtension("env"), receive_env_file)
            ]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )

    application.add_handler(conv_handler)
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('bpy', bpy))
    application.add_handler(CommandHandler('list', list_files))
    application.add_handler(CommandHandler('manage', manage))

    print("Bot is running...")
    application.run_polling()
