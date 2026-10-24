import os
import json
import asyncio
import logging
from datetime import datetime, timedelta
from dotenv import load_dotenv
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (Message, CallbackQuery, 
    ReplyKeyboardMarkup, KeyboardButton, 
    InlineKeyboardMarkup, InlineKeyboardButton,
    FSInputFile)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
BARBER_NAME = os.getenv("BARBER_NAME", "Мастер")
BARBER_ADDRESS = os.getenv("BARBER_ADDRESS", "ул. Пушкина, д. 1")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# --- ФАЙЛЫ ДЛЯ ХРАНЕНИЯ ---
BOOKINGS_FILE = "bookings.json"
SLOTS_FILE = "slots.json"

# --- СТРУКТУРЫ ДАННЫХ ---
SERVICES = {
    "hair": {"name": "Стрижка волос", "price": 1500, "duration": 45},
    "beard": {"name": "Стрижка бороды", "price": 800, "duration": 30},
    "combo": {"name": "Стрижка + Борода", "price": 2000, "duration": 75},
    "shave": {"name": "Бритье опасной бритвой", "price": 1200, "duration": 40},
}

BOOKINGS = {}
booking_counter = 0

def generate_slots_30_days():
    """Генерирует слоты на 30 дней вперед"""
    today = datetime.now()
    slots = {}
    base_times = ["10:00", "11:30", "13:00", "15:00", "16:30", "18:00"]
    for i in range(30):
        date = (today + timedelta(days=i)).strftime("%d.%m.%Y")
        slots[date] = base_times.copy()
    return slots

# Загружаем базу из файла или создаем новую
if os.path.exists(BOOKINGS_FILE):
    with open(BOOKINGS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
        BOOKINGS = {int(k): v for k, v in data.get("bookings", {}).items()}
        booking_counter = data.get("counter", 0)

if os.path.exists(SLOTS_FILE):
    with open(SLOTS_FILE, "r", encoding="utf-8") as f:
        SLOTS = json.load(f)
else:
    SLOTS = generate_slots_30_days()

def save_data():
    with open(BOOKINGS_FILE, "w", encoding="utf-8") as f:
        json.dump({"bookings": BOOKINGS, "counter": booking_counter}, f, ensure_ascii=False, indent=2)
    with open(SLOTS_FILE, "w", encoding="utf-8") as f:
        json.dump(SLOTS, f, ensure_ascii=False, indent=2)

# --- КЛАВИАТУРЫ ---
def main_menu():
    kb = [
        [KeyboardButton(text="Записаться"), KeyboardButton(text="О нас / FAQ")],
        [KeyboardButton(text="Мои записи"), KeyboardButton(text="Отменить запись")]
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def admin_menu():
    kb = [
        [KeyboardButton(text="Посмотреть все записи")],
        [KeyboardButton(text="Показать /backup /restore инструкции")]
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

# --- СОСТОЯНИЯ (FSM) ---
class BookingState(StatesGroup):
    choosing_service = State()
    choosing_date = State()
    choosing_time = State()
    entering_name = State()
    entering_phone = State()
    confirming = State()

# --- ХЭНДЛЕРЫ ---

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    if message.from_user.id == ADMIN_ID:
        await message.answer(f"Панель управления мастера {BARBER_NAME}.", reply_markup=admin_menu())
    else:
        await message.answer(
            f"Здравствуйте. Это бот записи к мастеру {BARBER_NAME}.\n\n"
            "Выберите действие на клавиатуре ниже.",
            reply_markup=main_menu()
        )

@dp.message(F.text == "О нас / FAQ")
async def faq_handler(message: Message):
    text = (
        f"Мастер: {BARBER_NAME}\n"
        f"Адрес: {BARBER_ADDRESS}\n"
        f"Часы работы: Ежедневно с 10:00 до 20:00\n\n"
        "Цены:\n"
        f"- Стрижка волос: 1500 руб.\n"
        f"- Стрижка бороды: 800 руб.\n"
        f"- Стрижка + Борода: 2000 руб.\n"
        f"- Бритье опасной бритвой: 1200 руб.\n\n"
        "Подготовка: Приходите за 5-10 минут до назначенного времени.\n\n"
        "Связаться с нами: напишите в этот бот."
    )
    await message.answer(text)

@dp.message(F.text == "Записаться")
async def start_booking(message: Message, state: FSMContext):
    if message.from_user.id == ADMIN_ID:
        await message.answer("Админ не может записываться.")
        return
    
    buttons = []
    for key, srv in SERVICES.items():
        buttons.append([InlineKeyboardButton(text=f"{srv['name']} ({srv['price']} руб.)", callback_data=f"srv_{key}")])
    
    await message.answer("Выберите услугу:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await state.set_state(BookingState.choosing_service)

@dp.callback_query(BookingState.choosing_service, F.data.startswith("srv_"))
async def process_service(callback: CallbackQuery, state: FSMContext):
    service_key = callback.data.split("_")[1]
    await state.update_data(service_key=service_key)
    
    # Показываем даты на 7 дней вперед для удобства (чтобы не листать 30 дней)
    buttons = []
    dates = list(SLOTS.keys())[:7] # Берем первые 7 дней
    for date in dates:
        buttons.append([InlineKeyboardButton(text=date, callback_data=f"date_{date}")])
    
    await callback.message.edit_text("Выберите дату (ближайшие 7 дней):", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await state.set_state(BookingState.choosing_date)
    await callback.answer()

@dp.callback_query(BookingState.choosing_date, F.data.startswith("date_"))
async def process_date(callback: CallbackQuery, state: FSMContext):
    date = callback.data.split("_")[1]
    await state.update_data(date=date)
    
    available_times = SLOTS.get(date, [])
    booked_times = [b["time"] for b in BOOKINGS.values() if b["date"] == date]
    available_times = [t for t in available_times if t not in booked_times]
    
    if not available_times:
        await callback.message.edit_text(f"На {date} нет свободных слотов.")
        await state.set_state(BookingState.choosing_date)
        await callback.answer()
        return

    buttons = []
    for time in available_times:
        buttons.append([InlineKeyboardButton(text=time, callback_data=f"time_{time}")])
    
    await callback.message.edit_text("Выберите время:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await state.set_state(BookingState.choosing_time)
    await callback.answer()

@dp.callback_query(BookingState.choosing_time, F.data.startswith("time_"))
async def process_time(callback: CallbackQuery, state: FSMContext):
    time = callback.data.split("_")[1]
    await state.update_data(time=time)
    
    await callback.message.edit_text("Введите ваше имя:")
    await state.set_state(BookingState.entering_name)
    await callback.answer()

@dp.message(BookingState.entering_name)
async def process_name(message: Message, state: FSMContext):
    if len(message.text) < 2 or len(message.text) > 50:
        await message.answer("Введите корректное имя (от 2 до 50 символов).")
        return
    await state.update_data(name=message.text)
    
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Отправить номер", request_contact=True)]], resize_keyboard=True)
    await message.answer("Введите номер телефона или нажмите кнопку ниже:", reply_markup=kb)
    await state.set_state(BookingState.entering_phone)

@dp.message(BookingState.entering_phone)
async def process_phone(message: Message, state: FSMContext):
    if message.contact:
        phone = message.contact.phone_number
    else:
        phone = message.text
        if not any(char.isdigit() for char in phone) or len(phone) < 5:
            await message.answer("Введите корректный номер телефона.")
            return

    await state.update_data(phone=phone)
    data = await state.get_data()
    
    srv = SERVICES[data['service_key']]
    text = (
        f"Проверьте данные:\n\n"
        f"Услуга: {srv['name']}\n"
        f"Дата: {data['date']}\n"
        f"Время: {data['time']}\n"
        f"Имя: {data['name']}\n"
        f"Телефон: {phone}\n"
        f"Стоимость: {srv['price']} руб.\n\n"
        f"Всё верно?"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Подтвердить", callback_data="confirm_yes")],
        [InlineKeyboardButton(text="Отмена", callback_data="confirm_no")]
    ])
    await message.answer(text, reply_markup=kb)
    await state.set_state(BookingState.confirming)

@dp.callback_query(BookingState.confirming)
async def process_confirm(callback: CallbackQuery, state: FSMContext):
    global booking_counter
    if callback.data == "confirm_yes":
        data = await state.get_data()
        booking_counter += 1
        booking_id = booking_counter
        srv = SERVICES[data['service_key']]
        
        BOOKINGS[booking_id] = {
            "user_id": callback.from_user.id,
            "name": data['name'],
            "phone": data['phone'],
            "service": srv['name'],
            "date": data['date'],
            "time": data['time']
        }
        save_data() # Сохраняем в файл
        
        await callback.message.edit_text(
            f"Вы успешно записаны.\n\n"
            f"Услуга: {srv['name']}\n"
            f"Дата: {data['date']} в {data['time']}\n"
            f"Адрес: {BARBER_ADDRESS}"
        )
        await callback.message.answer("Главное меню:", reply_markup=main_menu())
        
        admin_text = (
            f"НОВАЯ ЗАПИСЬ #{booking_id}\n\n"
            f"Клиент: {data['name']}\n"
            f"Телефон: {data['phone']}\n"
            f"Услуга: {srv['name']}\n"
            f"Время: {data['date']} в {data['time']}\n"
            f"TG ID: {callback.from_user.id}"
        )
        try:
            await bot.send_message(ADMIN_ID, admin_text)
        except Exception as e:
            logging.error(f"Ошибка отправки админу: {e}")
            
    else:
        await callback.message.edit_text("Запись отменена.")
        await callback.message.answer("Главное меню:", reply_markup=main_menu())
    
    await state.clear()
    await callback.answer()

@dp.message(F.text == "Мои записи")
async def my_bookings(message: Message):
    user_bookings = {k: v for k, v in BOOKINGS.items() if v["user_id"] == message.from_user.id}
    if not user_bookings:
        await message.answer("У вас нет активных записей.")
        return
    
    text = "Ваши записи:\n\n"
    for b_id, b in user_bookings.items():
        text += f"#{b_id} - {b['service']}\nДата: {b['date']} в {b['time']}\n\n"
    
    await message.answer(text)

@dp.message(F.text == "Отменить запись")
async def cancel_booking_menu(message: Message):
    user_bookings = {k: v for k, v in BOOKINGS.items() if v["user_id"] == message.from_user.id}
    if not user_bookings:
        await message.answer("У вас нет активных записей.")
        return
    
    buttons = []
    for b_id, b in user_bookings.items():
        buttons.append([InlineKeyboardButton(text=f"Отменить #{b_id} ({b['date']} {b['time']})", callback_data=f"cancel_{b_id}")])
    
    await message.answer("Выберите запись для отмены:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@dp.callback_query(F.data.startswith("cancel_"))
async def process_cancel(callback: CallbackQuery):
    booking_id = int(callback.data.split("_")[1])
    if booking_id in BOOKINGS:
        b = BOOKINGS.pop(booking_id)
        save_data() # Сохраняем изменения
        await callback.message.edit_text("Запись отменена.")
        
        admin_text = (
            f"КЛИЕНТ ОТМЕНИЛ ЗАПИСЬ #{booking_id}\n\n"
            f"Клиент: {b['name']}\n"
            f"Телефон: {b['phone']}\n"
            f"Услуга: {b['service']}\n"
            f"Время: {b['date']} в {b['time']}"
        )
        try:
            await bot.send_message(ADMIN_ID, admin_text)
        except Exception as e:
            logging.error(f"Ошибка отправки админу: {e}")
    else:
        await callback.message.edit_text("Запись не найдена.")

# --- АДМИН ПАНЕЛЬ ---
@dp.message(F.text == "Посмотреть все записи")
async def admin_view_bookings(message: Message):
    if message.from_user.id != ADMIN_ID: return
    
    if not BOOKINGS:
        await message.answer("Записей пока нет.")
        return
    
    text = "Все активные записи:\n\n"
    for b_id, b in BOOKINGS.items():
        text += (
            f"#{b_id} - {b['date']} в {b['time']}\n"
            f"Услуга: {b['service']}\n"
            f"Клиент: {b['name']}, тел: {b['phone']}\n"
            f"TG ID: {b['user_id']}\n\n"
        )
    await message.answer(text)

@dp.message(F.text == "Показать /backup /restore инструкции")
async def admin_backup_instructions(message: Message):
    if message.from_user.id != ADMIN_ID: return
    await message.answer(
        "Сохранение данных:\n\n"
        "Команда /backup пришлет вам файл с базой записей и слотами (bookings.json, slots.json) в архиве.\n\n"
        "Команда /restore используется для восстановления. Просто отправьте боту архив с базой (с подписью /restore), и он заменит текущие данные."
    )

# --- КОМАНДЫ БЭКАПА ---
@dp.message(Command("backup"))
async def cmd_backup(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        import zipfile
        zip_path = "backup_data.zip"
        with zipfile.ZipFile(zip_path, "w") as z:
            if os.path.exists(BOOKINGS_FILE):
                z.write(BOOKINGS_FILE)
            if os.path.exists(SLOTS_FILE):
                z.write(SLOTS_FILE)
        
        doc = FSInputFile(zip_path, filename="barber_backup.zip")
        await message.answer_document(doc, caption="Резервная копия базы данных.")
        os.remove(zip_path)
    except Exception as e:
        await message.answer(f"Ошибка бэкапа: {e}")

@dp.message(Command("restore"))
async def cmd_restore(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("Отправьте архив с базой (zip), в подписи к файлу укажите /restore.")

@dp.message(F.document)
async def process_restore_doc(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    if not message.caption or "/restore" not in message.caption:
        return
        
    try:
        import zipfile
        file = await bot.get_file(message.document.file_id)
        tmp_path = "tmp_restore.zip"
        await bot.download_file(file.file_path, tmp_path)
        
        with zipfile.ZipFile(tmp_path, "r") as z:
            z.extractall()
        
        global BOOKINGS, booking_counter, SLOTS
        if os.path.exists(BOOKINGS_FILE):
            with open(BOOKINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                BOOKINGS = {int(k): v for k, v in data.get("bookings", {}).items()}
                booking_counter = data.get("counter", 0)
        
        if os.path.exists(SLOTS_FILE):
            with open(SLOTS_FILE, "r", encoding="utf-8") as f:
                SLOTS = json.load(f)
                
        os.remove(tmp_path)
        await message.answer("База данных успешно восстановлена.")
    except Exception as e:
        await message.answer(f"Ошибка восстановления: {e}")

# --- ЗАПУСК ---
async def start_web_server():
    """Мини-сервер для BotHost, чтобы он видел, что бот работает"""
    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text="OK"))
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "3000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"Web server started on port {port}")

async def main():
    asyncio.create_task(start_web_server())
    logging.info("Бот запускается...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
