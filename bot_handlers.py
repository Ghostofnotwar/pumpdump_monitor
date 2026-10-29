"""Telegram commands and menus. All runtime dependencies live in application.bot_data."""

from __future__ import annotations

import html
import logging
import time

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Update, WebAppInfo
from telegram.error import BadRequest, TelegramError

from monitor import market_links
from settings_rules import TIMEFRAMES
from snapshots import snapshot_path

logger = logging.getLogger(__name__)
BOT_COMMANDS = [
    BotCommand("start", "Главное меню"),
    BotCommand("settings", "Статус и настройки"),
    BotCommand("alerts", "Последние 5 алертов"),
    BotCommand("pause", "Приостановить уведомления"),
    BotCommand("resume", "Возобновить уведомления"),
    BotCommand("test", "Тестовый график BTCUSDT"),
    BotCommand("help", "Как работают сигналы"),
]


def deps(context):
    data = context.application.bot_data
    return data["config"], data["db"], data["monitor"]


async def authorized(update: Update, context, *, write: bool = False) -> bool:
    config, _, _ = deps(context)
    chat, user = update.effective_chat, update.effective_user
    if not chat or not user:
        return False
    if config.allowed_chat_ids and chat.id not in config.allowed_chat_ids:
        if update.effective_message:
            await update.effective_message.reply_text("Этот чат не добавлен в ALLOWED_CHAT_IDS.")
        return False
    if write and chat.type != "private":
        try:
            member = await context.bot.get_chat_member(chat.id, user.id)
        except TelegramError:
            await context.bot.send_message(chat.id, "Не удалось проверить права администратора. Повторите позже.")
            return False
        if member.status not in ("creator", "administrator"):
            return False
    return True


def main_menu(config):
    rows = []
    if config.web_app_url:
        rows.append([InlineKeyboardButton("🖥 Открыть монитор", web_app=WebAppInfo(url=config.web_app_url))])
    rows.extend(
        [
            [InlineKeyboardButton("⚙️ Статус и настройки", callback_data="settings")],
            [InlineKeyboardButton("📊 Последние алерты", callback_data="alerts")],
            [InlineKeyboardButton("❌ Закрыть меню", callback_data="close_menu")],
        ]
    )
    return InlineKeyboardMarkup(rows)


async def show(update: Update, text: str, keyboard=None):
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                text,
                reply_markup=keyboard,
                parse_mode="HTML",
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        except BadRequest as exc:
            if "Message is not modified" not in str(exc):
                raise
    elif update.effective_message:
        await update.effective_message.reply_text(
            text, reply_markup=keyboard, parse_mode="HTML", link_preview_options=LinkPreviewOptions(is_disabled=True)
        )


async def start(update: Update, context):
    if not await authorized(update, context, write=True):
        return
    config, db, _ = deps(context)
    await db.get_settings(update.effective_chat.id)
    await show(
        update,
        "🤖 <b>Pump &amp; Dump Monitor</b>\n\n"
        "Слежу за движением цены USDT-перпетуалов Bybit. Настройте направление, порог и объём.\n"
        "/help — как рассчитываются сигналы.",
        main_menu(config),
    )


async def help_cmd(update: Update, context):
    if not await authorized(update, context):
        return
    _, _, monitor = deps(context)
    await show(
        update,
        f"📖 <b>Как читать сигналы</b>\n\n"
        f"Изменение последней цены считается за <b>{monitor.config.alert_window_sec / 60:g} минут</b>, "
        "а не между соседними тиками. После запуска нужно прогреть историю.\n"
        "Таймфрейм в настройках меняет только свечи графика. Порог задаёт минимальную амплитуду "
        "в обе стороны; можно выбрать только PUMP или DUMP.\n"
        "Объём — оборот контракта за 24 часа в USDT, не размер отдельной сделки.\n"
        f"Антиспам: одно направление / пара / чат раз в {monitor.config.alert_cooldown_sec / 60:g} мин.\n"
        "Графики: реальные OHLCV. CVD и профиль объёма — явно помеченные приближения. "
        "Реальных OI и ликвидаций в этом источнике нет.\n\n"
        "Сигнал не гарантирует продолжение движения и не является инвестиционной рекомендацией.",
        InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ Настройки", callback_data="settings")]]),
    )


async def settings_callback(update: Update, context):
    if not await authorized(update, context):
        return
    _, db, monitor = deps(context)
    settings = await db.get_settings(update.effective_chat.id)
    status = monitor.status()
    activity = "\n".join(monitor.recent_activity) or "Движений пока нет / история прогревается"
    last_poll = status["last_poll"][11:19] + " UTC" if status["last_poll"] else "ещё нет"
    state = "🟢 Данные поступают" if status["ready"] else "🟡 Ожидание данных"
    text = (
        f"🖥 <b>{state}</b>\n"
        f"Пар: <b>{status['pairs']}/{status['max_pairs']}</b> · тики: {status['poll_interval_sec']:g} с\n"
        f"Окно: <b>{status['window_sec'] / 60:g} мин</b> · последний тик: {last_poll}\n"
        f"В работе: <code>{status['uptime_sec'] // 3600}ч {(status['uptime_sec'] % 3600) // 60}м</code> · "
        f"отправлено: {status['sent_alerts']}\n\n"
        f"📝 <b>Движения ≥1% за окно</b>\n<code>{html.escape(activity)}</code>\n\n"
        f"⚙️ Порог: <b>±{settings['pump_threshold']:g}%</b> · "
        f"режим: <b>{settings['direction'].upper()}</b>\n"
        f"Уведомления: <b>{'⏸ OFF' if settings['paused'] else '🔔 ON'}</b> · "
        f"оборот ≥ <b>${settings['volume_min_usd']:,.0f}</b>\n"
        f"Свечи: <b>{settings['timeframe']}{'m' if settings['timeframe'] != 'D' else ''}</b> · "
        f"тема: <b>{settings['theme'].upper()}</b>"
    )
    if status["last_error"]:
        text += f"\nПоследняя ошибка: <code>{html.escape(status['last_error'])}</code>"
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"{'✅ ' if settings['pump_threshold'] == n else ''}±{n}%", callback_data=f"set_thr_{n}"
                )
                for n in (5, 10)
            ],
            [
                InlineKeyboardButton(
                    f"{'✅ ' if settings['direction'] == d else ''}{d.upper()}", callback_data=f"direction_{d}"
                )
                for d in ("both", "pump", "dump")
            ],
            [
                InlineKeyboardButton(f"{'✅ ' if settings['timeframe'] == tf else ''}{tf}m", callback_data=f"tf_{tf}")
                for tf in ("1", "5", "15")
            ],
            [
                InlineKeyboardButton("🌙 Dark", callback_data="set_theme_dark"),
                InlineKeyboardButton("☀️ Light", callback_data="set_theme_light"),
            ],
            [InlineKeyboardButton("📊 Панели графика", callback_data="indicator_settings")],
            [InlineKeyboardButton("🛠 Фильтр оборота", callback_data="expert_settings")],
            [
                InlineKeyboardButton(
                    "▶️ Возобновить" if settings["paused"] else "⏸ Приостановить", callback_data="toggle_pause"
                )
            ],
            [
                InlineKeyboardButton("🔄 Обновить", callback_data="settings"),
                InlineKeyboardButton("⬅️ Назад", callback_data="main_menu"),
            ],
        ]
    )
    await show(update, text, keyboard)


async def indicator_settings_callback(update: Update, context):
    if not await authorized(update, context):
        return
    _, db, _ = deps(context)
    settings = await db.get_settings(update.effective_chat.id)
    rows = [
        [InlineKeyboardButton(f"{'✅' if settings[field] else '❌'} {label}", callback_data=f"toggle_ind_{field}")]
        for field, label in (
            ("show_sma", "Volume SMA 9"),
            ("show_delta", "CVD proxy · оценка"),
            ("show_heatmap", "Профиль объёма · оценка"),
        )
    ]
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="settings")])
    await show(
        update,
        "📊 <b>Панели графика</b>\n\nCVD рассчитывается из OHLCV, а не из покупок/продаж. "
        "Профиль распределяет объём равномерно внутри свечи. Это не реальный поток сделок, OI или ликвидации.",
        InlineKeyboardMarkup(rows),
    )


async def expert_settings_callback(update: Update, context):
    if not await authorized(update, context):
        return
    _, db, _ = deps(context)
    settings = await db.get_settings(update.effective_chat.id)
    await show(
        update,
        f"🛠 <b>Минимальный оборот за 24 часа</b>\n\n"
        f"Текущий фильтр: <b>${settings['volume_min_usd']:,.0f}</b>\n"
        "Применяется к каждому алерту. Общий список ограничен ликвидными USDT-перпетуалами.",
        InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("➖ 100k", callback_data="vol_minus"),
                    InlineKeyboardButton("➕ 100k", callback_data="vol_plus"),
                ],
                [InlineKeyboardButton("⬅️ Назад", callback_data="settings")],
            ]
        ),
    )


async def alerts_cmd(update: Update, context):
    if not await authorized(update, context):
        return
    config, db, _ = deps(context)
    chat_id = update.effective_chat.id
    rows = await db.get_alerts(chat_id, limit=5)
    back = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Меню", callback_data="main_menu")]])
    if not rows:
        await show(update, "Алертов пока нет. История появится после первого доставленного сигнала.", back)
        return
    lines = ["📊 <b>Последние 5 алертов</b>\n"]
    for row in rows:
        icon = "🟢" if row["direction"] == "PUMP" else "🔴"
        lines.append(
            f"{icon} <b>{row['direction']}</b> <code>{html.escape(row['symbol'])}</code> "
            f"{row['change_percent']:+.2f}% @ <code>{row['price']:.8g}</code>\n"
            f"{html.escape(row['timestamp'][:19].replace('T', ' '))} UTC"
        )
    await show(update, "\n\n".join(lines), back)
    latest = rows[0]
    image = snapshot_path(config.snapshot_dir, latest["screenshot_path"])
    if image:
        try:
            with image.open("rb") as photo:
                await context.bot.send_photo(
                    chat_id,
                    photo,
                    caption=f"Последний график: {html.escape(latest['symbol'])}",
                    parse_mode="HTML",
                    reply_markup=market_links(latest["symbol"]),
                )
        except (OSError, TelegramError):
            logger.warning("Could not resend latest snapshot")
    elif latest["screenshot_path"]:
        await context.bot.send_message(chat_id, "График из истории удалён по сроку хранения; запись сигнала сохранена.")


async def button_callback(update: Update, context):
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()
    if not await authorized(update, context, write=True):
        return
    _, db, _ = deps(context)
    chat_id, data = update.effective_chat.id, query.data
    if data == "close_menu":
        await query.message.delete()
        return
    if data == "main_menu":
        await start(update, context)
        return
    if data == "expert_settings":
        await expert_settings_callback(update, context)
        return
    if data == "indicator_settings":
        await indicator_settings_callback(update, context)
        return
    if data == "alerts":
        await alerts_cmd(update, context)
        return
    settings = await db.get_settings(chat_id)
    changes = {}
    if data in {f"set_thr_{n}" for n in (5, 10)}:
        changes = {"pump_threshold": float(data.rsplit("_", 1)[1])}
    elif data in {f"tf_{tf}" for tf in TIMEFRAMES}:
        changes = {"timeframe": data.removeprefix("tf_")}
    elif data in {"direction_both", "direction_pump", "direction_dump"}:
        changes = {"direction": data.removeprefix("direction_")}
    elif data in {"set_theme_dark", "set_theme_light"}:
        changes = {"theme": data.removeprefix("set_theme_")}
    elif data in {"toggle_ind_show_sma", "toggle_ind_show_delta", "toggle_ind_show_heatmap"}:
        field = data.removeprefix("toggle_ind_")
        changes = {field: 0 if settings[field] else 1}
    elif data in {"vol_plus", "vol_minus"}:
        changes = {
            "volume_min_usd": max(
                100_000, min(10_000_000, settings["volume_min_usd"] + (100_000 if data == "vol_plus" else -100_000))
            )
        }
    elif data == "toggle_pause":
        changes = {"paused": 0 if settings["paused"] else 1}
    if changes:
        await db.save_settings(chat_id, changes)
    if data.startswith("toggle_ind_"):
        await indicator_settings_callback(update, context)
    elif data in {"vol_plus", "vol_minus"}:
        await expert_settings_callback(update, context)
    else:
        await settings_callback(update, context)


async def pause_cmd(update: Update, context):
    if not await authorized(update, context, write=True):
        return
    _, db, _ = deps(context)
    await db.save_settings(update.effective_chat.id, {"paused": 1})
    await show(update, "⏸ Уведомления приостановлены. Сбор рынка продолжается.", main_menu(deps(context)[0]))


async def resume_cmd(update: Update, context):
    if not await authorized(update, context, write=True):
        return
    _, db, _ = deps(context)
    await db.save_settings(update.effective_chat.id, {"paused": 0})
    await show(update, "🔔 Уведомления возобновлены.", main_menu(deps(context)[0]))


async def test_alert(update: Update, context):
    if not await authorized(update, context, write=True):
        return
    _, db, monitor = deps(context)
    chat_id = update.effective_chat.id
    timestamps = context.application.bot_data.setdefault("test_timestamps", {})
    if time.monotonic() - timestamps.get(chat_id, -60) < 60:
        await update.effective_message.reply_text("Тест можно повторить через минуту.")
        return
    timestamps[chat_id] = time.monotonic()
    await update.effective_message.reply_text("⏳ Генерирую тестовый график BTCUSDT. Это не рыночный сигнал.")
    settings = await db.get_settings(chat_id)
    try:
        image = await monitor.render("BTCUSDT", settings, {"direction": "TEST"})
        caption = (
            "🧪 <b>ТЕСТОВЫЙ ГРАФИК BTCUSDT</b>\n"
            "Проверка загрузки свечей, построения графика и доставки в Telegram.\n"
            "<i>Изменение цены не рассчитывалось. Это не рыночный сигнал и не запись в истории алертов.</i>"
        )
        if image:
            with image.open("rb") as photo:
                await context.bot.send_photo(chat_id, photo, caption=caption, parse_mode="HTML")
        else:
            await context.bot.send_message(chat_id, caption, parse_mode="HTML")
    except Exception:
        logger.exception("Test alert failed")
        await context.bot.send_message(
            chat_id,
            "❌ Тест не выполнен: источник данных или Telegram временно недоступен. "
            "Подробности — в логах сервера; токен не раскрывается.",
        )


async def error_handler(update: object, context):
    # Avoid request URLs in exception text: Telegram URLs contain the bot token.
    logger.error("Telegram update handling failed (%s)", type(context.error).__name__)
