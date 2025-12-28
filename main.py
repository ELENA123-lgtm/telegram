import os
import asyncio
import logging
import aiohttp
import base64
import uuid
from aiohttp import ClientTimeout
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import FSInputFile
from dotenv import load_dotenv
from deep_translator import GoogleTranslator

# Загружаем переменные окружения ИЗ ФАЙЛА .env
load_dotenv('.env')

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Получаем токены
BOT_TOKEN = os.getenv("BOT_TOKEN")
AITUNNEL_API_KEY = os.getenv("AITUNNEL_API_KEY")  # Ваш ключ sk-aitunnel-...

# Проверяем, что ключи загрузились
if not BOT_TOKEN:
    logger.error("❌ BOT_TOKEN не найден в .env файле!")
if not AITUNNEL_API_KEY:
    logger.error("❌ AITUNNEL_API_KEY не найден в .env файле!")

# Инициализация бота
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Инициализация переводчика
translator = GoogleTranslator(source='ru', target='en')


# ========== ФУНКЦИЯ ПЕРЕВОДА ПРОМПТА ==========
async def translate_to_english(text: str) -> str:
    """Переводит текст с русского на английский."""
    try:
        translation = await asyncio.to_thread(
            translator.translate, text
        )
        return translation
    except Exception as e:
        logger.error(f"❌ Ошибка перевода: {e}")
        return text  # Если перевод не сработал, возвращаем как есть


# ========== ИСПРАВЛЕННАЯ ФУНКЦИЯ ГЕНЕРАЦИИ ИЗОБРАЖЕНИЯ ==========
async def generate_image_aitunnel(prompt: str) -> str | None:
    """Генерирует изображение через AI Tunnel API. Возвращает путь к файлу или None."""
    API_URL = "https://api.aitunnel.ru/v1/images/generations"

    headers = {
        "Authorization": f"Bearer {AITUNNEL_API_KEY}",
        "Content-Type": "application/json"
    }

    # 1. Переводим русский промпт на английский
    english_prompt = await translate_to_english(prompt)
    logger.info(f"🌐 Оригинальный промпт: {prompt}")
    logger.info(f"🌐 Переведенный промпт: {english_prompt}")

    data = {
        "model": "flux.2-pro",
        "prompt": english_prompt,
        "width": 1024,
        "height": 1024,
        "steps": 20,
        "response_format": "b64_json"  # Важное исправление: запрашиваем base64
    }

    # Увеличиваем таймаут до 180 секунд (FLUX может генерировать долго)
    timeout = ClientTimeout(total=180)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            logger.info("🔄 Отправляю запрос в AI Tunnel...")

            async with session.post(API_URL, headers=headers, json=data) as response:
                # Читаем весь ответ для логирования
                resp_text = await response.text()

                if response.status != 200:
                    logger.error(f"❌ AI Tunnel вернул ошибку: status={response.status}, body={resp_text[:2000]}")
                    return None

                # Парсим JSON ответ
                result = await response.json()

                # Логируем структуру ответа для диагностики
                logger.info(f"✅ AI Tunnel response keys: {list(result.keys())}")

                # Получаем данные изображения
                item = (result.get("data") or [{}])[0]

                # Вариант A: если пришел base64 (предпочтительный вариант)
                if "b64_json" in item and item["b64_json"]:
                    logger.info("📥 Получено изображение в формате base64")
                    image_bytes = base64.b64decode(item["b64_json"])

                # Вариант B: если вдруг вернулся URL (резервный вариант)
                elif "url" in item and item["url"]:
                    logger.info(f"🌐 Получен URL, скачиваю: {item['url']}")
                    async with session.get(item["url"]) as img_resp:
                        if img_resp.status != 200:
                            err = await img_resp.text()
                            logger.error(f"❌ Не удалось скачать изображение: status={img_resp.status}")
                            return None
                        image_bytes = await img_resp.read()
                else:
                    logger.error(f"❌ Неожиданный формат ответа: {str(result)[:2000]}")
                    return None

                # Сохраняем изображение с уникальным именем
                file_name = f"generated_{uuid.uuid4().hex}.png"
                with open(file_name, "wb") as f:
                    f.write(image_bytes)

                logger.info(f"💾 Изображение сохранено: {file_name}")
                return file_name

        except asyncio.TimeoutError:
            logger.error("❌ Таймаут при запросе к AI Tunnel")
            return None
        except Exception as e:
            logger.exception(f"❌ Ошибка генерации/скачивания: {e}")
            return None


# ========== ОБРАБОТЧИК КОМАНД ТЕЛЕГРАМ-БОТА ==========

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "🎨 Добро пожаловать в PixelMage - ваш личный AI-художник!\n\n"
        "Теперь работает на AI Tunnel (модель FLUX.2-Pro)\n\n"
        "Доступные команды:\n"
        "🔹 /generate <описание> - Создать одно изображение\n"
        "🔹 /help - Справка\n\n"
        "Просто отправьте мне промпт на русском, и я создам изображение! ✨"
    )


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(
        "📋 Как пользоваться ботом:\n"
        "1. Отправьте команду /generate и описание изображения\n"
        "2. Например: /generate космический кот в скафандре\n"
        "3. Я переведу текст на английский для лучшего качества.\n"
        "4. Ждите до 2-3 минут\n\n"
        "❗️ Промпты пишите на русском — я сам переведу."
    )


@dp.message(Command("generate"))
async def cmd_generate(message: types.Message):
    # Извлекаем текст промпта из команды
    prompt = message.text.replace('/generate', '', 1).strip()

    if not prompt:
        await message.answer(
            "⚠️ Пожалуйста, укажите описание изображения.\nПример: `/generate красивое северное сияние`")
        return

    await message.answer(f"🔄 Создаю изображение по запросу:\n`{prompt}`\n\nЭто займет до 2-3 минут...")

    # Вызываем функцию генерации
    image_path = await generate_image_aitunnel(prompt)

    if image_path and os.path.exists(image_path):
        try:
            # Отправляем изображение пользователю
            photo = FSInputFile(image_path)
            await message.answer_photo(photo, caption=f"✅ Готово! Запрос: `{prompt}`")

            # Удаляем временный файл
            os.remove(image_path)
            logger.info(f"🗑️ Временный файл удален: {image_path}")
        except Exception as e:
            logger.error(f"❌ Ошибка отправки фото: {e}")
            await message.answer("❌ Не удалось отправить изображение.")
    else:
        await message.answer(
            "❌ Не удалось создать изображение.\n"
            "Возможные причины:\n"
            "1. Недостаточно средств на балансе AI Tunnel\n"
            "2. Промпт содержит запрещенный контент\n"
            "3. Технические проблемы у провайдера\n"
            "Попробуйте другой промпт или проверьте баланс на AI Tunnel."
        )


@dp.message()
async def handle_text(message: types.Message):
    """Обработчик простого текста (без команды)"""
    prompt = message.text.strip()
    if prompt:
        await message.answer(
            f"Чтобы создать изображение, используйте команду /generate\n"
            f"Например: `/generate {prompt[:50]}{'...' if len(prompt) > 50 else ''}`"
        )


# ========== ЗАПУСК БОТА ==========
async def main():
    logger.info("🤖 Starting PixelMage Bot with AI Tunnel...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())