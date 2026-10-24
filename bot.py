import os
import sqlite3
import asyncio
import logging
import calendar
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
                           ReplyKeyboardRemove, FSInputFile)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "username")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# --- БАЗА ДАННЫХ ---
def get_db():
    conn = sqlite3.connect("barber.db", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS services (
            id INTEGER PRIMARY KEY, name TEXT, duration INTEGER, price INTEGER, is_active INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT, client_id INTEGER, client_name TEXT, client_phone TEXT,
            service_id INTEGER, date TEXT, start_time TEXT, end_time TEXT, status TEXT DEFAULT 'active'
        );
        CREATE TABLE IF NOT EXISTS time_blocks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, start_time TEXT, end_time TEXT, reason TEXT
        );
        CREATE TABLE IF NOT EXISTS client_notes (
            client_id INTEGER PRIMARY KEY, note TEXT
        );
        """)
        # Добавляем базовые услуги, если их нет
        if not conn.execute("SELECT 1 FROM services").fetchone():
            conn.executemany("INSERT INTO services (name, duration, price) VALUES (?, ?, ?)",
                [("Стрижка волос", 45, 1500), ("Стрижка бороды", 30, 800), ("Комплекс", 75, 2000)])

# --- ЛОГИКА ВРЕМЕНИ ---
WORK_START = dt_time(10, 0)
WORK_END = dt_time(20, 0)

def time_to_min(t_str):
    h, m = map(int, t_str.split(':'))
    return h * 60 + m

def min_to_time(m):
    return f"{m // 60:02d}:{m % 60:02d}"

def get_available_slots(date_str, duration):
    """Возвращает список доступного времени с учетом длительности и занятости"""
    with get_db() as conn:
        # Все записи на этот день
        bookings = conn.execute("SELECT start_time, end_time FROM bookings WHERE date=? AND status='active'", (date_str,)).fetchall()
        # Все блокировки (перерывы, отпуска)
        blocks = conn.execute("SELECT start_time, end_time FROM time_blocks WHERE date=?", (date_str,)).fetchall()

    busy_intervals = []
    for b in bookings:
        busy_intervals.append((time_to_min(b['start_time']), time_to_min(b['end_time'])))
    for bl in blocks:
        busy_intervals.append((time_to_min(bl['start_time']), time_to_min(bl['end_time'])))
    
    # Сортируем интервалы
    busy_intervals.sort()
    
    available = []
    curr_time = time_to_min(WORK_START.strftime("%H:%M"))
    end_work = time_to_min(WORK_END.strftime("%H:%M"))
    
    # Шаг в 15 минут для генерации слотов
    while curr_time + duration <= end_work:
        slot_start = curr_time
        slot_end = curr_time + duration
        
        # Проверяем, не пересекается ли слен с занятыми интервалами
        overlap = False
        for b_start, b_end in busy_intervals:
            if max(slot_start, b_start) < min(slot_end, b_end):
                overlap = True
                break
                
        if not overlap:
            available.append(min_to_time(slot_start))
            
        curr_time += 15 # Следующий потенциальный слот
        
        # Оптимизация: если мы наткнулись на занятое время, перепрыгиваем его
        for b_start, b_end in busy_intervals:
            if b_start <= curr_time < b_end:
                curr_time = b_end

    return available

# --- КАЛЕНДАРЬ ---
def create_calendar(year, month):
    kb = []
    # Заголовок
    kb.append([InlineKeyboardButton(text=f"{calendar.month_name[month]} {year}", callback_data="ignore")])
    # Дни недели
    kb.append([InlineKeyboardButton(text=d, callback_data="ignore") for d in ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]])
    
    # Дни
    month_days = calendar.monthcalendar(year, month)
    for week in month_days:
        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(text=" ", callback_data="ignore"))
            else:
                date_str = f"{day:02d}.{month:02d}.{year}"
                row.append(InlineKeyboardButton(text=str(day), callback_data=f"date_{date_str}"))
        kb.append(row)
        
    # Навигация
    prev_month = month - 1 if month > 1 else 12
    prev_year = year if month > 1 else year - 1
    next_month = month + 1 if month < 12 else 1
    next_year = year if month < 12 else year + 1
    
    kb.append([
        InlineKeyboardButton(text="<<", callback_data=f"cal_{prev_year}_{prev_month}"),
        InlineKeyboardButton(text="Отмена", callback_data="main_menu"),
        InlineKeyboardButton(text=">>", callback_data=f"cal_{next_year}_{next_month}")
    ])
    return InlineKeyboardMarkup(inline_keyboard=kb)

# --- КЛАВИАТУРЫ ---
def main_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Записаться", callback_data="book_start")],
        [InlineKeyboardButton(text="Мои записи", callback_data="my_bookings")],
        [InlineKeyboardButton(text="О нас / FAQ", callback_data="faq")]
    ])

def admin_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Записи на сегодня", callback_data="admin_today")],
        [InlineKeyboardButton(text="Записи на завтра", callback_data="admin_tomorrow")],
        [InlineKeyboardButton(text="Все будущие записи", callback_data="admin_all")],
        [InlineKeyboardButton(text="Заблокировать время (перерыв)", callback_data="admin_block")],
        [InlineKeyboardButton(text="Ручная запись", callback_data="admin_manual")]
    ])

def back_to_main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="В меню", callback_data="main_menu")]])

# --- СОСТОЯНИЯ ---
class ClientState(StatesGroup):
    service = State()
    calendar = State()
    time = State()
    name = State()
    phone = State()
    confirm = State()
    reschedule_id = State() # ID записи, которую переносим

class AdminState(StatesGroup):
    manual_name = State()
    manual_phone = State()
    manual_service = State()
    manual_date = State()
    manual_time = State()
    block_date = State()
    block_start = State()
    block_end = State()

# --- ХЭНДЛЕРЫ КЛИЕНТА ---

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Загрузка...", reply_markup=ReplyKeyboardRemove())
    init_db()
    if message.from_user.id == ADMIN_ID:
        await message.answer("Панель мастера:", reply_markup=admin_menu_kb())
    else:
        await message.answer("Добро пожаловать. Выберите действие:", reply_markup=main_menu_kb())

@dp.callback_query(F.data == "main_menu")
async def cb_main_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Главное меню:", reply_markup=main_menu_kb())
    await callback.answer()

@dp.callback_query(F.data == "faq")
async def cb_faq(callback: CallbackQuery):
    text = f"Барбер: {ADMIN_USERNAME}\nАдрес: ул. Пушкина, д. 1\nРаботаем: 10:00 - 20:00\n\nСвязь: @{ADMIN_USERNAME}"
    await callback.message.edit_text(text, reply_markup=back_to_main_kb())
    await callback.answer()

@dp.callback_query(F.data == "book_start")
async def cb_book_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id == ADMIN_ID:
        await callback.answer("Вы админ. Используйте ручную запись.", show_alert=True)
        return
    
    with get_db() as conn:
        services = conn.execute("SELECT * FROM services WHERE is_active=1").fetchall()
    
    kb = [[InlineKeyboardButton(text=f"{s['name']} ({s['duration']} мин) - {s['price']}р", callback_data=f"srv_{s['id']}")] for s in services]
    kb.append([InlineKeyboardButton(text="Назад", callback_data="main_menu")])
    
    await callback.message.edit_text("Выберите услугу:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await state.set_state(ClientState.service)
    await callback.answer()

@dp.callback_query(ClientState.service, F.data.startswith("srv_"))
async def cb_service(callback: CallbackQuery, state: FSMContext):
    srv_id = int(callback.data.split("_")[1])
    await state.update_data(srv_id=srv_id)
    now = datetime.now()
    await callback.message.edit_text("Выберите дату:", reply_markup=create_calendar(now.year, now.month))
    await state.set_state(ClientState.calendar)
    await callback.answer()

@dp.callback_query(ClientState.calendar, F.data.startswith("cal_"))
async def cb_calendar_nav(callback: CallbackQuery):
    _, y, m = callback.data.split("_")
    await callback.message.edit_reply_markup(reply_markup=create_calendar(int(y), int(m)))
    await callback.answer()

@dp.callback_query(ClientState.calendar, F.data.startswith("date_"))
async def cb_date(callback: CallbackQuery, state: FSMContext):
    date_str = callback.data.split("_")[1]
    data = await state.get_data()
    
    with get_db() as conn:
        srv = conn.execute("SELECT duration FROM services WHERE id=?", (data['srv_id'],)).fetchone()
    
    slots = get_available_slots(date_str, srv['duration'])
    
    if not slots:
        await callback.answer("На эту дату нет свободного времени.", show_alert=True)
        return
        
    kb = []
    row = []
    for slot in slots:
        row.append(InlineKeyboardButton(text=slot, callback_data=f"time_{slot}"))
        if len(row) == 3:
            kb.append(row)
            row = []
    if row: kb.append(row)
    kb.append([InlineKeyboardButton(text="Назад к календарю", callback_data="book_start")])
    
    await state.update_data(date=date_str)
    await callback.message.edit_text(f"Свободное время на {date_str}:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await state.set_state(ClientState.time)
    await callback.answer()

@dp.callback_query(ClientState.time, F.data.startswith("time_"))
async def cb_time(callback: CallbackQuery, state: FSMContext):
    time_str = callback.data.split("_")[1]
    await state.update_data(time=time_str)
    
    data = await state.get_data()
    if data.get("reschedule_id"):
        # Если это перенос, пропускаем ввод имени и телефона
        await process_booking(callback.message, state, is_reschedule=True)
        await callback.answer()
        return

    await callback.message.edit_text("Введите ваше имя:")
    await state.set_state(ClientState.name)
    await callback.answer()

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
    with get_db() as conn:
        srv = conn.execute("SELECT * FROM services WHERE id=?", (data['srv_id'],)).fetchone()
    
    text = f"Проверьте данные:\nУслуга: {srv['name']}\nДата: {data['date']}\nВремя: {data['time']}\nИмя: {data['name']}\nТел: {phone}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Подтвердить", callback_data="confirm_yes")],
        [InlineKeyboardButton(text="Отмена", callback_data="main_menu")]
    ])
    await message.answer(text, reply_markup=ReplyKeyboardRemove())
    await message.answer("Подтвердите запись:", reply_markup=kb)
    await state.set_state(ClientState.confirm)

@dp.callback_query(ClientState.confirm, F.data == "confirm_yes")
async def process_booking(message_obj, state: FSMContext, is_reschedule=False):
    data = await state.get_data()
    
    with get_db() as conn:
        srv = conn.execute("SELECT * FROM services WHERE id=?", (data['srv_id'],)).fetchone()
        start_min = time_to_min(data['time'])
        end_time = min_to_time(start_min + srv['duration'])
        
        if is_reschedule and data.get("reschedule_id"):
            conn.execute("UPDATE bookings SET date=?, start_time=?, end_time=? WHERE id=?", 
                         (data['date'], data['time'], end_time, data['reschedule_id']))
            msg_text = "Запись успешно перенесена!"
        else:
            conn.execute("INSERT INTO bookings (client_id, client_name, client_phone, service_id, date, start_time, end_time) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (message_obj.chat.id, data['name'], data['phone'], srv['id'], data['date'], data['time'], end_time))
            msg_text = "Вы успешно записаны!"
            
            # Уведомление админу
            admin_text = f"НОВАЯ ЗАПИСЬ\nКлиент: {data['name']} (@{message_obj.chat.username})\nТел: {data['phone']}\nУслуга: {srv['name']}\nВремя: {data['date']} в {data['time']}"
            try: await bot.send_message(ADMIN_ID, admin_text)
            except: pass
    
    if is_reschedule:
        await message_obj.answer(msg_text, reply_markup=back_to_main_kb())
    else:
        await message_obj.edit_text(msg_text, reply_markup=back_to_main_kb())
    await state.clear()

@dp.callback_query(F.data == "my_bookings")
async def cb_my_bookings(callback: CallbackQuery):
    with get_db() as conn:
        bks = conn.execute("SELECT b.*, s.name as srv_name FROM bookings b JOIN services s ON b.service_id=s.id WHERE b.client_id=? AND b.status='active' AND b.date >= ?", 
                           (callback.from_user.id, datetime.now().strftime("%d.%m.%Y"))).fetchall()
    
    if not bks:
        await callback.message.edit_text("У вас нет активных записей.", reply_markup=back_to_main_kb())
        await callback.answer()
        return
        
    await callback.message.edit_text("Ваши записи:")
    for b in bks:
        text = f"Услуга: {b['srv_name']}\nДата: {b['date']} в {b['start_time']}\n"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Перенести", callback_data=f"resch_{b['id']}"),
             InlineKeyboardButton(text="Отменить", callback_data=f"canc_{b['id']}")]
        ])
        await callback.message.answer(text, reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("resch_"))
async def cb_reschedule(callback: CallbackQuery, state: FSMContext):
    b_id = int(callback.data.split("_")[1])
    await state.update_data(reschedule_id=b_id)
    
    with get_db() as conn:
        b = conn.execute("SELECT service_id FROM bookings WHERE id=?", (b_id,)).fetchone()
    
    await state.update_data(srv_id=b['service_id'])
    now = datetime.now()
    await callback.message.edit_text("Выберите новую дату:", reply_markup=create_calendar(now.year, now.month))
    await state.set_state(ClientState.calendar)
    await callback.answer()

@dp.callback_query(F.data.startswith("canc_"))
async def cb_cancel(callback: CallbackQuery):
    b_id = int(callback.data.split("_")[1])
    with get_db() as conn:
        b = conn.execute("SELECT * FROM bookings WHERE id=?", (b_id,)).fetchone()
        conn.execute("UPDATE bookings SET status='cancelled' WHERE id=?", (b_id,))
        
    await callback.message.edit_text("Запись отменена.", reply_markup=back_to_main_kb())
    admin_text = f"КЛИЕНТ ОТМЕНИЛ\n{b['client_name']}\n{b['date']} {b['start_time']}"
    try: await bot.send_message(ADMIN_ID, admin_text)
    except: pass
    await callback.answer()

# --- ПАНЕЛЬ МАСТЕРА ---

@dp.callback_query(F.data == "admin_today")
async def admin_today(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    today = datetime.now().strftime("%d.%m.%Y")
    with get_db() as conn:
        bks = conn.execute("SELECT b.*, s.name as srv_name FROM bookings b JOIN services s ON b.service_id=s.id WHERE b.date=? AND b.status='active' ORDER BY b.start_time", (today,)).fetchall()
    
    if not bks:
        await callback.message.edit_text("На сегодня записей нет.", reply_markup=back_to_main_kb())
        return
        
    text = f"Записи на сегодня ({today}):\n\n"
    for b in bks:
        text += f"{b['start_time']} - {b['srv_name']}\n{b['client_name']} ({b['client_phone']})\n\n"
    
    await callback.message.edit_text(text, reply_markup=back_to_main_kb())
    await callback.answer()

@dp.callback_query(F.data == "admin_block")
async def admin_block_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    await callback.message.edit_text("Введите дату блокировки (ДД.ММ.ГГГГ):")
    await state.set_state(AdminState.block_date)
    await callback.answer()

@dp.message(AdminState.block_date)
async def admin_block_date(message: Message, state: FSMContext):
    await state.update_data(block_date=message.text)
    await message.answer("Введите время начала перерыва (ЧЧ:ММ):")
    await state.set_state(AdminState.block_start)

@dp.message(AdminState.block_start)
async def admin_block_start_time(message: Message, state: FSMContext):
    await state.update_data(block_start=message.text)
    await message.answer("Введите время конца перерыва (ЧЧ:ММ):")
    await state.set_state(AdminState.block_end)

@dp.message(AdminState.block_end)
async def admin_block_end_time(message: Message, state: FSMContext):
    data = await state.get_data()
    with get_db() as conn:
        conn.execute("INSERT INTO time_blocks (date, start_time, end_time, reason) VALUES (?, ?, ?, ?)",
                     (data['block_date'], data['block_start'], message.text, "Перерыв мастера"))
    await message.answer("Время заблокировано. Клиенты не смогут записаться на этот слот.", reply_markup=back_to_main_kb())
    await state.clear()

# --- ЗАПУСК ---
async def start_web_server():
    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text="OK"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "3000"))).start()

async def main():
    asyncio.create_task(start_web_server())
    init_db()
    logging.info("Бот запущен.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
