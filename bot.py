import asyncio
import sqlite3
import json
import os
import pytz
from datetime import datetime, timedelta
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError
import time as time_module

# ============= КОНФИГУРАЦИЯ =============
BOT_TOKEN = '8994997605:AAF1IDs_-iuCA4H9mahjtrWA6zgMuDc96es'  # Замени на свой токен
ADMIN_ID = 8505402888  # Замени на свой Telegram ID

# Создаем папку для данных
if not os.path.exists('data'):
    os.makedirs('data')

# ============= РАБОТА С БАЗОЙ ДАННЫХ =============
def init_db():
    """Инициализация базы данных"""
    conn = sqlite3.connect('data/bot_data.db')
    c = conn.cursor()
    
    # Таблица для хранения сессий пользователей
    c.execute('''CREATE TABLE IF NOT EXISTS user_sessions
                 (user_id INTEGER PRIMARY KEY,
                  api_id TEXT,
                  api_hash TEXT,
                  account_token TEXT,
                  is_active INTEGER DEFAULT 0)''')
    
    # Таблица белого списка
    c.execute('''CREATE TABLE IF NOT EXISTS whitelist
                 (user_id INTEGER PRIMARY KEY,
                  added_by INTEGER,
                  added_date TEXT)''')
    
    # Таблица рассылок
    c.execute('''CREATE TABLE IF NOT EXISTS mailing_tasks
                 (task_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER,
                  chats TEXT,
                  message TEXT,
                  interval_seconds INTEGER,
                  time_range TEXT,
                  status TEXT,
                  created_date TEXT)''')
    
    # Добавляем админа в белый список
    c.execute("INSERT OR IGNORE INTO whitelist (user_id, added_by, added_date) VALUES (?, ?, ?)",
              (ADMIN_ID, ADMIN_ID, datetime.now().isoformat()))
    
    conn.commit()
    conn.close()

def is_whitelisted(user_id):
    """Проверка, есть ли пользователь в белом списке"""
    conn = sqlite3.connect('data/bot_data.db')
    c = conn.cursor()
    c.execute("SELECT 1 FROM whitelist WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    conn.close()
    return result is not None

def add_to_whitelist(admin_id, user_id):
    """Добавление пользователя в белый список (только админ)"""
    if admin_id != ADMIN_ID:
        return False, "Только главный админ может добавлять пользователей!"
    
    conn = sqlite3.connect('data/bot_data.db')
    c = conn.cursor()
    try:
        c.execute("INSERT INTO whitelist (user_id, added_by, added_date) VALUES (?, ?, ?)",
                  (user_id, admin_id, datetime.now().isoformat()))
        conn.commit()
        return True, f"✅ Пользователь {user_id} добавлен в белый список!"
    except sqlite3.IntegrityError:
        return False, f"❌ Пользователь {user_id} уже в белом списке!"
    finally:
        conn.close()

def remove_from_whitelist(admin_id, user_id):
    """Удаление пользователя из белого списка"""
    if admin_id != ADMIN_ID:
        return False, "Только главный админ может удалять пользователей!"
    
    if user_id == ADMIN_ID:
        return False, "❌ Нельзя удалить главного админа!"
    
    conn = sqlite3.connect('data/bot_data.db')
    c = conn.cursor()
    c.execute("DELETE FROM whitelist WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()
    return True, f"✅ Пользователь {user_id} удален из белого списка!"

def get_whitelist():
    """Получение списка всех пользователей"""
    conn = sqlite3.connect('data/bot_data.db')
    c = conn.cursor()
    c.execute("SELECT user_id, added_by, added_date FROM whitelist")
    results = c.fetchall()
    conn.close()
    return results

def save_user_session(user_id, api_id, api_hash, account_token):
    """Сохранение данных сессии пользователя"""
    conn = sqlite3.connect('data/bot_data.db')
    c = conn.cursor()
    c.execute("""INSERT OR REPLACE INTO user_sessions 
                 (user_id, api_id, api_hash, account_token, is_active) 
                 VALUES (?, ?, ?, ?, ?)""",
              (user_id, api_id, api_hash, account_token, 1))
    conn.commit()
    conn.close()

def get_user_session(user_id):
    """Получение данных сессии пользователя"""
    conn = sqlite3.connect('data/bot_data.db')
    c = conn.cursor()
    c.execute("SELECT api_id, api_hash, account_token FROM user_sessions WHERE user_id = ? AND is_active = 1", 
              (user_id,))
    result = c.fetchone()
    conn.close()
    return result

def delete_user_session(user_id):
    """Удаление сессии пользователя"""
    conn = sqlite3.connect('data/bot_data.db')
    c = conn.cursor()
    c.execute("UPDATE user_sessions SET is_active = 0 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def save_mailing_task(user_id, chats, message, interval_seconds, time_range):
    """Сохранение задачи рассылки"""
    conn = sqlite3.connect('data/bot_data.db')
    c = conn.cursor()
    c.execute("""INSERT INTO mailing_tasks 
                 (user_id, chats, message, interval_seconds, time_range, status, created_date) 
                 VALUES (?, ?, ?, ?, ?, ?, ?)""",
              (user_id, json.dumps(chats), message, interval_seconds, 
               json.dumps(time_range), 'active', datetime.now().isoformat()))
    task_id = c.lastrowid
    conn.commit()
    conn.close()
    return task_id

# ============= ХРАНИЛИЩЕ ДАННЫХ ПОЛЬЗОВАТЕЛЕЙ В ПАМЯТИ =============
user_states = {}  # {user_id: {'step': '...', 'temp_data': {...}}}
user_clients = {}  # {user_id: TelegramClient}

# ============= ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =============
def parse_interval(text):
    """Парсит интервал из текста"""
    text = text.lower().strip()
    
    if 'm' in text:
        return int(text.replace('m', '')) * 60
    elif 'h' in text:
        return int(text.replace('h', '')) * 3600
    elif 's' in text:
        return int(text.replace('s', ''))
    else:
        try:
            return int(text)
        except:
            return 3

def parse_time_range(text):
    """Парсит временной диапазон"""
    text = text.strip()
    
    if '-' not in text:
        try:
            single_time = datetime.strptime(text, '%H:%M').time()
            return ('single', single_time)
        except:
            return None
    
    parts = text.split('-')
    if len(parts) != 2:
        return None
    
    try:
        start = datetime.strptime(parts[0].strip(), '%H:%M').time()
        end = datetime.strptime(parts[1].strip(), '%H:%M').time()
        return ('range', start, end)
    except:
        return None

def format_time_range(time_range):
    """Форматирует время для вывода"""
    if time_range[0] == 'single':
        return f"Однократная отправка в {time_range[1].strftime('%H:%M')}"
    else:
        _, start, end = time_range
        return f"{start.strftime('%H:%M')} - {end.strftime('%H:%M')}"

def is_time_active(time_range, current_time):
    """Проверяет, активен ли бот в текущее время"""
    if time_range[0] == 'single':
        target = time_range[1]
        return current_time.hour == target.hour and current_time.minute == target.minute
    else:
        _, start, end = time_range
        return start <= current_time <= end

# ============= ФУНКЦИИ РАССЫЛКИ =============
async def send_messages(user_id, task_id, client, chats, message_text, interval, time_range):
    """Основная функция рассылки"""
    MSK = pytz.timezone('Europe/Moscow')
    
    # Для однократной отправки
    if time_range[0] == 'single':
        target_time = time_range[1]
        current_msk = datetime.now(MSK)
        target_datetime = datetime.combine(current_msk.date(), target_time)
        target_datetime = MSK.localize(target_datetime)
        
        if target_datetime < current_msk:
            target_datetime += timedelta(days=1)
        
        wait_seconds = (target_datetime - current_msk).total_seconds()
        await client.send_message(user_id, f"⏰ Жду до {target_time.strftime('%H:%M')} МСК...")
        
        if wait_seconds > 0:
            await asyncio.sleep(wait_seconds)
        
        for chat in chats:
            try:
                await client.send_message(chat['id'], message_text)
                await client.send_message(user_id, f"✅ Отправлено в {chat['title']}")
                await asyncio.sleep(interval)
            except Exception as e:
                await client.send_message(user_id, f"❌ Ошибка: {str(e)}")
        
        await client.send_message(user_id, "✅ Рассылка завершена!")
        return
    
    # Бесконечная рассылка
    while True:
        current_msk = datetime.now(MSK)
        current_time = current_msk.time()
        
        if is_time_active(time_range, current_time):
            for chat in chats:
                try:
                    await client.send_message(chat['id'], message_text)
                    print(f"[{current_msk}] Отправлено в {chat['title']}")
                    await asyncio.sleep(interval)
                except Exception as e:
                    print(f"Ошибка: {e}")
                    await asyncio.sleep(5)
        
        await asyncio.sleep(30)

# ============= СОЗДАНИЕ БОТА =============
bot = TelegramClient('bot_session', API_ID=None, API_HASH=None).start(bot_token=BOT_TOKEN)

# ============= ОБРАБОТЧИКИ КОМАНД =============
@bot.on(events.NewMessage(pattern='/start'))
async def start_cmd(event):
    user_id = event.sender_id
    
    # Проверка белого списка
    if not is_whitelisted(user_id):
        await event.respond("❌ Доступ запрещен! Вы не в белом списке.")
        return
    
    # Проверяем, есть ли у пользователя сессия
    session = get_user_session(user_id)
    
    if session:
        # У пользователя уже есть данные
        api_id, api_hash, account_token = session
        await event.respond(
            f"👋 С возвращением!\n\n"
            f"📊 Доступные команды:\n"
            f"/new_mailing - Новая рассылка\n"
            f"/my_mailings - Мои рассылки\n"
            f"/stop_mailing - Остановить рассылку\n"
            f"/reset_session - Сбросить данные аккаунта\n"
            f"/help - Помощь"
        )
        
        if user_id == ADMIN_ID:
            await event.respond(
                f"👑 *Админ-панель*\n"
                f"/whitelist - Список пользователей\n"
                f"/add_user <id> - Добавить пользователя\n"
                f"/remove_user <id> - Удалить пользователя",
                parse_mode='markdown'
            )
    else:
        # Новый пользователь - запрашиваем API ID
        user_states[user_id] = {'step': 'waiting_api_id'}
        await event.respond(
            "🔐 *Добро пожаловать!*\n\n"
            "Для работы с рассылками нужно настроить аккаунт.\n\n"
            "📌 *Шаг 1:* Введите ваш `api_id`\n"
            "Где взять: https://my.telegram.org → API development tools",
            parse_mode='markdown'
        )

@bot.on(events.NewMessage(pattern='/reset_session'))
async def reset_session_cmd(event):
    user_id = event.sender_id
    
    if not is_whitelisted(user_id):
        await event.respond("❌ Доступ запрещен!")
        return
    
    delete_user_session(user_id)
    if user_id in user_clients:
        await user_clients[user_id].disconnect()
        del user_clients[user_id]
    
    user_states[user_id] = {'step': 'waiting_api_id'}
    await event.respond(
        "🔄 Сессия сброшена!\n"
        "Давай настроим аккаунт заново.\n\n"
        "Введите ваш `api_id`:",
        parse_mode='markdown'
    )

@bot.on(events.NewMessage(pattern='/new_mailing'))
async def new_mailing_cmd(event):
    user_id = event.sender_id
    
    if not is_whitelisted(user_id):
        await event.respond("❌ Доступ запрещен!")
        return
    
    session = get_user_session(user_id)
    if not session:
        await event.respond("❌ Сначала настрой аккаунт через /start")
        return
    
    user_states[user_id] = {'step': 'waiting_chats', 'temp_data': {}}
    await event.respond(
        "📝 *Новая рассылка*\n\n"
        "Отправь ссылки на чаты (каждая с новой строки)\n"
        "Пример:\n"
        "https://t.me/chat1\n"
        "@chat2\n\n"
        "Когда закончишь, отправь слово *ГОТОВО*",
        parse_mode='markdown'
    )

@bot.on(events.NewMessage(pattern='/my_mailings'))
async def my_mailings_cmd(event):
    user_id = event.sender_id
    
    if not is_whitelisted(user_id):
        await event.respond("❌ Доступ запрещен!")
        return
    
    conn = sqlite3.connect('data/bot_data.db')
    c = conn.cursor()
    c.execute("SELECT task_id, created_date, status FROM mailing_tasks WHERE user_id = ? ORDER BY created_date DESC", 
              (user_id,))
    tasks = c.fetchall()
    conn.close()
    
    if not tasks:
        await event.respond("📭 У вас нет созданных рассылок")
        return
    
    response = "📊 *Ваши рассылки:*\n\n"
    for task in tasks:
        response += f"ID: {task[0]}\n📅 {task[1]}\nСтатус: {task[2]}\n\n"
    
    await event.respond(response, parse_mode='markdown')

@bot.on(events.NewMessage(pattern='/stop_mailing'))
async def stop_mailing_cmd(event):
    user_id = event.sender_id
    
    if not is_whitelisted(user_id):
        await event.respond("❌ Доступ запрещен!")
        return
    
    # Здесь можно реализовать остановку конкретной рассылки
    await event.respond("🛑 Команда в разработке. Пока что просто перезапусти бота.")

# ============= АДМИН-КОМАНДЫ =============
@bot.on(events.NewMessage(pattern='/whitelist'))
async def whitelist_cmd(event):
    user_id = event.sender_id
    
    if user_id != ADMIN_ID:
        await event.respond("❌ Только для админа!")
        return
    
    users = get_whitelist()
    if not users:
        await event.respond("📭 Белый список пуст")
        return
    
    response = "👥 *Белый список:*\n\n"
    for uid, added_by, added_date in users:
        role = "👑 Админ" if uid == ADMIN_ID else "👤 Пользователь"
        response += f"ID: `{uid}` - {role}\n📅 {added_date}\n\n"
    
    await event.respond(response, parse_mode='markdown')

@bot.on(events.NewMessage(pattern='/add_user (\\d+)'))
async def add_user_cmd(event):
    user_id = event.sender_id
    new_user_id = int(event.pattern_match.group(1))
    
    success, message = add_to_whitelist(user_id, new_user_id)
    await event.respond(message)

@bot.on(events.NewMessage(pattern='/remove_user (\\d+)'))
async def remove_user_cmd(event):
    user_id = event.sender_id
    remove_user_id = int(event.pattern_match.group(1))
    
    success, message = remove_from_whitelist(user_id, remove_user_id)
    await event.respond(message)

# ============= ОСНОВНОЙ ОБРАБОТЧИК СООБЩЕНИЙ =============
@bot.on(events.NewMessage)
async def handle_messages(event):
    user_id = event.sender_id
    text = event.raw_text
    
    # Пропускаем команды
    if text.startswith('/'):
        return
    
    # Проверка белого списка
    if not is_whitelisted(user_id):
        return
    
    # Проверяем, есть ли пользователь в процессе настройки
    if user_id not in user_states:
        return
    
    state = user_states[user_id]
    step = state.get('step')
    
    # Шаг 1: Получение API ID
    if step == 'waiting_api_id':
        try:
            api_id = int(text)
            state['temp_data'] = {'api_id': api_id}
            state['step'] = 'waiting_api_hash'
            await event.respond("✅ API ID принят!\n\n📌 *Шаг 2:* Введите ваш `api_hash`", parse_mode='markdown')
        except ValueError:
            await event.respond("❌ API ID должен быть числом! Попробуй еще раз:")
    
    # Шаг 2: Получение API HASH
    elif step == 'waiting_api_hash':
        api_hash = text
        state['temp_data']['api_hash'] = api_hash
        state['step'] = 'waiting_account_token'
        await event.respond(
            "✅ API HASH принят!\n\n"
            "📌 *Шаг 3:* Введите *токен аккаунта* (string session)\n"
            "Если нет токена, отправь 'нет' - тогда нужно будет ввести номер телефона",
            parse_mode='markdown'
        )
    
    # Шаг 3: Получение токена аккаунта или номера телефона
    elif step == 'waiting_account_token':
        api_id = state['temp_data']['api_id']
        api_hash = state['temp_data']['api_hash']
        
        if text.lower() == 'нет':
            # Будем использовать номер телефона
            state['step'] = 'waiting_phone'
            await event.respond("📱 Введите номер телефона в международном формате (например: +79123456789)")
        else:
            # Сохраняем токен
            account_token = text
            save_user_session(user_id, api_id, api_hash, account_token)
            
            # Создаем клиента
            client = TelegramClient(f'data/user_{user_id}', int(api_id), api_hash)
            await client.start(account_token)
            user_clients[user_id] = client
            
            del user_states[user_id]
            await event.respond("✅ Аккаунт успешно настроен! Теперь ты можешь использовать /new_mailing для создания рассылки")
    
    # Шаг 3.1: Получение номера телефона
    elif step == 'waiting_phone':
        phone = text
        state['temp_data']['phone'] = phone
        state['step'] = 'waiting_code'
        
        api_id = state['temp_data']['api_id']
        api_hash = state['temp_data']['api_hash']
        
        # Создаем клиента и запрашиваем код
        client = TelegramClient(f'data/user_{user_id}', int(api_id), api_hash)
        user_clients[user_id] = client
        
        try:
            await client.send_code_request(phone)
            await event.respond("📨 Код подтверждения отправлен в Telegram! Введите его:")
        except Exception as e:
            await event.respond(f"❌ Ошибка: {str(e)}\nНачни заново с /reset_session")
            del user_states[user_id]
    
    # Шаг 3.2: Получение кода
    elif step == 'waiting_code':
        code = text
        phone = state['temp_data']['phone']
        api_id = state['temp_data']['api_id']
        api_hash = state['temp_data']['api_hash']
        
        client = user_clients[user_id]
        
        try:
            await client.sign_in(phone, code)
            # Сохраняем сессию
            session_string = client.session.save()
            save_user_session(user_id, api_id, api_hash, session_string)
            
            del user_states[user_id]
            await event.respond("✅ Аккаунт успешно настроен! Теперь ты можешь использовать /new_mailing для создания рассылки")
        except SessionPasswordNeededError:
            state['step'] = 'waiting_password'
            await event.respond("🔐 Включена двухфакторная аутентификация. Введите пароль:")
        except Exception as e:
            await event.respond(f"❌ Ошибка: {str(e)}\nНачни заново с /reset_session")
            del user_states[user_id]
    
    # Шаг 3.3: Получение пароля 2FA
    elif step == 'waiting_password':
        password = text
        client = user_clients[user_id]
        
        try:
            await client.sign_in(password=password)
            session_string = client.session.save()
            save_user_session(user_id, state['temp_data']['api_id'], 
                            state['temp_data']['api_hash'], session_string)
            
            del user_states[user_id]
            await event.respond("✅ Аккаунт успешно настроен! Теперь ты можешь использовать /new_mailing для создания рассылки")
        except Exception as e:
            await event.respond(f"❌ Ошибка: {str(e)}\nНачни заново с /reset_session")
            del user_states[user_id]
    
    # Шаг 4: Сбор ссылок на чаты
    elif step == 'waiting_chats':
        if text.upper() == 'ГОТОВО':
            chats = state['temp_data'].get('chats', [])
            if not chats:
                await event.respond("❌ Ты не отправил ни одной ссылки! Отправь /new_mailing чтобы начать заново")
                return
            
            state['step'] = 'waiting_message'
            await event.respond(
                f"✅ Получено ссылок: {len(chats)}\n\n"
                "📝 *Шаг 5:* Отправь текст сообщения для рассылки",
                parse_mode='markdown'
            )
        else:
            if 'chats' not in state['temp_data']:
                state['temp_data']['chats'] = []
            state['temp_data']['chats'].append(text.strip())
            await event.respond(f"➕ Добавлено: {text}\nОтправь еще или напиши ГОТОВО")
    
    # Шаг 5: Получение сообщения
    elif step == 'waiting_message':
        state['temp_data']['message'] = text
        state['step'] = 'waiting_interval'
        
        await event.respond(
            "⏱️ *Шаг 6:* Введи интервал между сообщениями\n"
            "Примеры: 5 (секунд), 30s, 1m, 2h\n"
            "По умолчанию: 3 секунды",
            parse_mode='markdown'
        )
    
    # Шаг 6: Получение интервала
    elif step == 'waiting_interval':
        interval = parse_interval(text)
        state['temp_data']['interval'] = interval
        state['step'] = 'waiting_time'
        
        await event.respond(
            f"✅ Интервал: {interval} секунд\n\n"
            "⏰ *Шаг 7:* Введи время работы по МСК\n"
            "Примеры:\n"
            "09:00 - 18:00 (работает с 9 утра до 6 вечера)\n"
            "15:30 (однократная отправка в 15:30)\n"
            "00:00 - 23:59 (круглосуточно)",
            parse_mode='markdown'
        )
    
    # Шаг 7: Получение времени и запуск рассылки
    elif step == 'waiting_time':
        time_range = parse_time_range(text)
        if not time_range:
            await event.respond("❌ Неверный формат! Примеры: 09:00 - 18:00 или 15:30")
            return
        
        # Получаем данные
        temp_data = state['temp_data']
        chats = temp_data['chats']
        message_text = temp_data['message']
        interval = temp_data['interval']
        
        # Получаем клиента пользователя
        if user_id not in user_clients:
            session = get_user_session(user_id)
            if not session:
                await event.respond("❌ Ошибка: сессия не найдена. Используй /reset_session")
                del user_states[user_id]
                return
            
            api_id, api_hash, account_token = session
            client = TelegramClient(f'data/user_{user_id}', int(api_id), api_hash)
            await client.start(account_token)
            user_clients[user_id] = client
        else:
            client = user_clients[user_id]
        
        # Получаем информацию о чатах
        chat_entities = []
        for link in chats:
            try:
                if 't.me/' in link:
                    username = link.split('t.me/')[-1]
                else:
                    username = link.replace('@', '')
                
                entity = await client.get_entity(username)
                chat_entities.append({
                    'id': entity.id,
                    'title': entity.title if hasattr(entity, 'title') else username
                })
                await event.respond(f"✅ Найден чат: {chat_entities[-1]['title']}")
            except Exception as e:
                await event.respond(f"❌ Не удалось найти чат: {link}\nОшибка: {str(e)}")
        
        if not chat_entities:
            await event.respond("❌ Не найдено ни одного доступного чата!")
            del user_states[user_id]
            return
        
        # Сохраняем задачу
        task_id = save_mailing_task(user_id, chat_entities, message_text, interval, time_range)
        
        # Запускаем рассылку
        await event.respond(
            "🚀 *Рассылка запущена!*\n\n"
            f"📊 Статистика:\n"
            f"- Чатов: {len(chat_entities)}\n"
            f"- Интервал: {interval} сек\n"
            f"- Время работы: {format_time_range(time_range)}\n\n"
            "Для остановки используй /stop_mailing",
            parse_mode='markdown'
        )
        
        # Запускаем задачу
        asyncio.create_task(send_messages(user_id, task_id, client, chat_entities, 
                                         message_text, interval, time_range))
        
        # Очищаем состояние
        del user_states[user_id]

# ============= ЗАПУСК =============
async def main():
    init_db()
    print(f"🤖 Бот запущен! Админ ID: {ADMIN_ID}")
    print("Белый список инициализирован")
    await bot.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())
