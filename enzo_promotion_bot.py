#!/usr/bin/env python3
# enzo_promo_bot.py
# Full Enzo Promotion Bot main file (requires config.py in same folder)

import logging
import sqlite3
from datetime import datetime
from uuid import uuid4

import telebot
from telebot import types

# ----------------- load config -----------------
try:
    from config import BOT_TOKEN, ADMIN_IDS, WELCOME_GIF_FILE_ID, DB_PATH
except Exception as e:
    raise RuntimeError("Missing config.py with BOT_TOKEN, ADMIN_IDS, WELCOME_GIF_FILE_ID, DB_PATH") from e

# ----------------- init -----------------
logging.basicConfig(level=logging.INFO)
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# ----------------- DB -----------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS orders (
        id TEXT PRIMARY KEY,
        telegram_id INTEGER,
        username TEXT,
        service TEXT,
        package_group TEXT,
        package_qty TEXT,
        price TEXT,
        link_or_username TEXT,
        payment_method TEXT,
        receipt_file_id TEXT,
        status TEXT,
        created_at TEXT
    )
    """)
    conn.commit()
    conn.close()

init_db()

# ----------------- state (simple FSM) -----------------
USER_STATE = {}  # user_id -> {"stage": str, "order_id": str}

def set_state(user_id, stage, order_id=None):
    USER_STATE[user_id] = {"stage": stage, "order_id": order_id}

def get_state(user_id):
    return USER_STATE.get(user_id, {"stage": None, "order_id": None})

def clear_state(user_id):
    USER_STATE.pop(user_id, None)

# ----------------- utilities -----------------
def new_order_id():
    return str(uuid4())[:12]

def db_insert_order(order):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
    INSERT INTO orders (
        id, telegram_id, username, service, package_group, package_qty, price,
        link_or_username, payment_method, receipt_file_id, status, created_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        order['id'], order['telegram_id'], order.get('username',''),
        order.get('service',''), order.get('package_group',''), order.get('package_qty',''), order.get('price',''),
        order.get('link_or_username',''), order.get('payment_method',''), order.get('receipt_file_id',''),
        order.get('status','created'), order.get('created_at', datetime.utcnow().isoformat())
    ))
    conn.commit()
    conn.close()

def db_update_order_field(order_id, field, value):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # safe-ish update
    c.execute(f"UPDATE orders SET {field}=? WHERE id=?", (value, order_id))
    conn.commit()
    conn.close()

def db_get_order(order_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
    SELECT id, telegram_id, username, service, package_group, package_qty, price, link_or_username, payment_method, receipt_file_id, status, created_at
    FROM orders WHERE id=?
    """, (order_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return None
    keys = ["id","telegram_id","username","service","package_group","package_qty","price","link_or_username","payment_method","receipt_file_id","status","created_at"]
    return dict(zip(keys, row))

def notify_admins_with_receipt(order, photo_file_id):
    # compose admin message
    text = (
        f"📥 New Payment Received\n\n"
        f"🧾 Order ID: {order['id']}\n"
        f"👤 User: @{order['username'] or 'N/A'} (ID: {order['telegram_id']})\n"
        f"📱 Service: {order['service']}\n"
        f"📦 Package: {order['package_group']} — {order['package_qty']}\n"
        f"💰 Price: {order['price']}\n"
        f"🔗 Link/Username: {order['link_or_username']}\n"
        f"🏦 Payment Method: {order['payment_method'] or 'N/A'}\n"
        f"Status: pending_verification"
    )
    for aid in ADMIN_IDS:
        try:
            # send photo with caption (Telegram has limits on caption length)
            bot.send_photo(aid, photo_file_id, caption=text)
        except Exception:
            # fallback: send text then forward the user's photo message (we don't have message_id here)
            try:
                bot.send_message(aid, text)
            except Exception as e:
                logging.warning("Failed to notify admin %s: %s", aid, e)

# ----------------- services & packages -----------------
# Format: service -> [ (group_label, [ (qty_label, price_str), ... ]) ]
SERVICES = {
    "TikTok": [
        ("TikTok Followers", [("100","8.99 ETB"), ("500","39.99 ETB"), ("1000","69.99 ETB")]),
        ("TikTok Views", [("100","4.99 ETB"), ("500","19.99 ETB"), ("1000","34.99 ETB")]),
        ("TikTok Likes", [("100","7.99 ETB"), ("500","29.99 ETB")]),
        ("TikTok Shares", [("100","6.99 ETB"), ("500","24.99 ETB")]),
        ("TikTok Saves", [("100","5.99 ETB"), ("500","19.99 ETB")]),
    ],
    "Instagram": [
        ("Instagram Followers", [("100","9.99 ETB"), ("500","44.99 ETB")]),
        ("Instagram Likes", [("100","7.99 ETB"), ("500","29.99 ETB")]),
        ("Instagram Views", [("1000","12.99 ETB"), ("5000","49.99 ETB")]),
    ],
    "YouTube": [
        ("YouTube Subscribers", [("100","15.99 ETB"), ("500","69.99 ETB")]),
        ("YouTube Views", [("100","6.99 ETB"), ("500","24.99 ETB"), ("1000","39.99 ETB")]),
        ("YouTube Likes", [("100","8.99 ETB"), ("500","34.99 ETB")]),
    ],
    "Telegram": [
        ("Telegram Members", [("100","12.99 ETB"), ("500","49.99 ETB"), ("1000","89.99 ETB")]),
        ("Telegram Reactions", [("100","5.99 ETB"), ("500","19.99 ETB")]),
        ("Telegram Post Views", [("1000","9.99 ETB"), ("5000","39.99 ETB")]),
    ],
    "Facebook": [
        ("Facebook Page Followers", [("100","9.99 ETB"), ("500","39.99 ETB")]),
        ("Facebook Post Likes", [("100","7.99 ETB"), ("500","29.99 ETB")]),
        ("Facebook Followers", [("100","8.99 ETB"), ("500","34.99 ETB")]),
    ],
}

# ----------------- keyb builders -----------------
def kb_welcome():
    kb = types.InlineKeyboardMarkup(row_width=1)
    for svc in SERVICES.keys():
        kb.add(types.InlineKeyboardButton(svc, callback_data=f"svc|{svc}"))
    return kb

def kb_service_groups(service):
    kb = types.InlineKeyboardMarkup(row_width=1)
    groups = SERVICES.get(service, [])
    for idx, (group_label, _packages) in enumerate(groups):
        kb.add(types.InlineKeyboardButton(group_label, callback_data=f"grp|{service}|{idx}"))
    kb.row(
        types.InlineKeyboardButton("⬅️ Back", callback_data="back|welcome"),
        types.InlineKeyboardButton("❌ Cancel", callback_data="cancel|flow")
    )
    return kb

def kb_packages(service, group_idx):
    kb = types.InlineKeyboardMarkup(row_width=1)
    groups = SERVICES.get(service, [])
    if group_idx < 0 or group_idx >= len(groups):
        return kb
    group_label, packages = groups[group_idx]
    for p_idx, (qty_label, price_label) in enumerate(packages):
        kb.add(types.InlineKeyboardButton(f"{qty_label} {group_label.split()[-1]} - {price_label}", 
                                          callback_data=f"pkg|{service}|{group_idx}|{p_idx}"))
    kb.row(
        types.InlineKeyboardButton("⬅️ Back", callback_data=f"back|service|{service}"),
        types.InlineKeyboardButton("❌ Cancel", callback_data="cancel|flow")
    )
    return kb

def kb_order_confirm(order_id):
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("Submit Order", callback_data=f"submit|{order_id}"),
        types.InlineKeyboardButton("Change Link/Username", callback_data=f"change|{order_id}"),
        types.InlineKeyboardButton("❌ Cancel Order", callback_data=f"cancel_order|{order_id}")
    )
    return kb

def kb_payment_methods(order_id):
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("Telebirr", callback_data=f"pay|{order_id}|telebirr"),
        types.InlineKeyboardButton("CBE Mobile Banking", callback_data=f"pay|{order_id}|cbe"),
        types.InlineKeyboardButton("Abyssinia Bank", callback_data=f"pay|{order_id}|abyssinia")
    )
    kb.row(types.InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_order|{order_id}"))
    return kb

def kb_attach_receipt(order_id):
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("📎 Attach Receipt", callback_data=f"attach|{order_id}"))
    kb.row(types.InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_order|{order_id}"))
    return kb

def rb_cancel():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add(types.KeyboardButton("❌ Cancel"))
    return kb

# ----------------- messages -----------------
WELCOME_TEXT = ("ሰላም 👋 እንኳን ወደ Enzo ፕሮሞሽን የ ማስታወቅያ ድርጅት በሰላም መጡ! "
                "የ Enzo የማስተወቅያ ቴክኖሎጂ በመጠቀም በ Telegram, TikTok, Facebook , Instagram , YouTube ላይ "
                "ተከታይ፣ ላይክ፣ ቪው በመግዛት እና ሌሎች አገልግሎቶችን በመጠቀም እውቅናዎን ያሳድጉ! "
                "ለበለጠ መረጃ በ 0960480854 ይደውሉ ወይም @danbm0560 ላይ መልክት ይላኩልን።\n\n"
                "ምን ማስራት ይፈልጋሉ?")

LINK_PROMPT_VIDEO = "💬 Please provide your post/video link (eg. @enzopromo)"
LINK_PROMPT_ACCOUNT = "💬 Please provide your username/account (eg. @enzopromo)"
PAYMENT_INSTRUCTION = "💵 Please complete the payment and upload a screenshot of your receipt."
ORDER_RECEIVED_CONFIRM = "✅ We received your payment screenshot. Finance will check and we'll notify you."

# ----------------- helper to decide prompt -----------------
def expects_username(group_label):
    lower = group_label.lower()
    return any(k in lower for k in ["follower", "subscriber", "members", "member", "page followers"])

# ----------------- /start handler -----------------
@bot.message_handler(commands=['start'])
def handle_start(m):
    clear_state(m.from_user.id)
    # send gif if provided
    try:
        if WELCOME_GIF_FILE_ID:
            bot.send_animation(m.chat.id, WELCOME_GIF_FILE_ID)
    except Exception:
        pass
    bot.send_message(m.chat.id, WELCOME_TEXT, reply_markup=kb_welcome())

# ----------------- callback handler -----------------
@bot.callback_query_handler(func=lambda call: True)
def callback_router(call):
    uid = call.from_user.id
    data = call.data or ""
    # CANCEL flows
    if data.startswith("cancel"):
        parts = data.split("|")
        if parts[0] in ("cancel", "cancel|flow"):
            clear_state(uid)
            bot.answer_callback_query(call.id, "Cancelled.")
            bot.send_message(call.message.chat.id, "Operation cancelled.", reply_markup=kb_welcome())
            return
        if parts[0] == "cancel_order" and len(parts) == 2:
            oid = parts[1]
            db_update_order_field(oid, "status", "cancelled")
            clear_state(uid)
            bot.answer_callback_query(call.id, "Order cancelled.")
            bot.send_message(call.message.chat.id, "Order cancelled.", reply_markup=kb_welcome())
            return

    # BACK navigation
    if data.startswith("back|"):
        parts = data.split("|")
        if len(parts) >= 2 and parts[1] == "welcome":
            clear_state(uid)
            bot.answer_callback_query(call.id, "Back.")
            bot.edit_message_text("Choose a platform:", call.message.chat.id, call.message.message_id, reply_markup=kb_welcome())
            return
        if len(parts) >= 3 and parts[1] == "service":
            svc = parts[2]
            bot.answer_callback_query(call.id, "Back to service groups.")
            bot.edit_message_text(f"Choose a package for {svc}:", call.message.chat.id, call.message.message_id, reply_markup=kb_service_groups(svc))
            return

    # service selected
    if data.startswith("svc|"):
        _, svc = data.split("|", 1)
        bot.answer_callback_query(call.id, f"{svc} selected.")
        bot.edit_message_text(f"Choose the type of package for {svc}:", call.message.chat.id, call.message.message_id, reply_markup=kb_service_groups(svc))
        return

    # group chosen
    if data.startswith("grp|"):
        parts = data.split("|")
        if len(parts) != 3:
            bot.answer_callback_query(call.id, "Bad data.")
            return
        svc = parts[1]; gidx = int(parts[2])
        bot.answer_callback_query(call.id, "Choose quantity.")
        bot.edit_message_text("Choose quantity:", call.message.chat.id, call.message.message_id, reply_markup=kb_packages(svc, gidx))
        return

    # package quantity selected
    if data.startswith("pkg|"):
        parts = data.split("|")
        if len(parts) != 4:
            bot.answer_callback_query(call.id, "Bad data.")
            return
        svc, gidx_s, pidx_s = parts[1], parts[2], parts[3]
        try:
            gidx = int(gidx_s); pidx = int(pidx_s)
        except:
            bot.answer_callback_query(call.id, "Bad indexes.")
            return
        groups = SERVICES.get(svc, [])
        if gidx < 0 or gidx >= len(groups):
            bot.answer_callback_query(call.id, "Invalid group.")
            return
        group_label, packages = groups[gidx]
        if pidx < 0 or pidx >= len(packages):
            bot.answer_callback_query(call.id, "Invalid package.")
            return
        qty_label, price_label = packages[pidx]

        # create order stub in DB and set user to waiting_for_link_or_username
        order_id = new_order_id()
        order = {
            "id": order_id,
            "telegram_id": uid,
            "username": call.from_user.username or "",
            "service": svc,
            "package_group": group_label,
            "package_qty": qty_label,
            "price": price_label,
            "link_or_username": None,
            "payment_method": None,
            "receipt_file_id": None,
            "status": "created",
            "created_at": datetime.utcnow().isoformat()
        }
        db_insert_order(order)
        set_state(uid, "waiting_for_link_or_username", order_id)
        bot.answer_callback_query(call.id, "Provide required info.")

        # decide prompt
        if expects_username(group_label):
            bot.send_message(call.message.chat.id, LINK_PROMPT_ACCOUNT, reply_markup=rb_cancel())
        else:
            bot.send_message(call.message.chat.id, LINK_PROMPT_VIDEO, reply_markup=rb_cancel())
        return

    # order flow actions: submit, change, attach
    if data.startswith("submit|") or data.startswith("change|") or data.startswith("attach|"):
        parts = data.split("|")
        action = parts[0]
        if len(parts) < 2:
            bot.answer_callback_query(call.id, "Bad action.")
            return
        oid = parts[1]
        order = db_get_order(oid)
        if not order:
            bot.answer_callback_query(call.id, "Order not found.")
            return

        if action == "change":
            # ask user for new link/username
            set_state(uid, "changing_link_or_username", oid)
            if expects_username(order['package_group']):
                bot.send_message(call.message.chat.id, LINK_PROMPT_ACCOUNT, reply_markup=rb_cancel())
            else:
                bot.send_message(call.message.chat.id, LINK_PROMPT_VIDEO, reply_markup=rb_cancel())
            bot.answer_callback_query(call.id, "Send the new link/username now.")
            return

        if action == "submit":
            # choose payment method next
            set_state(uid, "waiting_payment_method", oid)
            bot.answer_callback_query(call.id, "Choose payment method.")
            bot.send_message(call.message.chat.id, "Choose payment method:", reply_markup=kb_payment_methods(oid))
            return

        if action == "attach":
            # ask user to upload receipt
            set_state(uid, "waiting_for_receipt", oid)
            bot.answer_callback_query(call.id, "Attach receipt.")
            bot.send_message(call.message.chat.id, "📸 Please upload a screenshot or photo of your payment receipt now:", reply_markup=rb_cancel())
            return

    # payment selected
    if data.startswith("pay|"):
        parts = data.split("|")
        if len(parts) != 3:
            bot.answer_callback_query(call.id, "Bad payment data.")
            return
        _, oid, method = parts
        order = db_get_order(oid)
        if not order:
            bot.answer_callback_query(call.id, "Order not found.")
            return
        db_update_order_field(oid, "payment_method", method)
        db_update_order_field(oid, "status", "awaiting_receipt")
        set_state(uid, "waiting_for_receipt", oid)
        bot.answer_callback_query(call.id, f"{method} selected.")
        if method == "telebirr":
            bot.send_message(call.message.chat.id, "Telebirr selected. Please transfer and upload the receipt when ready.", reply_markup=kb_attach_receipt(oid))
        elif method == "cbe":
            bot.send_message(call.message.chat.id, f"🏦 CBE Account:\n- Account Number: 1000498236271\n- Account Holder: Eyuel Abebe Bantie\n\nAmount: {order['price']}\n\nUpload receipt:", reply_markup=kb_attach_receipt(oid))
        elif method == "abyssinia":
            bot.send_message(call.message.chat.id, f"🏦 Abyssinia Bank:\n- Account Number: 236188477\n- Account Holder: Eyuel Abebe Bantie\n\nAmount: {order['price']}\n\nUpload receipt:", reply_markup=kb_attach_receipt(oid))
        else:
            bot.send_message(call.message.chat.id, "Selected payment method. Upload receipt when ready.", reply_markup=kb_attach_receipt(oid))
        return

    # unknown fallback
    bot.answer_callback_query(call.id, "Unknown action. Use /start to begin.")

# ----------------- text handlers -----------------
@bot.message_handler(func=lambda m: m.text and m.text.strip().lower() == "❌ cancel")
def text_cancel(m):
    clear_state(m.from_user.id)
    bot.send_message(m.chat.id, "Operation cancelled.", reply_markup=kb_welcome())

@bot.message_handler(func=lambda m: True, content_types=['text'])
def text_router(m):
    uid = m.from_user.id
    st = get_state(uid)
    stage = st.get("stage")
    oid = st.get("order_id")

    # changing link/username for existing order
    if stage == "changing_link_or_username" and oid:
        db_update_order_field(oid, "link_or_username", m.text.strip())
        db_update_order_field(oid, "status", "link_updated")
        bot.send_message(m.chat.id, "Updated. Please Submit Order when ready.", reply_markup=kb_order_confirm(oid))
        clear_state(uid)
        return

    # waiting for link or username (after package selection)
    if stage == "waiting_for_link_or_username" and oid:
        db_update_order_field(oid, "link_or_username", m.text.strip())
        db_update_order_field(oid, "status", "link_received")
        order = db_get_order(oid)
        summary = (
            f"Order Information\n"
            f"Service: {order['service']}\n"
            f"Package: {order['package_group']} — {order['package_qty']}\n"
            f"Price: {order['price']}\n"
            f"Link/Username: {order['link_or_username']}\n\n"
            "If everything is correct, press Submit Order. Otherwise, Change Link/Username or Cancel."
        )
        bot.send_message(m.chat.id, summary, reply_markup=kb_order_confirm(oid))
        clear_state(uid)
        return

    # waiting for payment method selection typed as text (user typed instead of using buttons)
    if stage == "waiting_payment_method":
        bot.send_message(m.chat.id, "Please pick a payment method using the buttons.", reply_markup=kb_payment_methods(oid))
        return

    # waiting for receipt but user typed text
    if stage == "waiting_for_receipt":
        bot.send_message(m.chat.id, "Please upload a photo or document of your receipt. Use the Attach Receipt button or send the image now.", reply_markup=rb_cancel())
        return

    # default fallback
    # if user typed a platform name directly, show its packages
    text = (m.text or "").strip()
    if text in SERVICES:
        bot.send_message(m.chat.id, f"Choose package types for {text}:", reply_markup=kb_service_groups(text))
        return

    # if user typed something like "TikTok Followers", find matching price list
    for svc, groups in SERVICES.items():
        for gid, (glabel, packages) in enumerate(groups):
            # glabel includes full social media name like "TikTok Followers"
            if text.lower() == glabel.lower() or text.lower().startswith(glabel.lower()):
                # show packages for this group
                bot.send_message(m.chat.id, "Choose package quantity:", reply_markup=kb_packages(svc, gid))
                return

    bot.send_message(m.chat.id, "I didn't understand that. Use /start to begin.", reply_markup=kb_welcome())

# ----------------- media handler (receipt) -----------------
@bot.message_handler(content_types=['photo','document'])
def media_handler(m):
    uid = m.from_user.id
    st = get_state(uid)
    stage = st.get("stage")
    oid = st.get("order_id")

    # expected receipt
    if stage == "waiting_for_receipt" and oid:
        # extract file_id
        file_id = None
        if m.photo:
            file_id = m.photo[-1].file_id
        elif m.document:
            file_id = m.document.file_id

        if not file_id:
            bot.send_message(m.chat.id, "Could not read the file. Send a photo or document file.", reply_markup=rb_cancel())
            return

        # update DB
        db_update_order_field(oid, "receipt_file_id", file_id)
        db_update_order_field(oid, "status", "pending_verification")

        # fetch order and notify admins with photo + details
        order = db_get_order(oid)
        notify_admins_with_receipt(order, file_id)

        # confirm to user
        bot.send_message(m.chat.id, ORDER_RECEIVED_CONFIRM, reply_markup=kb_welcome())
        clear_state(uid)
        return

    # if not expected
    bot.send_message(m.chat.id, "I wasn't expecting a file now. If you want to attach a receipt, first create an order and choose a payment method.", reply_markup=kb_welcome())

# ----------------- admin commands -----------------
def is_admin(uid):
    return uid in ADMIN_IDS

@bot.message_handler(commands=['orders'])
def cmd_orders(m):
    if not is_admin(m.from_user.id):
        bot.reply_to(m, "You are not allowed to use this.")
        return
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, telegram_id, username, service, package_group, package_qty, price, status, created_at FROM orders ORDER BY created_at DESC LIMIT 30")
    rows = c.fetchall()
    conn.close()
    if not rows:
        bot.send_message(m.chat.id, "No orders found.")
        return
    lines = []
    for r in rows:
        lines.append(f"ID:{r[0]} User:{r[2] or r[1]} Service:{r[3]} {r[4]}-{r[5]} Price:{r[6]} Status:{r[7]} At:{r[8]}")
    bot.send_message(m.chat.id, "\n\n".join(lines))

@bot.message_handler(commands=['approve'])
def cmd_approve(m):
    if not is_admin(m.from_user.id):
        bot.reply_to(m, "Denied.")
        return
    parts = m.text.strip().split()
    if len(parts) < 2:
        bot.reply_to(m, "Usage: /approve <order_id>")
        return
    oid = parts[1].strip()
    order = db_get_order(oid)
    if not order:
        bot.reply_to(m, "Order not found.")
        return
    db_update_order_field(oid, "status", "processing")
    try:
        bot.send_message(order['telegram_id'], f"🔄 Your order {oid} is now being processed.")
    except Exception:
        pass
    bot.reply_to(m, f"Order {oid} marked processing.")

@bot.message_handler(commands=['done'])
def cmd_done(m):
    if not is_admin(m.from_user.id):
        bot.reply_to(m, "Denied.")
        return
    parts = m.text.strip().split()
    if len(parts) < 2:
        bot.reply_to(m, "Usage: /done <order_id>")
        return
    oid = parts[1].strip()
    order = db_get_order(oid)
    if not order:
        bot.reply_to(m, "Order not found.")
        return
    db_update_order_field(oid, "status", "done")
    try:
        bot.send_message(order['telegram_id'], f"✅ Your order {oid} is complete. Thank you!")
    except Exception:
        pass
    bot.reply_to(m, f"Order {oid} marked done.")

# ----------------- run -----------------
if __name__ == "__main__":
    logging.info("Starting Enzo Promotion Bot...")
    bot.infinity_polling()
