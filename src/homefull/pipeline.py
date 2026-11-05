"""Один цикл роботи: зібрати нові оголошення -> доповнити -> проаналізувати LLM ->
підібрати користувачам -> надіслати. Викликається планувальником бота кожні N хвилин."""
import logging
import time
import threading
from dataclasses import dataclass, field
import requests
from urllib.parse import urlsplit

from .geo import DistrictKNN
from .http import Blocked, PoliteClient
from .llm import LLMError, analyze, explicit_analysis
from .matcher import format_message, match
from .pricing import FairPrice
from .sources import ALL
from .storage import Store

log = logging.getLogger(__name__)


@dataclass
class CycleStats:
    fetched: dict = field(default_factory=dict)
    new: int = 0
    analyzed: int = 0
    sent: int = 0
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    rules_analyzed: int = 0
    seconds: float = 0.0
    paused: bool = False


def report(callback, phase, **data):
    if callback:
        callback(phase, data)


class Pipeline:
    def __init__(self, store: Store, llm, fair: FairPrice | None = None, client=None, pages=1,
                 sources=None):
        self.store, self.llm, self.fair, self.pages = store, llm, fair, pages
        self.client = client or PoliteClient()
        self.sources = [cls(self.client) for cls in (ALL if sources is None else sources)]
        self._analysis_lock = threading.Lock()

    def collect(self, stats: CycleStats, progress=None, cancelled=None):
        new = []
        users = list(self.store.active_users())
        for src in self.sources:
            if cancelled and cancelled():
                break
            report(progress, "collect", source=src.name, pages=self.pages, fetched=dict(stats.fetched))
            try:
                ls = list({l.key: l for l in src.fetch(self.pages)}.values())
                stats.fetched[src.name] = len(ls)
                report(progress, "collect", source=src.name, fetched=dict(stats.fetched))
                fresh = self.store.upsert(ls)
                fresh_keys = {l.key for l in fresh}
                backfill_remaining = 3  # Upgrade existing adverts without delaying new alerts.
                for l in ls:
                    if cancelled and cancelled():
                        break
                    if users and not any(match(c, l, None).ok for _, c in users):
                        continue
                    needs_upgrade = not l.details_version and backfill_remaining > 0
                    if src.needs_details and (l.key in fresh_keys or needs_upgrade):
                        if l.key not in fresh_keys:
                            backfill_remaining -= 1
                        try:
                            report(progress, "details", source=src.name, listing=l.key)
                            src.details(l)
                            self.store.update_listing(l)
                        except Blocked as e:
                            stats.errors.append(f"{src.name} деталі {l.source_id}: {e}")
                            break
                        except Exception as e:  # noqa: BLE001
                            stats.errors.append(f"{src.name} деталі {l.source_id}: {e}")
                new += fresh
            except (Blocked, Exception) as e:  # noqa: BLE001 - одне джерело не валить цикл
                stats.errors.append(f"{src.name}: {type(e).__name__}: {e}")
                log.warning("джерело %s: %s", src.name, e)
                continue
            finally:
                close = getattr(src, "close", None)
                if close:
                    try:
                        close()
                    except Exception as e:  # noqa: BLE001
                        stats.errors.append(f"{src.name} cleanup: {type(e).__name__}")
        return new

    def candidates(self, chat_id, criteria, limit=40):
        """Advance past sent/rejected adverts instead of rechecking the first page forever."""
        sources = {source.name: source for source in self.sources}
        result = []
        for listing in self.store.listings():
            if self.store.was_sent(chat_id, listing.key) or not match(criteria, listing, None).ok:
                continue
            analysis = self.store.analysis(listing.key)
            source = sources.get(listing.source)
            incomplete = bool(source and source.needs_details and not listing.details_version)
            if analysis and not analysis.rules_only and not match(criteria, listing, analysis).ok and not incomplete:
                continue
            result.append(listing)
            if len(result) >= limit:
                break
        return result

    def prepare_candidates(self, listings, stats, progress=None, cancelled=None):
        """Read full characteristics before deciding a stove type is unknown."""
        # /check and the scheduled collector may run on different worker threads.
        # Chromium contexts must remain local to one call/thread.
        client = PoliteClient(self.client.min_interval) if isinstance(self.client, PoliteClient) else self.client
        sources = {source.name: type(source)(client) for source in self.sources}
        opened = set()
        blocked = set()
        for index, listing in enumerate(listings):
            if cancelled and cancelled():
                break
            report(progress, "details", details_done=index, details_total=len(listings), source=listing.source)
            source = sources.get(listing.source)
            host_key = (listing.source, urlsplit(listing.url).hostname)
            if not source or not source.needs_details or listing.details_version or host_key in blocked:
                continue
            opened.add(source.name)
            try:
                source.details(listing)
                self.store.update_listing(listing)
                self.store.clear_analysis(listing.key)
            except requests.HTTPError as error:
                if error.response is not None and error.response.status_code in (404, 410):
                    self.store.expire(listing)
                else:
                    stats.errors.append(f"{source.name} деталі: HTTP error")
            except Blocked as error:
                blocked.add(host_key)
                stats.errors.append(f"{source.name} деталі: {error}")
            except Exception as error:  # noqa: BLE001
                stats.errors.append(f"{source.name} деталі: {type(error).__name__}")
        for name in opened:
            close = getattr(sources[name], "close", None)
            if close:
                try:
                    close()
                except Exception:
                    pass
        if client is not self.client:
            client.close()
        report(progress, "details", details_done=len(listings), details_total=len(listings))

    def enrich(self, new):
        knn = DistrictKNN().fit(self.store.listings())
        for l in new:
            if l.district is None:
                l.district = knn.predict(l.lat, l.lon)
                if l.district:
                    self.store.update_listing(l)

    def run_analysis(self, listings, stats: CycleStats, progress=None, cancelled=None):
        report(progress, "waiting_analysis")
        while not self._analysis_lock.acquire(timeout=0.2):
            if cancelled and cancelled():
                return
        try:
            self._analyze_locked(listings, stats, progress, cancelled)
        finally:
            self._analysis_lock.release()

    def _analyze_locked(self, listings, stats, progress, cancelled):
        todo = []
        for listing in listings:
            cached = self.store.analysis(listing.key)
            if cached is None or cached.rules_only:
                todo.append(listing)
        if not todo:
            report(progress, "analysis", analysis_done=0, analysis_total=0, cached=len(listings))
            return
        completed = 0
        report(progress, "analysis", analysis_done=0, analysis_total=len(todo))
        for start in range(0, len(todo), 8):
            if cancelled and cancelled():
                return
            batch = todo[start:start + 8]
            try:
                res = analyze(self.llm, batch)
            except LLMError as e:
                stats.warnings.append(f"LLM: {e}; резервний відбір за явними даними")
                log.warning("LLM unavailable; using explicit advert fields: %s", e)
                res = {l.key: explicit_analysis(l) for l in batch}
                stats.rules_analyzed += len(res)
            for key, a in res.items():
                self.store.save_analysis(key, a, "rules-fallback" if a.rules_only else self.llm.name)
            stats.analyzed += len(res)
            completed += len(res)
            report(progress, "analysis", analysis_done=completed, analysis_total=len(todo), provider=self.llm.name)

    def notify(self, listings, send, stats: CycleStats, send_photo=None, progress=None):
        for chat_id, c in self.store.active_users():
            for l in listings:
                current = self.store.user(chat_id)
                if not current or not current[2]:
                    break
                c = current[0]
                if self.store.was_sent(chat_id, l.key):
                    continue
                a = self.store.analysis(l.key)
                if a is None:
                    continue  # Analysis failed: retain this advert for the next cycle.
                m = match(c, l, a)
                if not m.ok:
                    self.store.discard_pending(chat_id, l.key)
                    continue
                fair = self.fair.verdict(l) if self.fair else None
                text = format_message(l, a, m, fair)
                try:
                    if send_photo and l.photo_url:
                        send_photo(chat_id, l.photo_url, text)
                    else:
                        send(chat_id, text)
                except Exception as e:  # noqa: BLE001 - retry this advert next cycle
                    stats.errors.append(f"Telegram {l.key}: {type(e).__name__}")
                    log.warning("Telegram delivery failed for %s: %s", l.key, type(e).__name__)
                    continue
                self.store.mark_sent(chat_id, l.key)
                stats.sent += 1
                report(progress, "delivering", sent=stats.sent)

    def cycle(self, send, send_photo=None, progress=None) -> CycleStats:
        t0, stats = time.time(), CycleStats()
        cancelled = lambda: not list(self.store.active_users())
        if cancelled():
            stats.paused = True
            report(progress, "paused")
            return stats
        new = self.collect(stats, progress, cancelled)
        stats.new = len(new)
        self.enrich(new)
        # аналізуємо лише ті нові, що проходять «дешеві» фільтри хоча б одного користувача
        users = list(self.store.active_users())
        pending = {}
        for chat_id, criteria in users:
            self.store.enqueue(chat_id, [l for l in new if match(criteria, l, None).ok])
            self.store.enqueue(chat_id, self.candidates(chat_id, criteria))
            for listing in self.store.pending(chat_id):
                pending[listing.key] = listing
        cand = list(pending.values())
        report(progress, "filtering", candidates=len(cand), fetched=dict(stats.fetched), new=stats.new)
        self.prepare_candidates(cand, stats, progress, cancelled)
        cand = [l for l in cand if l.available]
        self.run_analysis(cand, stats, progress, cancelled)
        self.notify(cand, send, stats, send_photo, progress)
        stats.seconds = round(time.time() - t0, 1)
        stats.paused = cancelled()
        report(progress, "paused" if stats.paused else "complete", sent=stats.sent)
        return stats
