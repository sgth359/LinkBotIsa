import os, json, time, secrets, asyncio, logging
from aiohttp import web, ClientSession
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.types import (Message, CallbackQuery, InlineKeyboardMarkup,
                           InlineKeyboardButton, LinkPreviewOptions)

logging.basicConfig(level=logging.INFO)

TOKEN = os.environ["BOT_TOKEN"]
BASE_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
PORT = int(os.environ.get("PORT", 10000))
TTL = 600  # 10 минут
COOLDOWN = 1800  # 1 ссылка на человека на каждый ресурс раз в 30 минут (админы без лимита)

# Админы (Telegram ID через запятую). Свой ID: команда /id
ADMINS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
# Приватный канал/группа, где бот — админ. Там в закреплённом сообщении
# хранится список ресурсов (на бесплатном Render файлы стираются при перезапуске).
STORAGE_ID = os.environ.get("STORAGE_CHAT_ID", "").strip()

# Стартовый список, пока в хранилище пусто. Дальше правится командами админа.
DEFAULTS = [
    {"title": "💬 Чат", "type": "chat", "value": -1001234567890},
    {"title": "🌐 Сайт", "type": "url", "value": "https://example.com"},
]

bot = Bot(TOKEN)
dp = Dispatcher()
resources: list = []
store_msg_id = None
tokens = {}  # token -> (url, expires_at)
last_req = {}  # (user_id, ресурс) -> время последней выданной ссылки
bg = set()
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


def spawn(coro):
    t = asyncio.create_task(coro)
    bg.add(t)
    t.add_done_callback(bg.discard)


# ---------- хранилище ----------
async def save():
    global store_msg_id
    if not STORAGE_ID:
        return
    chat = int(STORAGE_ID)
    text = json.dumps(resources, ensure_ascii=False)
    if store_msg_id:
        try:
            await bot.edit_message_text(text, chat_id=chat, message_id=store_msg_id)
            return
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                return
    msg = await bot.send_message(chat, text)
    await bot.pin_chat_message(chat, msg.message_id, disable_notification=True)
    store_msg_id = msg.message_id


async def load():
    global resources, store_msg_id
    if STORAGE_ID:
        try:
            pm = (await bot.get_chat(int(STORAGE_ID))).pinned_message
            if pm and pm.text:
                resources = json.loads(pm.text)
                store_msg_id = pm.message_id
                return
        except Exception as e:
            logging.warning("load failed: %s", e)
    resources = list(DEFAULTS)
    try:
        await save()
    except Exception as e:
        logging.warning("save failed: %s", e)


# ---------- пользователи ----------
def menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=r["title"], callback_data=f"get:{i}")]
        for i, r in enumerate(resources)
    ])


async def delete_later(msg: Message):
    await asyncio.sleep(TTL)
    try:
        await msg.delete()
    except Exception:
        pass


@dp.message(Command("id"))
async def my_id(m: Message):
    await m.answer(f"Твой ID: {m.from_user.id}")


@dp.message(CommandStart())
async def start(m: Message):
    if not resources:
        return await m.answer("Ресурсов пока нет.")
    await m.answer("Выбери ресурс. Ссылка живёт 10 минут.", reply_markup=menu_kb())


@dp.callback_query(F.data.startswith("get:"))
async def get_link(c: CallbackQuery):
    try:
        r = resources[int(c.data[4:])]
    except (ValueError, IndexError):
        return await c.answer("Список обновился, нажми /start", show_alert=True)
    uid = c.from_user.id
    key = (uid, str(r["value"]))
    if uid not in ADMINS:
        left = last_req.get(key, 0) + COOLDOWN - time.time()
        if left > 0:
            return await c.answer(
                f"Эту ссылку можно взять снова через {int(left // 60) + 1} мин.",
                show_alert=True,
            )
        last_req[key] = time.time()
    if r["type"] == "chat":
        try:
            link = await bot.create_chat_invite_link(
                int(r["value"]), expire_date=int(time.time()) + TTL, member_limit=1
            )
        except TelegramBadRequest as e:
            logging.warning("invite failed: %s", e)
            last_req.pop(key, None)
            return await c.answer("Не удалось создать ссылку", show_alert=True)
        url = link.invite_link
    elif r["type"] == "tmp":
        t = secrets.token_urlsafe(16)
        tokens[t] = (r["value"], time.time() + TTL)
        url = f"{BASE_URL}/go/{t}"
    else:
        url = r["value"]
    if r["type"] in ("chat", "tmp"):
        note = "⏳ Действует 10 минут, потом сообщение удалится"
    else:
        note = "⏳ Сообщение удалится через 10 минут"
    sent = await c.message.answer(
        f"{r['title']}\n{url}\n{note}",
        link_preview_options=NO_PREVIEW,
    )
    spawn(delete_later(sent))
    await c.answer()


# ---------- админ ----------
HELP = (
    "Команды админа:\n"
    "/add Название | chat | -100123…  (ID чата, бот там админ)\n"
    "/add Название | url | https://…  (ссылка как есть)\n"
    "/add Название | tmp | https://…  (через бота, 10 минут)\n"
    "/edit N | Название | тип | значение\n"
    "/del N\n"
    "/admin — список и кнопки удаления"
)


def is_admin(m) -> bool:
    return m.from_user.id in ADMINS


def parse_item(parts):
    title, typ, val = [p.strip() for p in parts]
    if not title or typ not in ("chat", "url", "tmp"):
        raise ValueError
    if typ == "chat":
        val = int(val)
    elif not val.startswith("http"):
        raise ValueError
    return {"title": title, "type": typ, "value": val}


def admin_view():
    lines = [f"{i+1}. {r['title']} — {r['type']}: {r['value']}" for i, r in enumerate(resources)]
    text = ("Ресурсы:\n" + "\n".join(lines) if lines else "Список пуст.") + "\n\n" + HELP
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🗑 {i+1}. {r['title']}", callback_data=f"del:{i}")]
        for i, r in enumerate(resources)
    ])
    return text, kb


@dp.message(Command("admin"))
async def admin(m: Message):
    if not is_admin(m):
        return await m.answer("Нет прав.")
    text, kb = admin_view()
    await m.answer(text, reply_markup=kb, link_preview_options=NO_PREVIEW)


@dp.message(Command("add"))
async def add(m: Message, command: CommandObject):
    if not is_admin(m):
        return await m.answer("Нет прав.")
    try:
        item = parse_item((command.args or "").split("|"))
    except ValueError:
        return await m.answer("Формат: /add Название | chat или url | значение")
    resources.append(item)
    await save()
    await m.answer(f"Добавлено: {item['title']}")


@dp.message(Command("edit"))
async def edit(m: Message, command: CommandObject):
    if not is_admin(m):
        return await m.answer("Нет прав.")
    try:
        n, *rest = (command.args or "").split("|")
        idx = int(n) - 1
        if idx < 0:
            raise IndexError
        old = resources[idx]
        resources[idx] = parse_item(rest)
    except (ValueError, IndexError):
        return await m.answer("Формат: /edit N | Название | chat или url | значение")
    await save()
    await m.answer(f"Изменено: {resources[idx]['title']}")


@dp.message(Command("del"))
async def delete(m: Message, command: CommandObject):
    if not is_admin(m):
        return await m.answer("Нет прав.")
    try:
        idx = int(command.args) - 1
        if idx < 0:
            raise IndexError
        r = resources.pop(idx)
    except (TypeError, ValueError, IndexError):
        return await m.answer("Формат: /del N")
    await save()
    await m.answer(f"Удалено: {r['title']}")


@dp.callback_query(F.data.startswith("del:"))
async def del_btn(c: CallbackQuery):
    if c.from_user.id not in ADMINS:
        return await c.answer("Нет прав", show_alert=True)
    try:
        resources.pop(int(c.data[4:]))
    except (ValueError, IndexError):
        return await c.answer("Список уже изменился")
    await save()
    text, kb = admin_view()
    await c.message.edit_text(text, reply_markup=kb, link_preview_options=NO_PREVIEW)
    await c.answer("Удалено")


# ---------- веб-часть и автопинг ----------
async def go(req: web.Request):
    item = tokens.get(req.match_info["t"])
    if not item or item[1] < time.time():
        return web.Response(text="Ссылка истекла", status=410)
    raise web.HTTPFound(item[0])


async def health(_):
    return web.Response(text="ok")


async def keepalive():
    async with ClientSession() as s:
        while True:
            await asyncio.sleep(480)
            try:
                await s.get(f"{BASE_URL}/health")
            except Exception as e:
                logging.warning("ping failed: %s", e)
            now = time.time()
            for k in [k for k, v in tokens.items() if v[1] < now]:
                tokens.pop(k, None)
            for u in [u for u, t in last_req.items() if t + COOLDOWN < now]:
                last_req.pop(u, None)


async def main():
    app = web.Application()
    app.add_routes([web.get("/health", health), web.get("/go/{t}", go)])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    if BASE_URL:
        spawn(keepalive())
    await load()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
