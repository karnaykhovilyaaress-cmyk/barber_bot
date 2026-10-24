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
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "username") # Юзернейм без @
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
SLOTS = {}

def load_data():
    global BOOKINGS, booking_counter, SLOTS
    if os.path.exists(BOOKINGS_FILE):
        with open(BOOKINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            BOOKINGS = {int(k): v for k, v in data.get("bookings", {}).items()}
            booking_counter = data.get("counter", 0)
    
    if os.path.exists(SLOTS_FILE):
        with open(SLOTS_FILE, "r", encoding="utf-8") as f:
            SLOTS = json.load(f)

def save_data():
    with open(BOOKINGS_FILE, "w", encoding="utf-8") as f:
        json.dump({"bookings": BOOKINGS, "counter": booking_counter}, f, ensure_ascii=False, indent=2)
    with open(SLOTS_FILE, "w", encoding="utf-8") as f:
        json.dump(SLOTS, f, ensure_ascii=False, indent=2)

# Загружаем при старте
load_data()

# --- КЛАВИАТУРЫ ---
def main_menu():
    kb = [
        [KeyboardButton(text="Записаться"), KeyboardButton(text="О нас / FAQ")],
        [KeyboardButton(text="Мои записи"), KeyboardButton(text="Отменить запись")]
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def admin_menu():
    kb = [
        [KeyboardButton(text="Все записи"), KeyboardButton(text="Управление слотами")],
        [KeyboardButton(text="Инструкции /backup /restore")]
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def admin_slots_menu():
    kb = [
        [KeyboardButton(text="Добавить дату"), KeyboardButton(text="Добавить время")],
        [KeyboardButton(text="Удалить дату"), KeyboardButton(text="Показать расписание")],
        [KeyboardButton(text="Назад в админку")]
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

class AdminState(StatesGroup):
    add_date = State()
    add_time_date = State()
    add_time_value = State()
    delete_date = State()

# --- ХЭНДЛЕРЫ КЛИЕНТА ---

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
        f"Связаться с мастером: @{ADMIN_USERNAME}"
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
    
    available_dates = [d for d, times in SLOTS.items() if times]
    if not available_dates:
        await callback.message.answer("Пока нет доступных дат для записи. Свяжитесь с мастером.")
        await state.clear()
        await callback.answer()
        return

    buttons = []
    for date in available_dates[:10]: # Показываем до 10 доступных дат
        buttons.append([InlineKeyboardButton(text=date, callback_data=f"date_{date}")])
    
    await callback.message.answer("Выберите дату:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
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
        await callback.message.answer(f"На {date} нет свободных слотов. Выберите другую дату.")
        await state.set_state(BookingState.choosing_date)
        await callback.answer()
        return

    buttons = []
    for time in available_times:
        buttons.append([InlineKeyboardButton(text=time, callback_data=f"time_{time}")])
    
    await callback.message.answer("Выберите время:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await state.set_state(BookingState.choosing_time)
    await callback.answer()

@dp.callback_query(BookingState.choosing_time, F.data.startswith("time_"))
async def process_time(callback: CallbackQuery, state: FSMContext):
    time = callback.data.split("_")[1]
    await state.update_data(time=time)
    
    await callback.message.answer("Введите ваше имя:")
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
        
        username = f"@{callback.from_user.username}" if callback.from_user.username else f"ID: {callback.from_user.id}"
        
        BOOKINGS[booking_id] = {
            "user_id": callback.from_user.id,
            "username": username,
            "name": data['name'],
            "phone": data['phone'],
            "service": srv['name'],
            "date": data['date'],
            "time": data['time'],
            "reminded": False # Флаг для уведомления
        }
        save_data()
        
        await callback.message.answer(
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
            f"Telegram: {username}\n"
            f"Услуга: {srv['name']}\n"
            f"Время: {data['date']} в {data['time']}"
        )
        try:
            await bot.send_message(ADMIN_ID, admin_text)
        except Exception as e:
            logging.error(f"Ошибка отправки админу: {e}")
            
    else:
        await callback.message.answer("Запись отменена.")
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
        save_data()
        await callback.message.answer("Запись отменена.")
        
        admin_text = (
            f"КЛИЕНТ ОТМЕНИЛ ЗАПИСЬ #{booking_id}\n\n"
            f"Клиент: {b['name']}\n"
            f"Телефон: {b['phone']}\n"
            f"Telegram: {b.get('username', '')}\n"
            f"Услуга: {b['service']}\n"
            f"Время: {b['date']} в {b['time']}"
        )
        try:
            await bot.send_message(ADMIN_ID, admin_text)
        except Exception as e:
            logging.error(f"Ошибка отправки админу: {e}")
    else:
        await callback.message.answer("Запись не найдена.")

# --- АДМИН ПАНЕЛЬ ---

@dp.message(F.text == "Все записи")
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
            f"Telegram: {b.get('username', 'скрыт')}\n\n"
        )
    await message.answer(text)

@dp.message(F.text == "Управление слотами")
async def admin_slots(message: Message):
    if message.from_user.id != ADMIN_ID: return
    await message.answer("Управление расписанием:", reply_markup=admin_slots_menu())

@dp.message(F.text == "Назад в админку")
async def admin_back(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Возврат в админку.", reply_markup=admin_menu())

@dp.message(F.text == "Показать расписание")
async def admin_show_slots(message: Message):
    if message.from_user.id != ADMIN_ID: return
    if not SLOTS:
        await message.answer("Расписание пусто.")
        return
    
    text = "Текущее расписание:\n\n"
    for date, times in list(SLOTS.items())[:15]:
        booked = [b["time"] for b in BOOKINGS.values() if b["date"] == date]
        free_times = [t for t in times if t not in booked]
        text += f"{date}: {', '.join(free_times) if free_times else 'все занято'}\n"
    
    await message.answer(text)

@dp.message(F.text == "Добавить дату")
async def admin_add_date_start(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    await message.answer("Введите дату в формате ДД.ММ.ГГГГ (например, 25.10.2026):")
    await state.set_state(AdminState.add_date)

@dp.message(AdminState.add_date)
async def admin_add_date_process(message: Message, state: FSMContext):
    date_str = message.text.strip()
    try:
        datetime.strptime(date_str, "%d.%m.%Y")
    except ValueError:
        await message.answer("Неверный формат. Введите ДД.ММ.ГГГГ:")
        return
        
    if date_str not in SLOTS:
        SLOTS[date_str] = []
        save_data()
    
    await message.answer(f"Дата {date_str} добавлена. Теперь добавьте время для этой даты.", reply_markup=admin_slots_menu())
    await state.update_data(target_date=date_str)
    await state.set_state(AdminState.add_time_value)

@dp.message(F.text == "Добавить время")
async def admin_add_time_start(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    await message.answer("Введите дату, для которой добавляете время (ДД.ММ.ГГГГ):")
    await state.set_state(AdminState.add_time_date)

@dp.message(AdminState.add_time_date)
async def admin_add_time_date(message: Message, state: FSMContext):
    date_str = message.text.strip()
    try:
        datetime.strptime(date_str, "%d.%m.%Y")
    except ValueError:
        await message.answer("Неверный формат. Введите ДД.ММ.ГГГГ:")
        return
    
    if date_str not in SLOTS:
        SLOTS[date_str] = []
        
    await message.answer("Введите время в формате ЧЧ:ММ (например, 14:30):")
    await state.update_data(target_date=date_str)
    await state.set_state(AdminState.add_time_value)

@dp.message(AdminState.add_time_value)
async def admin_add_time_value(message: Message, state: FSMContext):
    time_str = message.text.strip()
    try:
        datetime.strptime(time_str, "%H:%M")
    except ValueError:
        await message.answer("Неверный формат. Введите ЧЧ:ММ:")
        return
        
    data = await state.get_data()
    date_str = data.get("target_date")
    
    if date_str in SLOTS:
        if time_str not in SLOTS[date_str]:
            SLOTS[date_str].append(time_str)
            SLOTS[date_str].sort()
            save_data()
            await message.answer(f"Время {time_str} добавлено на {date_str}.", reply_markup=admin_slots_menu())
        else:
            await message.answer(f"Время {time_str} уже есть на {date_str}.", reply_markup=admin_slots_menu())
    
    await state.clear()

@dp.message(F.text == "Удалить дату")
async def admin_del_date_start(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    await message.answer("Введите дату для удаления (ДД.ММ.ГГГГ):")
    await state.set_state(AdminState.delete_date)

@dp.message(AdminState.delete_date)
async def admin_del_date_process(message: Message, state: FSMContext):
    date_str = message.text.strip()
    if date_str in SLOTS:
        del SLOTS[date_str]
        # Удаляем записи на эту дату
        for b_id in list(BOOKINGS.keys()):
            if BOOKINGS[b_id]["date"] == date_str:
                del BOOKINGS[b_id]
        save_data()
        await message.answer(f"Дата {date_str} и все записи на нее удалены.", reply_markup=admin_slots_menu())
    else:
        await message.answer(f"Дата {date_str} не найдена.", reply_markup=admin_slots_menu())
    
    await state.clear()

@dp.message(F.text == "Инструкции /backup /restore")
async def admin_backup_instructions(message: Message):
    if message.from_user.id != ADMIN_ID: return
    await message.answer(
        "Сохранение данных:\n\n"
        "Команда /backup пришлет вам архив с файлами базы.\n\n"
        "Команда /restore используется для восстановления. Просто отправьте боту архив с подписью /restore, и он заменит текущие данные."
    )

# --- КОМАНДЫ БЭКАПА ---
@dp.message(Command("backup"))
async def cmd_backup(message: Message):
    if message.from_user.id != ADMIN_ID: return
    try:
        import zipfile
        zip_path = "backup_data.zip"
        with zipfile.ZipFile(zip_path, "w") as z:
            if os.path.exists(BOOKINGS_FILE): z.write(BOOKINGS_FILE)
            if os.path.exists(SLOTS_FILE): z.write(SLOTS_FILE)
        
        doc = FSInputFile(zip_path, filename="barber_backup.zip")
        await message.answer_document(doc, caption="Резервная копия.")
        os.remove(zip_path)
    except Exception as e:
        await message.answer(f"Ошибка бэкапа: {e}")

@dp.message(Command("restore"))
async def cmd_restore(message: Message):
    if message.from_user.id != ADMIN_ID: return
    await message.answer("Отправьте архив (zip) с базой, указав в подписи /restore.")

@dp.message(F.document)
async def process_restore_doc(message: Message):
    if message.from_user.id != ADMIN_ID: return
    if not message.caption or "/restore" not in message.caption: return
        
    try:
        import zipfile
        file = await bot.get_file(message.document.file_id)
        tmp_path = "tmp_restore.zip"
        await bot.download_file(file.file_path, tmp_path)
        
        with zipfile.ZipFile(tmp_path, "r") as z:
            z.extractall()
        
        load_data() # Перезагружаем данные из восстановленных файлов
        os.remove(tmp_path)
        await message.answer("База данных успешно восстановлена.")
    except Exception as e:
        await message.answer(f"Ошибка восстановления: {e}")

# --- УВЕДОМЛЕНИЯ ЗА ДЕНЬ ---
async def reminder_worker():
    """Фоновая задача: проверяет записи и шлет напоминания за 24 часа."""
    while True:
        try:
            now = datetime.now()
            tomorrow = (now + timedelta(days=1)).strftime("%d.%m.%Y")
            
            for b_id, b in list(BOOKINGS.items()):
                if b["date"] == tomorrow and not b.get("reminded", False):
                    try:
                        text = (
                            f"Напоминание о записи.\n\n"
                            f"Завтра в {b['time']} у вас запись на услугу: {b['service']}.\n"
                            f"Мастер: {BARBER_NAME}\n"
                            f"Адрес: {BARBER_ADDRESS}"
                        )
                        await bot.send_message(b["user_id"], text)
                        BOOKINGS[b_id]["reminded"] = True
                        save_data()
                    except Exception as e:
                        logging.warning(f"Не удалось отправить напоминание {b['user_id']}: {e}")
        except Exception as e:
            logging.error(f"Ошибка в reminder_worker: {e}")
            
        await asyncio.sleep(1800) # Проверка каждые 30 минут

# --- ЗАПУСК ---
async def start_web_server():
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
    asyncio.create_task(reminder_worker()) # Запуск напоминаний
    logging.info("Бот запускается...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
