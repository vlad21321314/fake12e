import asyncio
import html
import json
import logging
import os
import re
from pathlib import Path

import aiohttp
from bs4 import BeautifulSoup
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramAPIError
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

# Install dependencies:
# pip install -U aiogram aiohttp beautifulsoup4
#
# Set BOT_TOKEN as an environment variable. Never put the token in this file.
BOT_TOKEN = os.getenv("", "").strip()
if not BOT_TOKEN:
    raise RuntimeError(
        "Не задан BOT_TOKEN. Добавьте новый токен в переменные окружения."
    )

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("nft_info_bot")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Replace these with your own Telegram numeric IDs.
SUPER_ADMIN_IDS = [8975317102, 629408999, 6973187474, 6217489845]

ALLOWED_FILE = Path("allowed_users.json")
ALL_USERS_FILE = Path("all_users.json")
CONFIG_FILE = Path("config.json")


class AdminStates(StatesGroup):
    waiting_for_grant_id = State()
    waiting_for_revoke_id = State()
    waiting_for_broadcast_text = State()
    waiting_for_nft_url = State()


def load_json_set(path: Path) -> set[int]:
    try:
        if path.exists():
            with path.open("r", encoding="utf-8") as file:
                values = json.load(file)
            return {int(value) for value in values}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        logger.exception("Не удалось прочитать %s", path)
    return set()


def save_json_set(path: Path, values: set[int]) -> None:
    with path.open("w", encoding="utf-8") as file:
        json.dump(sorted(values), file, ensure_ascii=False, indent=2)


def load_config() -> dict:
    try:
        if CONFIG_FILE.exists():
            with CONFIG_FILE.open("r", encoding="utf-8") as file:
                value = json.load(file)
            if isinstance(value, dict):
                return value
    except (OSError, ValueError, json.JSONDecodeError):
        logger.exception("Не удалось прочитать config.json")
    return {"nft_url": "https://t.me/"}


def save_config(config: dict) -> None:
    with CONFIG_FILE.open("w", encoding="utf-8") as file:
        json.dump(config, file, ensure_ascii=False, indent=2)


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
    for marker in ("t.me/nft/", "fragment.com/gift/"):
        if marker in value:
            value = value.split(marker, 1)[1].split("?", 1)[0].strip("/")
            break
    else:
        value = value.replace("#", "-")
    value = re.sub(r"\s+", "", value)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value):
        raise ValueError("Не удалось распознать ссылку или название NFT.")
    return value


def format_fallback_name(slug: str) -> str:
    match = re.fullmatch(r"(.+?)-(\d+)", slug)
    if not match:
        return slug
    raw_name, number = match.groups()
    spaced_name = re.sub(r"([a-z])([A-Z])", r"\1 \2", raw_name)
    return f"{spaced_name} #{number}"


def parse_query_and_price(raw_text: str) -> tuple[str, str, str]:
    tokens = raw_text.strip().split()
    if not tokens:
        raise ValueError(
            "Использование: /offer MirageLamp-44307 2727 stars\n"
            "Или: /offer https://t.me/nft/MirageLamp-44307 2.5 TON"
        )

    # Find the first token beginning with a number; everything after it is price.
    price_index = next(
        (i for i, token in enumerate(tokens) if re.match(r"^\d", token)),
        len(tokens),
    )
    nft_text = "".join(tokens[:price_index]) if price_index else tokens[0]
    price_tokens = tokens[price_index:] if price_index < len(tokens) else []
    slug = normalize_slug(nft_text or tokens[0])

    if not price_tokens:
        return slug, "Цена не указана", "Цена не указана"

    price_text = " ".join(price_tokens)
    lower_price = price_text.lower()
    ton_match = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:ton|тон|💎)", lower_price)
    star_match = re.search(
        r"(\d[\d ]*)\s*(?:star|stars|звезд|звёзд|зв|⭐️|⭐|\*)",
        lower_price,
    )
    if ton_match:
        amount = ton_match.group(1)
        return slug, f"{amount} 💎 TON", f"{amount} TON"
    if star_match:
        amount = f"{int(star_match.group(1).replace(' ', '')):,}".replace(",", " ")
        return slug, f"{amount} ⭐️ Звёзд", f"{amount} ⭐️"

    number = re.search(r"\d+(?:[.,]\d+)?", price_text)
    amount = number.group(0) if number else price_text
    return slug, f"{amount} (указанная цена)", amount


async def fetch_real_nft_data(slug: str) -> dict:
    url = f"https://t.me/nft/{slug}"
    fallback = {
        "slug": slug,
        "url": url,
        "name": format_fallback_name(slug),
        "thumb": "https://telegram.org/img/t_logo.png",
        "model": "Не указана",
        "backdrop": "Не указан",
    }
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        timeout = aiohttp.ClientTimeout(total=6)
        async with aiohttp.ClientSession(
            headers=headers, timeout=timeout
        ) as session:
            async with session.get(url) as response:
                if response.status != 200:
                    return fallback
                page_html = await response.text()

        soup = BeautifulSoup(page_html, "html.parser")
        title = soup.find("meta", property="og:title")
        image = soup.find("meta", property="og:image")
        if not title or not title.get("content") or title["content"] == "Telegram":
            return fallback

        details = {}
        for row in soup.find_all("tr"):
            th, td = row.find("th"), row.find("td")
            if th and td:
                details[th.get_text(" ", strip=True).lower()] = td.get_text(
                    " ", strip=True
                )

        fallback.update(
            {
                "name": title["content"].strip()[:100],
                "thumb": image.get("content", fallback["thumb"]) if image else fallback["thumb"],
                "model": details.get("model", "Не указана")[:50],
                "backdrop": details.get("backdrop", "Не указан")[:50],
            }
        )
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        logger.exception("Не удалось получить метаданные NFT %s", slug)
    return fallback


def format_parsed_card(data: dict, price_display: str) -> str:
    # Escape all scraped/user-controlled values before HTML formatting.
    url = html.escape(data["url"], quote=True)
    name = html.escape(data["name"])
    price = html.escape(price_display)
    model = html.escape(data.get("model", "Не указана"))
    backdrop = html.escape(data.get("backdrop", "Не указан"))
    return (
        "🎁 <b>Информация о NFT</b>\n\n"
        f"Подарок: <b><a href=\"{url}\">{name}</a></b>\n"
        f"Указанная цена: <b>{price}</b>\n"
        f"Модель: {model}\n"
        f"Фон: {backdrop}\n\n"
        "Это информационная карточка. Бот не подтверждает владение NFT, "
        "не принимает оплату и не проверяет передачу подарка. "
        "Проверьте сведения и условия самостоятельно."
    )


def get_offer_keyboard(slug: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Открыть NFT в Telegram", url=f"https://t.me/nft/{slug}")],
            [InlineKeyboardButton(text="Закрыть карточку", callback_data="close_info_card")],
        ]
    )


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📋 Список доступов", callback_data="admin_list")],
            [InlineKeyboardButton(text="🔗 Изменить ссылку", callback_data="admin_set_url")],
            [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast_start")],
            [
                InlineKeyboardButton(text="➕ Выдать доступ", callback_data="admin_grant_start"),
                InlineKeyboardButton(text="❌ Забрать доступ", callback_data="admin_revoke_start"),
            ],
        ]
    )


def admin_summary() -> str:
    config = load_config()
    current_url = html.escape(str(config.get("nft_url", "https://t.me/")), quote=True)
    return (
        "👑 <b>Админ-панель</b>\n\n"
        f"Всего пользователей: <b>{len(all_users)}</b>\n"
        f"С доступом: <b>{len(allowed_users)}</b>\n"
        f"🔗 Текущая ссылка: <code>{current_url}</code>"
    )


@dp.message(F.text.in_({"/start", "/id"}))
async def cmd_start(message: Message) -> None:
    if not message.from_user:
        return
    register_user(message.from_user.id)
    uid = message.from_user.id
    status = (
        "✅ <b>У вас есть доступ.</b>"
        if is_authorized(uid)
        else "❌ <b>Доступ не выдан. Обратитесь к администратору.</b>"
    )
    text = (
        "🎁 <b>Информационный помощник NFT</b>\n\n"
        f"🔑 Ваш Telegram ID: <code>{uid}</code>\n"
        f"{status}\n\n"
        "Команда: <code>/offer [ссылка NFT] [цена]</code>"
    )
    if uid in SUPER_ADMIN_IDS:
        text += "\nКоманда администратора: <code>/admins</code>"
    await message.answer(text, parse_mode="HTML")


@dp.message(F.text == "/admins")
async def cmd_admins(message: Message) -> None:
    if not message.from_user or message.from_user.id not in SUPER_ADMIN_IDS:
        await message.answer("❌ У вас нет прав администратора.")
        return
    register_user(message.from_user.id)
    await message.answer(admin_summary(), parse_mode="HTML", reply_markup=admin_keyboard())


@dp.callback_query(F.data == "admin_list")
async def cb_admin_list(callback: CallbackQuery) -> None:
    if callback.from_user.id not in SUPER_ADMIN_IDS:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    users = "\n".join(f"• <code>{uid}</code>" for uid in sorted(allowed_users)) or "Список пуст"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_back")]
    ])
    if callback.message:
        await callback.message.edit_text(f"📋 <b>Доступы:</b>\n\n{users}", parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data == "admin_back")
async def cb_admin_back(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id not in SUPER_ADMIN_IDS:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await state.clear()
    if callback.message:
        await callback.message.edit_text(admin_summary(), parse_mode="HTML", reply_markup=admin_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "admin_set_url")
async def cb_set_url(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id not in SUPER_ADMIN_IDS:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_for_nft_url)
    current_url = html.escape(str(load_config().get("nft_url", "https://t.me/")), quote=True)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Отмена", callback_data="admin_back")]
    ])
    if callback.message:
        await callback.message.edit_text(
            f"Текущая ссылка: <code>{current_url}</code>\n\nОтправьте новую ссылку:",
            parse_mode="HTML",
            reply_markup=kb,
        )
    await callback.answer()


@dp.callback_query(F.data == "admin_grant_start")
async def cb_grant_start(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id not in SUPER_ADMIN_IDS:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_for_grant_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Отмена", callback_data="admin_back")]
    ])
    if callback.message:
        await callback.message.edit_text("Введите числовой Telegram ID пользователя:", reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data == "admin_revoke_start")
async def cb_revoke_start(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id not in SUPER_ADMIN_IDS:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_for_revoke_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Отмена", callback_data="admin_back")]
    ])
    if callback.message:
        await callback.message.edit_text("Введите числовой Telegram ID пользователя:", reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data == "admin_broadcast_start")
async def cb_broadcast_start(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id not in SUPER_ADMIN_IDS:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_for_broadcast_text)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Отмена", callback_data="admin_back")]
    ])
    if callback.message:
        await callback.message.edit_text(
            "Введите текст рассылки пользователям, которые ранее запускали бота:",
            reply_markup=kb,
        )
    await callback.answer()


@dp.message(AdminStates.waiting_for_nft_url)
async def process_nft_url(message: Message, state: FSMContext) -> None:
    if not message.from_user or message.from_user.id not in SUPER_ADMIN_IDS:
        return
    new_url = (message.text or "").strip()
    if not re.match(r"^https?://", new_url, flags=re.I):
        await message.answer("Нужна полная ссылка, начинающаяся с https:// или http://")
        return
    config = load_config()
    config["nft_url"] = new_url
    save_config(config)
    await message.answer("✅ Ссылка сохранена.")
    await state.clear()


@dp.message(AdminStates.waiting_for_grant_id)
async def process_grant_id(message: Message, state: FSMContext) -> None:
    if not message.from_user or message.from_user.id not in SUPER_ADMIN_IDS:
        return
    try:
        target_id = int((message.text or "").strip())
        allowed_users.add(target_id)
        save_json_set(ALLOWED_FILE, allowed_users)
        await message.answer(f"✅ Доступ выдан пользователю <code>{target_id}</code>.", parse_mode="HTML")
    except ValueError:
        await message.answer("❌ Введите числовой Telegram ID.")
        return
    await state.clear()


@dp.message(AdminStates.waiting_for_revoke_id)
async def process_revoke_id(message: Message, state: FSMContext) -> None:
    if not message.from_user or message.from_user.id not in SUPER_ADMIN_IDS:
        return
    try:
        target_id = int((message.text or "").strip())
    except ValueError:
        await message.answer("❌ Введите числовой Telegram ID.")
        return
    if target_id in allowed_users:
        allowed_users.remove(target_id)
        save_json_set(ALLOWED_FILE, allowed_users)
        await message.answer(f"✅ Доступ у пользователя <code>{target_id}</code> отозван.", parse_mode="HTML")
    else:
        await message.answer("⚠️ Этого ID нет в списке выданных доступов.")
    await state.clear()


@dp.message(AdminStates.waiting_for_broadcast_text)
async def process_broadcast(message: Message, state: FSMContext) -> None:
    if not message.from_user or message.from_user.id not in SUPER_ADMIN_IDS:
        return
    broadcast_text = (message.text or "").strip()
    if not broadcast_text:
        await message.answer("Текст рассылки пуст.")
        return
    await state.clear()
    status = await message.answer("⏳ Рассылка запущена...")
    success = 0
    failed = 0
    for uid in sorted(all_users):
        try:
            await bot.send_message(uid, broadcast_text)
            success += 1
        except TelegramAPIError:
            failed += 1
            logger.warning("Рассылка не доставлена пользователю %s", uid, exc_info=True)
        await asyncio.sleep(0.5)
    await status.edit_text(
        f"📢 Рассылка завершена.\n\n✅ Доставлено: {success}\n❌ Ошибок: {failed}"
    )


async def execute_offer(message: Message) -> None:
    if not message.from_user:
        return
    register_user(message.from_user.id)
    if not is_authorized(message.from_user.id):
        # For a business message, reply via the same business connection when available.
        if message.business_connection_id:
            await bot.send_message(
                chat_id=message.chat.id,
                business_connection_id=message.business_connection_id,
                text="❌ У вас нет доступа к использованию этого бота.",
            )
        else:
            await message.answer("❌ У вас нет доступа к использованию этого бота.")
        return

    if not message.business_connection_id:
        await message.answer(
            "⚠️ Используйте /offer в чате, подключённом через Telegram Business."
        )
        return

    args = re.sub(r"^/offer(?:@\w+)?\s*", "", message.text or "", flags=re.I).strip()
    try:
        slug, price_display, _ = parse_query_and_price(args)
        data = await fetch_real_nft_data(slug)
    except ValueError as exc:
        await bot.send_message(
            chat_id=message.chat.id,
            business_connection_id=message.business_connection_id,
            text=html.escape(str(exc)),
        )
        return

    try:
        await bot.send_message(
            chat_id=message.chat.id,
            business_connection_id=message.business_connection_id,
            text=format_parsed_card(data, price_display),
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(
                is_disabled=False,
                prefer_large_media=True,
                show_above_text=True,
            ),
            reply_markup=get_offer_keyboard(slug),
        )
    except TelegramAPIError as exc:
        error_text = str(exc)
        if "PEER_FLOOD" in error_text.upper():
            # Do not retry or send another message: that can trigger more flood errors.
            logger.error(
                "Telegram returned PEER_FLOOD for business message in chat_id=%s. "
                "No retry was attempted. Wait and try manually later.",
                message.chat.id,
            )
            return

        logger.exception(
            "Не удалось отправить информационную карточку в chat_id=%s",
            message.chat.id,
        )
        # For other errors, make one best-effort notice; never loop/retry the card.
        try:
            await bot.send_message(
                chat_id=message.chat.id,
                business_connection_id=message.business_connection_id,
                text=(
                    "⚠️ Telegram не принял карточку. "
                    "Причина записана в журнал запуска бота."
                ),
            )
        except TelegramAPIError:
            logger.exception("Не удалось отправить уведомление об ошибке")
        return

    # Intentionally do not delete the command automatically. The previous
    # delete_business_messages call is not available in all installed aiogram
    # versions, and deletion permissions/method support vary.


@dp.business_message(F.text.startswith("/offer"))
async def handle_business_offer(message: Message) -> None:
    await execute_offer(message)


@dp.callback_query(F.data == "close_info_card")
async def close_info_card(callback: CallbackQuery) -> None:
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramAPIError:
            logger.exception("Не удалось убрать кнопки информационной карточки")
    await callback.answer("Карточка закрыта.")


async def main() -> None:
    logger.info("Информационный NFT-бот запускается")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
