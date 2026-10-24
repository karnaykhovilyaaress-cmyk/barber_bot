import os
import sqlite3
import asyncio
import logging
import calendar
import re
import zipfile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (Message, CallbackQuery, ReplyKeyboardMarkup, 
                           KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, 
                           ReplyKeyboardRemove, FSInputFile)
from aiogram.exceptions import TelegramBadRequest

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "username")
BARBER_NAME = os.getenv("BARBER_NAME", "Илья")
BARBER_ADDRESS = os.getenv("BARBER_ADDRESS", "ул. Пушкина, д. 1")
BARBER_PHONE = os.getenv("BARBER_PHONE", "+7 (999) 123-45-67")
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Asia/Irkutsk"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
db_lock = asyncio.Lock()

# ==========================================
# 1. БАЗА ДАННЫХ
# ==========================================
DB_FILE = "barber_crm.db"

def get_db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

def init_db():
    with get_db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS services (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, description TEXT, 
            duration INTEGER, prep_time INTEGER DEFAULT 0, price INTEGER, is_active INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS clients (
            id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER UNIQUE, name TEXT, phone TEXT, 
            notes TEXT DEFAULT '', created_at TEXT, is_archived INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT, client_id INTEGER, service_id INTEGER,
            service_name TEXT, price_paid INTEGER, date TEXT, start_time TEXT, end_time TEXT, 
            status TEXT DEFAULT 'active', comment TEXT DEFAULT '', reminded INTEGER DEFAULT 0,
            FOREIGN KEY(client_id) REFERENCES clients(id)
        );
        CREATE TABLE IF NOT EXISTS default_schedule (
            day_of_week INTEGER PRIMARY KEY, start_time TEXT, end_time TEXT, 
            lunch_start TEXT, lunch_end TEXT, is_working INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS schedule_exceptions (
            date TEXT PRIMARY KEY, start_time TEXT, end_time TEXT, 
            lunch_start TEXT, lunch_end TEXT, is_working INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS promocodes (code TEXT PRIMARY KEY, discount_percent INTEGER, is_active INTEGER DEFAULT 1);
        """)
        
        if not conn.execute("SELECT 1 FROM settings WHERE key='interval'").fetchone():
            conn.execute("INSERT INTO settings (key, value) VALUES ('interval', '30')")
            
        if not conn.execute("SELECT 1 FROM services").fetchone():
            conn.executemany("INSERT INTO services (name, description, duration, prep_time, price) VALUES (?, ?, ?, ?, ?)",
                [("Мужская стрижка", "Классическая стрижка", 45, 15, 1000), 
                 ("Стрижка машинкой", "Быстрая стрижка", 30, 10, 600),
                 ("Стрижка и борода", "Комплексный уход", 60, 15, 1500)])

init_db()

# ==========================================
# 2. УТИЛИТЫ
# ==========================================
def get_now():
    return datetime.now(TIMEZONE)

def get_setting(key, default=None):
    with get_db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row['value'] if row else default

def time_to_min(t_str):
    if not t_str: return 0
    h, m = map(int, t_str.split(':'))
    return h * 60 + m

def min_to_time(m):
    return f"{m // 60:02d}:{m % 60:02d}"

def get_day_schedule(date_str):
    with get_db() as conn:
        exc = conn.execute("SELECT * FROM schedule_exceptions WHERE date=?", (date_str,)).fetchone()
        if exc: return exc
        date_obj = datetime.strptime(date_str, "%d.%m.%Y").replace(tzinfo=TIMEZONE)
        return conn.execute("SELECT * FROM default_schedule WHERE day_of_week=?", (date_obj.weekday(),)).fetchone()

def get_slots_for_day(date_str, total_duration):
    sched = get_day_schedule(date_str)
    if not sched or sched['is_working'] == 0: return []

    interval = int(get_setting('interval', 30))
    work_start = time_to_min(sched['start_time'])
    work_end = time_to_min(sched['end_time'])

    with get_db() as conn:
        bookings = conn.execute("SELECT start_time, end_time FROM bookings WHERE date=? AND status='active'", (date_str,)).fetchall()
    
    busy_intervals = [(time_to_min(b['start_time']), time_to_min(b['end_time'])) for b in bookings]
    if sched['lunch_start'] and sched['lunch_end']:
        busy_intervals.append((time_to_min(sched['lunch_start']), time_to_min(sched['lunch_end'])))
        
    busy_intervals.sort()
    slots = []
    curr_time = work_start
    now_min = get_now().hour * 60 + get_now().minute if date_str == get_now().strftime("%d.%m.%Y") else -1
    
    while curr_time + total_duration <= work_end:
        slot_start = curr_time
        slot_end = curr_time + total_duration
        overlap = any(max(slot_start, b_start) < min(slot_end, b_end) for b_start, b_end in busy_intervals)
        is_free = not overlap and (curr_time > now_min)
        slots.append((min_to_time(slot_start), is_free))
        curr_time += interval
    return slots

def create_calendar(year, month, prefix="date_", back_action="admin_records_back"):
    kb = [[InlineKeyboardButton(text=f"{calendar.month_name[month]} {year}", callback_data="ignore")]]
    kb.append([InlineKeyboardButton(text=d, callback_data="ignore") for d in ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]])
    today = get_now().date()
    for week in calendar.monthcalendar(year, month):
        row = []
        for day in week:
            if day == 0: row.append(InlineKeyboardButton(text=" ", callback_data="ignore"))
            else:
                date_obj = datetime(year, month, day).date()
                if date_obj < today: row.append(InlineKeyboardButton(text="-", callback_data="ignore"))
                else: row.append(InlineKeyboardButton(text=str(day), callback_data=f"{prefix}{day:02d}.{month:02d}.{year}"))
        kb.append(row)
    prev_m, prev_y = (month - 1, year) if month > 1 else (12, year - 1)
    next_m, next_y = (month + 1, year) if month < 12 else (1, year + 1)
    kb.append([InlineKeyboardButton(text="<<", callback_data=f"cal_{prefix}{prev_y}_{prev_m}"),
               InlineKeyboardButton(text="Назад", callback_data=back_action),
               InlineKeyboardButton(text=">>", callback_data=f"cal_{prefix}{next_y}_{next_m}")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

async def safe_edit(message: Message, text: str, reply_markup=None):
    try: await message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
    except TelegramBadRequest: pass

# ==========================================
# 3. КЛАВИАТУРЫ
# ==========================================
def get_client_reply_kb():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Записаться")], [KeyboardButton(text="Мои записи"), KeyboardButton(text="О нас")]], resize_keyboard=True)

def get_admin_reply_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="Записи"), KeyboardButton(text="Добавить запись")],
        [KeyboardButton(text="Клиенты"), KeyboardButton(text="Услуги")],
        [KeyboardButton(text="Настройки")]
    ], resize_keyboard=True)

def get_admin_records_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="На сегодня", callback_data="adm_today"), InlineKeyboardButton(text="На завтра", callback_data="adm_tomorrow")],
        [InlineKeyboardButton(text="На неделю", callback_data="adm_week"), InlineKeyboardButton(text="На месяц", callback_data="adm_month")],
        [InlineKeyboardButton(text="Добавить запись", callback_data="adm_add_today")],
        [InlineKeyboardButton(text="Назад", callback_data="admin_main_menu")]
    ])

# ==========================================
# 4. СОСТОЯНИЯ
# ==========================================
class ClientState(StatesGroup):
    service = State(); date_choice = State(); calendar = State(); time = State()
    name = State(); phone = State(); confirm = State(); reschedule_id = State()

class AdminState(StatesGroup):
    manual_service = State(); manual_date = State(); manual_calendar = State(); manual_time = State()
    manual_client_search = State(); manual_new_name = State(); manual_new_phone = State(); manual_confirm = State()
    search_query = State(); client_note = State()
    srv_name = State(); srv_price = State(); srv_duration = State(); srv_prep = State()
    change_hours_time = State()
    promo_code = State(); promo_discount = State(); broadcast_text = State()
    setup_days = State(); setup_start = State(); setup_end = State(); setup_lunch_start = State(); setup_lunch_end = State()

# ==========================================
# 5. БАЗОВЫЕ ХЭНДЛЕРЫ
# ==========================================
@dp.callback_query(F.data == "ignore")
async def cb_ignore(callback: CallbackQuery): await callback.answer()

@dp.callback_query(F.data == "admin_main_menu")
async def cb_admin_main(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(callback.message, "Панель управления.", reply_markup=None)
    await callback.message.answer("Главное меню:", reply_markup=get_admin_reply_kb())
    await callback.answer()

@dp.callback_query(F.data == "cancel_client_action")
async def cb_cancel_client(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(callback.message, "Действие отменено.", reply_markup=None)
    await callback.answer()

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    if message.from_user.id == ADMIN_ID:
        with get_db() as conn: has_sched = conn.execute("SELECT 1 FROM default_schedule").fetchone()
        if not has_sched:
            await message.answer("Привет. Давай настроим твой график работы на 1 минуту.", reply_markup=ReplyKeyboardRemove())
            await start_setup_wizard(message, state)
        else:
            await message.answer(f"Панель управления ({BARBER_NAME}).", reply_markup=get_admin_reply_kb())
    else:
        await message.answer(f"BARBER - Запись\n\nПривет, {message.from_user.first_name}.\nЗдесь ты можешь записаться на стрижку.", reply_markup=get_client_reply_kb())

# ==========================================
# 6. АДМИН: РАЗДЕЛ "ЗАПИСИ"
# ==========================================
@dp.message(F.text == "Записи")
async def msg_admin_records(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    await state.clear()
    await message.answer("Управление записями. Выберите период:", reply_markup=get_admin_records_kb())

def get_records_text_and_kb(date_str, title):
    with get_db() as conn:
        bks = conn.execute("SELECT b.*, c.name as client_name FROM bookings b LEFT JOIN clients c ON b.client_id = c.id WHERE b.date=? AND b.status IN ('active', 'completed', 'no_show', 'cancelled_client') ORDER BY b.start_time", (date_str,)).fetchall()
    
    total = len([b for b in bks if b['status'] == 'active'])
    completed = len([b for b in bks if b['status'] == 'completed'])
    cancelled = len([b for b in bks if b['status'] == 'cancelled_client'])
    no_show = len([b for b in bks if b['status'] == 'no_show'])
    expected_rev = sum([b['price_paid'] for b in bks if b['status'] == 'active'])
    actual_rev = sum([b['price_paid'] for b in bks if b['status'] == 'completed'])
    
    text = f"<b>{title}: {date_str}</b>\n\nВсего записей: {total + completed + cancelled + no_show}\nОжидаются: {total}\nЗавершены: {completed}\nОтменены: {cancelled}\nНеявки: {no_show}\n\nОжидаемая выручка: {expected_rev} руб.\nФактическая выручка: {actual_rev} руб.\n\n"
    
    kb = []
    if not bks: text += "Записей нет."
    else:
        for b in bks:
            status_str = " [Завершено]" if b['status'] == 'completed' else (" [Неявка]" if b['status'] == 'no_show' else (" [Отмена]" if b['status'] == 'cancelled_client' else ""))
            text += f"{b['start_time']} - {b['client_name'] or 'Клиент'}{status_str}\n{b['service_name']} ({b['price_paid']} руб.)\n\n"
            if b['status'] == 'active': kb.append([InlineKeyboardButton(text=f"Открыть {b['start_time']} {b['client_name']}", callback_data=f"adm_bk_{b['id']}")])
    
    kb.append([InlineKeyboardButton(text="Добавить запись", callback_data=f"adm_add_date_{date_str}")])
    kb.append([InlineKeyboardButton(text="Назад", callback_data="admin_records_back")])
    return text, InlineKeyboardMarkup(inline_keyboard=kb)

@dp.callback_query(F.data == "admin_records_back")
async def cb_admin_records_back(callback: CallbackQuery):
    await safe_edit(callback.message, "Управление записями. Выберите период:", reply_markup=get_admin_records_kb())
    await callback.answer()

@dp.callback_query(F.data == "adm_today")
async def cb_adm_today(callback: CallbackQuery):
    text, kb = get_records_text_and_kb(get_now().strftime("%d.%m.%Y"), "Записи на сегодня")
    await safe_edit(callback.message, text, reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data == "adm_tomorrow")
async def cb_adm_tomorrow(callback: CallbackQuery):
    text, kb = get_records_text_and_kb((get_now() + timedelta(days=1)).strftime("%d.%m.%Y"), "Записи на завтра")
    await safe_edit(callback.message, text, reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data == "adm_week")
async def cb_adm_week(callback: CallbackQuery):
    today = get_now()
    end_week = today + timedelta(days=7)
    with get_db() as conn:
        bks = conn.execute("SELECT date, COUNT(*) as c FROM bookings WHERE status='active' AND date >= ? AND date <= ? GROUP BY date ORDER BY date", (today.strftime("%d.%m.%Y"), end_week.strftime("%d.%m.%Y"))).fetchall()
    
    text = "<b>Записи на неделю</b>\n\n"
    kb = []
    for i in range(7):
        d = (today + timedelta(days=i)).strftime("%d.%m.%Y")
        count = next((b['c'] for b in bks if b['date'] == d), 0)
        text += f"{d}: {count} записей\n"
        if count > 0: kb.append([InlineKeyboardButton(text=f"Открыть {d}", callback_data=f"adm_view_{d}")])
    
    kb.append([InlineKeyboardButton(text="Назад", callback_data="admin_records_back")])
    await safe_edit(callback.message, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await callback.answer()

@dp.callback_query(F.data.startswith("adm_view_"))
async def cb_adm_view_day(callback: CallbackQuery):
    date_str = callback.data.split("_")[2]
    text, kb = get_records_text_and_kb(date_str, "Записи на день")
    await safe_edit(callback.message, text, reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data == "adm_month")
async def cb_adm_month(callback: CallbackQuery):
    now = get_now()
    await safe_edit(callback.message, "Выберите дату в календаре:", reply_markup=create_calendar(now.year, now.month, prefix="adm_month_"))

@dp.callback_query(F.data.startswith("cal_adm_month_"))
async def cb_adm_month_nav(callback: CallbackQuery):
    _, _, _, y, m = callback.data.split("_")
    try: await callback.message.edit_reply_markup(reply_markup=create_calendar(int(y), int(m), prefix="adm_month_"))
    except: pass
    await callback.answer()

@dp.callback_query(F.data.startswith("adm_month_"))
async def cb_adm_month_day(callback: CallbackQuery):
    date_str = callback.data.split("_")[2]
    text, kb = get_records_text_and_kb(date_str, "Записи на день")
    await safe_edit(callback.message, text, reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("adm_bk_"))
async def admin_booking_details(callback: CallbackQuery):
    b_id = int(callback.data.split("_")[2])
    with get_db() as conn: b = conn.execute("SELECT b.*, c.name as client_name, c.phone as client_phone FROM bookings b LEFT JOIN clients c ON b.client_id = c.id WHERE b.id=?", (b_id,)).fetchone()
    
    text = f"Запись: {b['client_name']}\nТел: {b['client_phone']}\nУслуга: {b['service_name']}\nВремя: {b['start_time']}-{b['end_time']}\nЦена: {b['price_paid']} руб."
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Завершить", callback_data=f"adm_done_{b_id}"), InlineKeyboardButton(text="Неявка", callback_data=f"adm_miss_{b_id}")],
        [InlineKeyboardButton(text="Назад к списку", callback_data=f"adm_view_{b['date']}")]
    ])
    await safe_edit(callback.message, text, reply_markup=kb)

@dp.callback_query(F.data.startswith("adm_done_"))
@dp.callback_query(F.data.startswith("adm_miss_"))
async def admin_mark_visit(callback: CallbackQuery):
    parts = callback.data.split("_")
    with get_db() as conn: 
        b = conn.execute("SELECT date FROM bookings WHERE id=?", (int(parts[2]),)).fetchone()
        conn.execute("UPDATE bookings SET status=? WHERE id=?", ('completed' if parts[1] == 'done' else 'no_show', int(parts[2])))
    await callback.answer("Статус обновлен.")
    
    class DummyCallback:
        def __init__(self, original): self.message = original.message
        async def answer(self, *args, **kwargs): pass
    await cb_adm_view_day(DummyCallback(callback)) # Имитация нажатия для обновления списка

# ==========================================
# 7. АДМИН: ДОБАВЛЕНИЕ ЗАПИСИ (РУЧНАЯ)
# ==========================================
@dp.callback_query(F.data == "adm_add_today")
async def cb_adm_add_today(callback: CallbackQuery, state: FSMContext):
    await state.update_data(date=get_now().strftime("%d.%m.%Y"))
    await start_manual_service_selection(callback.message, state)

@dp.callback_query(F.data.startswith("adm_add_date_"))
async def cb_adm_add_date(callback: CallbackQuery, state: FSMContext):
    await state.update_data(date=callback.data.split("_")[3])
    await start_manual_service_selection(callback.message, state)

@dp.message(F.text == "Добавить запись")
async def msg_add_booking_start(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    await state.clear()
    today = get_now()
    tomorrow = today + timedelta(days=1)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Сегодня", callback_data=f"adm_add_date_{today.strftime('%d.%m.%Y')}"), InlineKeyboardButton(text="Завтра", callback_data=f"adm_add_date_{tomorrow.strftime('%d.%m.%Y')}")],
        [InlineKeyboardButton(text="Выбрать дату", callback_data="adm_add_custom_date")],
        [InlineKeyboardButton(text="Назад", callback_data="admin_main_menu")]
    ])
    await message.answer("Выберите дату для новой записи:", reply_markup=kb)

@dp.callback_query(F.data == "adm_add_custom_date")
async def cb_adm_add_custom_date(callback: CallbackQuery):
    now = get_now()
    await safe_edit(callback.message, "Выберите дату:", reply_markup=create_calendar(now.year, now.month, prefix="adm_newdate_"))

@dp.callback_query(F.data.startswith("cal_adm_newdate_"))
async def cb_adm_newdate_nav(callback: CallbackQuery):
    _, _, _, y, m = callback.data.split("_")
    try: await callback.message.edit_reply_markup(reply_markup=create_calendar(int(y), int(m), prefix="adm_newdate_"))
    except: pass

@dp.callback_query(F.data.startswith("adm_newdate_"))
async def cb_adm_newdate_selected(callback: CallbackQuery, state: FSMContext):
    await state.update_data(date=callback.data.split("_")[2])
    await start_manual_service_selection(callback.message, state)

async def start_manual_service_selection(message, state: FSMContext):
    data = await state.get_data()
    with get_db() as conn: services = conn.execute("SELECT * FROM services WHERE is_active=1").fetchall()
    kb = [[InlineKeyboardButton(text=f"{s['name']} - {s['price']} руб.", callback_data=f"adm_msrv_{s['id']}")] for s in services]
    kb.append([InlineKeyboardButton(text="Назад", callback_data="admin_records_back")])
    await safe_edit(message, f"Дата: {data['date']}\nВыберите услугу:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await state.set_state(AdminState.manual_service)

@dp.callback_query(AdminState.manual_service, F.data.startswith("adm_msrv_"))
async def admin_manual_service(callback: CallbackQuery, state: FSMContext):
    srv_id = int(callback.data.split("_")[2])
    with get_db() as conn: srv = conn.execute("SELECT * FROM services WHERE id=?", (srv_id,)).fetchone()
    await state.update_data(srv_id=srv_id, srv_name=srv['name'], srv_price=srv['price'], total_duration=srv['duration']+srv['prep_time'])
    data = await state.get_data()
    slots = get_slots_for_day(data['date'], data['total_duration'])
    if not slots: return await callback.answer("На эту дату нет свободных слотов.", show_alert=True)
    
    kb, row = [], []
    for time_str, is_free in slots:
        if is_free: row.append(InlineKeyboardButton(text=time_str, callback_data=f"adm_mtime_{time_str}"))
        else: row.append(InlineKeyboardButton(text=f"{time_str} Занято", callback_data="ignore"))
        if len(row) == 3: kb.append(row); row = []
    if row: kb.append(row)
    kb.append([InlineKeyboardButton(text="Назад", callback_data=f"adm_add_date_{data['date']}")])
    await safe_edit(callback.message, f"Услуга: {srv['name']}\nДата: {data['date']}\nВыберите время:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await state.set_state(AdminState.manual_time)

@dp.callback_query(AdminState.manual_time, F.data.startswith("adm_mtime_"))
async def admin_manual_time(callback: CallbackQuery, state: FSMContext):
    await state.update_data(time=callback.data.split("_")[2])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Найти в базе", callback_data="adm_macl_search")],
        [InlineKeyboardButton(text="Добавить нового", callback_data="adm_macl_new")],
        [InlineKeyboardButton(text="Назад", callback_data="adm_back_to_service")]
    ])
    await safe_edit(callback.message, "Выберите клиента:", reply_markup=kb)
    await state.set_state(AdminState.manual_client_search)

@dp.callback_query(F.data == "adm_back_to_service")
async def cb_adm_back_to_service(callback: CallbackQuery, state: FSMContext):
    await start_manual_service_selection(callback.message, state)

@dp.callback_query(F.data == "adm_macl_search")
async def admin_manual_client_search_start(callback: CallbackQuery, state: FSMContext):
    with get_db() as conn: clients = conn.execute("SELECT * FROM clients WHERE is_archived=0 ORDER BY name LIMIT 10").fetchall()
    kb = [[InlineKeyboardButton(text=f"{c['name']} ({c['phone']})", callback_data=f"adm_macl_sel_{c['id']}")] for c in clients]
    kb.append([InlineKeyboardButton(text="Ввести вручную", callback_data="adm_macl_type_search")])
    kb.append([InlineKeyboardButton(text="Назад", callback_data="adm_back_to_service")])
    await safe_edit(callback.message, "Выберите клиента:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data == "adm_macl_type_search")
async def admin_manual_type_search(callback: CallbackQuery):
    await safe_edit(callback.message, "Введите имя или телефон клиента:")

@dp.message(AdminState.manual_client_search)
async def admin_manual_search_process(message: Message, state: FSMContext):
    query = f"%{message.text}%"
    with get_db() as conn: clients = conn.execute("SELECT * FROM clients WHERE (name LIKE ? OR phone LIKE ?) AND is_archived=0", (query, query)).fetchall()
    if not clients: return await message.answer("Никого не найдено.")
    kb = [[InlineKeyboardButton(text=f"{c['name']} ({c['phone']})", callback_data=f"adm_macl_sel_{c['id']}")] for c in clients]
    kb.append([InlineKeyboardButton(text="Назад", callback_data="adm_back_to_service")])
    await message.answer("Результаты:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data.startswith("adm_macl_sel_"))
async def admin_manual_client_selected(callback: CallbackQuery, state: FSMContext):
    c_id = int(callback.data.split("_")[3])
    with get_db() as conn: c = conn.execute("SELECT * FROM clients WHERE id=?", (c_id,)).fetchone()
    await state.update_data(client_id=c_id, client_name=c['name'], client_phone=c['phone'])
    await admin_manual_confirm(callback.message, state)

@dp.callback_query(F.data == "adm_macl_new")
async def admin_manual_new_client(callback: CallbackQuery, state: FSMContext):
    await safe_edit(callback.message, "Введите имя нового клиента:")
    await state.set_state(AdminState.manual_new_name)

@dp.message(AdminState.manual_new_name)
async def admin_manual_new_name(message: Message, state: FSMContext):
    await state.update_data(new_c_name=message.text)
    await message.answer("Введите телефон нового клиента:")
    await state.set_state(AdminState.manual_new_phone)

@dp.message(AdminState.manual_new_phone)
async def admin_manual_new_phone(message: Message, state: FSMContext):
    phone = message.text
    data = await state.get_data()
    with get_db() as conn:
        dup = conn.execute("SELECT * FROM clients WHERE phone=? AND is_archived=0", (phone,)).fetchone()
        if dup: await state.update_data(client_id=dup['id'], client_name=dup['name'], client_phone=dup['phone'])
        else:
            cur = conn.execute("INSERT INTO clients (name, phone, created_at) VALUES (?, ?, ?)", (data['new_c_name'], phone, get_now().isoformat()))
            await state.update_data(client_id=cur.lastrowid, client_name=data['new_c_name'], client_phone=phone)
    await admin_manual_confirm(message, state)

async def admin_manual_confirm(message_obj, state: FSMContext):
    data = await state.get_data()
    text = f"Клиент: {data['client_name']}\nУслуга: {data['srv_name']}\nДата: {data['date']}\nВремя: {data['time']}\nЦена: {data['srv_price']} руб."
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Подтвердить", callback_data="adm_final_save")], [InlineKeyboardButton(text="Отмена", callback_data="admin_records_back")]])
    if isinstance(message_obj, Message): await message_obj.answer(text, reply_markup=kb)
    else: await safe_edit(message_obj, text, reply_markup=kb)
    await state.set_state(AdminState.manual_confirm)

@dp.callback_query(AdminState.manual_confirm, F.data == "adm_final_save")
async def admin_manual_final_save(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    async with db_lock:
        slots = get_slots_for_day(data['date'], data['total_duration'])
        if not any(t == data['time'] and free for t, free in slots): return await callback.answer("Время занято.", show_alert=True)
        with get_db() as conn:
            end_time = min_to_time(time_to_min(data['time']) + data['total_duration'])
            conn.execute("INSERT INTO bookings (client_id, service_id, service_name, price_paid, date, start_time, end_time) VALUES (?, ?, ?, ?, ?, ?, ?)", (data['client_id'], data['srv_id'], data['srv_name'], data['srv_price'], data['date'], data['time'], end_time))
    
    await safe_edit(callback.message, "Запись создана.", reply_markup=None)
    await state.clear()
    text, kb = get_records_text_and_kb(data['date'], "Записи на день")
    await callback.message.answer(text, reply_markup=kb)

# ==========================================
# 8. АДМИН: УПРАВЛЕНИЕ КЛИЕНТАМИ (CRM)
# ==========================================
@dp.message(F.text == "Клиенты")
async def msg_clients_menu(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Все клиенты", callback_data="cl_list")], [InlineKeyboardButton(text="Поиск", callback_data="cl_search")], [InlineKeyboardButton(text="Закрыть", callback_data="admin_main_menu")]])
    await message.answer("База клиентов:", reply_markup=kb)

@dp.callback_query(F.data == "cl_list")
async def clients_list(callback: CallbackQuery):
    with get_db() as conn: clients = conn.execute("SELECT c.*, MAX(b.date) as last_date FROM clients c LEFT JOIN bookings b ON c.id=b.client_id WHERE c.is_archived=0 GROUP BY c.id ORDER BY last_date DESC LIMIT 20").fetchall()
    if not clients: return await safe_edit(callback.message, "База пуста.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Назад", callback_data="admin_main_menu")]]))
    kb = [[InlineKeyboardButton(text=f"{c['name']} ({c['phone']})", callback_data=f"cl_view_{c['id']}")] for c in clients]
    kb.append([InlineKeyboardButton(text="Назад", callback_data="admin_main_menu")])
    await safe_edit(callback.message, "Последние клиенты:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data == "cl_search")
async def clients_search_start(callback: CallbackQuery, state: FSMContext):
    await safe_edit(callback.message, "Введите имя или телефон:")
    await state.set_state(AdminState.search_query)

@dp.message(AdminState.search_query)
async def clients_search_process(message: Message, state: FSMContext):
    query = f"%{message.text}%"
    with get_db() as conn: clients = conn.execute("SELECT * FROM clients WHERE (name LIKE ? OR phone LIKE ?) AND is_archived=0", (query, query)).fetchall()
    if not clients: return await message.answer("Никого не найдено.")
    kb = [[InlineKeyboardButton(text=f"{c['name']} ({c['phone']})", callback_data=f"cl_view_{c['id']}")] for c in clients]
    await message.answer("Результаты:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await state.clear()

@dp.callback_query(F.data.startswith("cl_view_"))
async def client_card(callback: CallbackQuery):
    c_id = int(callback.data.split("_")[2])
    with get_db() as conn:
        c = conn.execute("SELECT * FROM clients WHERE id=?", (c_id,)).fetchone()
        completed = conn.execute("SELECT COUNT(*) FROM bookings WHERE client_id=? AND status='completed'", (c_id,)).fetchone()[0]
        last_visit = conn.execute("SELECT MAX(date) FROM bookings WHERE client_id=? AND status='completed'", (c_id,)).fetchone()[0]
        upcoming = conn.execute("SELECT date, start_time, service_name FROM bookings WHERE client_id=? AND status='active' AND date >= ? ORDER BY date ASC LIMIT 1", (c_id, get_now().strftime("%d.%m.%Y"))).fetchone()
        history = conn.execute("SELECT * FROM bookings WHERE client_id=? AND status='completed' ORDER BY date DESC LIMIT 5", (c_id,)).fetchall()
    text = f"Карточка клиента\n\nИмя: {c['name']}\nТелефон: {c['phone']}\nПосещений: {completed}\n"
    if last_visit: text += f"Последний визит: {last_visit}\n"
    if upcoming: text += f"Ближайшая запись: {upcoming['date']} в {upcoming['start_time']}\n"
    if c['notes']: text += f"\nЗаметка: {c['notes']}\n"
    if history:
        text += "\nИстория:\n"
        for h in history: text += f"- {h['date']}: {h['service_name']} ({h['price_paid']} руб.)\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Добавить заметку", callback_data=f"cl_note_{c_id}")], [InlineKeyboardButton(text="В архив", callback_data=f"cl_archive_{c_id}")], [InlineKeyboardButton(text="Назад", callback_data="cl_list")]])
    await safe_edit(callback.message, text, reply_markup=kb)

@dp.callback_query(F.data.startswith("cl_note_"))
async def client_add_note(callback: CallbackQuery, state: FSMContext):
    await state.update_data(note_client_id=int(callback.data.split("_")[2]))
    await safe_edit(callback.message, "Введите текст заметки:")
    await state.set_state(AdminState.client_note)

@dp.message(AdminState.client_note)
async def client_save_note(message: Message, state: FSMContext):
    with get_db() as conn: conn.execute("UPDATE clients SET notes=? WHERE id=?", (message.text, (await state.get_data())['note_client_id']))
    await message.answer("Заметка сохранена.", reply_markup=get_admin_reply_kb())
    await state.clear()

@dp.callback_query(F.data.startswith("cl_archive_"))
async def client_archive(callback: CallbackQuery):
    with get_db() as conn: conn.execute("UPDATE clients SET is_archived=1 WHERE id=?", (int(callback.data.split("_")[2]),))
    await safe_edit(callback.message, "Клиент в архиве.", reply_markup=None)
    await callback.answer()

# ==========================================
# 9. АДМИН: УПРАВЛЕНИЕ УСЛУГАМИ
# ==========================================
@dp.message(F.text == "Услуги")
async def msg_admin_services(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    await state.clear()
    with get_db() as conn: services = conn.execute("SELECT * FROM services").fetchall()
    kb = [[InlineKeyboardButton(text=f"{s['name']} - {s['price']} руб.{'' if s['is_active'] else ' (Скрыта)'}", callback_data=f"viewsrv_{s['id']}")] for s in services]
    kb.append([InlineKeyboardButton(text="Добавить услугу", callback_data="addsrv")])
    kb.append([InlineKeyboardButton(text="Назад", callback_data="admin_main_menu")])
    await message.answer("Мои услуги:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data.startswith("viewsrv_"))
async def view_service(callback: CallbackQuery):
    srv_id = int(callback.data.split("_")[1])
    with get_db() as conn: s = conn.execute("SELECT * FROM services WHERE id=?", (srv_id,)).fetchone()
    prep = f"Плюс {s['prep_time']} мин" if s['prep_time'] > 0 else "Без подготовки"
    text = f"{s['name']}\n{s['description'] or ''}\nДлительность: {s['duration']} мин ({prep})\nЦена: {s['price']} руб.\nСтатус: {'Показывается' if s['is_active'] else 'Скрыта'}"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Скрыть или Показать", callback_data=f"toggsrv_{s['id']}")], [InlineKeyboardButton(text="Удалить", callback_data=f"delsrv_{s['id']}")], [InlineKeyboardButton(text="Назад", callback_data="admin_services_menu")]])
    await safe_edit(callback.message, text, reply_markup=kb)

@dp.callback_query(F.data == "admin_services_menu")
async def cb_admin_services_menu(callback: CallbackQuery):
    with get_db() as conn: services = conn.execute("SELECT * FROM services").fetchall()
    kb = [[InlineKeyboardButton(text=f"{s['name']} - {s['price']} руб.{'' if s['is_active'] else ' (Скрыта)'}", callback_data=f"viewsrv_{s['id']}")] for s in services]
    kb.append([InlineKeyboardButton(text="Добавить услугу", callback_data="addsrv")])
    kb.append([InlineKeyboardButton(text="Назад", callback_data="admin_main_menu")])
    await safe_edit(callback.message, "Мои услуги:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data.startswith("toggsrv_"))
async def toggle_service(callback: CallbackQuery):
    with get_db() as conn: conn.execute("UPDATE services SET is_active = 1 - is_active WHERE id=?", (int(callback.data.split("_")[1]),))
    await callback.answer("Статус изменен")
    await view_service(callback)

@dp.callback_query(F.data.startswith("delsrv_"))
async def delete_service(callback: CallbackQuery):
    srv_id = int(callback.data.split("_")[1])
    with get_db() as conn:
        if conn.execute("SELECT COUNT(*) FROM bookings WHERE service_id=?", (srv_id,)).fetchone()[0] > 0: return await callback.answer("Нельзя удалить. Есть записи.", show_alert=True)
        conn.execute("DELETE FROM services WHERE id=?", (srv_id,))
    await callback.answer("Удалено")
    await cb_admin_services_menu(callback)

@dp.callback_query(F.data == "addsrv")
async def add_service_start(callback: CallbackQuery, state: FSMContext):
    await safe_edit(callback.message, "Введите название новой услуги:")
    await state.set_state(AdminState.srv_name)

@dp.message(AdminState.srv_name)
async def add_srv_name(message: Message, state: FSMContext):
    await state.update_data(srv_name=message.text)
    await message.answer("Введите цену (число):")
    await state.set_state(AdminState.srv_price)

@dp.message(AdminState.srv_price)
async def add_srv_price(message: Message, state: FSMContext):
    if not message.text.isdigit(): return await message.answer("Введите число.")
    await state.update_data(srv_price=int(message.text))
    await message.answer("Введите длительность (мин):")
    await state.set_state(AdminState.srv_duration)

@dp.message(AdminState.srv_duration)
async def add_srv_duration(message: Message, state: FSMContext):
    if not message.text.isdigit(): return await message.answer("Введите число.")
    await state.update_data(srv_duration=int(message.text))
    await message.answer("Введите время на подготовку (мин) или 0:")
    await state.set_state(AdminState.srv_prep)

@dp.message(AdminState.srv_prep)
async def add_srv_prep(message: Message, state: FSMContext):
    if not message.text.isdigit(): return await message.answer("Введите число.")
    data = await state.get_data()
    with get_db() as conn: conn.execute("INSERT INTO services (name, duration, prep_time, price) VALUES (?, ?, ?, ?)", (data['srv_name'], data['srv_duration'], int(message.text), data['srv_price']))
    await message.answer("Услуга добавлена.", reply_markup=get_admin_reply_kb())
    await state.clear()

# ==========================================
# 10. АДМИН: НАСТРОЙКИ
# ==========================================
@dp.message(F.text == "Настройки")
async def msg_admin_settings(message: Message):
    if message.from_user.id != ADMIN_ID: return
    current_interval = get_setting('interval', '30')
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Интервал записи: {current_interval} мин", callback_data="set_interval_menu")],
        [InlineKeyboardButton(text="Изменить часы на сегодня", callback_data="adm_change_hours_today")],
        [InlineKeyboardButton(text="Промокоды", callback_data="admin_promo")],
        [InlineKeyboardButton(text="Рассылка клиентам", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="Настроить график заново", callback_data="reset_wizard")]
    ])
    await message.answer("Настройки:", reply_markup=kb)

@dp.callback_query(F.data == "adm_change_hours_today")
async def cb_adm_change_hours_today(callback: CallbackQuery, state: FSMContext):
    today = get_now().strftime("%d.%m.%Y")
    await safe_edit(callback.message, f"Сегодня ({today}).\nВведите новое время окончания работы (ЧЧ:ММ):")
    await state.set_state(AdminState.change_hours_time)

@dp.message(AdminState.change_hours_time)
async def process_change_hours(message: Message, state: FSMContext):
    if not re.match(r"^([01]\d|2[0-3]):([0-5]\d)$", message.text): return await message.answer("Неверный формат. Введите ЧЧ:ММ:")
    new_end = message.text
    today = get_now().strftime("%d.%m.%Y")
    with get_db() as conn:
        sched = get_day_schedule(today)
        conn.execute("INSERT OR REPLACE INTO schedule_exceptions (date, start_time, end_time, lunch_start, lunch_end, is_working) VALUES (?, ?, ?, ?, ?, 1)",
                     (today, sched['start_time'] if sched else "10:00", new_end, sched['lunch_start'] if sched else None, sched['lunch_end'] if sched else None))
    await message.answer(f"Рабочий день сокращен до {new_end}.", reply_markup=get_admin_reply_kb())
    await state.clear()

@dp.callback_query(F.data == "set_interval_menu")
async def set_interval_menu(callback: CallbackQuery):
    current = get_setting('interval', '30')
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"15 мин {'(Выбрано)' if current == '15' else ''}", callback_data="set_int_15"),
         InlineKeyboardButton(text=f"30 мин {'(Выбрано)' if current == '30' else ''}", callback_data="set_int_30")],
        [InlineKeyboardButton(text=f"45 мин {'(Выбрано)' if current == '45' else ''}", callback_data="set_int_45"),
         InlineKeyboardButton(text=f"60 мин {'(Выбрано)' if current == '60' else ''}", callback_data="set_int_60")],
        [InlineKeyboardButton(text="Назад", callback_data="cancel_action")]
    ])
    await safe_edit(callback.message, "Выберите интервал записи:", reply_markup=kb)

@dp.callback_query(F.data.startswith("set_int_"))
async def set_interval_val(callback: CallbackQuery):
    val = callback.data.split("_")[2]
    with get_db() as conn: conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('interval', ?)", (val,))
    await callback.answer(f"Интервал: {val} мин")
    await msg_admin_settings(callback.message)

@dp.callback_query(F.data == "admin_promo")
async def admin_promo_start(callback: CallbackQuery, state: FSMContext):
    await safe_edit(callback.message, "Введите код промокода (например, SALE10):")
    await state.set_state(AdminState.promo_code)

@dp.message(AdminState.promo_code)
async def admin_promo_code(message: Message, state: FSMContext):
    await state.update_data(pcode=message.text.upper())
    await message.answer("Введите скидку в процентах (число от 1 до 100):")
    await state.set_state(AdminState.promo_discount)

@dp.message(AdminState.promo_discount)
async def admin_promo_discount(message: Message, state: FSMContext):
    if not message.text.isdigit() or not (1 <= int(message.text) <= 100): return await message.answer("Введите число от 1 до 100.")
    with get_db() as conn: conn.execute("INSERT OR REPLACE INTO promocodes (code, discount_percent) VALUES (?, ?)", ((await state.get_data())['pcode'], int(message.text)))
    await message.answer(f"Промокод создан.", reply_markup=get_admin_reply_kb())
    await state.clear()

@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_start(callback: CallbackQuery, state: FSMContext):
    await safe_edit(callback.message, "Введите текст для рассылки:")
    await state.set_state(AdminState.broadcast_text)

@dp.message(AdminState.broadcast_text)
async def admin_broadcast_send(message: Message, state: FSMContext):
    with get_db() as conn: clients = conn.execute("SELECT tg_id FROM clients WHERE tg_id IS NOT NULL AND is_archived=0").fetchall()
    count = 0
    await message.answer("Рассылка запущена.", reply_markup=get_admin_reply_kb())
    for c in clients:
        try:
            await bot.send_message(c['tg_id'], message.text)
            count += 1
            await asyncio.sleep(0.05)
        except: pass
    await message.answer(f"Отправлено: {count} чел.")
    await state.clear()

# ==========================================
# 11. МАСТЕР-НАСТРОЙКА ГРАФИКА
# ==========================================
async def start_setup_wizard(message: Message, state: FSMContext):
    await state.update_data(selected_days=[])
    await state.set_state(AdminState.setup_days)
    await message.answer("Шаг 1 из 3\n\nВ какие дни ты работаешь?\nВыбери рабочие дни.", reply_markup=get_days_kb([]))

def get_days_kb(selected):
    days = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    kb, row = [], []
    for i, d in enumerate(days):
        mark = "(Рабочий)" if i in selected else "(Выходной)"
        row.append(InlineKeyboardButton(text=f"{d} {mark}", callback_data=f"togday_{i}"))
        if len(row) == 2: kb.append(row); row = []
    if row: kb.append(row)
    kb.append([InlineKeyboardButton(text="Далее", callback_data="days_done")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

@dp.callback_query(AdminState.setup_days, F.data.startswith("togday_"))
async def cb_toggle_day(callback: CallbackQuery, state: FSMContext):
    day = int(callback.data.split("_")[1])
    data = await state.get_data()
    selected = data.get("selected_days", [])
    if day in selected: selected.remove(day)
    else: selected.append(day)
    await state.update_data(selected_days=selected)
    await callback.message.edit_reply_markup(reply_markup=get_days_kb(selected))

@dp.callback_query(AdminState.setup_days, F.data == "days_done")
async def cb_days_done(callback: CallbackQuery, state: FSMContext):
    if not (await state.get_data()).get("selected_days"): return await callback.answer("Выбери хотя бы один день!", show_alert=True)
    await safe_edit(callback.message, "Шаг 2 из 3\n\nВведи время начала работы (ЧЧ:ММ):")
    await state.set_state(AdminState.setup_start)

@dp.message(AdminState.setup_start)
async def setup_start_time(message: Message, state: FSMContext):
    if not re.match(r"^([01]\d|2[0-3]):([0-5]\d)$", message.text): return await message.answer("Неверный формат. Введи ЧЧ:ММ:")
    await state.update_data(start=message.text)
    await message.answer("Введи время конца работы (ЧЧ:ММ):")
    await state.set_state(AdminState.setup_end)

@dp.message(AdminState.setup_end)
async def setup_end_time(message: Message, state: FSMContext):
    if not re.match(r"^([01]\d|2[0-3]):([0-5]\d)$", message.text): return await message.answer("Неверный формат. Введи ЧЧ:ММ:")
    await state.update_data(end=message.text)
    await message.answer("Введи время начала обеда (ЧЧ:ММ) или '0':")
    await state.set_state(AdminState.setup_lunch_start)

@dp.message(AdminState.setup_lunch_start)
async def setup_lunch_start(message: Message, state: FSMContext):
    if message.text == '0':
        await state.update_data(lunch_start=None, lunch_end=None)
        await finish_setup(message, state)
        return
    if not re.match(r"^([01]\d|2[0-3]):([0-5]\d)$", message.text): return await message.answer("Неверный формат. Введи ЧЧ:ММ или 0:")
    await state.update_data(lunch_start=message.text)
    await message.answer("Введи время конца обеда (ЧЧ:ММ):")
    await state.set_state(AdminState.setup_lunch_end)

@dp.message(AdminState.setup_lunch_end)
async def setup_lunch_end(message: Message, state: FSMContext):
    if not re.match(r"^([01]\d|2[0-3]):([0-5]\d)$", message.text): return await message.answer("Неверный формат. Введи ЧЧ:ММ:")
    await state.update_data(lunch_end=message.text)
    await finish_setup(message, state)

async def finish_setup(message: Message, state: FSMContext):
    data = await state.get_data()
    with get_db() as conn:
        conn.execute("DELETE FROM default_schedule")
        for dow in range(7):
            is_working = 1 if dow in data['selected_days'] else 0
            conn.execute("INSERT INTO default_schedule (day_of_week, start_time, end_time, lunch_start, lunch_end, is_working) VALUES (?, ?, ?, ?, ?, ?)", (dow, data['start'], data['end'], data.get('lunch_start'), data.get('lunch_end'), is_working))
    await message.answer("Настройка завершена.", reply_markup=get_admin_reply_kb())
    await state.clear()

@dp.callback_query(F.data == "reset_wizard")
async def cb_reset_wizard(callback: CallbackQuery, state: FSMContext):
    await safe_edit(callback.message, "Запускаю настройку заново...")
    await start_setup_wizard(callback.message, state)

# ==========================================
# 12. КЛИЕНТСКАЯ ЧАСТЬ
# ==========================================
@dp.message(F.text == "Записаться")
async def msg_book_start(message: Message, state: FSMContext):
    if message.from_user.id == ADMIN_ID: return
    await state.clear()
    with get_db() as conn: services = conn.execute("SELECT * FROM services WHERE is_active=1").fetchall()
    kb = [[InlineKeyboardButton(text=f"{s['name']} ({s['duration']} мин) - {s['price']} руб.", callback_data=f"cli_srv_{s['id']}")] for s in services]
    kb.append([InlineKeyboardButton(text="Отмена", callback_data="cancel_client_action")])
    await message.answer("Выберите услугу:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await state.set_state(ClientState.service)

@dp.callback_query(ClientState.service, F.data.startswith("cli_srv_"))
async def cb_client_service(callback: CallbackQuery, state: FSMContext):
    srv_id = int(callback.data.split("_")[2])
    with get_db() as conn: srv = conn.execute("SELECT * FROM services WHERE id=?", (srv_id,)).fetchone()
    await state.update_data(srv_id=srv_id, srv_name=srv['name'], srv_price=srv['price'], total_duration=srv['duration']+srv['prep_time'])
    today = get_now()
    tomorrow = today + timedelta(days=1)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Сегодня", callback_data=f"cli_date_{today.strftime('%d.%m.%Y')}"),
         InlineKeyboardButton(text="Завтра", callback_data=f"cli_date_{tomorrow.strftime('%d.%m.%Y')}")],
        [InlineKeyboardButton(text="Календарь", callback_data="cli_custom_date")],
        [InlineKeyboardButton(text="Отмена", callback_data="cancel_client_action")]
    ])
    await safe_edit(callback.message, f"Услуга: {srv['name']}\nВыберите дату:", reply_markup=kb)
    await state.set_state(ClientState.date_choice)

@dp.callback_query(ClientState.date_choice, F.data == "cli_custom_date")
async def cb_client_custom_date(callback: CallbackQuery):
    now = get_now()
    await safe_edit(callback.message, "Выберите дату:", reply_markup=create_calendar(now.year, now.month, prefix="cli_date_", back_action="cancel_client_action"))
    await state.set_state(ClientState.calendar)

@dp.callback_query(ClientState.date_choice, F.data.startswith("cli_date_"))
@dp.callback_query(ClientState.calendar, F.data.startswith("cli_date_"))
async def cb_client_date_selected(callback: CallbackQuery, state: FSMContext):
    date_str = callback.data.split("_")[2]
    data = await state.get_data()
    slots = get_slots_for_day(date_str, data['total_duration'])
    if not slots: return await callback.answer("Нет свободного времени.", show_alert=True)
    
    kb, row = [], []
    for time_str, is_free in slots:
        if is_free: row.append(InlineKeyboardButton(text=time_str, callback_data=f"cli_time_{time_str}"))
        else: row.append(InlineKeyboardButton(text=f"{time_str} Занято", callback_data="ignore"))
        if len(row) == 3: kb.append(row); row = []
    if row: kb.append(row)
    kb.append([InlineKeyboardButton(text="Назад", callback_data="cli_custom_date")])
    
    await state.update_data(date=date_str)
    await safe_edit(callback.message, f"Дата: {date_str}\nВыберите время:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await state.set_state(ClientState.time)

@dp.callback_query(ClientState.time, F.data.startswith("cli_time_"))
async def cb_client_time(callback: CallbackQuery, state: FSMContext):
    time_str = callback.data.split("_")[2]
    await state.update_data(time=time_str)
    with get_db() as conn: client = conn.execute("SELECT * FROM clients WHERE tg_id=?", (callback.from_user.id,)).fetchone()
    if client:
        await state.update_data(client_id=client['id'], name=client['name'], phone=client['phone'])
        await show_client_booking_confirm(callback.message, state)
    else:
        await safe_edit(callback.message, "Введите ваше имя:")
        await state.set_state(ClientState.name)

@dp.message(ClientState.name)
async def process_client_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text)
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Отправить номер", request_contact=True)]], resize_keyboard=True, one_time_keyboard=True)
    await message.answer("Отправьте номер телефона:", reply_markup=kb)
    await state.set_state(ClientState.phone)

@dp.message(ClientState.phone)
async def process_client_phone(message: Message, state: FSMContext):
    phone = message.contact.phone_number if message.contact else message.text
    await state.update_data(phone=phone)
    await message.answer("Данные сохранены.", reply_markup=ReplyKeyboardRemove())
    await show_client_booking_confirm(message, state)

async def show_client_booking_confirm(message_obj, state: FSMContext):
    data = await state.get_data()
    text = f"Услуга: {data['srv_name']}\nДата: {data['date']}\nВремя: {data['time']}\nСтоимость: {data['srv_price']} руб.\n\nПодтвердить запись?"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Подтвердить", callback_data="cli_confirm_yes")], [InlineKeyboardButton(text="Отмена", callback_data="cancel_client_action")]])
    if isinstance(message_obj, Message): await message_obj.answer(text, reply_markup=kb)
    else: await safe_edit(message_obj, text, reply_markup=kb)
    await state.set_state(ClientState.confirm)

@dp.callback_query(ClientState.confirm, F.data == "cli_confirm_yes")
async def cb_client_confirm(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    async with db_lock:
        slots = get_slots_for_day(data['date'], data['total_duration'])
        if not any(t == data['time'] and free for t, free in slots): return await callback.answer("Время занято.", show_alert=True)
        with get_db() as conn:
            end_time = min_to_time(time_to_min(data['time']) + data['total_duration'])
            client = conn.execute("SELECT id FROM clients WHERE tg_id=?", (callback.from_user.id,)).fetchone()
            c_id = client['id'] if client else conn.execute("INSERT INTO clients (tg_id, name, phone) VALUES (?, ?, ?)", (callback.from_user.id, data['name'], data['phone'])).lastrowid
            conn.execute("INSERT INTO bookings (client_id, service_id, service_name, price_paid, date, start_time, end_time) VALUES (?, ?, ?, ?, ?, ?, ?)", (c_id, data['srv_id'], data['srv_name'], data['srv_price'], data['date'], data['time'], end_time))
    
    await safe_edit(callback.message, "Вы успешно записаны.")
    await callback.message.answer("Главное меню:", reply_markup=get_client_reply_kb())
    try: await bot.send_message(ADMIN_ID, f"НОВАЯ ЗАПИСЬ\n{data['name']} ({data['phone']})\n{data['srv_name']} - {data['date']} в {data['time']}")
    except: pass
    await state.clear()

# --- МОИ ЗАПИСИ ---
@dp.message(F.text == "Мои записи")
async def msg_my_bookings(message: Message, state: FSMContext):
    await state.clear()
    with get_db() as conn:
        client = conn.execute("SELECT id FROM clients WHERE tg_id=?", (message.from_user.id,)).fetchone()
        if not client: return await message.answer("У вас пока нет записей.")
        c_id = client['id']
        upcoming = conn.execute("SELECT * FROM bookings WHERE client_id=? AND status='active' AND date >= ? ORDER BY date ASC, start_time ASC", (c_id, get_now().strftime("%d.%m.%Y"))).fetchall()
        history = conn.execute("SELECT * FROM bookings WHERE client_id=? AND status IN ('completed', 'cancelled_client', 'no_show') ORDER BY date DESC LIMIT 5", (c_id,)).fetchall()

    text = "<b>Ваши записи</b>\n\n"
    if upcoming:
        text += "<b>Предстоящие:</b>\n"
        for b in upcoming: text += f"- {b['service_name']}\n  {b['date']} - {b['start_time']}-{b['end_time']}\n  {b['price_paid']} руб.\n\n"
    else: text += "У вас нет предстоящих записей.\n\n"
    if history:
        text += "<b>История:</b>\n"
        for b in history: text += f"{b['date']} - {b['service_name']}\n"

    await message.answer(text, parse_mode="HTML")
    for b in upcoming:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Перенести", callback_data=f"cli_resch_{b['id']}"), InlineKeyboardButton(text="Отменить", callback_data=f"cli_cancel_{b['id']}")]])
        await message.answer(f"Управление записью на {b['date']} в {b['start_time']}:", reply_markup=kb)

@dp.callback_query(F.data.startswith("cli_cancel_"))
async def cb_client_cancel(callback: CallbackQuery):
    b_id = int(callback.data.split("_")[2])
    with get_db() as conn: b = conn.execute("SELECT * FROM bookings WHERE id=?", (b_id,)).fetchone()
    if datetime.strptime(f"{b['date']} {b['start_time']}", "%d.%m.%Y %H:%M").replace(tzinfo=TIMEZONE) - get_now() < timedelta(hours=2):
        return await callback.answer("Отмена невозможна менее чем за 2 часа до визита.", show_alert=True)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Да, отменить", callback_data=f"cli_cancel_yes_{b_id}")], [InlineKeyboardButton(text="Нет", callback_data="cancel_client_action")]])
    await safe_edit(callback.message, "Вы уверены, что хотите отменить запись?", reply_markup=kb)

@dp.callback_query(F.data.startswith("cli_cancel_yes_"))
async def cb_client_cancel_confirm(callback: CallbackQuery):
    b_id = int(callback.data.split("_")[3])
    with get_db() as conn: conn.execute("UPDATE bookings SET status='cancelled_client' WHERE id=?", (b_id,))
    await safe_edit(callback.message, "Запись отменена.", reply_markup=None)
    try: await bot.send_message(ADMIN_ID, f"КЛИЕНТ ОТМЕНИЛ ЗАПИСЬ #{b_id}")
    except: pass

@dp.callback_query(F.data.startswith("cli_resch_"))
async def cb_client_reschedule(callback: CallbackQuery, state: FSMContext):
    b_id = int(callback.data.split("_")[2])
    with get_db() as conn: b = conn.execute("SELECT b.*, s.duration, s.prep_time FROM bookings b JOIN services s ON b.service_id=s.id WHERE b.id=?", (b_id,)).fetchone()
    if datetime.strptime(f"{b['date']} {b['start_time']}", "%d.%m.%Y %H:%M").replace(tzinfo=TIMEZONE) - get_now() < timedelta(hours=2):
        return await callback.answer("Перенос невозможен менее чем за 2 часа.", show_alert=True)
    await state.update_data(reschedule_id=b_id, srv_id=b['service_id'], srv_name=b['service_name'], srv_price=b['price_paid'], total_duration=b['duration']+b['prep_time'])
    today = get_now()
    tomorrow = today + timedelta(days=1)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Сегодня", callback_data=f"cli_date_{today.strftime('%d.%m.%Y')}"), InlineKeyboardButton(text="Завтра", callback_data=f"cli_date_{tomorrow.strftime('%d.%m.%Y')}")], [InlineKeyboardButton(text="Выбрать дату", callback_data="cli_custom_date")], [InlineKeyboardButton(text="Отмена", callback_data="cancel_client_action")]])
    await safe_edit(callback.message, f"Перенос записи\nУслуга: {b['service_name']}\nВыберите новую дату:", reply_markup=kb)
    await state.set_state(ClientState.date_choice)

# --- О НАС ---
@dp.message(F.text == "О нас")
async def msg_faq(message: Message):
    with get_db() as conn: scheds = conn.execute("SELECT * FROM default_schedule WHERE is_working=1").fetchall()
    days_map = {0:"Пн", 1:"Вт", 2:"Ср", 3:"Чт", 4:"Пт", 5:"Сб", 6:"Вс"}
    schedule_text = "".join([f"{days_map[s['day_of_week']]}: {s['start_time']}-{s['end_time']} " + (f"(обед {s['lunch_start']}-{s['lunch_end']})\n" if s['lunch_start'] else "\n") for s in scheds]) or "Уточняйте по телефону\n"

    text = f"<b>О барбере</b>\nМастер: {BARBER_NAME}\nАдрес: {BARBER_ADDRESS}\n\n<b>График работы:</b>\n{schedule_text}\n<b>Контакты:</b>\nТелефон: {BARBER_PHONE}\nTelegram: @{ADMIN_USERNAME}\n"
    await message.answer(text, parse_mode="HTML")

# ==========================================
# 13. ФОНОВЫЕ ЗАДАЧИ И ЗАПУСК
# ==========================================
async def reminder_worker():
    while True:
        try:
            target_date = (get_now() + timedelta(days=1)).strftime("%d.%m.%Y")
            with get_db() as conn:
                bks = conn.execute("SELECT b.id, b.start_time, c.tg_id FROM bookings b JOIN clients c ON b.client_id=c.id WHERE b.date=? AND b.status='active' AND b.reminded=0 AND c.tg_id IS NOT NULL", (target_date,)).fetchall()
            for b in bks:
                try: 
                    await bot.send_message(b['tg_id'], f"Напоминание.\nЗавтра в {b['start_time']} у вас запись к мастеру {BARBER_NAME}.")
                    with get_db() as conn: conn.execute("UPDATE bookings SET reminded=1 WHERE id=?", (b['id'],))
                except: pass
        except Exception as e: logging.error(f"Reminder: {e}")
        await asyncio.sleep(3600)

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text="OK"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "3000"))).start()

async def main():
    asyncio.create_task(start_web_server())
    asyncio.create_task(reminder_worker())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
