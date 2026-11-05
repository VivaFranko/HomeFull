"""HomeFull — персональний пошук оренди з кнопками, прогресом і паузою.

Команди:
  /start     - привітання і прохання описати, яку квартиру шукаєте
  (текст)    - LLM перетворює опис на фільтр; бот показує його з кнопками «Зберегти/Змінити»
  /criteria  - поточні критерії
  /check     - одразу підібрати до 10 варіантів серед уже зібраних оголошень
  /pause, /resume - вимкнути/увімкнути сповіщення
  /stats     - скільки оголошень зібрано
Кожні POLL_MINUTES хвилин запускається цикл Pipeline: нові оголошення -> аналіз -> сповіщення.

Запуск: TELEGRAM_BOT_TOKEN=... python -m homefull.bot   (з теки src)
"""
import asyncio
import json
import logging
import os
import time
from pathlib import Path
from dataclasses import asdict
from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (Application, ApplicationBuilder, CallbackQueryHandler,
                          CommandHandler, ContextTypes, MessageHandler, filters)

from . import fx
from .delivery import send_listing
from .instance import single_instance
from .intents import has_preferences, is_search_question
from .llm import LLMError, make_llm, parse_criteria
from .matcher import format_message, match
from .models import Criteria
from .pipeline import CycleStats, Pipeline
from .pricing import FairPrice
from .storage import Store
from .config import ROOT, DATA, TIMEZONE
from .ui import MENU, FIND, STATUS, PAUSE, RESUME, CRITERIA, controls, progress_text, status_text

POLL_MINUTES = int(os.environ.get("POLL_MINUTES", "10"))
log = logging.getLogger("homefull")

HELLO = ("Привіт! Я HomeFull. Шукаю квартири в оренду у Львові на DOM.RIA, Rieltor, ЛУН та OLX "
         "і надсилаю нові оголошення, що вам підходять.\n\n"
         "Опишіть своїми словами, що шукаєте. Наприклад:\n"
         "<i>1-2 кімнати на Сихові або в центрі, до 600$, маю кота, бажано без комісії</i>")


def message_of(update):
    return (getattr(update, "effective_message", None) or getattr(update, "message", None)
            or update.callback_query.message)


def describe(c: Criteria) -> str:
    p = []
    if c.max_price_uah or c.min_price_uah:
        lo = f"від {c.min_price_uah:,.0f} " if c.min_price_uah else ""
        hi = f"до {c.max_price_uah:,.0f} " if c.max_price_uah else ""
        p.append(f"💰 {lo}{hi}грн/міс".replace(",", " "))
    p.append("🚪 кімнат: " + (", ".join(map(str, c.rooms)) if c.rooms else "будь-яка кількість"))
    if c.min_area:
        p.append(f"📐 від {c.min_area:g} м²")
    p.append("📍 " + (", ".join(escape(d) for d in c.districts) if c.districts else "будь-який район"))
    if c.excluded_areas:
        p.append("🚫 Виключити: " + ", ".join(escape(area) for area in c.excluded_areas))
    if c.gas_stove:
        p.append("🔥 Обов'язково газова плита")
    pets = "з тваринами" + (f" ({', '.join(escape(kind) for kind in c.pet_kinds)})" if c.pet_kinds else "")
    flags = [t for f, t in [(c.pets, pets), (c.room_ok, "згоден на кімнату"),
                            (c.no_commission, "без комісії"),
                            (c.owner_only, "лише власник"), (c.not_first_floor, "не 1-й поверх")]
             if f]
    if flags:
        p.append("✅ " + ", ".join(flags))
    if c.extra:
        p.append("📝 " + escape(c.extra))
    return "\n".join(p)


class RentBot:
    def __init__(self, token: str):
        DATA.mkdir(parents=True, exist_ok=True)
        self.store = Store(DATA / "homefull.sqlite")
        self.store.recover_runs()
        self._checks = set()
        self._polling = False
        self.llm = make_llm()
        self.fair = FairPrice(DATA / "fair_price.pkl")
        settings_path = DATA / "runtime_settings.json"
        settings = json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {}
        self.pipeline = Pipeline(self.store, self.llm, self.fair,
                                 pages=int(os.environ.get("PAGES", settings.get("pages", 1))))
        self.app: Application = ApplicationBuilder().token(token).concurrent_updates(8).build()
        a = self.app
        a.add_handler(CommandHandler("start", self.start))
        a.add_handler(CommandHandler("criteria", self.show_criteria))
        a.add_handler(CommandHandler("check", self.check))
        a.add_handler(CommandHandler("pause", self.pause))
        a.add_handler(CommandHandler("resume", self.resume))
        a.add_handler(CommandHandler("stats", self.stats))
        a.add_handler(CommandHandler("status", self.stats))
        a.add_handler(CallbackQueryHandler(self.on_button))
        a.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))
        a.job_queue.run_repeating(self.poll, interval=POLL_MINUTES * 60, first=15,
                                 name="homefull-scan", job_kwargs={"max_instances": 1, "coalesce": True})

    # ── команди ──
    async def start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await message_of(update).reply_html(HELLO, reply_markup=MENU)

    async def on_text(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        action = {FIND: self.check, STATUS: self.stats, PAUSE: self.pause,
                  RESUME: self.resume, CRITERIA: self.show_criteria}.get(text)
        if action:
            await action(update, ctx)
            return
        if is_search_question(text):
            await self.check(update, ctx)
            return
        self._preference_locks = getattr(self, "_preference_locks", {})
        lock = self._preference_locks.setdefault(update.effective_chat.id, asyncio.Lock())
        if lock.locked():
            await update.message.reply_text("Ще розбираю попередні побажання. Дочекайся підтвердження й надішли зміни ще раз.",
                                            reply_markup=MENU)
            return
        async with lock:
            await self._parse_preferences(update, ctx, text)

    async def _parse_preferences(self, update, ctx, text):
        current = self.store.user(update.effective_chat.id)
        await update.message.reply_text("Розбираю побажання…", reply_markup=MENU)
        try:
            usd = await asyncio.to_thread(lambda: fx.rates()["USD"])
            c = await asyncio.to_thread(parse_criteria, self.llm, text, usd,
                                        current[0] if current else None)
        except (LLMError, Exception) as e:  # noqa: BLE001
            log.exception("розбір критеріїв")
            await update.message.reply_text(f"Не вдалося розібрати ({e}). Спробуйте ще раз.")
            return
        if not has_preferences(c):
            await update.message.reply_text(
                "Не знайшов у повідомленні нових побажань. Збережені критерії залишились. "
                "Натисни «🔎 Знайти квартири» або «⚙️ Мої вимоги».", reply_markup=MENU)
            return
        if current and c.to_dict() == current[0].to_dict():
            await update.message.reply_text(
                "Критерії не змінилися. Скористайся кнопками пошуку або перегляду вимог.", reply_markup=MENU)
            return
        ctx.user_data["pending"] = (c.to_dict(), text)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Зберегти", callback_data="save"),
                                    InlineKeyboardButton("✏️ Змінити", callback_data="edit")]])
        confirmation = await update.message.reply_html("Я зрозумів так:\n\n" + describe(c), reply_markup=kb)
        ctx.user_data["pending_message_id"] = confirmation.message_id

    async def on_button(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        action = {"search:pause": self.pause, "search:resume": self.resume,
                  "search:status": self.stats, "search:check": self.check}.get(q.data)
        if action:
            await action(update, ctx)
            return
        if q.data == "edit":
            await q.edit_message_text("Опишіть ще раз, що змінити або що шукаєте.")
            return
        pending_message = ctx.user_data.get("pending_message_id")
        if pending_message is not None and getattr(q.message, "message_id", None) != pending_message:
            await q.edit_message_text("Це старе підтвердження. Скористайся кнопками під останніми побажаннями.")
            return
        pending = ctx.user_data.pop("pending", None)
        ctx.user_data.pop("pending_message_id", None)
        if not pending:
            await q.edit_message_text("Немає критеріїв для збереження - опишіть, що шукаєте.")
            return
        c, text = Criteria.from_dict(pending[0]), pending[1]
        if not has_preferences(c):
            await q.edit_message_text("Порожні критерії не збережено. Поточні вимоги залишились.")
            return
        self.store.set_criteria(q.message.chat_id, c, text)
        active = self.store.user(q.message.chat_id)[2]
        state = (f"Перевіряю сайти кожні {POLL_MINUTES} хв." if active
                 else "Пошук залишається на паузі. Натисни «▶️ Відновити», щоб продовжити.")
        await q.edit_message_text(
            "Збережено ✅\n\n" + describe(c) + "\n\n" + state,
            parse_mode=ParseMode.HTML, reply_markup=controls(active))

    async def show_criteria(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        u = self.store.user(update.effective_chat.id)
        if not u:
            await message_of(update).reply_text("Критеріїв ще немає — опиши, що шукаєш.", reply_markup=MENU)
            return
        c, _, active = u
        state = "пошук увімкнено" if active else "пошук на паузі"
        await message_of(update).reply_html(describe(c) + f"\n\n<i>{state}</i>", reply_markup=controls(active))

    async def check(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        chat = update.effective_chat.id
        message = message_of(update)
        u = self.store.user(chat)
        if not u:
            await message.reply_text("Спершу опиши, яку квартиру шукаєш.", reply_markup=MENU)
            return
        if not u[2]:
            await message.reply_text("⏸ Пошук на паузі. Натисни «▶️ Відновити», щоб продовжити.",
                                     reply_markup=controls(False))
            return
        self._checks = getattr(self, "_checks", set())
        if chat in self._checks:
            await self.stats(update, ctx)
            return
        self._checks.add(chat)
        c = u[0]
        stats = CycleStats()
        run_id = self.store.begin_run("manual", chat)
        started = time.time()
        done = asyncio.Event()
        progress_message = await message.reply_text("HomeFull · готую підбір…", reply_markup=controls(True))
        pump = asyncio.create_task(self._progress_updates(chat, run_id, progress_message, done))

        def cancelled():
            current = self.store.user(chat)
            return not current or not current[2]

        def progress(phase, data):
            self.store.update_run(run_id, phase, data, asdict(stats))

        try:
            recent = self.pipeline.candidates(chat, c)
            progress("filtering", {"candidates": len(recent)})
            await asyncio.to_thread(self.pipeline.prepare_candidates, recent, stats, progress, cancelled)
            recent = [listing for listing in recent if listing.available]
            if not cancelled():
                self.store.enqueue(chat, recent)
                await asyncio.to_thread(self.pipeline.run_analysis, recent, stats, progress, cancelled)
            progress("delivering", {"sent": stats.sent})
            for listing in recent:
                if cancelled():
                    break
                current = self.store.user(chat)
                if self.store.was_sent(chat, listing.key):
                    continue
                analysis = self.store.analysis(listing.key)
                if analysis is None:
                    continue
                result = match(current[0], listing, analysis)
                if not result.ok:
                    self.store.discard_pending(chat, listing.key)
                    continue
                try:
                    await send_listing(self.app.bot, chat,
                        format_message(listing, analysis, result, self.fair.verdict(listing)), listing.photo_url)
                except TelegramError as error:
                    stats.errors.append(f"Telegram: {type(error).__name__}")
                    continue
                self.store.mark_sent(chat, listing.key)
                stats.sent += 1
                progress("delivering", {"sent": stats.sent})
                if stats.sent >= 10:
                    break
            stats.paused = cancelled()
            stats.seconds = round(time.time() - started, 1)
            self.store.update_run(run_id, "paused" if stats.paused else "complete", stats=asdict(stats),
                                  status="paused" if stats.paused else "completed")
        except Exception as error:
            log.exception("manual search failed")
            stats.errors.append(type(error).__name__)
            self.store.update_run(run_id, "failed", stats=asdict(stats), status="failed")
        finally:
            done.set()
            await pump
            self._checks.discard(chat)
        summary = self.store.search_summary(chat)
        text = progress_text(self.store.get_run(run_id), summary)
        if summary["remaining"] and not stats.paused:
            text += "\n\nЩе є неперевірені оголошення. Вони обробляються наступними перевірками."
        try:
            await progress_message.edit_text(text, reply_markup=controls(summary["active"]))
        except TelegramError:
            await message.reply_text(text, reply_markup=controls(summary["active"]))

    async def _progress_updates(self, chat, run_id, message, done):
        previous = ""
        while not done.is_set():
            try:
                await asyncio.wait_for(done.wait(), timeout=3)
                break
            except asyncio.TimeoutError:
                pass
            run = self.store.get_run(run_id)
            if not run or run["id"] != run_id:
                continue
            text = progress_text(run)
            if text != previous:
                try:
                    await message.edit_text(text, reply_markup=controls(True))
                    previous = text
                except TelegramError:
                    pass

    async def pause(self, update: Update, ctx):
        self.store.set_active(update.effective_chat.id, False)
        await message_of(update).reply_text(
            "⏸ Пошук призупинено. Нові етапи перевірки та сповіщення зупинені. "
            "Для продовження натисни «▶️ Відновити».", reply_markup=controls(False))

    async def resume(self, update: Update, ctx):
        if not self.store.user(update.effective_chat.id):
            await message_of(update).reply_text("Спершу опиши й збережи вимоги до квартири.", reply_markup=MENU)
            return
        self.store.set_active(update.effective_chat.id, True)
        await message_of(update).reply_text(
            f"▶️ Пошук відновлено. Автоматична перевірка — кожні {POLL_MINUTES} хв. "
            "Натисни «🔎 Знайти квартири», щоб перевірити вже зібрані варіанти.", reply_markup=controls(True))

    async def stats(self, update: Update, ctx):
        chat = update.effective_chat.id
        summary = self.store.search_summary(chat)
        run = self.store.latest_run(chat, running=True) or self.store.latest_run(chat)
        await message_of(update).reply_text(status_text(summary, run, POLL_MINUTES),
                                             reply_markup=controls(summary["active"]))

    # ── фоновий цикл ──
    async def poll(self, ctx: ContextTypes.DEFAULT_TYPE):
        if self._polling:
            return
        self._polling = True
        run_id = self.store.begin_run("scheduled")
        loop = asyncio.get_running_loop()
        bot = self.app.bot

        def send(chat_id, html):
            asyncio.run_coroutine_threadsafe(
                send_listing(bot, chat_id, html), loop).result(timeout=90)

        def send_photo(chat_id, photo_url, html):
            asyncio.run_coroutine_threadsafe(
                send_listing(bot, chat_id, html, photo_url), loop).result(timeout=90)

        def progress(phase, data):
            self.store.update_run(run_id, phase, data)

        try:
            stats = await asyncio.to_thread(self.pipeline.cycle, send, send_photo, progress)
            self.store.update_run(run_id, "paused" if stats.paused else "complete", stats=asdict(stats),
                                  status="paused" if stats.paused else "completed")
            log.info("cycle: %s", json.dumps(asdict(stats), ensure_ascii=False))
            (DATA / "last_cycle.json").write_text(json.dumps(asdict(stats), ensure_ascii=False), encoding="utf-8")
        except Exception as error:
            log.exception("scheduled search failed")
            self.store.update_run(run_id, "failed", stats={"errors": [type(error).__name__]}, status="failed")
        finally:
            self._polling = False

    def run(self):
        self.app.run_polling()


def main(argv=None):
    import argparse
    from . import __version__
    parser = argparse.ArgumentParser(prog="homefull", description="HomeFull rental search assistant")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--check-config", action="store_true", help="Check local settings without starting Telegram")
    args = parser.parse_args(argv)
    if args.check_config:
        print(json.dumps({"project": str(ROOT), "data": str(DATA),
                          "telegram_configured": bool(os.environ.get("TELEGRAM_BOT_TOKEN")),
                          "provider": os.environ.get("LLM_PROVIDER", "auto"),
                          "pages": int(os.environ.get("PAGES", "3")), "timezone": TIMEZONE}, ensure_ascii=False))
        return
    DATA.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(DATA / "homefull.log", encoding="utf-8"),
                                  logging.StreamHandler()])
    logging.getLogger("httpx").setLevel(logging.WARNING)
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Заповни TELEGRAM_BOT_TOKEN у HomeFull/.env або змінній середовища")
    with single_instance(DATA):
        (DATA / "homefull_process.json").write_text(
            json.dumps({"pid": os.getpid(), "started_at": time.time(),
                        "log": str(DATA / "homefull.log")}, ensure_ascii=False), encoding="utf-8")
        RentBot(token).run()


if __name__ == "__main__":
    main()
