import os
import sqlite3
import asyncio
import logging
import calendar
import re
from datetime import datetime, timedelta, time as dt_time
from dotenv import load_dotenv
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (Message, CallbackQuery, ReplyKeyboardMarkup, 
                           KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, 
                           ReplyKeyboardRemove)
from aiogram.exceptions import TelegramBadRequest

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "username")
BARBER_ADDRESS = os.getenv("BARBER_ADDRESS", "ул. Пушкина, д. 1")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ==========================================
# 1. БАЗА ДАННЫХ
# ==========================================
DB_FILE = "barber_crm.db"

def get_db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS services (
            id INTEGER PRIMARY KEY, name TEXT, description TEXT, 
            duration INTEGER, price INTEGER, is_active INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT, client_id INTEGER, client_name TEXT, client_phone TEXT,
            service_id INTEGER, date TEXT, start_time TEXT, end_time TEXT, 
            status TEXT DEFAULT 'active', discount INTEGER DEFAULT 0, price_paid INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS time_blocks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, start_time TEXT, end_time TEXT
        );
        CREATE TABLE IF NOT EXISTS client_notes (
            client_id INTEGER PRIMARY KEY, note TEXT
        );
        CREATE TABLE IF NOT EXISTS promocodes (
            code TEXT PRIMARY KEY, discount_percent INTEGER, is_active INTEGER DEFAULT 1
        );
        """)
        if not conn.execute("SELECT 1 FROM services").fetchone():
            conn.executemany("INSERT INTO services (name, description, duration, price) VALUES (?, ?, ?, ?)",
                [("Стрижка волос", "Классическая или модельная стрижка.", 45, 1500), 
                 ("Стрижка бороды", "Оформление бороды и усов.", 30, 800), 
                 ("Комплекс", "Стрижка головы и бороды.", 75, 2000)])

init_db()

WORK_START = dt_time(10, 0)
WORK_END = dt_time(20, 0)

def time_to_min(t_str):
    h, m = map(int, t_str.split(':'))
    return h * 60 + m

def min_to_time(m):
    return f"{m // 60:02d}:{m % 60:02d}"

def validate_time(t_str):
    if not re.match(r"^([01]\d|2[0-3]):([0-5]\d)$", t_str):
        return False
    return True

def validate_date(d_str):
    try:
        datetime.strptime(d_str, "%d.%m.%Y")
        return True
    except ValueError:
        return False

def get_available_slots(date_str, duration):
    with get_db() as conn:
        bookings = conn.execute("SELECT start_time, end_time FROM bookings WHERE date=? AND status='active'", (date_str,)).fetchall()
        blocks = conn.execute("SELECT start_time, end_time FROM time_blocks WHERE date=?", (date_str,)).fetchall()

    busy_intervals = [(time_to_min(b['start_time']), time_to_min(b['end_time'])) for b in bookings]
    busy_intervals.extend([(time_to_min(bl['start_time']), time_to_min(bl['end_time'])) for bl in blocks])
    busy_intervals.sort()
    
    available = []
    curr_time = time_to_min(WORK_START.strftime("%H:%M"))
    end_work = time_to_min(WORK_END.strftime("%H:%M"))
    
    while curr_time + duration <= end_work:
        slot_start = curr_time
        slot_end = curr_time + duration
        overlap = False
        for b_start, b_end in busy_intervals:
            if max(slot_start, b_start) < min(slot_end, b_end):
                overlap = True
                break
        if not overlap:
            available.append(min_to_time(slot_start))
        curr_time += 15
        for b_start, b_end in busy_intervals:
            if b_start <= curr_time < b_end:
                curr_time = b_end
    return available

def create_calendar(year, month):
    kb = [[InlineKeyboardButton(text=f"{calendar.month_name[month]} {year}", callback_data="ignore")]]
    kb.append([InlineKeyboardButton(text=d, callback_data="ignore") for d in ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]])
    
    today = datetime.now().date()
    
    for week in calendar.monthcalendar(year, month):
        row = []
        for day in week:
            if day == 0: 
                row.append(InlineKeyboardButton(text=" ", callback_data="ignore"))
            else:
                date_obj = datetime(year, month, day).date()
                if date_obj < today:
                    # Прошедшие даты неактивны
                    row.append(InlineKeyboardButton(text=f"·", callback_data="ignore"))
                else:
                    row.append(InlineKeyboardButton(text=str(day), callback_data=f"date_{day:02d}.{month:02d}.{year}"))
        kb.append(row)
        
    prev_m, prev_y = (month - 1, year) if month > 1 else (12, year - 1)
    next_m, next_y = (month + 1, year) if month < 12 else (1, year + 1)
    kb.append([InlineKeyboardButton(text="<<", callback_data=f"cal_{prev_y}_{prev_m}"),
               InlineKeyboardButton(text="Отмена", callback_data="cancel_action"),
               InlineKeyboardButton(text=">>", callback_data=f"cal_{next_y}_{next_m}")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

async def safe_edit(message: Message, text: str, reply_markup=None):
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest:
        pass

# ==========================================
# 2. КЛАВИАТУРЫ
# ==========================================
def get_client_reply_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="Записаться")],
        [KeyboardButton(text="Мои записи"), KeyboardButton(text="О нас")]
    ], resize_keyboard=True)

def get_admin_reply_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="На сегодня"), KeyboardButton(text="Ручная запись")],
        [KeyboardButton(text="Клиенты"), KeyboardButton(text="Настройки")]
    ], resize_keyboard=True)

def get_admin_inline_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Управление услугами", callback_data="admin_services")],
        [InlineKeyboardButton(text="Статистика выручки", callback_data="admin_stats")],
        [InlineKeyboardButton(text="Промокоды и Рассылка", callback_data="admin_settings")],
        [InlineKeyboardButton(text="Блокировка времени", callback_data="admin_block")]
    ])

# ==========================================
# 3. СОСТОЯНИЯ
# ==========================================
class ClientState(StatesGroup):
    service = State(); calendar = State(); time = State()
    name = State(); phone = State(); confirm = State()
    promo = State(); reschedule_id = State()

class AdminState(StatesGroup):
    manual_name = State(); manual_phone = State(); manual_service = State()
    manual_date = State(); manual_time = State()
    block_date = State(); block_start = State(); block_end = State()
    service_name = State(); service_desc = State(); service_duration = State(); service_price = State()
    promo_code = State(); promo_discount = State()
    broadcast_text = State()
    add_note = State()

# ==========================================
# 4. БАЗОВЫЕ ХЭНДЛЕРЫ
# ==========================================
@dp.callback_query(F.data == "ignore")
async def cb_ignore(callback: CallbackQuery):
    await callback.answer()

@dp.callback_query(F.data == "cancel_action")
async def cb_cancel_action(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.delete()
    await callback.answer("Действие отменено")

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    if message.from_user.id == ADMIN_ID:
        await message.answer("Панель управления барбера.", reply_markup=get_admin_reply_kb())
    else:
        await message.answer("Здравствуйте! Выберите действие на клавиатуре ниже.", reply_markup=get_client_reply_kb())

# ==========================================
# 5. КЛИЕНТ: ЗАПИСЬ
# ==========================================
@dp.message(F.text == "Записаться")
async def msg_book_start(message: Message, state: FSMContext):
    if message.from_user.id == ADMIN_ID: return await message.answer("Вы админ.")
    await state.clear()
    with get_db() as conn: services = conn.execute("SELECT * FROM services WHERE is_active=1").fetchall()
    kb = [[InlineKeyboardButton(text=f"{s['name']} ({s['duration']} мин) - {s['price']}р", callback_data=f"srv_{s['id']}")] for s in services]
    kb.append([InlineKeyboardButton(text="Отмена", callback_data="cancel_action")])
    await message.answer("Выберите услугу:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await state.set_state(ClientState.service)

@dp.callback_query(ClientState.service, F.data.startswith("srv_"))
async def cb_service(callback: CallbackQuery, state: FSMContext):
    srv_id = int(callback.data.split("_")[1])
    await state.update_data(srv_id=srv_id)
    with get_db() as conn: srv = conn.execute("SELECT * FROM services WHERE id=?", (srv_id,)).fetchone()
    
    text = f"Вы выбрали: {srv['name']}\n{srv['description']}\nДлительность: {srv['duration']} мин.\nЦена: {srv['price']} руб."
    await safe_edit(callback.message, text)
    now = datetime.now()
    await callback.message.answer("Выберите дату:", reply_markup=create_calendar(now.year, now.month))
    await state.set_state(ClientState.calendar)
    await callback.answer()

@dp.callback_query(ClientState.calendar, F.data.startswith("cal_"))
async def cb_calendar_nav(callback: CallbackQuery):
    _, y, m = callback.data.split("_")
    try: await callback.message.edit_reply_markup(reply_markup=create_calendar(int(y), int(m)))
    except: pass
    await callback.answer()

@dp.callback_query(ClientState.calendar, F.data.startswith("date_"))
async def cb_date(callback: CallbackQuery, state: FSMContext):
    date_str = callback.data.split("_")[1]
    data = await state.get_data()
    with get_db() as conn: srv = conn.execute("SELECT duration FROM services WHERE id=?", (data['srv_id'],)).fetchone()
    slots = get_available_slots(date_str, srv['duration'])
    if not slots: 
        await callback.answer("На эту дату нет свободного времени.", show_alert=True)
        return
    kb = []
    row = []
    for slot in slots:
        row.append(InlineKeyboardButton(text=slot, callback_data=f"time_{slot}"))
        if len(row) == 3: kb.append(row); row = []
    if row: kb.append(row)
    kb.append([InlineKeyboardButton(text="Отмена", callback_data="cancel_action")])
    
    await state.update_data(date=date_str)
    await safe_edit(callback.message, f"Свободное время на {date_str}:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await state.set_state(ClientState.time)
    await callback.answer()

@dp.callback_query(ClientState.time, F.data.startswith("time_"))
async def cb_time(callback: CallbackQuery, state: FSMContext):
    await state.update_data(time=callback.data.split("_")[1])
    if (await state.get_data()).get("reschedule_id"):
        await finalize_booking(callback.message, state, callback.from_user.id, is_reschedule=True)
        await callback.answer()
        return
    
    await safe_edit(callback.message, "Введите Промокод (если есть) или нажмите 'Пропустить':", 
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Пропустить", callback_data="skip_promo")]]))
    await state.set_state(ClientState.promo)
    await callback.answer()

@dp.callback_query(ClientState.promo, F.data == "skip_promo")
async def cb_skip_promo(callback: CallbackQuery, state: FSMContext):
    await state.update_data(discount=0, promo=None)
    await safe_edit(callback.message, "Введите ваше имя:")
    await state.set_state(ClientState.name)
    await callback.answer()

@dp.message(ClientState.promo)
async def process_promo(message: Message, state: FSMContext):
    code = message.text.strip().upper()
    with get_db() as conn:
        promo = conn.execute("SELECT * FROM promocodes WHERE code=? AND is_active=1", (code,)).fetchone()
    
    if promo:
        await state.update_data(discount=promo['discount_percent'], promo=code)
        await message.answer(f"Промокод применен! Скидка: {promo['discount_percent']}%")
    else:
        await state.update_data(discount=0, promo=None)
        await message.answer("Промокод не найден. Записываем без скидки.")
    
    await message.answer("Введите ваше имя:")
    await state.set_state(ClientState.name)

@dp.message(ClientState.name)
async def process_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text)
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Отправить номер", request_contact=True)]], resize_keyboard=True, one_time_keyboard=True)
    await message.answer("Отправьте номер телефона кнопкой или введите вручную:", reply_markup=kb)
    await state.set_state(ClientState.phone)

@dp.message(ClientState.phone)
async def process_phone(message: Message, state: FSMContext):
    phone = message.contact.phone_number if message.contact else message.text
    await state.update_data(phone=phone)
    data = await state.get_data()
    with get_db() as conn: srv = conn.execute("SELECT * FROM services WHERE id=?", (data['srv_id'],)).fetchone()
    
    total_discount = data.get('discount', 0)
    final_price = int(srv['price'] * (1 - total_discount / 100))
    
    text = f"Проверьте данные:\nУслуга: {srv['name']}\nДата: {data['date']} в {data['time']}\nИмя: {data['name']}\nТел: {phone}\n"
    if total_discount > 0: text += f"Скидка: {total_discount}%\nЦена: {srv['price']}р -> {final_price}р"
    else: text += f"Цена: {srv['price']}р"
    
    await state.update_data(total_discount=total_discount, final_price=final_price)
    await message.answer(text, reply_markup=ReplyKeyboardRemove())
    await message.answer("Подтвердите запись:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Подтвердить", callback_data="confirm_yes")],
        [InlineKeyboardButton(text="Отмена", callback_data="cancel_action")]]))
    await state.set_state(ClientState.confirm)

@dp.callback_query(ClientState.confirm, F.data == "confirm_yes")
async def cb_confirm_booking(callback: CallbackQuery, state: FSMContext):
    await finalize_booking(callback.message, state, callback.from_user.id)
    await callback.answer()

async def finalize_booking(message_obj, state: FSMContext, client_id=None, is_reschedule=False):
    data = await state.get_data()
    with get_db() as conn:
        srv = conn.execute("SELECT * FROM services WHERE id=?", (data['srv_id'],)).fetchone()
        end_time = min_to_time(time_to_min(data['time']) + srv['duration'])
        
        if is_reschedule and data.get('reschedule_id'):
            conn.execute("UPDATE bookings SET date=?, start_time=?, end_time=? WHERE id=?", 
                         (data['date'], data['time'], end_time, data['reschedule_id']))
            msg_text = "Запись успешно перенесена!"
        else:
            cur = conn.execute("INSERT INTO bookings (client_id, client_name, client_phone, service_id, date, start_time, end_time, discount, price_paid) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (client_id, data.get('name', 'Клиент'), data.get('phone', ''), srv['id'], data['date'], data['time'], end_time, data.get('total_discount', 0), data.get('final_price', srv['price'])))
            b_id = cur.lastrowid
            msg_text = f"Вы успешно записаны!\n\nЖдем вас {data['date']} в {data['time']}.\nАдрес: {BARBER_ADDRESS}"
            admin_text = f"НОВАЯ ЗАПИСЬ #{b_id}\n{data.get('name', 'Клиент')}\nТел: {data.get('phone', '')}\n{srv['name']} - {data['date']} в {data['time']}"
            try: await bot.send_message(ADMIN_ID, admin_text)
            except: pass
            
    await message_obj.edit_text(msg_text)
    await bot.send_message(client_id, "Главное меню:", reply_markup=get_client_reply_kb())
    await state.clear()

# ==========================================
# 6. КЛИЕНТ: МОИ ЗАПИСИ
# ==========================================
@dp.message(F.text == "Мои записи")
async def msg_my_bookings(message: Message):
    with get_db() as conn:
        bks = conn.execute("SELECT b.*, s.name as srv_name FROM bookings b JOIN services s ON b.service_id=s.id WHERE b.client_id=? AND b.status IN ('active', 'completed') ORDER BY b.date DESC, b.start_time DESC LIMIT 5", (message.from_user.id,)).fetchall()
    
    if not bks: return await message.answer("У вас нет записей.")
    
    await message.answer("Ваши последние записи:")
    for b in bks:
        status = "Активна" if b['status'] == 'active' else "Завершена"
        text = f"Услуга: {b['srv_name']}\nДата: {b['date']} в {b['start_time']}\nСтатус: {status}"
        kb = []
        if b['status'] == 'active':
            kb.append([InlineKeyboardButton(text="Перенести", callback_data=f"resch_{b['id']}"),
                       InlineKeyboardButton(text="Отменить", callback_data=f"canc_{b['id']}")])
        elif b['status'] == 'completed':
            kb.append([InlineKeyboardButton(text="Записаться снова", callback_data=f"rebook_{b['service_id']}")])
        
        await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb) if kb else None)

@dp.callback_query(F.data.startswith("rebook_"))
async def cb_rebook(callback: CallbackQuery, state: FSMContext):
    srv_id = int(callback.data.split("_")[1])
    await state.update_data(srv_id=srv_id)
    now = datetime.now()
    await safe_edit(callback.message, "Выберите новую дату:", reply_markup=create_calendar(now.year, now.month))
    await state.set_state(ClientState.calendar)
    await callback.answer()

@dp.callback_query(F.data.startswith("resch_"))
async def cb_reschedule(callback: CallbackQuery, state: FSMContext):
    b_id = int(callback.data.split("_")[1])
    await state.update_data(reschedule_id=b_id)
    with get_db() as conn: b = conn.execute("SELECT service_id FROM bookings WHERE id=?", (b_id,)).fetchone()
    await state.update_data(srv_id=b['service_id'])
    now = datetime.now()
    await safe_edit(callback.message, "Выберите новую дату:", reply_markup=create_calendar(now.year, now.month))
    await state.set_state(ClientState.calendar)
    await callback.answer()

@dp.callback_query(F.data.startswith("canc_"))
async def cb_cancel(callback: CallbackQuery):
    b_id = int(callback.data.split("_")[1])
    with get_db() as conn:
        b = conn.execute("SELECT * FROM bookings WHERE id=?", (b_id,)).fetchone()
        conn.execute("UPDATE bookings SET status='cancelled' WHERE id=?", (b_id,))
    
    await safe_edit(callback.message, "Запись отменена.", reply_markup=None)
    try: await bot.send_message(ADMIN_ID, f"КЛИЕНТ ОТМЕНИЛ #{b_id}\n{b['client_name']} {b['date']} {b['start_time']}")
    except: pass
    await callback.answer()

@dp.message(F.text == "О нас")
async def msg_faq(message: Message):
    text = f"Барбер: @{ADMIN_USERNAME}\nАдрес: {BARBER_ADDRESS}\nРаботаем: 10:00 - 20:00\n\nПравила отмены: не позднее чем за 2 часа до визита.\nСвязь: @{ADMIN_USERNAME}"
    await message.answer(text)

# ==========================================
# 7. АДМИН ПАНЕЛЬ
# ==========================================
@dp.message(F.text == "На сегодня")
async def msg_admin_today(message: Message):
    if message.from_user.id != ADMIN_ID: return
    today = datetime.now().strftime("%d.%m.%Y")
    with get_db() as conn:
        bks = conn.execute("SELECT b.*, s.name as srv_name FROM bookings b JOIN services s ON b.service_id=s.id WHERE b.date=? AND b.status='active' ORDER BY b.start_time", (today,)).fetchall()
    
    if not bks: return await message.answer("На сегодня записей нет.")
    
    text = f"Записи на сегодня ({today}):\n\n"
    for b in bks:
        text += f"{b['start_time']} - {b['srv_name']}\nКлиент: {b['client_name']} ({b['client_phone']})\n\n"
    
    await message.answer(text)

@dp.message(F.text == "Настройки")
async def msg_admin_settings(message: Message):
    if message.from_user.id != ADMIN_ID: return
    await message.answer("Дополнительные настройки:", reply_markup=get_admin_inline_menu())

# --- СТАТИСТИКА ---
@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    with get_db() as conn:
        # Выручка за текущий месяц (по завершенным)
        month_start = datetime.now().replace(day=1).strftime("%d.%m.%Y")
        rev = conn.execute("SELECT SUM(price_paid) FROM bookings WHERE status='completed' AND date >= ?", (month_start,)).fetchone()[0]
        rev = rev if rev else 0
        
        # Количество записей (всего за месяц)
        total = conn.execute("SELECT COUNT(*) FROM bookings WHERE date >= ?", (month_start,)).fetchone()[0]
        
        # Популярные услуги
        pop = conn.execute("""SELECT s.name, COUNT(b.id) as c FROM bookings b JOIN services s ON b.service_id=s.id 
                              WHERE b.date >= ? GROUP BY s.id ORDER BY c DESC LIMIT 3""", (month_start,)).fetchall()
    
    text = f"Статистика за текущий месяц:\n\n"
    text += f"Выручка: {rev} руб.\n"
    text += f"Всего записей: {total}\n\n"
    text += "Топ услуг:\n"
    for p in pop: text += f"- {p['name']} ({p['c']} шт.)\n"
    
    await safe_edit(callback.message, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Закрыть", callback_data="cancel_action")]]))
    await callback.answer()

# --- УПРАВЛЕНИЕ УСЛУГАМИ ---
@dp.callback_query(F.data == "admin_services")
async def admin_services(callback: CallbackQuery):
    with get_db() as conn: services = conn.execute("SELECT * FROM services").fetchall()
    
    text = "Список услуг (нажмите для ВКЛ/ВЫКЛ):\n\n"
    kb = []
    for s in services:
        status = "+" if s['is_active'] else "-"
        kb.append([InlineKeyboardButton(text=f"[{status}] {s['name']} - {s['price']}р", callback_data=f"toggsrv_{s['id']}")])
    
    kb.append([InlineKeyboardButton(text="Добавить услугу", callback_data="newsrv")])
    kb.append([InlineKeyboardButton(text="Назад", callback_data="cancel_action")])
    await safe_edit(callback.message, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await callback.answer()

@dp.callback_query(F.data.startswith("toggsrv_"))
async def admin_toggle_service(callback: CallbackQuery):
    srv_id = int(callback.data.split("_")[1])
    with get_db() as conn:
        conn.execute("UPDATE services SET is_active = 1 - is_active WHERE id=?", (srv_id,))
    await callback.answer("Статус обновлен.", show_alert=True)
    await admin_services(callback) 

@dp.callback_query(F.data == "newsrv")
async def admin_new_service(callback: CallbackQuery, state: FSMContext):
    await safe_edit(callback.message, "Введите название услуги:")
    await state.set_state(AdminState.service_name)
    await callback.answer()

@dp.message(AdminState.service_name)
async def admin_service_name(message: Message, state: FSMContext):
    await state.update_data(sname=message.text)
    await message.answer("Введите описание:")
    await state.set_state(AdminState.service_desc)

@dp.message(AdminState.service_desc)
async def admin_service_desc(message: Message, state: FSMContext):
    await state.update_data(sdesc=message.text)
    await message.answer("Введите длительность в минутах (число):")
    await state.set_state(AdminState.service_duration)

@dp.message(AdminState.service_duration)
async def admin_service_duration(message: Message, state: FSMContext):
    if not message.text.isdigit(): return await message.answer("Введите число!")
    await state.update_data(sdur=int(message.text))
    await message.answer("Введите цену (число):")
    await state.set_state(AdminState.service_price)

@dp.message(AdminState.service_price)
async def admin_service_price(message: Message, state: FSMContext):
    if not message.text.isdigit(): return await message.answer("Введите число!")
    data = await state.get_data()
    with get_db() as conn:
        conn.execute("INSERT INTO services (name, description, duration, price) VALUES (?, ?, ?, ?)",
                     (data['sname'], data['sdesc'], data['sdur'], int(message.text)))
    await message.answer("Услуга добавлена.", reply_markup=get_admin_reply_kb())
    await state.clear()

# --- РУЧНАЯ ЗАПИСЬ ---
@dp.message(F.text == "Ручная запись")
async def admin_manual_start(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    await state.clear()
    await message.answer("Введите имя клиента:")
    await state.set_state(AdminState.manual_name)

@dp.message(AdminState.manual_name)
async def admin_manual_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text)
    await message.answer("Введите телефон клиента:")
    await state.set_state(AdminState.manual_phone)

@dp.message(AdminState.manual_phone)
async def admin_manual_phone(message: Message, state: FSMContext):
    await state.update_data(phone=message.text)
    with get_db() as conn: services = conn.execute("SELECT * FROM services WHERE is_active=1").fetchall()
    kb = [[InlineKeyboardButton(text=s['name'], callback_data=f"mansrv_{s['id']}")] for s in services]
    kb.append([InlineKeyboardButton(text="Отмена", callback_data="cancel_action")])
    await message.answer("Выберите услугу:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await state.set_state(AdminState.manual_service)

@dp.callback_query(AdminState.manual_service, F.data.startswith("mansrv_"))
async def admin_manual_service(callback: CallbackQuery, state: FSMContext):
    await state.update_data(srv_id=int(callback.data.split("_")[1]))
    now = datetime.now()
    await safe_edit(callback.message, "Выберите дату:", reply_markup=create_calendar(now.year, now.month))
    await state.set_state(AdminState.manual_date)
    await callback.answer()

@dp.callback_query(AdminState.manual_date, F.data.startswith("cal_"))
async def admin_manual_cal_nav(callback: CallbackQuery):
    _, y, m = callback.data.split("_")
    try: await callback.message.edit_reply_markup(reply_markup=create_calendar(int(y), int(m)))
    except: pass
    await callback.answer()

@dp.callback_query(AdminState.manual_date, F.data.startswith("date_"))
async def admin_manual_date(callback: CallbackQuery, state: FSMContext):
    date_str = callback.data.split("_")[1]
    await state.update_data(date=date_str)
    data = await state.get_data()
    with get_db() as conn: srv = conn.execute("SELECT duration FROM services WHERE id=?", (data['srv_id'],)).fetchone()
    slots = get_available_slots(date_str, srv['duration'])
    if not slots: return await callback.answer("Нет мест.", show_alert=True)
    
    kb = []
    row = []
    for slot in slots:
        row.append(InlineKeyboardButton(text=slot, callback_data=f"mtime_{slot}"))
        if len(row) == 3: kb.append(row); row = []
    if row: kb.append(row)
    kb.append([InlineKeyboardButton(text="Отмена", callback_data="cancel_action")])
    
    await safe_edit(callback.message, f"Свободное время на {date_str}:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await state.set_state(AdminState.manual_time)
    await callback.answer()

@dp.callback_query(AdminState.manual_time, F.data.startswith("mtime_"))
async def admin_manual_time(callback: CallbackQuery, state: FSMContext):
    time_str = callback.data.split("_")[1]
    data = await state.get_data()
    with get_db() as conn:
        srv = conn.execute("SELECT * FROM services WHERE id=?", (data['srv_id'],)).fetchone()
        end_time = min_to_time(time_to_min(time_str) + srv['duration'])
        conn.execute("INSERT INTO bookings (client_id, client_name, client_phone, service_id, date, start_time, end_time, status, price_paid) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)",
                     (0, data['name'], data['phone'], srv['id'], data['date'], time_str, end_time, srv['price']))
    await safe_edit(callback.message, f"Клиент {data['name']} добавлен на {data['date']} в {time_str}.", reply_markup=None)
    await callback.message.answer("Главное меню:", reply_markup=get_admin_reply_kb())
    await state.clear()
    await callback.answer()

# --- БЛОКИРОВКА ВРЕМЕНИ ---
@dp.callback_query(F.data == "admin_block")
async def admin_block_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    await safe_edit(callback.message, "Введите дату блокировки (ДД.ММ.ГГГГ):")
    await state.set_state(AdminState.block_date)
    await callback.answer()

@dp.message(AdminState.block_date)
async def admin_block_date(message: Message, state: FSMContext):
    if not validate_date(message.text): return await message.answer("Неверный формат. Введите ДД.ММ.ГГГГ:")
    await state.update_data(block_date=message.text)
    await message.answer("Введите время начала (ЧЧ:ММ):")
    await state.set_state(AdminState.block_start)

@dp.message(AdminState.block_start)
async def admin_block_start_time(message: Message, state: FSMContext):
    if not validate_time(message.text): return await message.answer("Неверный формат. Введите ЧЧ:ММ:")
    await state.update_data(block_start=message.text)
    await message.answer("Введите время конца (ЧЧ:ММ):")
    await state.set_state(AdminState.block_end)

@dp.message(AdminState.block_end)
async def admin_block_end_time(message: Message, state: FSMContext):
    if not validate_time(message.text): return await message.answer("Неверный формат. Введите ЧЧ:ММ:")
    data = await state.get_data()
    with get_db() as conn:
        conn.execute("INSERT INTO time_blocks (date, start_time, end_time) VALUES (?, ?, ?)", (data['block_date'], data['block_start'], message.text))
    await message.answer("Время заблокировано для клиентов.", reply_markup=get_admin_reply_kb())
    await state.clear()

# --- КЛИЕНТЫ ---
@dp.message(F.text == "Клиенты")
async def admin_clients_msg(message: Message):
    if message.from_user.id != ADMIN_ID: return
    with get_db() as conn:
        clients = conn.execute("SELECT DISTINCT client_id, client_name, client_phone FROM bookings WHERE client_id > 0").fetchall()
    if not clients: return await message.answer("База пуста.")
    kb = [[InlineKeyboardButton(text=f"{c['client_name']} ({c['client_phone']})", callback_data=f"adm_client_{c['client_id']}")] for c in clients]
    await message.answer("Выберите клиента:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data.startswith("adm_client_"))
async def admin_view_client(callback: CallbackQuery):
    client_id = int(callback.data.split("_")[2])
    with get_db() as conn:
        history = conn.execute("SELECT b.*, s.name as srv_name FROM bookings b JOIN services s ON b.service_id=s.id WHERE b.client_id=? AND b.status='completed' ORDER BY b.date DESC", (client_id,)).fetchall()
        note = conn.execute("SELECT note FROM client_notes WHERE client_id=?", (client_id,)).fetchone()
    
    text = f"История клиента (ID: {client_id}):\n\n"
    if note: text += f"Заметка мастера: {note['note']}\n\n"
    text += "Посещения:\n"
    for h in history: text += f"- {h['date']} ({h['srv_name']})\n"
    if not history: text += "Пока нет завершенных визитов."
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Добавить заметку", callback_data=f"adm_note_{client_id}")],
        [InlineKeyboardButton(text="Закрыть", callback_data="cancel_action")]
    ])
    await safe_edit(callback.message, text, reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("adm_note_"))
async def admin_add_note_start(callback: CallbackQuery, state: FSMContext):
    client_id = int(callback.data.split("_")[2])
    await state.update_data(note_client_id=client_id)
    await safe_edit(callback.message, "Введите текст заметки:")
    await state.set_state(AdminState.add_note)
    await callback.answer()

@dp.message(AdminState.add_note)
async def admin_add_note_save(message: Message, state: FSMContext):
    data = await state.get_data()
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO client_notes (client_id, note) VALUES (?, ?)", (data['note_client_id'], message.text))
    await message.answer("Заметка сохранена.", reply_markup=get_admin_reply_kb())
    await state.clear()

# --- ПРОМОКОДЫ И РАССЫЛКА ---
@dp.callback_query(F.data == "admin_settings")
async def admin_settings_extra(callback: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Создать промокод", callback_data="admin_new_promo")],
        [InlineKeyboardButton(text="Рассылка клиентам", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="Закрыть", callback_data="cancel_action")]
    ])
    await safe_edit(callback.message, "Дополнительные настройки:", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data == "admin_new_promo")
async def admin_new_promo(callback: CallbackQuery, state: FSMContext):
    await safe_edit(callback.message, "Введите код промокода (например, SALE10):")
    await state.set_state(AdminState.promo_code)
    await callback.answer()

@dp.message(AdminState.promo_code)
async def admin_promo_code(message: Message, state: FSMContext):
    await state.update_data(pcode=message.text.upper())
    await message.answer("Введите размер скидки в % (число от 1 до 100):")
    await state.set_state(AdminState.promo_discount)

@dp.message(AdminState.promo_discount)
async def admin_promo_discount(message: Message, state: FSMContext):
    if not message.text.isdigit() or not (1 <= int(message.text) <= 100):
        return await message.answer("Введите число от 1 до 100!")
    data = await state.get_data()
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO promocodes (code, discount_percent) VALUES (?, ?)", (data['pcode'], int(message.text)))
    await message.answer(f"Промокод {data['pcode']} создан.", reply_markup=get_admin_reply_kb())
    await state.clear()

@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_start(callback: CallbackQuery, state: FSMContext):
    await safe_edit(callback.message, "Введите текст сообщения для рассылки всем клиентам:")
    await state.set_state(AdminState.broadcast_text)
    await callback.answer()

@dp.message(AdminState.broadcast_text)
async def admin_broadcast_send(message: Message, state: FSMContext):
    text = message.text
    with get_db() as conn:
        clients = conn.execute("SELECT DISTINCT client_id FROM bookings WHERE client_id > 0").fetchall()
    
    count = 0
    await message.answer("Рассылка запущена...", reply_markup=get_admin_reply_kb())
    for c in clients:
        try:
            await bot.send_message(c['client_id'], text)
            count += 1
            await asyncio.sleep(0.05)
        except: pass
    
    await message.answer(f"Рассылка завершена. Отправлено: {count} чел.")
    await state.clear()

# ==========================================
# 8. ФОНОВЫЕ ЗАДАЧИ
# ==========================================
async def reminder_worker():
    while True:
        try:
            tomorrow = (datetime.now() + timedelta(days=1)).strftime("%d.%m.%Y")
            with get_db() as conn:
                bks = conn.execute("SELECT * FROM bookings WHERE date=? AND status='active' AND client_id > 0", (tomorrow,)).fetchall()
            for b in bks:
                try: await bot.send_message(b['client_id'], f"Напоминание.\nЗавтра в {b['start_time']} у вас запись.\nАдрес: {BARBER_ADDRESS}")
                except: pass
        except Exception as e: logging.error(f"Reminder: {e}")
        await asyncio.sleep(3600)

async def report_worker():
    last_report_date = None
    while True:
        now = datetime.now()
        today = now.strftime("%d.%m.%Y")
        if now.hour == 20 and now.minute == 0 and last_report_date != today:
            last_report_date = today
            with get_db() as conn:
                res = conn.execute("SELECT status, COUNT(*) as c FROM bookings WHERE date=? GROUP BY status", (today,)).fetchall()
            text = f"Отчет за {today}:\n"
            for r in res: text += f"{r['status']}: {r['c']}\n"
            if not res: text += "Записей не было."
            try: await bot.send_message(ADMIN_ID, text)
            except: pass
        await asyncio.sleep(30)

# ==========================================
# 9. ЗАПУСК
# ==========================================
async def start_web_server():
    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text="OK"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "3000"))).start()

async def main():
    asyncio.create_task(start_web_server())
    asyncio.create_task(reminder_worker())
    asyncio.create_task(report_worker())
    logging.info("CRM Бот запущен.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
