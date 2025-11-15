# promo_bot.py
import os
import logging
import sqlite3
from datetime import datetime, timedelta
import asyncio

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler,
)

# ---------- CONFIG ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")  # Required
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
CHANNEL_IDS = [int(x) for x in os.getenv("CHANNEL_IDS", "").split(",") if x.strip()]  # where to post
DB_PATH = os.getenv("DB_PATH", "promotions.db")
POST_CHECK_INTERVAL = int(os.getenv("POST_CHECK_INTERVAL", "15"))  # seconds
RATE_LIMIT_PER_DAY = int(os.getenv("RATE_LIMIT_PER_DAY", "3"))
# ----------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------- DB helpers ----------
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(
            """CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                tg_id INTEGER UNIQUE,
                name TEXT,
                registered_at TEXT
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS promotions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                tg_user_id INTEGER,
                content_type TEXT,
                media_file_id TEXT,
                caption TEXT,
                price REAL DEFAULT 0,
                payment_proof TEXT,
                status TEXT,
                admin_note TEXT,
                scheduled_at TEXT,
                created_at TEXT
            )"""
        )
        conn.commit()

def db_add_user(tg_id, name):
    now = datetime.utcnow().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(
            "INSERT OR IGNORE INTO users (tg_id, name, registered_at) VALUES (?, ?, ?)",
            (tg_id, name, now),
        )
        conn.commit()

def db_create_promo(tg_user_id, content_type, media_file_id, caption, price, scheduled_at=None):
    now = datetime.utcnow().isoformat()
    status = "pending"
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(
            "INSERT INTO promotions (user_id, tg_user_id, content_type, media_file_id, caption, price, status, scheduled_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (None, tg_user_id, content_type, media_file_id, caption, price, status, scheduled_at, now),
        )
        promo_id = c.lastrowid
        conn.commit()
        return promo_id

def db_set_payment_proof(promo_id, proof):
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("UPDATE promotions SET payment_proof = ? WHERE id = ?", (proof, promo_id))
        conn.commit()

def db_get_pending():
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("SELECT id, tg_user_id, content_type, caption, media_file_id, price, created_at FROM promotions WHERE status = 'pending' ORDER BY created_at ASC")
        return c.fetchall()

def db_update_status(promo_id, status, admin_note=None):
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("UPDATE promotions SET status = ?, admin_note = ? WHERE id = ?", (status, admin_note, promo_id))
        conn.commit()

def db_get_promo(promo_id):
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM promotions WHERE id = ?", (promo_id,))
        return c.fetchone()

def db_get_due_promos():
    now = datetime.utcnow().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        # approved and scheduled_at <= now OR approved and scheduled_at is null (post immediately)
        c.execute("""SELECT id, tg_user_id, content_type, caption, media_file_id FROM promotions
                     WHERE status = 'approved' AND (scheduled_at IS NULL OR scheduled_at <= ?)""", (now,))
        return c.fetchall()

def db_mark_posted(promo_id):
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("UPDATE promotions SET status = 'posted' WHERE id = ?", (promo_id,))
        conn.commit()

def db_user_daily_count(tg_user_id):
    since = (datetime.utcnow() - timedelta(days=1)).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM promotions WHERE tg_user_id = ? AND created_at >= ?", (tg_user_id, since))
        return c.fetchone()[0]

def db_user_promos(tg_user_id):
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("SELECT id, content_type, caption, status, created_at FROM promotions WHERE tg_user_id = ? ORDER BY created_at DESC", (tg_user_id,))
        return c.fetchall()

# ---------- Bot Handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_add_user(user.id, user.full_name)
    text = (
        f"Hello {user.first_name}! 👋\n\n"
        "This is Promotion Bot. Send me the promo text or media you'd like to post, then tell me the price/package and upload payment proof. Admin will review and publish.\n\n"
        "Commands:\n"
        "/newpromo - create a new promotion\n"
        "/my_promos - view your promotions\n"
        "/help - help\n"
    )
    await update.message.reply_text(text)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Use /newpromo to create, /my_promos to view, or contact support.")

# create promotion: simplified flow
# 1) user sends /newpromo, bot asks for media or text
# 2) user sends media/text; bot asks for price and scheduled datetime (optional)
# 3) user sends payment proof
async def newpromo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user_id = update.effective_user.id
    count = db_user_daily_count(tg_user_id)
    if count >= RATE_LIMIT_PER_DAY:
        await update.message.reply_text(f"You have reached the daily limit ({RATE_LIMIT_PER_DAY}) for submissions.")
        return
    context.user_data['creating_promo'] = True
    await update.message.reply_text("Send the promo text, or send a photo/video with a caption. When done, reply with /done to submit or /cancel to abort.")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop('creating_promo', None)
    context.user_data.pop('promo_media', None)
    context.user_data.pop('promo_caption', None)
    await update.message.reply_text("Promo creation cancelled.")

async def handle_media_or_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('creating_promo'):
        return  # ignore for now

    # store media or text
    msg = update.message
    media_file_id = None
    content_type = 'text'
    caption = msg.text or ""
    if msg.photo:
        # choose highest res
        media_file_id = msg.photo[-1].file_id
        content_type = 'photo'
        caption = caption or msg.caption or ""
    elif msg.video:
        media_file_id = msg.video.file_id
        content_type = 'video'
        caption = caption or msg.caption or ""
    elif msg.document:
        media_file_id = msg.document.file_id
        content_type = 'document'
        caption = caption or msg.caption or ""
    else:
        # plain text
        content_type = 'text'
        caption = msg.text

    context.user_data['promo_media'] = media_file_id
    context.user_data['promo_caption'] = caption
    context.user_data['promo_content_type'] = content_type

    await update.message.reply_text(
        "Got it. Now reply with the price (number) or package name (e.g. 'standard'), and optionally include scheduled datetime in ISO (YYYY-MM-DD HH:MM) separated by a '|'.\n"
        "Example: `10` or `standard | 2025-11-12 15:00`\n\n"
        "After you send that, upload payment proof (image) or a transaction ID."
    )

async def price_and_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('creating_promo'):
        return
    text = update.message.text.strip()
    parts = [p.strip() for p in text.split("|")]
    price_part = parts[0]
    scheduled = None
    try:
        price = float(price_part)
    except:
        price = 0.0
    if len(parts) > 1:
        try:
            scheduled = datetime.fromisoformat(parts[1]).isoformat()
        except Exception as e:
            scheduled = None

    context.user_data['promo_price'] = price
    context.user_data['promo_scheduled'] = scheduled

    # create promo in DB (status pending) -> user needs to send payment proof next
    promo_id = db_create_promo(
        tg_user_id=update.effective_user.id,
        content_type=context.user_data.get('promo_content_type') or 'text',
        media_file_id=context.user_data.get('promo_media'),
        caption=context.user_data.get('promo_caption') or "",
        price=price,
        scheduled_at=scheduled,
    )
    context.user_data['last_promo_id'] = promo_id

    await update.message.reply_text(
        f"Promo saved as ID #{promo_id}. Now please upload payment proof image or send the transaction ID (text). Admin will review when payment proof is received."
    )

async def payment_proof_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Accept image or text and attach to last_promo_id
    promo_id = context.user_data.get('last_promo_id')
    if not promo_id:
        await update.message.reply_text("No promo in progress. Start with /newpromo.")
        return
    proof = None
    if update.message.photo:
        proof = update.message.photo[-1].file_id
    elif update.message.document:
        proof = update.message.document.file_id
    elif update.message.text:
        proof = update.message.text.strip()
    else:
        await update.message.reply_text("Couldn't read that. Please send an image or the transaction id as text.")
        return

    db_set_payment_proof(promo_id, proof)

    # notify admins
    text = f"New payment proof for promo #{promo_id}. Review with /pending"
    for aid in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=aid, text=text)
        except Exception as e:
            logger.warning("Could not notify admin %s: %s", aid, e)

    context.user_data.pop('creating_promo', None)
    await update.message.reply_text(f"Payment proof saved. Promo #{promo_id} is pending admin review. We'll notify you when approved.")

# Admin handlers
async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Unauthorized.")
        return
    pending = db_get_pending()
    if not pending:
        await update.message.reply_text("No pending promotions.")
        return
    texts = []
    for row in pending:
        pid, uid, ctype, caption, media_file_id, price, created_at = row
        txt = f"ID: {pid}\nFrom: {uid}\nType: {ctype}\nPrice: {price}\nCreated: {created_at}\nCaption: {caption[:300]}"
        texts.append(txt)
        # If media exists, send it
        if media_file_id:
            try:
                if ctype == 'photo':
                    await context.bot.send_photo(chat_id=update.effective_chat.id, photo=media_file_id, caption=txt)
                elif ctype == 'video':
                    await context.bot.send_video(chat_id=update.effective_chat.id, video=media_file_id, caption=txt)
                else:
                    await context.bot.send_message(chat_id=update.effective_chat.id, text=txt)
            except Exception:
                await context.bot.send_message(chat_id=update.effective_chat.id, text=txt)
        else:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=txt)
    await update.message.reply_text("Use /approve <id> or /reject <id> <reason> to manage promos.")

async def approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Unauthorized.")
        return
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /approve <promo_id>")
        return
    promo_id = int(args[0])
    db_update_status(promo_id, "approved", admin_note=f"Approved by {update.effective_user.id}")
    await update.message.reply_text(f"Promo #{promo_id} approved.")
    # notify user
    promo = db_get_promo(promo_id)
    if promo:
        tg_user = promo[2]  # tg_user_id (table columns from creation)
        try:
            await context.bot.send_message(chat_id=tg_user, text=f"Your promo #{promo_id} has been approved and will be posted soon.")
        except Exception:
            pass

async def reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Unauthorized.")
        return
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /reject <promo_id> [reason]")
        return
    promo_id = int(args[0])
    reason = " ".join(args[1:]) if len(args) > 1 else "No reason provided."
    db_update_status(promo_id, "rejected", admin_note=reason)
    await update.message.reply_text(f"Promo #{promo_id} rejected.")
    promo = db_get_promo(promo_id)
    if promo:
        tg_user = promo[2]
        try:
            await context.bot.send_message(chat_id=tg_user, text=f"Your promo #{promo_id} was rejected. Reason: {reason}")
        except Exception:
            pass

async def my_promos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user.id
    rows = db_user_promos(tg_user)
    if not rows:
        await update.message.reply_text("You have no promotions.")
        return
    msgs = []
    for r in rows:
        pid, ctype, caption, status, created_at = r
        msgs.append(f"#{pid} | {ctype} | {status} | {created_at}\n{(caption[:120] + '...') if caption and len(caption) > 120 else caption}")
    await update.message.reply_text("\n\n".join(msgs))

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Unauthorized.")
        return
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("SELECT status, COUNT(*) FROM promotions GROUP BY status")
        rows = c.fetchall()
    txt = "\n".join(f"{s}: {n}" for s, n in rows)
    await update.message.reply_text(f"Promotion stats:\n{txt}")

# Scheduler to post approved promotions when due
async def posting_loop(app):
    while True:
        try:
            due = db_get_due_promos()
            for promo in due:
                promo_id, tg_user_id, ctype, caption, media_file_id = promo
                # post to configured channels
                for ch in CHANNEL_IDS:
                    try:
                        if ctype == 'photo' and media_file_id:
                            await app.bot.send_photo(chat_id=ch, photo=media_file_id, caption=caption)
                        elif ctype == 'video' and media_file_id:
                            await app.bot.send_video(chat_id=ch, video=media_file_id, caption=caption)
                        else:
                            await app.bot.send_message(chat_id=ch, text=caption)
                    except Exception as e:
                        logger.exception("Failed to post promo %s to channel %s: %s", promo_id, ch, e)
                db_mark_posted(promo_id)
                # notify user
                try:
                    await app.bot.send_message(chat_id=tg_user_id, text=f"Your promo #{promo_id} has been posted.")
                except Exception:
                    pass
        except Exception as e:
            logger.exception("Error in posting loop: %s", e)
        await asyncio.sleep(POST_CHECK_INTERVAL)

# fallback message handler
async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Sorry, I didn't understand that. Use /help.")

# ---------- Main ----------
def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable required.")
    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("newpromo", newpromo))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("my_promos", my_promos))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("approve", approve))
    app.add_handler(CommandHandler("reject", reject))
    app.add_handler(CommandHandler("stats", stats))

    # Message handlers
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_media_or_text))
    app.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO | filters.DOCUMENT, handle_media_or_text))
    # price/schedule detection: if user sends text that looks like price or has '|' we treat as price
    app.add_handler(MessageHandler(filters.Regex(r".*\|.*") | filters.Regex(r"^\d+(\.\d+)?$"), price_and_schedule))
    # payment proof (photo or text) - catch all after a promo id exists in session
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), payment_proof_handler))
    app.add_handler(MessageHandler(filters.PHOTO | filters.DOCUMENT, payment_proof_handler))

    # fallback
    app.add_handler(MessageHandler(filters.ALL, unknown))

    # Start posting loop
    async def run():
        # run scheduler concurrently
        loop = asyncio.get_running_loop()
        loop.create_task(posting_loop(app))
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        await app.wait_closed()

    logger.info("Starting bot...")
    import asyncio
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")

if __name__ == "__main__":
    main()
