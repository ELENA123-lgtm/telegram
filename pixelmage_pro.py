import os
import asyncio
import logging
import aiohttp
import base64
import uuid
import json
import hashlib
import sqlite3
from datetime import datetime
from collections import deque
from typing import List, Dict, Any, Union, Optional
from aiohttp import ClientTimeout
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.types import (
    FSInputFile, ReplyKeyboardMarkup,
    KeyboardButton, ReplyKeyboardRemove, InputMediaPhoto
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv

# ========== НАСТРОЙКА ЛОГИРОВАНИЯ ==========
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ========== ЗАГРУЗКА КЛЮЧЕЙ ==========
load_dotenv('.env')
BOT_TOKEN = os.getenv("BOT_TOKEN")
AITUNNEL_API_KEY = os.getenv("AITUNNEL_API_KEY")

if not BOT_TOKEN or not AITUNNEL_API_KEY:
    logger.error("❌ Не найдены BOT_TOKEN или AITUNNEL_API_KEY!")
    exit(1)

# ========== ИНИЦИАЛИЗАЦИЯ ==========
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ========== ОЧЕРЕДЬ ЗАПРОСОВ ==========
request_queue = deque()
queue_lock = asyncio.Lock()
PROCESSING_LIMIT = 3
MAX_PROMPTS_PER_BATCH = 5


# ========== БАЗА ДАННЫХ ДЛЯ КЭША ==========
def init_db():
    """Инициализация базы данных"""
    conn = sqlite3.connect('bot_cache.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS image_cache
                 (prompt_hash TEXT PRIMARY KEY,
                  file_path TEXT,
                  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS user_stats
                 (user_id INTEGER PRIMARY KEY,
                  requests_count INTEGER DEFAULT 0,
                  total_images INTEGER DEFAULT 0,
                  last_request TIMESTAMP)''')
    conn.commit()
    conn.close()


init_db()


# ========== ФУНКЦИИ КЭША ==========
def get_cached_image(prompt: str) -> Optional[str]:
    """Получает изображение из кэша"""
    prompt_hash = hashlib.md5(prompt.encode()).hexdigest()
    conn = sqlite3.connect('bot_cache.db')
    c = conn.cursor()
    c.execute("SELECT file_path FROM image_cache WHERE prompt_hash = ?", (prompt_hash,))
    result = c.fetchone()
    conn.close()
    return result[0] if result else None


def save_to_cache(prompt: str, file_path: str):
    """Сохраняет изображение в кэш"""
    prompt_hash = hashlib.md5(prompt.encode()).hexdigest()
    conn = sqlite3.connect('bot_cache.db')
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO image_cache (prompt_hash, file_path) VALUES (?, ?)",
              (prompt_hash, file_path))
    conn.commit()
    conn.close()


def update_user_stats(user_id: int, images_count: int = 1):
    """Обновляет статистику пользователя"""
    conn = sqlite3.connect('bot_cache.db')
    c = conn.cursor()
    c.execute('''INSERT OR REPLACE INTO user_stats 
                 (user_id, requests_count, total_images, last_request) 
                 VALUES (?, COALESCE((SELECT requests_count FROM user_stats WHERE user_id = ?), 0) + 1,
                         COALESCE((SELECT total_images FROM user_stats WHERE user_id = ?), 0) + ?,
                         ?)''',
              (user_id, user_id, user_id, images_count, datetime.now()))
    conn.commit()
    conn.close()


def enhance_edit_prompt(original_prompt: str) -> str:
    """Автоматически улучшаем промпт для сохранения лиц"""

    keywords_for_background = ['фон', 'background', 'задний план', 'пейзаж', 'окружение', 'пейзаж', 'обстановка']
    keywords_for_style = ['стиль', 'style', 'в стиле', 'как', 'похоже на', 'стилизация']
    keywords_for_clothing = ['одежда', 'костюм', 'платье', 'футболка', 'clothing', 'outfit', 'наряд', 'форма']
    keywords_for_addition = ['добавь', 'добавить', 'add', 'положи', 'размести', 'вставь']
    keywords_for_removal = ['убери', 'удалить', 'remove', 'убери', 'сотри', 'убери']

    prompt_lower = original_prompt.lower()

    if any(keyword in prompt_lower for keyword in keywords_for_background):
        return (
            f"Change ONLY the background to: {original_prompt}. "
            f"Keep ALL people EXACTLY the same. "
            f"Preserve facial features, hair, clothing, poses, body positions. "
            f"Only the background should change, people remain identical."
        )
    elif any(keyword in prompt_lower for keyword in keywords_for_clothing):
        return (
            f"Change clothing/style to: {original_prompt}. "
            f"But keep faces 100% identical. "
            f"Preserve facial features, expressions, hairstyle. "
            f"Only modify clothing, accessories, outfit."
        )
    elif any(keyword in prompt_lower for keyword in keywords_for_addition):
        return (
            f"Add to the image: {original_prompt}. "
            f"Do NOT change existing people. "
            f"Keep faces, bodies, clothing exactly as they are. "
            f"Only add new elements to the scene."
        )
    elif any(keyword in prompt_lower for keyword in keywords_for_removal):
        return (
            f"Remove from the image: {original_prompt}. "
            f"Keep all people unchanged. "
            f"Preserve faces, features, poses. "
            f"Only remove specified elements."
        )
    elif any(keyword in prompt_lower for keyword in keywords_for_style):
        return (
            f"Apply this artistic style to the image: {original_prompt}. "
            f"Try to keep faces recognizable. "
            f"Maintain general composition, subjects, and poses. "
            f"Preserve the essence of the original photo."
        )
    else:
        # Общий шаблон для любых изменений
        return (
            f"{original_prompt}. "
            f"Try to preserve faces and people if possible. "
            f"Keep facial features similar. "
            f"Maintain the original composition and subjects."
        )


# ========== СОСТОЯНИЯ FSM ==========
class Form(StatesGroup):
    waiting_for_prompt = State()
    waiting_for_batch_prompts = State()
    waiting_for_edit_prompt = State()
    waiting_for_photo = State()


# ========== КЛАВИАТУРЫ ==========
def get_main_keyboard():
    """Основная клавиатура с кнопками"""
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🖼️ Создать"), KeyboardButton(text="📝 Пакет промптов")],
            [KeyboardButton(text="✏️ Редактировать"), KeyboardButton(text="ℹ️ Помощь")],
            [KeyboardButton(text="💰 Баланс"), KeyboardButton(text="📊 Статистика")],
            [KeyboardButton(text="🚪 /start"), KeyboardButton(text="⬅️ Назад")]
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите действие..."
    )
    return keyboard


def get_cancel_keyboard():
    """Клавиатура для отмены"""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="⬅️ Назад")]],
        resize_keyboard=True
    )


# ========== ФУНКЦИЯ РЕДАКТИРОВАНИЯ ИЗОБРАЖЕНИЙ ==========
async def edit_image_api(photo_bytes: bytes, edit_prompt: str) -> Dict[str, Any]:
    """
    Редактирует загруженное фото через AI Tunnel API.
    Использует метод edits для редактирования изображений.
    """
    # Сохраняем фото во временный файл
    temp_file_name = f"temp_upload_{uuid.uuid4().hex}.png"
    with open(temp_file_name, "wb") as f:
        f.write(photo_bytes)

    API_URL = "https://api.aitunnel.ru/v1/images/edits"

    headers = {
        "Authorization": f"Bearer {AITUNNEL_API_KEY}",
        "Accept": "application/json"
    }

    # Создаем FormData и открываем файл внутри контекстного менеджера
    data = aiohttp.FormData()
    data.add_field('model', 'flux.2-pro')
    data.add_field('prompt', edit_prompt)
    data.add_field('n', '1')
    data.add_field('size', '1024x1024')
    data.add_field('response_format', 'b64_json')

    timeout = ClientTimeout(total=120)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            logger.info(f"✏️ Редактирую фото: '{edit_prompt[:50]}...'")

            # Открываем файл для чтения в бинарном режиме
            with open(temp_file_name, 'rb') as image_file:
                # Создаем новую FormData внутри сессии
                form_data = aiohttp.FormData()
                form_data.add_field('model', 'flux.2-pro')
                form_data.add_field('prompt', edit_prompt)
                form_data.add_field('n', '1')
                form_data.add_field('size', '1024x1024')
                form_data.add_field('response_format', 'b64_json')
                form_data.add_field('image',
                                    image_file,
                                    filename='image.png',
                                    content_type='image/png')

                async with session.post(API_URL, headers=headers, data=form_data) as response:
                    response_text = await response.text()

                    if response.status == 200:
                        result = await response.json()
                        logger.info(f"✅ API редактирования вернуло ответ")

                        if 'data' in result and result['data']:
                            # Получаем base64 изображение
                            if 'b64_json' in result['data'][0]:
                                image_data = result['data'][0]['b64_json']
                            elif 'url' in result['data'][0] and result['data'][0]['url'].startswith('data:image/'):
                                # Извлекаем base64 из data URL
                                base64_data = result['data'][0]['url'].split('base64,')[1]
                                image_data = base64_data
                            else:
                                logger.error(f"❌ Неверный формат ответа API: {result}")
                                return {
                                    "success": False,
                                    "error": "invalid_response",
                                    "message": "Неверный формат ответа API"
                                }

                            image_bytes = base64.b64decode(image_data)

                            # Сохраняем изображение
                            file_name = f"edited_{uuid.uuid4().hex}.png"
                            with open(file_name, "wb") as f:
                                f.write(image_bytes)

                            logger.info(f"✅ Изображение сохранено: {file_name}")
                            return {"success": True, "file_path": file_name}
                        else:
                            logger.error(f"❌ Нет данных в ответе API: {result}")
                            return {
                                "success": False,
                                "error": "no_data",
                                "message": "API не вернул данные"
                            }
                    else:
                        logger.error(f"❌ Ошибка API {response.status}: {response_text}")

                        # Пытаемся разобрать JSON ошибки
                        try:
                            error_json = json.loads(response_text)
                            error_msg = error_json.get('error', {}).get('message', response_text)
                        except:
                            error_msg = response_text[:200]

                        return {
                            "success": False,
                            "error": f"api_error_{response.status}",
                            "message": f"Ошибка API: {error_msg}"
                        }

    except asyncio.TimeoutError:
        logger.error("❌ Таймаут при редактировании")
        return {
            "success": False,
            "error": "timeout",
            "message": "Таймаут при обработке запроса"
        }
    except Exception as e:
        logger.exception(f"💥 Ошибка при редактировании: {e}")
        return {
            "success": False,
            "error": "unexpected_error",
            "message": f"Внутренняя ошибка: {str(e)}"
        }
    finally:
        # Удаляем временный файл
        try:
            if os.path.exists(temp_file_name):
                os.remove(temp_file_name)
        except:
            pass


# ========== ФУНКЦИЯ ГЕНЕРАЦИИ ИЗОБРАЖЕНИЙ ==========
async def generate_images_api(prompts: List[str]) -> Dict[str, Any]:
    """Генерирует изображения через AI Tunnel API"""
    if not prompts:
        return {"error": "no_prompts", "message": "Нет промптов для генерации"}

    if len(prompts) > 10:
        return {"error": "too_many_images", "message": f"Слишком много промптов ({len(prompts)} > 10)"}

    cached_images = {}
    uncached_prompts = []

    for prompt in prompts:
        cached = get_cached_image(prompt)
        if cached and os.path.exists(cached):
            cached_images[prompt] = cached
        else:
            uncached_prompts.append(prompt)

    if not uncached_prompts and cached_images:
        return {
            "success": True,
            "from_cache": True,
            "results": [{"prompt": p, "file_paths": [cached_images[p]], "from_cache": True} for p in prompts],
            "cached_count": len(cached_images)
        }

    API_URL = "https://api.aitunnel.ru/v1/images/generations"
    headers = {
        "Authorization": f"Bearer {AITUNNEL_API_KEY}",
        "Content-Type": "application/json"
    }

    all_results = []

    for prompt in uncached_prompts:
        data = {
            "model": "flux.2-pro",
            "prompt": prompt,
            "width": 1024,
            "height": 1024,
            "steps": 20,
            "num_images": 1
        }

        timeout = ClientTimeout(total=120)

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                logger.info(f"🔄 Генерирую изображение для: {prompt[:50]}...")

                async with session.post(API_URL, headers=headers, json=data) as response:
                    if response.status == 200:
                        result = await response.json()

                        if 'data' in result and isinstance(result['data'], list):
                            file_paths = []

                            for idx, item in enumerate(result['data']):
                                if 'url' in item and item['url'].startswith('data:image/'):
                                    if 'base64,' in item['url']:
                                        base64_data = item['url'].split('base64,')[1]
                                        image_bytes = base64.b64decode(base64_data)

                                        file_name = f"generated_{uuid.uuid4().hex}_{idx}.png"
                                        with open(file_name, "wb") as f:
                                            f.write(image_bytes)

                                        file_paths.append(file_name)
                                elif 'b64_json' in item:
                                    # Если API вернуло base64 напрямую
                                    image_bytes = base64.b64decode(item['b64_json'])
                                    file_name = f"generated_{uuid.uuid4().hex}_{idx}.png"
                                    with open(file_name, "wb") as f:
                                        f.write(image_bytes)
                                    file_paths.append(file_name)

                            if file_paths:
                                save_to_cache(prompt, file_paths[0])
                                all_results.append({
                                    "prompt": prompt,
                                    "file_paths": file_paths,
                                    "from_cache": False
                                })
                                logger.info(f"✅ Успешно сгенерирован промпт: {prompt[:50]}")
                            else:
                                all_results.append({
                                    "prompt": prompt,
                                    "error": "no_images",
                                    "message": "API не вернул изображения"
                                })
                        else:
                            logger.error(f"❌ Неверный ответ от API для промпта: {prompt[:50]}")
                            all_results.append({
                                "prompt": prompt,
                                "error": "invalid_response",
                                "message": "Неверный ответ от API"
                            })
                    else:
                        error_text = await response.text()
                        logger.error(f"❌ Ошибка API {response.status} для промпта: {prompt[:50]}")
                        all_results.append({
                            "prompt": prompt,
                            "error": "api_error",
                            "message": f"Ошибка API: {response.status}"
                        })

        except Exception as e:
            logger.error(f"❌ Ошибка генерации для промпта '{prompt}': {e}")
            all_results.append({
                "prompt": prompt,
                "error": "processing_error",
                "message": str(e)[:100]
            })

    # Добавляем кэшированные результаты
    for prompt in cached_images:
        all_results.append({
            "prompt": prompt,
            "file_paths": [cached_images[prompt]],
            "from_cache": True
        })

    # Проверяем успешность
    successful_results = [r for r in all_results if "file_paths" in r]

    return {
        "success": len(successful_results) > 0,
        "from_cache": False,
        "results": all_results,
        "cached_count": len(cached_images),
        "total_requested": len(prompts),
        "total_received": len(successful_results)
    }


# ========== ОБРАБОТЧИКИ КОМАНД ==========
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    """Команда /start"""
    welcome_text = (
        "🎨 <b>PixelMage Pro 2.0</b>\n\n"
        "<b>Продвинутый генератор изображений</b>\n\n"
        "<b>Основные функции:</b>\n"
        "🖼️ <b>Создать</b> - одно изображение по промпту\n"
        "📝 <b>Пакет промптов</b> - до 5 промптов → до 5 изображений за раз\n"
        "✏️ <b>Редактировать</b> - изменить фон, стиль или элементы на фото\n\n"
        "<i>💡 При редактировании AI старается сохранить лица</i>\n"
        "<i>💡 Для замены фона лучше всего сохраняются лица</i>\n\n"
        "<b>Статистика:</b> кэширование, очередь запросов, лимиты\n\n"
        "<i>Используйте кнопки ниже или команды:</i>\n"
        "/generate - одно изображение\n"
        "/batch - пакетная обработка\n"
        "/help - справка"
    )

    await message.answer(welcome_text, parse_mode="HTML", reply_markup=get_main_keyboard())


@dp.message(F.text == "🚪 /start")
async def btn_start_again(message: types.Message, state: FSMContext):
    """Повторный запуск через кнопку"""
    await state.clear()
    await cmd_start(message)


@dp.message(F.text == "⬅️ Назад")
async def cancel_action(message: types.Message, state: FSMContext):
    """Отмена текущего действия"""
    await state.clear()
    await message.answer("✅ Возвращаюсь в главное меню", reply_markup=get_main_keyboard())


@dp.message(F.text == "ℹ️ Помощь")
@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    """Справка"""
    help_text = (
        "📋 <b>PixelMage Pro - Полная справка</b>\n\n"
        "<b>🖼️ Создать (один промпт):</b>\n"
        "• Введите описание изображения\n"
        "• Получите один результат\n"
        "• Используется кэш для повторных запросов\n\n"
        "<b>📝 Пакет промптов (до 5):</b>\n"
        "• Введите до 5 промптов через точку с запятой\n"
        "• Каждый промпт → отдельное изображение\n"
        "• Эффективная пакетная обработка\n\n"
        "<b>✏️ Редактировать (улучшенная версия):</b>\n"
        "• Загрузите фото как образец\n"
        "• Введите, что изменить (фон, стиль, элементы)\n"
        "• AI старается сохранить лица людей\n"
        "• Лучше всего работает для замены фона\n\n"
        "<b>Советы по редактированию:</b>\n"
        "• 'поменяй фон на пляж' - хорошо сохраняет лица\n"
        "• 'добавь солнцезащитные очки' - добавляет элементы\n"
        "• 'в стиле советской открытки' - меняет стиль\n"
        "• 'убери человека справа' - удаляет элементы\n\n"
        "<b>💰 Баланс:</b> Информация о средствах\n"
        "<b>📊 Статистика:</b> Ваша активность\n\n"
        "<b>Примеры промптов:</b>\n"
        "• космический кот в скафандре\n"
        "• портрет эльфа; фэнтези арт; магический лес\n"
        "• поменяй фон на пляж (лучше всего сохраняет лица)"
    )
    await message.answer(help_text, parse_mode="HTML", reply_markup=get_main_keyboard())


@dp.message(F.text == "💰 Баланс")
@dp.message(Command("credits"))
async def cmd_credits(message: types.Message):
    """Проверка баланса"""
    await message.answer(
        "💰 <b>Информация о балансе</b>\n\n"
        "Баланс можно проверить в личном кабинете:\n"
        "https://platform.aitunnel.ru/\n\n"
        "<b>Примерные расценки:</b>\n"
        "• flux.2-pro: ~5.35 руб/изображение\n"
        "• Другие модели: смотрите в кабинете\n\n"
        "<i>Кэширование позволяет экономить на повторных запросах</i>",
        parse_mode="HTML",
        reply_markup=get_main_keyboard()
    )


@dp.message(F.text == "📊 Статистика")
@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    """Статистика пользователя"""
    user_id = message.from_user.id
    conn = sqlite3.connect('bot_cache.db')
    c = conn.cursor()

    c.execute("SELECT requests_count, total_images, last_request FROM user_stats WHERE user_id = ?", (user_id,))
    user_stats = c.fetchone()

    c.execute("SELECT COUNT(*) FROM image_cache")
    cache_count = c.fetchone()[0]

    conn.close()

    if user_stats:
        requests_count, total_images, last_request = user_stats
        stats_text = (
            f"📊 <b>Ваша статистика</b>\n\n"
            f"<b>Запросов:</b> {requests_count}\n"
            f"<b>Изображений создано:</b> {total_images}\n"
            f"<b>Последний запрос:</b> {last_request}\n"
            f"<b>Изображений в кэше:</b> {cache_count}\n\n"
            f"<i>Кэш экономит время и деньги!</i>"
        )
    else:
        stats_text = (
            f"📊 <b>Статистика</b>\n\n"
            f"Вы еще не создавали изображений\n"
            f"<b>Изображений в кэше бота:</b> {cache_count}\n\n"
            f"Попробуйте создать первое изображение!"
        )

    await message.answer(stats_text, parse_mode="HTML", reply_markup=get_main_keyboard())


@dp.message(F.text == "🖼️ Создать")
async def btn_single(message: types.Message, state: FSMContext):
    """Одно изображение"""
    await message.answer(
        "✍️ <b>Введите описание изображения:</b>\n\n"
        "<i>Пример: космический пейзаж с планетами</i>\n"
        "<i>Или нажмите ⬅️ Назад</i>",
        parse_mode="HTML",
        reply_markup=get_cancel_keyboard()
    )
    await state.set_state(Form.waiting_for_prompt)


@dp.message(StateFilter(Form.waiting_for_prompt))
async def process_single_prompt(message: types.Message, state: FSMContext):
    """Обработка одиночного промпта"""
    if message.text == "⬅️ Назад":
        await state.clear()
        await message.answer("⬅️ Возвращаюсь в главное меню", reply_markup=get_main_keyboard())
        return

    prompt = message.text.strip()
    if not prompt:
        await message.answer("⚠️ Введите описание изображения")
        return

    if len(prompt) > 1000:
        await message.answer("⚠️ Промпт слишком длинный (макс. 1000 символов)")
        return

    await message.answer(
        f"🎨 <b>Генерирую:</b> <i>{prompt}</i>\n"
        f"⏳ Подождите 20-30 секунд...",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove()
    )

    async with queue_lock:
        if len(request_queue) >= PROCESSING_LIMIT:
            await message.answer(
                "⏳ Очередь переполнена. Попробуйте через минуту.",
                reply_markup=get_main_keyboard()
            )
            await state.clear()
            return
        request_queue.append(message.from_user.id)

    try:
        result = await generate_images_api([prompt])

        if result.get("success"):
            update_user_stats(message.from_user.id, 1)
            await handle_generation_results(message, result)
        else:
            error_msg = result.get("message", "Неизвестная ошибка")
            await message.answer(
                f"❌ <b>Ошибка:</b> {error_msg}",
                parse_mode="HTML",
                reply_markup=get_main_keyboard()
            )

    except Exception as e:
        logger.error(f"Ошибка обработки: {e}")
        await message.answer(
            f"❌ <b>Системная ошибка:</b> {str(e)}",
            parse_mode="HTML",
            reply_markup=get_main_keyboard()
        )
    finally:
        async with queue_lock:
            if message.from_user.id in request_queue:
                request_queue.remove(message.from_user.id)

        await state.clear()


@dp.message(F.text == "📝 Пакет промптов")
async def btn_batch(message: types.Message, state: FSMContext):
    """Пакетная обработка промптов"""
    await message.answer(
        "📝 <b>Введите до 5 промптов через точку с запятой:</b>\n\n"
        "<i>Пример: космический кот; фэнтези замок; неоновый город</i>\n"
        "<i>Каждый промпт → отдельное изображение</i>\n"
        "<i>Или нажмите ⬅️ Назад</i>",
        parse_mode="HTML",
        reply_markup=get_cancel_keyboard()
    )
    await state.set_state(Form.waiting_for_batch_prompts)


@dp.message(StateFilter(Form.waiting_for_batch_prompts))
async def process_batch_prompts(message: types.Message, state: FSMContext):
    """Обработка пакета промптов"""
    if message.text == "⬅️ Назад":
        await state.clear()
        await message.answer("⬅️ Возвращаюсь в главное меню", reply_markup=get_main_keyboard())
        return

    prompts_text = message.text.strip()
    if not prompts_text:
        await message.answer("⚠️ Введите промпты через точку с запятой")
        return

    # Разделяем промпты и очищаем
    prompts = []
    for p in prompts_text.split(';'):
        p = p.strip()
        if p:  # Добавляем только непустые промпты
            prompts.append(p)

    if not prompts:
        await message.answer("⚠️ Не найдено валидных промптов")
        return

    if len(prompts) > MAX_PROMPTS_PER_BATCH:
        prompts = prompts[:MAX_PROMPTS_PER_BATCH]
        await message.answer(f"⚠️ Будут обработаны первые {MAX_PROMPTS_PER_BATCH} промптов")

    # Проверяем длину каждого промпта
    for i, prompt in enumerate(prompts):
        if len(prompt) > 1000:
            await message.answer(f"⚠️ Промпт #{i + 1} слишком длинный (макс. 1000 символов)")
            return

    # Показываем, что будем обрабатывать
    prompt_preview = "\n".join([f"• {p[:30]}{'...' if len(p) > 30 else ''}" for p in prompts[:3]])
    if len(prompts) > 3:
        prompt_preview += f"\n• ... и еще {len(prompts) - 3} промптов"

    await message.answer(
        f"📦 <b>Обрабатываю {len(prompts)} промптов:</b>\n"
        f"{prompt_preview}\n"
        f"⏳ Это займет {len(prompts) * 15} секунд...",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove()
    )

    async with queue_lock:
        if len(request_queue) >= PROCESSING_LIMIT:
            await message.answer(
                "⏳ Очередь переполнена. Попробуйте через минуту.",
                reply_markup=get_main_keyboard()
            )
            await state.clear()
            return
        request_queue.append(message.from_user.id)

    try:
        result = await generate_images_api(prompts)

        if result.get("success"):
            successful_count = result.get("total_received", 0)
            update_user_stats(message.from_user.id, successful_count)
            await handle_generation_results(message, result, is_batch=True)
        else:
            error_msg = result.get("message", "Неизвестная ошибка")
            await message.answer(
                f"❌ <b>Ошибка:</b> {error_msg}",
                parse_mode="HTML",
                reply_markup=get_main_keyboard()
            )

    except Exception as e:
        logger.error(f"Ошибка обработки: {e}")
        await message.answer(
            f"❌ <b>Системная ошибка:</b> {str(e)}",
            parse_mode="HTML",
            reply_markup=get_main_keyboard()
        )
    finally:
        async with queue_lock:
            if message.from_user.id in request_queue:
                request_queue.remove(message.from_user.id)

        await state.clear()


@dp.message(F.text == "✏️ Редактировать")
async def btn_edit(message: types.Message, state: FSMContext):
    """Редактирование фото"""
    await message.answer(
        "✏️ <b>Редактирование фото (улучшенная версия)</b>\n\n"
        "📤 <b>Загрузите фото для редактирования:</b>\n\n"
        "<i>Что лучше всего работает:</i>\n"
        "• Замена фона (лучше всего сохраняет лица) 🏆\n"
        "• Добавление элементов к фото\n"
        "• Изменение стиля изображения\n"
        "• Удаление объектов с фото\n\n"
        "<i>⚠️ AI постарается сохранить лица, но результат не гарантирован</i>\n"
        "<i>Поддерживаются: JPG, PNG</i>\n"
        "<i>Или нажмите ⬅️ Назад</i>",
        parse_mode="HTML",
        reply_markup=get_cancel_keyboard()
    )
    await state.set_state(Form.waiting_for_photo)


@dp.message(StateFilter(Form.waiting_for_photo), F.photo)
async def process_edit_photo(message: types.Message, state: FSMContext):
    """Обработка загруженного фото"""
    if message.text == "⬅️ Назад":
        await state.clear()
        await message.answer("⬅️ Возвращаюсь в главное меню", reply_markup=get_main_keyboard())
        return

    try:
        file_id = message.photo[-1].file_id
        file = await bot.get_file(file_id)

        temp_file = f"temp_edit_{uuid.uuid4().hex}.jpg"
        await bot.download_file(file.file_path, temp_file)

        with open(temp_file, "rb") as f:
            photo_bytes = f.read()

        await state.update_data(photo_bytes=photo_bytes)

        await message.answer(
            "✍️ <b>Что изменить на фото?</b>\n\n"
            "<i>Примеры (с сохранением лиц):</i>\n"
            "• поменяй фон на пляж 🏝️\n"
            "• добавь солнцезащитные очки 😎\n"
            "• убери человека справа 🚫\n"
            "• сделай в стиле пиксель-арт 🎮\n"
            "• поменяй время суток на ночь 🌙\n\n"
            "<i>💡 Для лучшего результата:</i>\n"
            "• Указывайте конкретные изменения\n"
            "• Для замены фона лица сохраняются лучше всего\n"
            "• AI постарается сохранить оригинальные лица\n\n"
            "<i>Или нажмите ⬅️ Назад</i>",
            parse_mode="HTML",
            reply_markup=get_cancel_keyboard()
        )
        await state.set_state(Form.waiting_for_edit_prompt)

        # Удаляем временный файл
        try:
            os.remove(temp_file)
        except:
            pass

    except Exception as e:
        logger.error(f"Ошибка загрузки фото: {e}")
        await message.answer(
            f"❌ <b>Ошибка загрузки фото:</b> {str(e)[:100]}",
            parse_mode="HTML",
            reply_markup=get_main_keyboard()
        )
        await state.clear()


async def process_with_queue(user_id: int, func, *args, **kwargs):
    """Обрабатывает запрос с учетом очереди"""
    async with queue_lock:
        if len(request_queue) >= PROCESSING_LIMIT:
            return {"error": "queue_full", "message": "⏳ Очередь переполнена. Попробуйте позже."}

        request_queue.append(user_id)

    try:
        result = await func(*args, **kwargs)
        return result
    finally:
        async with queue_lock:
            if user_id in request_queue:
                request_queue.remove(user_id)


@dp.message(StateFilter(Form.waiting_for_edit_prompt))
async def process_edit_request(message: types.Message, state: FSMContext):
    """Обработка запроса на редактирование"""
    if message.text == "⬅️ Назад":
        await state.clear()
        await message.answer("⬅️ Возвращаюсь в главное меню", reply_markup=get_main_keyboard())
        return

    data = await state.get_data()
    photo_bytes = data.get("photo_bytes")
    edit_prompt = message.text.strip()

    if not photo_bytes:
        await message.answer("❌ Фото не загружено", reply_markup=get_main_keyboard())
        await state.clear()
        return

    if not edit_prompt:
        await message.answer("⚠️ Введите, что изменить на фото")
        return

    # Улучшаем промпт для лучшего сохранения лиц
    enhanced_prompt = enhance_edit_prompt(edit_prompt)

    await message.answer(
        f"✏️ <b>Редактирую (стараюсь сохранить лица):</b> <i>{edit_prompt[:80]}</i>\n"
        f"⏳ Подождите 20-30 секунд...\n\n"
        f"<i>AI получил улучшенную инструкцию для сохранения лиц</i>",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove()
    )

    # Используем улучшенный промпт
    result = await process_with_queue(message.from_user.id, edit_image_api, photo_bytes, enhanced_prompt)

    if result.get("success"):
        file_path = result.get("file_path")

        if file_path and os.path.exists(file_path):
            try:
                photo = FSInputFile(file_path)
                await message.answer_photo(
                    photo,
                    caption=f"✅ Отредактировано: {edit_prompt[:100]}",
                    reply_markup=get_main_keyboard()
                )

                # Чистим файл
                try:
                    os.remove(file_path)
                except:
                    pass

            except Exception as e:
                logger.error(f"Ошибка отправки фото: {e}")
                await message.answer(
                    "✅ Редактирование завершено, но не удалось отправить фото",
                    reply_markup=get_main_keyboard()
                )
        else:
            await message.answer(
                "❌ Ошибка при сохранении файла",
                reply_markup=get_main_keyboard()
            )
    else:
        error_type = result.get("error", "unknown")
        error_msg = result.get("message", "Неизвестная ошибка")

        # Более дружелюбные сообщения об ошибках
        if "400" in error_type:
            user_msg = (
                "⚠️ <b>Не удалось отредактировать фото</b>\n\n"
                "Возможные причины:\n"
                "• Промпт слишком сложный\n"
                "• API не понял запрос\n"
                "• Попробуйте упростить описание\n\n"
                "<i>Пример: 'поменяй фон на пляж' вместо длинного описания</i>"
            )
        elif "rate_limit" in error_type or "429" in error_type:
            user_msg = "⏳ Превышен лимит запросов. Попробуйте через 1-2 минуты."
        elif "timeout" in error_type:
            user_msg = "⏳ Превышено время ожидания. Попробуйте позже."
        else:
            user_msg = f"❌ Ошибка редактирования: {error_msg}"

        await message.answer(
            user_msg,
            parse_mode="HTML" if "<b>" in user_msg else None,
            reply_markup=get_main_keyboard()
        )

    await state.clear()


async def handle_generation_results(message: types.Message, result: Dict[str, Any],
                                    is_batch: bool = False):
    """Универсальная обработка результатов генерации"""
    if not result.get("success"):
        error_msg = result.get("message", "Неизвестная ошибка")
        await message.answer(
            f"❌ <b>Ошибка:</b> {error_msg}\n\n"
            f"<i>Попробуйте упростить промпт или использовать другую функцию</i>",
            parse_mode="HTML",
            reply_markup=get_main_keyboard()
        )
        return

    results = result.get("results", [])
    cached_count = result.get("cached_count", 0)
    total_requested = result.get("total_requested", 0)
    total_received = result.get("total_received", 0)

    if not results:
        await message.answer(
            "❌ Нет результатов генерации\n"
            "Попробуйте другой промпт",
            reply_markup=get_main_keyboard()
        )
        return

    if cached_count > 0:
        await message.answer(f"⚡ Использовано из кэша: {cached_count}", parse_mode="HTML")

    successful_results = [r for r in results if "file_paths" in r and not r.get("error")]

    for res in successful_results:
        prompt = res.get("prompt", "Без названия")
        file_paths = res.get("file_paths", [])
        from_cache = res.get("from_cache", False)

        if not file_paths:
            continue

        # Отправляем каждое изображение отдельно
        for i, file_path in enumerate(file_paths):
            try:
                photo = FSInputFile(file_path)
                caption = f"✅ {prompt[:100]}"
                if from_cache:
                    caption += " (из кэша)"
                if len(file_paths) > 1:
                    caption += f" [{i + 1}/{len(file_paths)}]"

                await message.answer_photo(
                    photo,
                    caption=caption,
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.error(f"Ошибка отправки фото: {e}")

        # Удаляем временные файлы, если они не из кэша
        if not from_cache:
            for file_path in file_paths:
                try:
                    if os.path.exists(file_path):
                        os.remove(file_path)
                except:
                    pass

    # Показываем ошибки, если есть
    error_results = [r for r in results if r.get("error")]
    if error_results:
        error_msg = "⚠️ <b>Частичные ошибки:</b>\n"
        for res in error_results[:3]:
            error_msg += f"• {res.get('prompt', '?')[:30]}: {res.get('message', 'Ошибка')}\n"

        if len(error_results) > 3:
            error_msg += f"<i>... и еще {len(error_results) - 3} ошибок</i>"

        await message.answer(error_msg, parse_mode="HTML")

    success_count = len(successful_results)

    if is_batch:
        summary = f"📦 <b>Пакетная обработка завершена:</b> {success_count}/{total_requested} успешно"
    else:
        summary = f"🖼️ <b>Генерация завершена:</b> {success_count} изображений"

    if cached_count > 0:
        summary += f", {cached_count} из кэша"

    summary += "\n\n✅ <i>Готово! Что создаем дальше?</i>"

    await message.answer(summary, parse_mode="HTML", reply_markup=get_main_keyboard())


# ========== ТЕКСТОВЫЕ КОМАНДЫ ==========
@dp.message(Command("generate"))
async def cmd_generate_text(message: types.Message):
    """Текстовая команда /generate"""
    prompt = message.text.replace('/generate', '', 1).strip()
    if not prompt:
        await message.answer(
            "📝 <b>Использование:</b> /generate <описание>\n\n"
            "<b>Пример:</b> /generate космический кот в скафандре\n\n"
            "<i>Или используйте кнопку 🖼️ Создать</i>",
            parse_mode="HTML",
            reply_markup=get_main_keyboard()
        )
        return

    await message.answer(
        f"🎨 <b>Генерирую:</b> <i>{prompt}</i>\n⏳ Подождите...",
        parse_mode="HTML"
    )

    async with queue_lock:
        if len(request_queue) >= PROCESSING_LIMIT:
            await message.answer(
                "⏳ Очередь переполнена. Попробуйте через минуту.",
                reply_markup=get_main_keyboard()
            )
            return
        request_queue.append(message.from_user.id)

    try:
        result = await generate_images_api([prompt])

        if result.get("success"):
            update_user_stats(message.from_user.id, 1)
            await handle_generation_results(message, result)
        else:
            error_msg = result.get("message", "Неизвестная ошибка")
            await message.answer(
                f"❌ <b>Ошибка:</b> {error_msg}",
                parse_mode="HTML",
                reply_markup=get_main_keyboard()
            )

    except Exception as e:
        logger.error(f"Ошибка обработки: {e}")
        await message.answer(
            f"❌ <b>Системная ошибка:</b> {str(e)}",
            parse_mode="HTML",
            reply_markup=get_main_keyboard()
        )
    finally:
        async with queue_lock:
            if message.from_user.id in request_queue:
                request_queue.remove(message.from_user.id)


@dp.message(Command("batch"))
async def cmd_batch_text(message: types.Message):
    """Текстовая команда /batch"""
    prompts_text = message.text.replace('/batch', '', 1).strip()

    if not prompts_text:
        await message.answer(
            "📝 <b>Использование:</b> /batch <промпт1>; <промпт2>; ...\n\n"
            "<b>Пример:</b> /batch космический кот; фэнтези замок; неоновый город\n"
            "<b>Максимум:</b> 5 промптов за раз\n\n"
            "<i>Или используйте кнопку 📝 Пакет промптов</i>",
            parse_mode="HTML",
            reply_markup=get_main_keyboard()
        )
        return

    # Разделяем промпты
    prompts = []
    for p in prompts_text.split(';'):
        p = p.strip()
        if p:
            prompts.append(p)

    if not prompts:
        await message.answer("⚠️ Не найдено валидных промптов")
        return

    if len(prompts) > MAX_PROMPTS_PER_BATCH:
        prompts = prompts[:MAX_PROMPTS_PER_BATCH]
        await message.answer(f"⚠️ Будут обработаны первые {MAX_PROMPTS_PER_BATCH} промптов")

    await message.answer(
        f"📦 <b>Обрабатываю {len(prompts)} промптов:</b>\n"
        f"<i>{' • '.join(p[:20] + '...' if len(p) > 20 else p for p in prompts)}</i>\n"
        f"⏳ Это займет {len(prompts) * 15} секунд...",
        parse_mode="HTML"
    )

    async with queue_lock:
        if len(request_queue) >= PROCESSING_LIMIT:
            await message.answer(
                "⏳ Очередь переполнена. Попробуйте через минуту.",
                reply_markup=get_main_keyboard()
            )
            return
        request_queue.append(message.from_user.id)

    try:
        result = await generate_images_api(prompts)

        if result.get("success"):
            successful_count = result.get("total_received", 0)
            update_user_stats(message.from_user.id, successful_count)
            await handle_generation_results(message, result, is_batch=True)
        else:
            error_msg = result.get("message", "Неизвестная ошибка")
            await message.answer(
                f"❌ <b>Ошибка:</b> {error_msg}",
                parse_mode="HTML",
                reply_markup=get_main_keyboard()
            )

    except Exception as e:
        logger.error(f"Ошибка обработки: {e}")
        await message.answer(
            f"❌ <b>Системная ошибка:</b> {str(e)}",
            parse_mode="HTML",
            reply_markup=get_main_keyboard()
        )
    finally:
        async with queue_lock:
            if message.from_user.id in request_queue:
                request_queue.remove(message.from_user.id)


# ========== ОБРАБОТЧИК ЛЮБЫХ СООБЩЕНИЙ ==========
@dp.message()
async def handle_any_message(message: types.Message, state: FSMContext):
    """Обработчик любых сообщений"""
    current_state = await state.get_state()

    if current_state is None:
        # Если нет активного состояния, предлагаем вернуться в меню
        await message.answer(
            "🤖 Я тебя не понял. Используй кнопки или команды!\n\n"
            "Попробуй:\n"
            "/start - перезапустить бота\n"
            "/help - показать справку\n"
            "Или выбери действие из меню ниже 👇",
            reply_markup=get_main_keyboard()
        )
    else:
        # Если есть состояние, но сообщение не обработалось
        await message.answer(
            "⚠️ Пожалуйста, используй кнопки для текущего действия.\n"
            "Или нажми '⬅️ Назад' чтобы вернуться в меню.",
            reply_markup=get_cancel_keyboard()
        )


# ========== ЗАПУСК БОТА ==========
async def main():
    logger.info("=" * 50)
    logger.info("🚀 PIXELMAGE PRO 2.0 ЗАПУЩЕН")
    logger.info("=" * 50)
    logger.info("Функции: кнопки, кэш, очередь, пакетная обработка, улучшенное редактирование")
    logger.info("=" * 50)

    await dp.start_polling(bot)


if __name__ == "__main__":
    print("=" * 50)
    print("🤖 PixelMage Pro 2.0 запускается...")
    print("=" * 50)
    print("Отправьте /start в Telegram чтобы увидеть кнопки")
    print("=" * 50)
    print("Основные функции:")
    print("• 🖼️  Одно изображение")
    print("• 📝  Пакетная обработка (до 5 промптов → изображений)")
    print("• ✏️  Улучшенное редактирование (старается сохранять лица)")
    print("=" * 50)
    print("🔥 Редактирование теперь лучше сохраняет лица!")
    print("=" * 50)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Бот остановлен")