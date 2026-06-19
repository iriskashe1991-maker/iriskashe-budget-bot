"""
Финансовый бот @iriskashe — «не по сценарию».
Считает бюджет по данным пользователя и присылает Excel-шаблон.

Запуск:
    export BOT_TOKEN="токен_от_BotFather"
    pip install -r requirements.txt
    python bot.py
"""

import os
import math
import time
import uuid
import asyncio
import logging

import httpx

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ConversationHandler, MessageHandler, ContextTypes, filters,
)

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
EXCEL_PATH = os.path.join(os.path.dirname(__file__), "Бюджет_шаблон.xlsx")
# Ссылка на шаблон в Google Таблицах (удобно для телефонов). Если задана —
# бот пришлёт ссылку «сделать копию» вместе с файлом.
GSHEET_LINK = os.environ.get("GSHEET_LINK", "")

# GigaChat (ИИ «Ворчливый финдир»). GIGACHAT_KEY — Authorization key из кабинета GigaChat.
GIGACHAT_KEY = os.environ.get("GIGACHAT_KEY", "")
GIGACHAT_SCOPE = os.environ.get("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")

SAVINGS_RATE = 0.20          # рекомендованный % накоплений
DEFAULT_CUSHION_MONTHS = 6   # запас в месяцах для подушки

INCOME, OBLIG, VAR, MONTHS, LEAKS = range(5)

GRUMPY_SYSTEM = (
    "Ты — «Ворчливый финдир», ИИ-помощник из Telegram-бота финансового блогера "
    "Иришки (@iriskashe). Характер: ворчишь с юмором и лёгким сарказмом, но по-доброму, "
    "как подруга-финдир. Тебе присылают список трат. Ты: 1) считаешь общую сумму; "
    "2) находишь 2–3 главные «дыры» — куда утекает больше всего; 3) ворчливо, но с любовью "
    "комментируешь; 4) даёшь 1–2 конкретных совета, где и сколько реально урезать без страданий; "
    "5) прикидываешь, сколько в месяц можно отправлять в накопления; 6) если остаются свободные "
    "деньги — по-доброму подтолкни не держать их «под подушкой», а положить на накопительный счёт "
    "или короткий вклад, чтобы капал процент и инфляция не съедала, и советуешь сравнить условия "
    "в своём банке. Пиши коротко и живо, на «ты», эмодзи в меру (🩷👀😤). Без занудства и нотаций. "
    "В конце ОБЯЗАТЕЛЬНО добавь отдельной строкой: "
    "«Я ИИ и слегка ворчу — это не личная финансовая рекомендация 🩷». "
    "ВАЖНО: НЕ называй конкретные банки, бренды, продукты или акции и НЕ указывай конкретные "
    "проценты/ставки (ты их не знаешь, они меняются) — говори только общим принципом."
)

_giga = {"token": None, "exp": 0.0}


async def _giga_token() -> str:
    """Получаем и кэшируем access_token GigaChat (живёт ~30 мин)."""
    if _giga["token"] and time.time() < _giga["exp"] - 60:
        return _giga["token"]
    async with httpx.AsyncClient(verify=False, timeout=30) as c:
        r = await c.post(
            "https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
            headers={
                "Authorization": f"Basic {GIGACHAT_KEY}",
                "RqUID": str(uuid.uuid4()),
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"scope": GIGACHAT_SCOPE},
        )
        r.raise_for_status()
        _giga["token"] = r.json()["access_token"]
        _giga["exp"] = time.time() + 25 * 60
        return _giga["token"]


async def grumpy_analyze(text: str) -> str:
    """Отправляем траты «Ворчливому финдиру» и получаем разбор."""
    token = await _giga_token()
    async with httpx.AsyncClient(verify=False, timeout=60) as c:
        r = await c.post(
            "https://gigachat.devices.sberbank.ru/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={
                "model": "GigaChat",
                "messages": [
                    {"role": "system", "content": GRUMPY_SYSTEM},
                    {"role": "user", "content": text},
                ],
                "temperature": 0.8,
            },
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


def parse_money(text: str) -> float:
    """Достаём число из текста: '120 000 руб', '120000', '120к' и т.п."""
    t = text.lower().replace(" ", "").replace("руб", "").replace("₽", "").replace(",", ".")
    mult = 1
    if t.endswith("к") or t.endswith("k") or t.endswith("т"):
        mult = 1000
        t = t[:-1]
    cleaned = "".join(ch for ch in t if ch.isdigit() or ch == ".")
    if not cleaned:
        raise ValueError("не число")
    return float(cleaned) * mult


def rub(x: float) -> str:
    return f"{int(round(x)):,}".replace(",", " ") + " ₽"


# ---------- /start ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Посчитать мой бюджет", callback_data="calc")],
        [InlineKeyboardButton("🔍 Куда утекли деньги", callback_data="leaks")],
        [InlineKeyboardButton("📥 Получить Excel-шаблон", callback_data="excel")],
    ])
    await update.message.reply_text(
        "Привет! Я финансовый бот Иришки 🤩\n\n"
        "Я финдир в кармане: посчитаю твой бюджет за минуту и покажу, "
        "сколько реально откладывать и за сколько ты соберёшь подушку.\n\n"
        "С чего начнём?",
        reply_markup=kb,
    )


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "excel":
        await send_excel(query, context)
        return ConversationHandler.END
    if query.data == "calc":
        await query.message.reply_text(
            "Окей, считаем 💪\n\nСколько у тебя доходов в месяц? "
            "(зарплата + подработки, одной суммой)\n\nНапиши, например: 120000 или 120к",
        )
        return INCOME
    if query.data == "leaks":
        await query.message.reply_text(
            "Окей, неси свои траты 👀\n\nМожно списком, через запятую или просто словами, как помнишь — "
            "я разберусь. Например:\n\n"
            "кофе 1800\nдоставка 4200\nтакси 2600\nмаркетплейс 5300\nподписки 1400\n\n"
            "или: «за неделю кофе тысячи 2, такси не помню, маркетплейс много» — тоже сойдёт.\n\n"
            "Я поворчу и найду, куда утекает 🩷",
        )
        return LEAKS


# ---------- расчёт ----------
async def get_income(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["income"] = parse_money(update.message.text)
    except ValueError:
        await update.message.reply_text("Не поняла сумму 🙈 Напиши числом, например 120000")
        return INCOME
    await update.message.reply_text(
        "Принято! Теперь обязательные расходы в месяц — "
        "аренда/ипотека, коммуналка, кредиты, продукты, транспорт. Одной суммой:",
    )
    return OBLIG


async def get_oblig(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["oblig"] = parse_money(update.message.text)
    except ValueError:
        await update.message.reply_text("Напиши числом, например 65000")
        return OBLIG
    await update.message.reply_text(
        "Отлично. А переменные расходы — кафе, развлечения, одежда, красота, прочее. Одной суммой:",
    )
    return VAR


async def get_var(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["var"] = parse_money(update.message.text)
    except ValueError:
        await update.message.reply_text("Напиши числом, например 20000")
        return VAR
    await update.message.reply_text(
        "Последнее: на сколько месяцев хочешь подушку?\n"
        "Стабильный доход — 3–6, нестабильный — 6–12. Напиши число (например 6):",
    )
    return MONTHS


async def get_months(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        months = int(parse_money(update.message.text))
        if months <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Напиши число месяцев, например 6")
        return MONTHS

    d = context.user_data
    income, oblig, var = d["income"], d["oblig"], d["var"]
    expenses = oblig + var
    free = income - expenses
    cushion_target = oblig * months

    def term(m):
        """Срок в месяцах → красивая строка с годами."""
        if m < 12:
            return f"{m} мес"
        years, rem = m // 12, m % 12
        return f"{years} г {rem} мес" if rem else f"{years} г"

    msg = [
        "*Готово! Вот твой расклад 👇*\n",
        f"💰 Доходы: *{rub(income)}*",
        f"🧾 Расходы: *{rub(expenses)}*  (обязательные {rub(oblig)} + переменные {rub(var)})",
        f"🟢 Свободно в месяц: *{rub(free)}*",
        f"🛟 Цель подушки ({months} мес расходов): *{rub(cushion_target)}*\n",
        "*За сколько соберёшь подушку, если откладывать:*",
    ]
    for r in (0.10, 0.20, 0.30):
        save = income * r
        line = f"• {int(r * 100)}% = {rub(save)}/мес → "
        if save > 0:
            line += f"*{term(math.ceil(cushion_target / save))}*"
            if save > free:
                line += " ⚠️ больше, чем свободно"
        else:
            line += "—"
        msg.append(line)

    if free <= 0:
        msg.append("\n⚠️ Сейчас расходы съедают весь доход — сначала ужми переменные траты, потом копи.")
    else:
        msg.append("\nНачни с того %, что реально тянешь, и постепенно повышай 🩷")

    msg.append(
        "\n💡 *Лайфхак, чтобы копить незаметно:* подключи в приложении банка "
        "округление покупок до 50 или 100 ₽ — разницу банк сам отправляет в копилку "
        "(или инвесткопилку). Капает сверху к твоему проценту, в среднем +1000–2000 ₽ "
        "в месяц без усилий. Деньги копятся, пока ты живёшь 🙂"
    )

    msg.append("\nЛови мой шаблон, чтобы вести деньги 👇")

    await update.message.reply_text("\n".join(msg), parse_mode="Markdown")
    await send_excel(update, context)

    await update.message.reply_text(
        "Это бесплатно 🩷 Если было полезно — подпишись на @iriskashe, "
        "там я честно показываю путь финдира из найма к свободе.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


async def get_leaks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not GIGACHAT_KEY:
        await update.message.reply_text(
            "ИИ-разбор пока не подключён 🙈 Загляни позже. А пока жми /start и посчитай бюджет."
        )
        return ConversationHandler.END
    await update.message.reply_text("Так, секунду, разбираю твои траты... 👀")
    try:
        reply = await grumpy_analyze(update.message.text)
    except Exception:
        logging.exception("GigaChat error")
        await update.message.reply_text(
            "Упс, мой ворчливый мозг сейчас недоступен 😤 Попробуй ещё раз чуть позже."
        )
        return ConversationHandler.END
    await update.message.reply_text(reply)
    await update.message.reply_text(
        "Хочешь ещё разбор или посчитать бюджет — жми /start 🩷 И подпишись на @iriskashe.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


async def send_excel(target, context: ContextTypes.DEFAULT_TYPE):
    chat = target.message.chat if hasattr(target, "message") else target.effective_chat
    if GSHEET_LINK:
        await context.bot.send_message(
            chat.id,
            "📱 Удобнее с телефона? Открой шаблон в Google Таблицах и нажми «Создать копию» — "
            f"редактируется прямо в телефоне, ничего ставить не нужно:\n{GSHEET_LINK}",
        )
    if os.path.exists(EXCEL_PATH):
        with open(EXCEL_PATH, "rb") as f:
            await context.bot.send_document(
                chat_id=chat.id, document=f,
                filename="Бюджет_шаблон.xlsx",
                caption="Мой шаблон бюджета 🩷 Жёлтое — настройки, синее — твои цифры, остальное считается само.",
            )
    else:
        await context.bot.send_message(chat.id, "Шаблон временно недоступен — загляни в шапку профиля @iriskashe.")

    # Универсальная карманная версия — работает на любом телефоне без приложений
    await context.bot.send_message(
        chat.id,
        "📝 *Нет Excel или Таблиц под рукой?* Лови карманную версию — "
        "скопируй в Заметки и заполняй прямо там:\n\n"
        "🩷 Карманный бюджет\n"
        "Доход: \n"
        "Обязательные расходы: \n"
        "Переменные расходы: \n"
        "Откладываю (10–30%): \n"
        "Цель подушки: \n"
        "Осталось накопить: ",
        parse_mode="Markdown",
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Окей, остановились. Напиши /start, когда захочешь продолжить.")
    return ConversationHandler.END


def main():
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN. Сделай: export BOT_TOKEN='токен_от_BotFather'")
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(on_button, pattern="^(calc|excel|leaks)$")],
        states={
            INCOME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_income)],
            OBLIG: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_oblig)],
            VAR: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_var)],
            MONTHS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_months)],
            LEAKS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_leaks)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv)

    # Python 3.12+/3.14: убеждаемся, что в главном потоке есть event loop
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    print("Бот запущен. Останови через Ctrl+C.")
    app.run_polling()


if __name__ == "__main__":
    main()
