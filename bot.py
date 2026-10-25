import asyncio
import html
import json
import logging
import os
import re
import time
from collections import defaultdict
from pathlib import Path

import aiohttp
from bs4 import BeautifulSoup
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    LinkPreviewOptions,
)

# Set BOT_TOKEN in your environment. Do not paste bot tokens into source code.
BOT_TOKEN = os.getenv("8642779036:AAEwpCQ-i26_YMX9mz_nuBZckL9TjWoqcug")
if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN is not set. Create a new token with @BotFather and set "
        "the BOT_TOKEN environment variable."
    )

# Keep your existing administrator IDs here if these accounts are yours.
SUPER_ADMIN_IDS = [8975317102, 629408999, 6973187474, 6217489845]

ALLOWED_FILE = Path("allowed_users.json")
ALL_USERS_FILE = Path("all_users.json")
CONFIG_FILE = Path("config.json")

USER_COOLDOWN_SECONDS = 3.0
user_last_request = defaultdict(float)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("nft_offer_bot")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


class AdminStates(StatesGroup):
    waiting_for_grant_id = State()
    waiting_for_revoke_id = State()
    waiting_for_broadcast_text = State()
    waiting_for_nft_url = State()


def load_json_set(path: Path) -> set[int]:
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {int(item) for item in data}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        logger.exception("Could not load %s", path)
        return set()


def save_json_set(path: Path, data: set[int]) -> None:
    path.write_text(
        json.dumps(sorted(data), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {"nft_url": "https://t.me/nft"}
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"nft_url": "https://t.me/nft"}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        logger.exception("Could not load config")
        return {"nft_url": "https://t.me/nft"}


def save_config(config: dict) -> None:
    CONFIG_FILE.write_text(
        json.dumps(config, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


allowed_users = load_json_set(ALLOWED_FILE)
all_users = load_json_set(ALL_USERS_FILE)


def register_user(user_id: int) -> None:
    if user_id not in all_users:
        all_users.add(user_id)
        save_json_set(ALL_USERS_FILE, all_users)


def is_authorized(user_id: int) -> bool:
    return user_id in SUPER_ADMIN_IDS or user_id in allowed_users


def normalize_slug(raw_input: str) -> str:
    value = raw_input.strip()
    for marker in ("https://t.me/nft/", "http://t.me/nft/", "t.me/nft/",
                   "https://fragment.com/gift/", "http://fragment.com/gift/",
                   "fragment.com/gift/"):
        if marker in value:
            value = value.split(marker, 1)[1]
            break
    value = value.split("?", 1)[0].strip("/")
    value = value.replace("#", "-")
    return re.sub(r"\s+", "", value)


def format_fallback_name(slug: str) -> str:
    if "-" in slug:
        name, number = slug.rsplit("-", 1)
        name = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
        return f"{name} #{number}"
    return slug


def parse_query_and_price(raw_text: str) -> tuple[str, str]:
    tokens = raw_text.strip().split()
    if not tokens:
        return "MirageLamp-44307", "2 727 ⭐️"

    nft_parts, price_parts = [], []
    for token in tokens:
        if price_parts or re.match(r"^\d", token):
            price_parts.append(token)
        else:
            nft_parts.append(token)

    raw_nft = "".join(nft_parts) if nft_parts else tokens[0]
    slug = normalize_slug(raw_nft)
    price_text = " ".join(price_parts).lower()

    ton_match = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:ton|тон|💎)", price_text)
    star_match = re.search(r"(\d+)\s*(?:star|stars|звезд|звёзд|зв|⭐️|⭐|\*)", price_text)

    if ton_match:
        return slug, f"{ton_match.group(1)} 💎 TON"
    if star_match:
        amount = f"{int(star_match.group(1)):,}".replace(",", " ")
        return slug, f"{amount} ⭐️"
    number_match = re.search(r"(\d+(?:[.,]\d+)?)", price_text)
    if number_match:
        return slug, f"{number_match.group(1)} ⭐️"
    return slug, "2 727 ⭐️"


async def fetch_real_nft_data(slug: str) -> dict:
    url = f"https://t.me/nft/{slug}"
    fallback = {
        "slug": slug,
        "url": url,
        "name": format_fallback_name(slug),
        "thumb": "https://telegram.org/img/t_logo.png",
    }
    headers = {"User-Agent": "Mozilla/5.0"}
    timeout = aiohttp.ClientTimeout(total=8)

    try:
        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
            async with session.get(url, allow_redirects=True) as response:
                if response.status != 200:
                    return fallback
                page = await response.text()

        soup = BeautifulSoup(page, "html.parser")
        title = soup.find("meta", property="og:title")
        image = soup.find("meta", property="og:image")
        if not title or not title.get("content") or title["content"] == "Telegram":
            return fallback

        fallback["name"] = title["content"].strip()[:100]
        if image and image.get("content"):
            fallback["thumb"] = image["content"]
        return fallback
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        logger.exception("NFT page lookup failed for slug %s", slug)
        return fallback


def format_offer_card(data: dict, price_display: str) -> str:
    safe_name = html.escape(data["name"])
    safe_price = html.escape(price_display)
    safe_url = html.escape(data["url"], quote=True)
    return (
        f"🎁 <b>Предложение по NFT</b>\n\n"
        f"Цена: <b>{safe_price}</b>\n"
        f"Подарок: <a href='{safe_url}'>{safe_name}</a>\n\n"
        "Проверьте подарок и условия сделки самостоятельно перед подтверждением.\n"
        f"{safe_url}"
    )


def get_offer_keyboard(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔎 Открыть NFT в Telegram", url=url)],
    ])


def valid_http_url(value: str) -> bool:
    return value.startswith(("https://", "http://"))


@dp.message(F.text.in_({"/start", "/id"}))
async def cmd_start(message: Message):
    if not message.from_user:
        return
    register_user(message.from_user.id)
    uid = message.from_user.id
    status = (
        "✅ У вас есть доступ."
        if is_authorized(uid)
        else "❌ Доступ не выдан. Обратитесь к администратору."
    )
    text = (
        "🎁 <b>Помощник для карточек NFT</b>\n\n"
        f"Ваш Telegram ID: <code>{uid}</code>\n"
        f"{status}\n\n"
        "Команда:\n"
        "• <code>/ar [ссылка или slug] [цена]</code> — создать карточку NFT\n"
    )
    if uid in SUPER_ADMIN_IDS:
        text += "• <code>/admins</code> — панель администратора"
    await message.answer(text, parse_mode="HTML")


@dp.message(F.text == "/admins")
async def cmd_admins(message: Message):
    if not message.from_user or message.from_user.id not in SUPER_ADMIN_IDS:
        await message.answer("❌ У вас нет прав администратора.")
        return

    config = load_config()
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Список доступов", callback_data="admin_list")],
        [InlineKeyboardButton(text="🔗 Изменить ссылку NFT", callback_data="admin_set_url")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast_start")],
        [
            InlineKeyboardButton(text="➕ Выдать доступ", callback_data="admin_grant_start"),
            InlineKeyboardButton(text="❌ Забрать доступ", callback_data="admin_revoke_start"),
        ],
    ])
    await message.answer(
        "👑 <b>Админ-панель</b>\n\n"
        f"Всего пользователей: <b>{len(all_users)}</b>\n"
        f"С доступом: <b>{len(allowed_users)}</b>\n"
        f"Ссылка NFT: <code>{html.escape(config.get('nft_url', 'https://t.me/nft'))}</code>",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


def admin_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_back")]
    ])


@dp.callback_query(F.data == "admin_back")
async def admin_back(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in SUPER_ADMIN_IDS:
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.clear()
    config = load_config()
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Список доступов", callback_data="admin_list")],
        [InlineKeyboardButton(text="🔗 Изменить ссылку NFT", callback_data="admin_set_url")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast_start")],
        [
            InlineKeyboardButton(text="➕ Выдать доступ", callback_data="admin_grant_start"),
            InlineKeyboardButton(text="❌ Забрать доступ", callback_data="admin_revoke_start"),
        ],
    ])
    if callback.message:
        await callback.message.edit_text(
            "👑 <b>Админ-панель</b>\n\n"
            f"Всего пользователей: <b>{len(all_users)}</b>\n"
            f"С доступом: <b>{len(allowed_users)}</b>\n"
            f"Ссылка NFT: <code>{html.escape(config.get('nft_url', 'https://t.me/nft'))}</code>",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    await callback.answer()


@dp.callback_query(F.data == "admin_list")
async def admin_list(callback: CallbackQuery):
    if callback.from_user.id not in SUPER_ADMIN_IDS:
        await callback.answer("Нет доступа", show_alert=True)
        return
    users = "\n".join(f"• <code>{uid}</code>" for uid in sorted(allowed_users))
    if not users:
        users = "Список пуст"
    if callback.message:
        await callback.message.edit_text(
            f"📋 <b>Пользователи с доступом:</b>\n\n{users}",
            parse_mode="HTML",
            reply_markup=admin_back_keyboard(),
        )
    await callback.answer()


@dp.callback_query(F.data == "admin_set_url")
async def admin_set_url(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in SUPER_ADMIN_IDS:
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_for_nft_url)
    config = load_config()
    if callback.message:
        await callback.message.edit_text(
            "Отправьте новую ссылку, начинающуюся с https:// или http://:",
            reply_markup=admin_back_keyboard(),
        )
    await callback.answer()


@dp.callback_query(F.data == "admin_grant_start")
async def admin_grant_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in SUPER_ADMIN_IDS:
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_for_grant_id)
    if callback.message:
        await callback.message.edit_text(
            "Введите числовой Telegram ID пользователя:",
            reply_markup=admin_back_keyboard(),
        )
    await callback.answer()


@dp.callback_query(F.data == "admin_revoke_start")
async def admin_revoke_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in SUPER_ADMIN_IDS:
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_for_revoke_id)
    if callback.message:
        await callback.message.edit_text(
            "Введите числовой Telegram ID пользователя:",
            reply_markup=admin_back_keyboard(),
        )
    await callback.answer()


@dp.callback_query(F.data == "admin_broadcast_start")
async def admin_broadcast_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in SUPER_ADMIN_IDS:
        await callback.answer("Нет доступа", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_for_broadcast_text)
    if callback.message:
        await callback.message.edit_text(
            "Введите текст рассылки. Отправляйте сообщения только пользователям, "
            "которые согласились их получать.",
            reply_markup=admin_back_keyboard(),
        )
    await callback.answer()


@dp.message(AdminStates.waiting_for_nft_url)
async def process_nft_url(message: Message, state: FSMContext):
    if not message.from_user or message.from_user.id not in SUPER_ADMIN_IDS:
        return
    new_url = (message.text or "").strip()
    if not valid_http_url(new_url):
        await message.answer("❌ Ссылка должна начинаться с https:// или http://.")
        return
    config = load_config()
    config["nft_url"] = new_url
    save_config(config)
    await message.answer(f"✅ Ссылка сохранена: {html.escape(new_url)}", parse_mode="HTML")
    await state.clear()


@dp.message(AdminStates.waiting_for_grant_id)
async def process_grant_id(message: Message, state: FSMContext):
    if not message.from_user or message.from_user.id not in SUPER_ADMIN_IDS:
        return
    try:
        target_id = int((message.text or "").strip())
        allowed_users.add(target_id)
        save_json_set(ALLOWED_FILE, allowed_users)
        await message.answer(f"✅ Доступ выдан пользователю <code>{target_id}</code>.", parse_mode="HTML")
        await state.clear()
    except ValueError:
        await message.answer("❌ Введите корректный числовой Telegram ID.")


@dp.message(AdminStates.waiting_for_revoke_id)
async def process_revoke_id(message: Message, state: FSMContext):
    if not message.from_user or message.from_user.id not in SUPER_ADMIN_IDS:
        return
    try:
        target_id = int((message.text or "").strip())
        if target_id in allowed_users:
            allowed_users.remove(target_id)
            save_json_set(ALLOWED_FILE, allowed_users)
            await message.answer(f"✅ Доступ у пользователя <code>{target_id}</code> отозван.", parse_mode="HTML")
        else:
            await message.answer("Пользователь не найден в списке выданных доступов.")
        await state.clear()
    except ValueError:
        await message.answer("❌ Введите корректный числовой Telegram ID.")


@dp.message(AdminStates.waiting_for_broadcast_text)
async def process_broadcast(message: Message, state: FSMContext):
    if not message.from_user or message.from_user.id not in SUPER_ADMIN_IDS:
        return
    text = (message.text or "").strip()
    if not text:
        await message.answer("Текст рассылки пуст. Отправьте текст или отмените действие.")
        return
    await state.clear()
    status = await message.answer("⏳ Начинаю рассылку с обработкой ограничений Telegram…")
    success = 0
    failed = 0

    for uid in list(all_users):
        try:
            await bot.send_message(uid, text, parse_mode="HTML")
            success += 1
            await asyncio.sleep(1.0)
        except TelegramRetryAfter as exc:
            # Respect Telegram's requested cooldown; do not hammer the API.
            await asyncio.sleep(float(exc.retry_after) + 1)
            try:
                await bot.send_message(uid, text, parse_mode="HTML")
                success += 1
            except TelegramAPIError:
                failed += 1
        except (TelegramForbiddenError, TelegramBadRequest):
            failed += 1
        except TelegramAPIError:
            logger.exception("Broadcast failed for user %s", uid)
            failed += 1

    await status.edit_text(
        f"📢 <b>Рассылка завершена</b>\n\n"
        f"Доставлено: <b>{success}</b>\n"
        f"Не доставлено: <b>{failed}</b>",
        parse_mode="HTML",
    )


async def execute_ar_command(message: Message, is_business: bool = False):
    if not message.from_user:
        return
    user_id = message.from_user.id
    register_user(user_id)

    if not is_authorized(user_id):
        await message.answer("❌ У вас нет доступа к этому боту.")
        return

    now = time.monotonic()
    if now - user_last_request[user_id] < USER_COOLDOWN_SECONDS:
        await message.answer("⏳ Подождите несколько секунд перед следующим запросом.")
        return
    user_last_request[user_id] = now

    command_text = message.text or ""
    args_text = re.sub(r"^/ar(@\w+)?\s*", "", command_text, flags=re.IGNORECASE).strip()
    slug, price_display = parse_query_and_price(args_text or "MirageLamp-44307 2727 stars")

    if not slug or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", slug):
        await message.answer("❌ Не удалось распознать NFT. Укажите slug или ссылку t.me/nft/…")
        return

    try:
        data = await fetch_real_nft_data(slug)
    except Exception:
        logger.exception("NFT lookup failed")
        await message.answer("⚠️ Не удалось получить данные NFT. Попробуйте позже.")
        return

    business_connection_id = (
        getattr(message, "business_connection_id", None) if is_business else None
    )
    preview_options = LinkPreviewOptions(
        is_disabled=False,
        prefer_large_media=True,
        show_above_text=True,
    )

    try:
        await bot.send_message(
            chat_id=message.chat.id,
            business_connection_id=business_connection_id,
            text=format_offer_card(data, price_display),
            parse_mode="HTML",
            link_preview_options=preview_options,
            reply_markup=get_offer_keyboard(data["url"]),
        )
    except TelegramRetryAfter as exc:
        logger.warning("Telegram rate limit: retry after %s seconds", exc.retry_after)
        await message.answer(
            f"⏳ Telegram временно ограничил отправку. Подождите {exc.retry_after} секунд."
        )
    except TelegramForbiddenError:
        logger.warning("Telegram forbade sending message in chat %s", message.chat.id)
        await message.answer(
            "⚠️ Telegram запретил отправку в этот чат. Проверьте права бота и Business-подключение."
        )
    except TelegramBadRequest as exc:
        logger.warning("Bad request while sending NFT card: %s", exc)
        # Avoid repeated sends or automatic retries for PEER_FLOOD.
        if "PEER_FLOOD" in str(exc).upper():
            await message.answer(
                "⚠️ Telegram временно ограничил отправку (PEER_FLOOD). "
                "Бот не будет повторять запрос автоматически. Попробуйте позже."
            )
        else:
            await message.answer("⚠️ Telegram отклонил сообщение. Проверьте ссылку и формат данных.")
    except TelegramAPIError:
        logger.exception("Telegram API error while sending NFT card")
        await message.answer("⚠️ Ошибка Telegram API. Подробности записаны в журнал.")
        return

    # Delete the command only if Telegram allows it. Failure does not trigger another send.
    try:
        if business_connection_id:
            await bot.delete_business_messages(
                business_connection_id=business_connection_id,
                message_ids=[message.message_id],
            )
        else:
            await message.delete()
    except TelegramAPIError as exc:
        logger.info("Could not delete command message: %s", exc)


@dp.business_message(F.text.startswith("/ar"))
async def handle_business_ar(message: Message):
    await execute_ar_command(message, is_business=True)


@dp.message(F.text.startswith("/ar"))
async def handle_regular_ar(message: Message):
    await execute_ar_command(message, is_business=False)


async def main():
    logger.info("Bot starting")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
