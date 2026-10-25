import asyncio
import html
import json
import logging
import os
import re
import time
from pathlib import Path
from collections import defaultdict

import aiohttp
from bs4 import BeautifulSoup
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, LinkPreviewOptions

# =========================
# НАСТРОЙКИ
# =========================
# Вставь НОВЫЙ токен от @BotFather вместо PASTE_NEW_TOKEN_HERE.
# Либо задай переменную окружения BOT_TOKEN в панели хостинга.
BOT_TOKEN = os.getenv("BOT_TOKEN", "PASTE_NEW_TOKEN_HERE")

if not BOT_TOKEN or BOT_TOKEN == "PASTE_NEW_TOKEN_HERE":
    raise RuntimeError(
        "Не задан токен бота. Вставь новый токен в BOT_TOKEN в bot.py "
        "или создай переменную окружения BOT_TOKEN в настройках хостинга."
    )

# Оставлены ID администраторов из твоего исходного файла.
SUPER_ADMIN_IDS = {8975317102, 629408999, 6973187474, 6217489845}

ALLOWED_FILE = Path("allowed_users.json")
ALL_USERS_FILE = Path("all_users.json")
USER_COOLDOWN_SECONDS = 3.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("nft_card_bot")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

last_request_at = defaultdict(float)


# =========================
# ХРАНЕНИЕ ПОЛЬЗОВАТЕЛЕЙ
# =========================
def load_id_set(path: Path) -> set[int]:
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {int(x) for x in data}
    except (OSError, ValueError, TypeError):
        logger.exception("Не удалось прочитать %s", path)
        return set()


def save_id_set(path: Path, values: set[int]) -> None:
    path.write_text(
        json.dumps(sorted(values), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


allowed_users = load_id_set(ALLOWED_FILE)
all_users = load_id_set(ALL_USERS_FILE)


def register_user(user_id: int) -> None:
    if user_id not in all_users:
        all_users.add(user_id)
        save_id_set(ALL_USERS_FILE, all_users)


def is_authorized(user_id: int) -> bool:
    # Администраторы всегда имеют доступ; остальные должны быть добавлены админом.
    return user_id in SUPER_ADMIN_IDS or user_id in allowed_users


# =========================
# ОБРАБОТКА ССЫЛКИ И ЦЕНЫ
# =========================
def parse_offer_args(text: str) -> tuple[str, str]:
    """
    Формат:
      /offer https://t.me/nft/MirageLamp-76604 1000 звезд
      /offer MirageLamp-76604 1000 stars
    """
    args = re.sub(r"^/offer(?:@\w+)?\s*", "", text.strip(), flags=re.IGNORECASE).strip()
    if not args:
        raise ValueError(
            "Укажи ссылку на NFT и цену.\n"
            "Пример: /offer https://t.me/nft/MirageLamp-76604 1000 звезд"
        )

    # Ссылка/slug — первый аргумент, всё остальное — цена.
    parts = args.split(maxsplit=1)
    raw_nft = parts[0].strip()
    raw_price = parts[1].strip() if len(parts) > 1 else "1000 звезд"

    if raw_nft.startswith(("https://t.me/nft/", "http://t.me/nft/")):
        slug = raw_nft.split("/nft/", 1)[1].split("?", 1)[0].strip("/")
    elif raw_nft.startswith(("https://fragment.com/gift/", "http://fragment.com/gift/")):
        slug = raw_nft.split("/gift/", 1)[1].split("?", 1)[0].strip("/")
    elif raw_nft.startswith(("t.me/nft/", "fragment.com/gift/")):
        slug = raw_nft.split("/", 2)[-1].split("?", 1)[0].strip("/")
    else:
        slug = raw_nft.replace("#", "-")

    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", slug):
        raise ValueError("Не удалось распознать NFT. Проверь ссылку или slug.")

    price_match = re.search(r"(\d[\d\s.,]*)\s*(.*)", raw_price)
    if not price_match:
        raise ValueError("Не удалось распознать цену. Пример: 1000 звезд")

    amount = price_match.group(1).strip().replace(" ", "")
    unit_text = price_match.group(2).lower()

    if not amount:
        raise ValueError("Укажи числовую цену.")

    if any(word in unit_text for word in ("ton", "тон", "💎")):
        price_display = f"{amount} 💎 TON"
    else:
        # По умолчанию цена задаётся в Telegram Stars.
        digits = re.sub(r"\D", "", amount)
        if not digits:
            raise ValueError("Цена должна содержать число.")
        price_display = f"{int(digits):,}".replace(",", " ") + " ⭐️"

    return slug, price_display


def fallback_name(slug: str) -> str:
    if "-" in slug:
        name, number = slug.rsplit("-", 1)
        name = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
        return f"{name} #{number}"
    return slug


async def fetch_nft_data(slug: str) -> dict:
    url = f"https://t.me/nft/{slug}"
    result = {"url": url, "name": fallback_name(slug)}
    timeout = aiohttp.ClientTimeout(total=8)
    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(url, allow_redirects=True) as response:
                if response.status != 200:
                    return result
                page = await response.text()

        soup = BeautifulSoup(page, "html.parser")
        title = soup.find("meta", property="og:title")
        if title and title.get("content") and title["content"] != "Telegram":
            result["name"] = title["content"].strip()[:120]
    except (aiohttp.ClientError, asyncio.TimeoutError):
        logger.exception("Не удалось загрузить страницу NFT %s", slug)

    return result


def build_card(data: dict, price: str) -> str:
    safe_name = html.escape(data["name"])
    safe_price = html.escape(price)
    safe_url = html.escape(data["url"], quote=True)
    return (
        "🎁 <b>Карточка NFT</b>\n\n"
        f"Подарок: <a href=\"{safe_url}\">{safe_name}</a>\n"
        f"Цена: <b>{safe_price}</b>\n\n"
        "Перед сделкой самостоятельно проверь подарок, цену и получателя.\n"
        f"{safe_url}"
    )


def card_keyboard(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔎 Открыть NFT", url=url)]
    ])


# =========================
# КОМАНДЫ
# =========================
@dp.message(F.text.in_({"/start", "/id"}))
async def start_handler(message: Message):
    if not message.from_user:
        return
    register_user(message.from_user.id)
    status = "✅ Доступ разрешён" if is_authorized(message.from_user.id) else "❌ Доступ не выдан"
    text = (
        "🎁 <b>Генератор карточек NFT</b>\n\n"
        f"Ваш Telegram ID: <code>{message.from_user.id}</code>\n"
        f"{status}\n\n"
        "Команда:\n"
        "<code>/offer ссылка_на_NFT цена</code>\n\n"
        "Пример:\n"
        "<code>/offer https://t.me/nft/MirageLamp-76604 1000 звезд</code>"
    )
    if message.from_user.id in SUPER_ADMIN_IDS:
        text += "\n\nКоманда администратора: /admins"
    await message.answer(text, parse_mode="HTML")


@dp.message(F.text == "/admins")
async def admins_handler(message: Message):
    if not message.from_user or message.from_user.id not in SUPER_ADMIN_IDS:
        await message.answer("❌ Нет прав администратора.")
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Список доступов", callback_data="admin:list")],
        [InlineKeyboardButton(text="➕ Выдать доступ", callback_data="admin:grant")],
        [InlineKeyboardButton(text="➖ Забрать доступ", callback_data="admin:revoke")],
    ])
    await message.answer(
        f"👑 <b>Админ-панель</b>\n\nПользователей: {len(all_users)}\n"
        f"Пользователей с доступом: {len(allowed_users)}",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


@dp.callback_query(F.data == "admin:list")
async def admin_list(callback: CallbackQuery):
    if callback.from_user.id not in SUPER_ADMIN_IDS:
        await callback.answer("Нет доступа", show_alert=True)
        return
    ids = "\n".join(f"<code>{uid}</code>" for uid in sorted(allowed_users)) or "Список пуст"
    if callback.message:
        await callback.message.edit_text(
            f"📋 <b>Список доступов</b>\n\n{ids}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Закрыть", callback_data="admin:close")]
            ]),
        )
    await callback.answer()


@dp.callback_query(F.data.in_({"admin:grant", "admin:revoke"}))
async def admin_access_action(callback: CallbackQuery):
    if callback.from_user.id not in SUPER_ADMIN_IDS:
        await callback.answer("Нет доступа", show_alert=True)
        return
    action = "grant" if callback.data == "admin:grant" else "revoke"
    # Store the action for this administrator until their next ID message.
    admin_pending_action[callback.from_user.id] = action
    prompt = "Отправь числовой Telegram ID, которому выдать доступ." if action == "grant" else "Отправь числовой Telegram ID, у которого забрать доступ."
    if callback.message:
        await callback.message.answer(prompt)
    await callback.answer()


admin_pending_action: dict[int, str] = {}


@dp.callback_query(F.data == "admin:close")
async def admin_close(callback: CallbackQuery):
    if callback.message:
        await callback.message.delete()
    await callback.answer()


@dp.message(F.text.regexp(r"^\d+$"))
async def admin_process_id(message: Message):
    if not message.from_user or message.from_user.id not in SUPER_ADMIN_IDS:
        return
    action = admin_pending_action.pop(message.from_user.id, None)
    if not action:
        return
    target_id = int(message.text.strip())
    if action == "grant":
        allowed_users.add(target_id)
        save_id_set(ALLOWED_FILE, allowed_users)
        await message.answer(f"✅ Доступ выдан ID <code>{target_id}</code>.", parse_mode="HTML")
    else:
        allowed_users.discard(target_id)
        save_id_set(ALLOWED_FILE, allowed_users)
        await message.answer(f"✅ Доступ отозван у ID <code>{target_id}</code>.", parse_mode="HTML")


async def process_offer(message: Message, is_business: bool = False):
    if not message.from_user:
        return
    user_id = message.from_user.id
    register_user(user_id)

    if not is_authorized(user_id):
        await message.answer("❌ У вас нет доступа к этому боту. Обратитесь к администратору.")
        return

    now = time.monotonic()
    remaining = USER_COOLDOWN_SECONDS - (now - last_request_at[user_id])
    if remaining > 0:
        await message.answer(f"⏳ Подожди {remaining:.1f} сек. перед следующей карточкой.")
        return
    last_request_at[user_id] = now

    try:
        slug, price = parse_offer_args(message.text or "")
    except ValueError as exc:
        await message.answer(f"⚠️ {html.escape(str(exc))}", parse_mode="HTML")
        return

    data = await fetch_nft_data(slug)
    business_connection_id = getattr(message, "business_connection_id", None) if is_business else None

    try:
        await bot.send_message(
            chat_id=message.chat.id,
            business_connection_id=business_connection_id,
            text=build_card(data, price),
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(
                is_disabled=False,
                prefer_large_media=True,
                show_above_text=True,
            ),
            reply_markup=card_keyboard(data["url"]),
        )
    except TelegramRetryAfter as exc:
        logger.warning("Telegram rate limit; retry_after=%s", exc.retry_after)
        await message.answer(
            f"⚠️ Telegram временно ограничил отправку. Подожди {exc.retry_after} секунд; автоматических повторов не будет."
        )
    except TelegramForbiddenError:
        logger.exception("Telegram запрещает отправку в этот чат")
        await message.answer("⚠️ Telegram запретил отправку в этот чат. Проверь права бота и Business-подключение.")
    except TelegramBadRequest as exc:
        logger.warning("Telegram BadRequest: %s", exc)
        if "PEER_FLOOD" in str(exc).upper():
            await message.answer(
                "⚠️ Telegram вернул PEER_FLOOD — отправка временно ограничена на стороне Telegram. "
                "Бот не будет повторять запрос автоматически."
            )
        else:
            await message.answer("⚠️ Telegram отклонил сообщение. Проверь ссылку и формат.")
    except TelegramAPIError:
        logger.exception("Ошибка Telegram API")
        await message.answer("⚠️ Ошибка Telegram API. Подробности есть в журнале хостинга.")


@dp.message(F.text.startswith("/offer"))
async def offer_handler(message: Message):
    await process_offer(message, is_business=False)


@dp.business_message(F.text.startswith("/offer"))
async def business_offer_handler(message: Message):
    await process_offer(message, is_business=True)


async def main():
    logger.info("Бот запускается")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
