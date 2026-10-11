import asyncio
import logging
import os
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, FSInputFile
)
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
DB_PATH = os.getenv("DB_PATH", "data/store.db")
Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN is missing. Copy .env.example to .env and set your BotFather token.")

logging.basicConfig(level=logging.INFO)
bot = Bot(BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🛍 Buy"), KeyboardButton(text="👤 Profile")],
        [KeyboardButton(text="💰 Balance"), KeyboardButton(text="➕ Deposit")],
        [KeyboardButton(text="📦 My Orders"), KeyboardButton(text="🆘 Support")],
    ],
    resize_keyboard=True
)

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
            balance REAL NOT NULL DEFAULT 0, total_deposit REAL NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS products(
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            description TEXT DEFAULT '', price REAL NOT NULL CHECK(price >= 0),
            active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS stock(
            id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER NOT NULL,
            item TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'available',
            order_id INTEGER, added_at TEXT NOT NULL,
            FOREIGN KEY(product_id) REFERENCES products(id)
        );
        CREATE TABLE IF NOT EXISTS deposits(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            amount REAL NOT NULL, method TEXT NOT NULL, reference TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL,
            reviewed_by INTEGER, reviewed_at TEXT,
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        );
        CREATE TABLE IF NOT EXISTS orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL, quantity INTEGER NOT NULL,
            total REAL NOT NULL, created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(user_id),
            FOREIGN KEY(product_id) REFERENCES products(id)
        );
        CREATE TABLE IF NOT EXISTS ledger(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            delta REAL NOT NULL, kind TEXT NOT NULL, note TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('payment_instructions','Admin has not configured payment instructions yet.')")

def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def is_admin(user_id):
    return user_id in ADMIN_IDS

def user_upsert(u):
    with db() as c:
        c.execute("""INSERT INTO users(user_id,username,first_name,created_at)
        VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name""",
        (u.id, u.username or "", u.first_name or "", now()))

def get_user(uid):
    with db() as c:
        return c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()

def stock_count(pid):
    with db() as c:
        return c.execute("SELECT COUNT(*) n FROM stock WHERE product_id=? AND status='available'", (pid,)).fetchone()["n"]

def main_menu_text():
    return "Welcome to Digital Store!\nChoose an option below."

class Forms(StatesGroup):
    deposit_amount = State()
    deposit_method = State()
    deposit_reference = State()
    add_product = State()
    add_stock = State()
    set_payment = State()
    adjust_user = State()
    support = State()

def admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Add product", callback_data="admin:add_product"),
         InlineKeyboardButton(text="📥 Add TXT stock", callback_data="admin:add_stock")],
        [InlineKeyboardButton(text="💳 Payment instructions", callback_data="admin:set_payment")],
        [InlineKeyboardButton(text="🧾 Pending deposits", callback_data="admin:deposits")],
        [InlineKeyboardButton(text="➕/➖ Balance adjustment", callback_data="admin:adjust")],
        [InlineKeyboardButton(text="📊 Store summary", callback_data="admin:summary")],
    ])

@dp.message(CommandStart())
async def start(m: Message):
    user_upsert(m.from_user)
    await m.answer(main_menu_text(), reply_markup=MAIN_KB)

@dp.message(Command("help"))
async def help_cmd(m: Message):
    await m.answer("Use Buy to purchase in-stock items, Deposit to request a balance top-up, Profile to see your account. Admins: /admin")

@dp.message(Command("admin"))
async def admin(m: Message):
    if not is_admin(m.from_user.id):
        await m.answer("⛔ Admin access only.")
        return
    await m.answer("Admin Panel", reply_markup=admin_kb())

@dp.message(F.text == "👤 Profile")
async def profile(m: Message):
    user_upsert(m.from_user)
    u = get_user(m.from_user.id)
    with db() as c:
        spent = c.execute("SELECT COALESCE(SUM(total),0) s FROM orders WHERE user_id=?", (m.from_user.id,)).fetchone()["s"]
        count = c.execute("SELECT COUNT(*) n FROM orders WHERE user_id=?", (m.from_user.id,)).fetchone()["n"]
    await m.answer(
        f"👤 Profile\nID: {m.from_user.id}\nUsername: @{m.from_user.username or 'not set'}\n"
        f"Current balance: ৳{u['balance']:.2f}\nTotal deposits: ৳{u['total_deposit']:.2f}\n"
        f"Total purchases: ৳{spent:.2f}\nOrders: {count}",
        reply_markup=MAIN_KB
    )

@dp.message(F.text == "💰 Balance")
async def balance(m: Message):
    user_upsert(m.from_user)
    u = get_user(m.from_user.id)
    await m.answer(f"💰 Current balance: ৳{u['balance']:.2f}\nTotal deposited: ৳{u['total_deposit']:.2f}")

@dp.message(F.text == "🛍 Buy")
async def buy(m: Message):
    user_upsert(m.from_user)
    with db() as c:
        products = c.execute("SELECT * FROM products WHERE active=1 ORDER BY id DESC").fetchall()
    if not products:
        await m.answer("এখনো কোনো প্রোডাক্ট যোগ করা হয়নি।")
        return
    rows = []
    for p in products:
        qty = stock_count(p["id"])
        rows.append([InlineKeyboardButton(
            text=f"{p['name']} — ৳{p['price']:.2f} | Stock: {qty}",
            callback_data=f"product:{p['id']}"
        )])
    await m.answer("🛍 Products (price and available stock):", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

@dp.callback_query(F.data.startswith("product:"))
async def product_detail(q: CallbackQuery):
    pid = int(q.data.split(":")[1])
    with db() as c:
        p = c.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
    if not p:
        await q.answer("Product not found", show_alert=True); return
    qty = stock_count(pid)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Buy 1", callback_data=f"buy:{pid}:1"),
         InlineKeyboardButton(text="Buy 2", callback_data=f"buy:{pid}:2"),
         InlineKeyboardButton(text="Buy 5", callback_data=f"buy:{pid}:5")],
        [InlineKeyboardButton(text="Buy custom quantity", callback_data=f"custom:{pid}")]
    ])
    await q.message.answer(f"📦 {p['name']}\n{p['description']}\nPrice each: ৳{p['price']:.2f}\nStock: {qty}", reply_markup=kb)
    await q.answer()

@dp.callback_query(F.data.startswith("custom:"))
async def custom_buy(q: CallbackQuery, state: FSMContext):
    pid = int(q.data.split(":")[1])
    await state.update_data(custom_product_id=pid)
    await state.set_state(Forms.support)  # shared one-step text state
    await q.message.answer("কতটি কিনতে চান? শুধু পূর্ণসংখ্যা লিখুন।")
    await q.answer()

@dp.callback_query(F.data.startswith("buy:"))
async def buy_items(q: CallbackQuery):
    _, pid_s, qty_s = q.data.split(":")
    await process_purchase(q.from_user.id, int(pid_s), int(qty_s), q.message)
    await q.answer()

async def process_purchase(uid, pid, qty, message):
    if qty < 1 or qty > 500:
        await message.answer("পরিমাণ ১ থেকে ৫০০-এর মধ্যে দিন।"); return
    user_upsert_by_id(uid)
    delivered = []
    error = None
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        p = c.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
        u = c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
        if not p:
            error = "Product not found."
        else:
            total = round(float(p["price"]) * qty, 2)
            available = c.execute("SELECT id,item FROM stock WHERE product_id=? AND status='available' ORDER BY id LIMIT ?", (pid, qty)).fetchall()
            if len(available) < qty:
                error = f"স্টক যথেষ্ট নেই। এখন আছে {len(available)}টি।"
            elif not u or float(u["balance"]) < total:
                error = f"ব্যালেন্স যথেষ্ট নয়। মোট দরকার ৳{total:.2f}। Deposit করুন।"
            else:
                cur = c.execute("INSERT INTO orders(user_id,product_id,quantity,total,created_at) VALUES(?,?,?,?,?)", (uid,pid,qty,total,now()))
                oid = cur.lastrowid
                for row in available:
                    c.execute("UPDATE stock SET status='sold',order_id=? WHERE id=? AND status='available'", (oid,row["id"]))
                    delivered.append(row["item"])
                c.execute("UPDATE users SET balance=balance-? WHERE user_id=?", (total,uid))
                c.execute("INSERT INTO ledger(user_id,delta,kind,note,created_at) VALUES(?,?,?,?,?)", (uid,-total,"purchase",f"Order #{oid}",now()))
    if error:
        await message.answer(error); return
    # Each purchased item is sent as a line in a TXT file.
    out = Path(f"data/order_{uid}_{oid}.txt")
    out.write_text("\n".join(delivered) + "\n", encoding="utf-8")
    await message.answer_document(FSInputFile(out, filename=f"order_{oid}.txt"),
        caption=f"✅ Order #{oid} complete\nProduct: {p['name']}\nQuantity: {qty}\nPaid: ৳{total:.2f}")
    out.unlink(missing_ok=True)

def user_upsert_by_id(uid):
    with db() as c:
        c.execute("INSERT OR IGNORE INTO users(user_id,created_at) VALUES(?,?)", (uid,now()))

@dp.message(F.text == "➕ Deposit")
async def deposit_start(m: Message, state: FSMContext):
    user_upsert(m.from_user)
    with db() as c:
        instr = c.execute("SELECT value FROM settings WHERE key='payment_instructions'").fetchone()["value"]
    await state.set_state(Forms.deposit_amount)
    await m.answer(f"➕ Deposit\nPayment instructions:\n{instr}\n\nকত টাকা ডিপোজিট করতে চান? শুধু সংখ্যা লিখুন (যেমন 200)।")

@dp.message(Forms.deposit_amount)
async def deposit_amount(m: Message, state: FSMContext):
    try:
        amount = float(m.text.strip())
        if amount < 10 or amount > 1000000: raise ValueError()
    except Exception:
        await m.answer("সঠিক পরিমাণ দিন। সর্বনিম্ন ৳10।"); return
    await state.update_data(amount=round(amount,2))
    await state.set_state(Forms.deposit_method)
    await m.answer("কোন মাধ্যমে টাকা পাঠিয়েছেন? লিখুন: bKash / Nagad / অন্য")

@dp.message(Forms.deposit_method)
async def deposit_method(m: Message, state: FSMContext):
    await state.update_data(method=m.text.strip()[:50])
    await state.set_state(Forms.deposit_reference)
    await m.answer("Transaction ID / reference লিখুন। এটি Admin যাচাই করবে।")

@dp.message(Forms.deposit_reference)
async def deposit_reference(m: Message, state: FSMContext):
    data = await state.get_data()
    ref = (m.text or "").strip()
    if len(ref) < 4 or len(ref) > 100:
        await m.answer("সঠিক Transaction ID/reference লিখুন।"); return
    with db() as c:
        cur = c.execute("INSERT INTO deposits(user_id,amount,method,reference,created_at) VALUES(?,?,?,?,?)",
            (m.from_user.id,data["amount"],data["method"],ref,now()))
        did = cur.lastrowid
    await state.clear()
    await m.answer(f"Deposit request #{did} জমা হয়েছে। Admin যাচাই না করা পর্যন্ত ব্যালেন্স যোগ হবে না।")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Approve", callback_data=f"dep:approve:{did}"),
        InlineKeyboardButton(text="❌ Reject", callback_data=f"dep:reject:{did}")
    ]])
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(aid, f"Deposit #{did}\nUser: {m.from_user.id}\nAmount: ৳{data['amount']:.2f}\nMethod: {data['method']}\nReference: {ref}", reply_markup=kb)
        except Exception:
            logging.exception("Could not notify admin %s", aid)

@dp.callback_query(F.data.startswith("dep:"))
async def review_deposit(q: CallbackQuery):
    if not is_admin(q.from_user.id):
        await q.answer("Admin only", show_alert=True); return
    _, action, did_s = q.data.split(":")
    did = int(did_s)
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        d = c.execute("SELECT * FROM deposits WHERE id=?", (did,)).fetchone()
        if not d or d["status"] != "pending":
            await q.answer("Already reviewed or missing.", show_alert=True); return
        if action == "approve":
            c.execute("UPDATE deposits SET status='approved',reviewed_by=?,reviewed_at=? WHERE id=?", (q.from_user.id,now(),did))
            c.execute("UPDATE users SET balance=balance+?,total_deposit=total_deposit+? WHERE user_id=?", (d["amount"],d["amount"],d["user_id"]))
            c.execute("INSERT INTO ledger(user_id,delta,kind,note,created_at) VALUES(?,?,?,?,?)", (d["user_id"],d["amount"],"deposit",f"Deposit #{did}",now()))
        else:
            c.execute("UPDATE deposits SET status='rejected',reviewed_by=?,reviewed_at=? WHERE id=?", (q.from_user.id,now(),did))
    await q.message.edit_reply_markup(reply_markup=None)
    await q.answer("Updated")
    await q.message.answer(f"Deposit #{did}: {action}")
    try:
        await bot.send_message(d["user_id"], f"Deposit #{did} {action.upper()}." + (f" ৳{d['amount']:.2f} balance-এ যোগ হয়েছে।" if action == "approve" else " ব্যালেন্স যোগ হয়নি।"))
    except Exception:
        pass

@dp.message(F.text == "📦 My Orders")
async def my_orders(m: Message):
    with db() as c:
        rows = c.execute("""SELECT o.id,o.quantity,o.total,o.created_at,p.name FROM orders o
        JOIN products p ON p.id=o.product_id WHERE o.user_id=? ORDER BY o.id DESC LIMIT 10""", (m.from_user.id,)).fetchall()
    if not rows:
        await m.answer("এখনো কোনো অর্ডার নেই।"); return
    text = "📦 Recent orders:\n" + "\n".join([f"#{r['id']} {r['name']} ×{r['quantity']} — ৳{r['total']:.2f}" for r in rows])
    await m.answer(text)

@dp.message(F.text == "🆘 Support")
async def support(m: Message):
    await m.answer("সাহায্যের জন্য Admin-কে Telegram ID ও সমস্যার বিবরণ পাঠান।")

@dp.callback_query(F.data.startswith("admin:"))
async def admin_actions(q: CallbackQuery, state: FSMContext):
    if not is_admin(q.from_user.id):
        await q.answer("Admin only", show_alert=True); return
    action = q.data.split(":")[1]
    if action == "add_product":
        await state.set_state(Forms.add_product)
        await q.message.answer("এক লাইনে লিখুন: Product Name | Price | Description\nউদাহরণ: TXT Pack | 10 | One item per line")
    elif action == "add_stock":
        with db() as c:
            ps = c.execute("SELECT id,name FROM products WHERE active=1 ORDER BY id").fetchall()
        if not ps:
            await q.message.answer("আগে Add product দিয়ে প্রোডাক্ট তৈরি করুন."); await q.answer(); return
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"{p['name']} (ID {p['id']})", callback_data=f"stock:{p['id']}")] for p in ps])
        await q.message.answer("কোন প্রোডাক্টের স্টক যোগ করবেন?", reply_markup=kb)
    elif action == "set_payment":
        await state.set_state(Forms.set_payment)
        await q.message.answer("Deposit screen-এ দেখানোর নির্দেশনা লিখুন। নম্বর/পেমেন্ট বিবরণ দিন।\nউদাহরণ: bKash Send Money: 01XXXXXXXXX; টাকা পাঠিয়ে TxID দিন।")
    elif action == "deposits":
        with db() as c:
            rows = c.execute("SELECT * FROM deposits WHERE status='pending' ORDER BY id LIMIT 20").fetchall()
        if not rows:
            await q.message.answer("কোনো pending deposit নেই.")
        for d in rows:
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Approve", callback_data=f"dep:approve:{d['id']}"),InlineKeyboardButton(text="❌ Reject", callback_data=f"dep:reject:{d['id']}")]])
            await q.message.answer(f"Deposit #{d['id']} | User {d['user_id']} | ৳{d['amount']:.2f}\n{d['method']} | {d['reference']}", reply_markup=kb)
    elif action == "adjust":
        await state.set_state(Forms.adjust_user)
        await q.message.answer("এক লাইনে লিখুন: USER_ID | AMOUNT\nযোগ করতে 100, কমাতে -50। পরিবর্তনের রেকর্ড রাখা হবে।")
    elif action == "summary":
        with db() as c:
            users = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
            prods = c.execute("SELECT COUNT(*) n FROM products WHERE active=1").fetchone()["n"]
            stock = c.execute("SELECT COUNT(*) n FROM stock WHERE status='available'").fetchone()["n"]
            pend = c.execute("SELECT COUNT(*) n FROM deposits WHERE status='pending'").fetchone()["n"]
            sold = c.execute("SELECT COALESCE(SUM(total),0) s FROM orders").fetchone()["s"]
        await q.message.answer(f"📊 Store summary\nUsers: {users}\nProducts: {prods}\nAvailable stock items: {stock}\nPending deposits: {pend}\nTotal sales: ৳{sold:.2f}")
    await q.answer()

@dp.callback_query(F.data.startswith("stock:"))
async def stock_select(q: CallbackQuery, state: FSMContext):
    if not is_admin(q.from_user.id):
        await q.answer("Admin only", show_alert=True); return
    pid = int(q.data.split(":")[1])
    await state.update_data(stock_product_id=pid)
    await state.set_state(Forms.add_stock)
    await q.message.answer("এই প্রোডাক্টের জন্য .txt ফাইল পাঠান। প্রতি লাইনে একটি আইটেম থাকবে। ফাঁকা লাইন বাদ যাবে।")
    await q.answer()

@dp.message(Forms.add_product)
async def add_product(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): await state.clear(); return
    try:
        name, price, desc = [x.strip() for x in m.text.split("|", 2)]
        price = float(price)
        if not name or price < 0: raise ValueError()
    except Exception:
        await m.answer("ফরম্যাট ভুল। লিখুন: Product Name | Price | Description"); return
    with db() as c:
        cur = c.execute("INSERT INTO products(name,price,description,created_at) VALUES(?,?,?,?)", (name,price,desc,now()))
        pid = cur.lastrowid
    await state.clear()
    await m.answer(f"✅ Product created: {name}\nID: {pid}\nPrice: ৳{price:.2f}")

@dp.message(Forms.add_stock, F.document)
async def add_stock_file(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): await state.clear(); return
    doc = m.document
    if not doc.file_name.lower().endswith(".txt") or doc.file_size > 5_000_000:
        await m.answer("শুধু 5MB-এর মধ্যে .txt ফাইল দিন।"); return
    data = await state.get_data()
    pid = data.get("stock_product_id")
    if not pid:
        await m.answer("প্রোডাক্ট নির্বাচন হারিয়ে গেছে। /admin দিয়ে আবার শুরু করুন."); await state.clear(); return
    temp = Path("data") / f"upload_{m.from_user.id}.txt"
    await bot.download(doc, destination=temp)
    try:
        lines = [line.strip() for line in temp.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    except UnicodeDecodeError:
        await m.answer("ফাইলটি UTF-8 text হিসেবে পড়া যাচ্ছে না। UTF-8 TXT ফাইল দিন।")
        temp.unlink(missing_ok=True); return
    temp.unlink(missing_ok=True)
    if not lines:
        await m.answer("ফাইলে কোনো আইটেম নেই।"); return
    with db() as c:
        p = c.execute("SELECT id FROM products WHERE id=? AND active=1", (pid,)).fetchone()
        if not p:
            await m.answer("প্রোডাক্ট পাওয়া যায়নি."); await state.clear(); return
        c.executemany("INSERT INTO stock(product_id,item,status,added_at) VALUES(?,?,'available',?)", [(pid,line,now()) for line in lines])
    await state.clear()
    await m.answer(f"✅ {len(lines)}টি স্টক আইটেম যোগ হয়েছে।")

@dp.message(Forms.adjust_user)
async def adjust_balance(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): await state.clear(); return
    try:
        uid_s, amount_s = [x.strip() for x in m.text.split("|",1)]
        uid, amount = int(uid_s), round(float(amount_s),2)
        if amount == 0: raise ValueError()
    except Exception:
        await m.answer("ফরম্যাট ভুল। USER_ID | AMOUNT\nউদাহরণ: 123456789 | -50"); return
    user_upsert_by_id(uid)
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        u = c.execute("SELECT balance FROM users WHERE user_id=?", (uid,)).fetchone()
        if u["balance"] + amount < 0:
            await m.answer("এই পরিবর্তনে ব্যালেন্স শূন্যের নিচে যাবে।"); return
        c.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (amount,uid))
        c.execute("INSERT INTO ledger(user_id,delta,kind,note,created_at) VALUES(?,?,?,?,?)", (uid,amount,"admin_adjustment",f"Admin {m.from_user.id} adjustment",now()))
    await state.clear()
    await m.answer(f"✅ User {uid} balance adjusted by ৳{amount:.2f}.")
    try:
        await bot.send_message(uid, f"Admin balance adjustment: ৳{amount:+.2f}.")
    except Exception:
        pass

@dp.message(Forms.set_payment)
async def save_payment(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id): await state.clear(); return
    text = (m.text or "").strip()
    if len(text) < 3 or len(text) > 1000:
        await m.answer("৩ থেকে ১০০০ অক্ষরের নির্দেশনা লিখুন।"); return
    with db() as c:
        c.execute("INSERT INTO settings(key,value) VALUES('payment_instructions',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (text,))
    await state.clear()
    await m.answer("✅ Payment instructions saved. এটি এখন Deposit অপশনে দেখা যাবে।\n\nনোট: এই সেটিংস শুধু নির্দেশনা দেখায়; এটি নিজে থেকে অটো পেমেন্ট চালু করে না।")

@dp.message(Forms.support)
async def custom_quantity_or_support(m: Message, state: FSMContext):
    data = await state.get_data()
    pid = data.get("custom_product_id")
    if pid is not None:
        try:
            qty = int((m.text or "").strip())
        except Exception:
            await m.answer("শুধু পূর্ণসংখ্যা লিখুন, যেমন 2।"); return
        await state.clear()
        await process_purchase(m.from_user.id, int(pid), qty, m)
        return
    await state.clear()
    await m.answer("মূল মেনু থেকে একটি অপশন বেছে নিন।", reply_markup=MAIN_KB)

async def main():
    init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
