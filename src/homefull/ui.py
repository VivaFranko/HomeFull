"""Telegram buttons and plain-language search progress."""
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup

from .dates import duration

FIND = "🔎 Знайти квартири"
STATUS = "📊 Статус пошуку"
PAUSE = "⏸ Пауза"
RESUME = "▶️ Відновити"
CRITERIA = "⚙️ Мої вимоги"
MENU = ReplyKeyboardMarkup([[FIND, STATUS], [PAUSE, RESUME], [CRITERIA]],
                           resize_keyboard=True, is_persistent=True)
SOURCES = {"domria": "DOM.RIA", "rieltor": "Rieltor", "flatfy": "ЛУН", "olx": "OLX"}
PHASES = {
    "queued": "Підготовка пошуку", "collect": "Збираю оголошення",
    "details": "Читаю повні характеристики", "filtering": "Відбираю за твоїми вимогами",
    "waiting_analysis": "Очікую черги перевірки описів", "analysis": "Перевіряю описи",
    "delivering": "Надсилаю відповідні квартири", "complete": "Перевірку завершено",
    "paused": "Пошук на паузі", "failed": "Перевірку перервано помилкою",
    "interrupted": "Попередню перевірку перервав перезапуск",
}


def controls(active):
    toggle = (PAUSE, "search:pause") if active else (RESUME, "search:resume")
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(toggle[0], callback_data=toggle[1])],
        [InlineKeyboardButton(FIND, callback_data="search:check"),
         InlineKeyboardButton("↻ Оновити статус", callback_data="search:status")],
    ])


def progress_text(run, summary=None):
    if not run:
        return "Перевірка ще не запускалась. Натисни «Знайти квартири»."
    data = run.get("progress") or {}
    phase = run.get("phase", "queued")
    lines = ["HomeFull · " + PHASES.get(phase, "Пошук")]
    if run.get("started"):
        lines.append("Тривалість: " + duration((run.get("finished") or time.time()) - run["started"]))
    if phase == "collect" and data.get("source"):
        lines.append("Джерело: " + SOURCES.get(data["source"], data["source"]))
        if data.get("pages"):
            lines.append(f"Глибина: до {data['pages']} сторінок")
    fetched = data.get("fetched") or {}
    if fetched:
        lines.append(f"Зібрано в цій перевірці: {sum(fetched.values())}")
    if phase == "details" and data.get("details_total") is not None:
        lines.append(f"Характеристики: {data.get('details_done', 0)}/{data['details_total']}")
    if phase == "analysis":
        lines.append(f"Описи: {data.get('analysis_done', 0)}/{data.get('analysis_total', 0)}")
        if data.get("cached"):
            lines.append(f"Уже перевірені: {data['cached']}")
    if data.get("candidates") is not None:
        lines.append(f"Кандидатів на перевірку: {data['candidates']}")
    stats = run.get("stats") or {}
    if run.get("status") != "running":
        lines.append(f"Перевірено описів: {stats.get('analyzed', 0)} · надіслано: {stats.get('sent', 0)}")
        if stats.get("warnings"):
            lines.append("Частину умов перевірено резервним відбором за явними даними.")
        if stats.get("errors"):
            lines.append(f"Помилок джерел або обробки: {len(stats['errors'])}. Решту перевірено.")
    if summary:
        lines.extend(["", f"У базі: {summary['total']} · без дублів: {summary['unique']}",
                      f"Доступні за останньою перевіркою: {summary['available']}",
                      f"Після первинних фільтрів: {summary['eligible']}",
                      f"Перевірено описів: {summary['analyzed']} · ще перевірити: {summary['remaining']}",
                      f"Попередньо підходять: {summary['matching']}"])
        if summary["unresolved_links"]:
            lines.append(f"Без прямого посилання (не надсилаю): {summary['unresolved_links']}")
    return "\n".join(lines)


def status_text(summary, run=None, interval=10):
    if not summary["has_criteria"]:
        return "Опиши, яку квартиру шукаєш, і збережи свої вимоги. Тоді HomeFull почне підбір."
    state = "▶️ Пошук увімкнено" if summary["active"] else "⏸ Твій пошук на паузі"
    lines = [state, f"Автоматична перевірка: кожні {interval} хв.", ""]
    if run and summary["active"]:
        lines.append(progress_text(run))
        lines.append("")
    lines.extend([f"Оголошень у базі: {summary['total']}",
                  f"Унікальних: {summary['unique']} · дублів: {summary['duplicates']}",
                  f"Доступні за останньою перевіркою: {summary['available']}",
                  f"Зняті з публікації: {summary['unavailable']}", "",
                  f"Після первинних фільтрів: {summary['eligible']}",
                  f"Описи перевірено: {summary['analyzed']} · залишилось: {summary['remaining']}",
                  f"Попередньо підходять: {summary['matching']}",
                  f"Надіслано тобі: {summary['sent']} · у черзі: {summary['queued']}"])
    if summary["unresolved_links"]:
        lines.append(f"Без прямого посилання (не надсилаю): {summary['unresolved_links']}")
    top = sorted(summary["rejections"].items(), key=lambda row: row[1], reverse=True)[:3]
    if top:
        lines.append("\nОсновні причини відсіву:")
        lines.extend(f"• {reason}: {count}" for reason, count in top)
    if summary["sources"]:
        lines.append("\nЗа джерелами:")
        lines.extend(f"• {SOURCES.get(name, name)}: {value['total']}"
                     for name, value in summary["sources"].items())
    return "\n".join(lines)
