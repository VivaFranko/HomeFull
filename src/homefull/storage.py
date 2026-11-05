"""SQLite: оголошення, результати аналізу LLM, користувачі та їхні критерії, надіслані
сповіщення. Один файл data/homefull.sqlite - достатньо для одного бота і легко бекапиться."""
import json
import math
import sqlite3
import time
import threading
import uuid
from functools import wraps
from pathlib import Path

from .models import Analysis, Criteria, Listing
from .dates import set_published


def synchronized(cls):
    """Serialize each public operation on the shared SQLite connection."""
    for name, method in list(vars(cls).items()):
        if name.startswith("_") or not callable(method):
            continue
        def wrap(function):
            @wraps(function)
            def guarded(self, *args, **kwargs):
                with self._lock:
                    return function(self, *args, **kwargs)
            return guarded
        setattr(cls, name, wrap(method))
    return cls

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
  key TEXT PRIMARY KEY, source TEXT, data TEXT NOT NULL,
  first_seen REAL, last_seen REAL, dup_of TEXT
);
CREATE TABLE IF NOT EXISTS analyses (key TEXT PRIMARY KEY, data TEXT NOT NULL, model TEXT,
  created REAL);
CREATE TABLE IF NOT EXISTS users (chat_id INTEGER PRIMARY KEY, criteria TEXT,
  criteria_text TEXT, active INTEGER DEFAULT 1, created REAL);
CREATE TABLE IF NOT EXISTS sent (chat_id INTEGER, key TEXT, sent_at REAL,
  PRIMARY KEY (chat_id, key));
CREATE TABLE IF NOT EXISTS notification_queue (chat_id INTEGER, key TEXT,
  PRIMARY KEY (chat_id, key));
CREATE TABLE IF NOT EXISTS search_runs (
  id TEXT PRIMARY KEY, kind TEXT, chat_id INTEGER, started REAL, finished REAL,
  status TEXT, phase TEXT, progress TEXT, stats TEXT
);
CREATE INDEX IF NOT EXISTS search_runs_chat_started ON search_runs(chat_id, started DESC);
"""


@synchronized
class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.executescript(SCHEMA)
        self._migrate_listing_metadata()

    @staticmethod
    def _listing(row):
        if row is None:
            return None
        data = json.loads(row[0])
        data["first_seen_at"], data["last_seen_at"] = row[1], row[2]
        listing = Listing(**{k: v for k, v in data.items() if k in Listing.__dataclass_fields__})
        if listing.published_at is None and listing.published:
            set_published(listing, listing.published, "aggregator" if listing.source == "flatfy" else "source")
        return listing

    def _migrate_listing_metadata(self):
        for key, data, first, last in self.db.execute("SELECT key,data,first_seen,last_seen FROM listings").fetchall():
            listing = self._listing((data, first, last))
            if listing.source == "flatfy":
                from .sources.flatfy import property_link
                if not property_link(listing.url):
                    listing.link_kind = "unresolved"
            self.db.execute("UPDATE listings SET data=? WHERE key=?",
                            (json.dumps(listing.to_dict(), ensure_ascii=False), key))
        self.db.commit()

    # ── оголошення ──
    def upsert(self, ls: list[Listing]) -> list[Listing]:
        """Зберігає оголошення; повертає НОВІ (яких ще не було) і не дублікати інших сайтів."""
        now, new = time.time(), []
        for l in ls:
            row = self.db.execute("SELECT data,first_seen,last_seen FROM listings WHERE key=?", (l.key,)).fetchone()
            l.first_seen_at = row[1] if row else now
            l.last_seen_at = now
            if l.published_at is None and l.published:
                set_published(l, l.published, "aggregator" if l.source == "flatfy" else "source")
            if not l.available and l.unavailable_at is None:
                l.unavailable_at = now
            if row:
                # Catalogs omit detail fields: polling must not erase descriptions,
                # floor or room count fetched from the individual advert.
                previous = json.loads(row[0])
                if previous.get("details_version") and not l.details_version:
                    l.description = previous.get("description") or l.description
                for name in ("description", "rooms", "area", "floor", "floors", "district",
                             "street", "lat", "lon", "published", "photo_url", "contact_name",
                             "published_at", "published_precision", "publication_basis", "refreshed_at"):
                    if getattr(l, name) in (None, "") and previous.get(name) is not None:
                        setattr(l, name, previous[name])
                if not l.phones:
                    l.phones = previous.get("phones") or []
                if not l.details_version:
                    l.details_version = previous.get("details_version", 0)
                old_publication = previous.get("published_at")
                if previous.get("publication_basis") == "source" and old_publication is not None:
                    if l.publication_basis != "source" or old_publication < l.published_at:
                        for field in ("published", "published_at", "published_precision", "publication_basis"):
                            setattr(l, field, previous.get(field))
                if previous.get("description") != l.description:
                    self.db.execute("DELETE FROM analyses WHERE key=?", (l.key,))
                self.db.execute("UPDATE listings SET last_seen=?, data=? WHERE key=?",
                                (now, json.dumps(l.to_dict(), ensure_ascii=False), l.key))
                continue
            dup = self.find_duplicate(l)
            self.db.execute("INSERT INTO listings VALUES (?,?,?,?,?,?)",
                            (l.key, l.source, json.dumps(l.to_dict(), ensure_ascii=False),
                             now, now, dup))
            if dup is None:
                new.append(l)
        self.db.commit()
        return new

    def find_duplicate(self, l: Listing):
        """Те саме житло на іншому сайті: координати в межах ~40 м, та сама кількість кімнат,
        ціна відрізняється не більш ніж на 3 %."""
        if l.lat is None or l.price_uah is None:
            return None
        for key, data in self.db.execute(
                "SELECT key, data FROM listings WHERE source != ? AND dup_of IS NULL", (l.source,)):
            o = json.loads(data)
            if o.get("lat") is None or o.get("price_uah") is None:
                continue
            if l.rooms and o.get("rooms") and l.rooms != o["rooms"]:
                continue
            if abs(o["price_uah"] - l.price_uah) > 0.03 * l.price_uah:
                continue
            if _meters(l.lat, l.lon, o["lat"], o["lon"]) < 40:
                return key
        return None

    def listings(self, unique=True) -> list[Listing]:
        q = "SELECT data,first_seen,last_seen FROM listings" + (" WHERE dup_of IS NULL" if unique else "")
        q += " ORDER BY first_seen DESC, key"
        return [self._listing(row) for row in self.db.execute(q).fetchall()]

    def get(self, key) -> Listing | None:
        r = self.db.execute("SELECT data,first_seen,last_seen FROM listings WHERE key=?", (key,)).fetchone()
        return self._listing(r)

    def update_listing(self, l: Listing):
        if not l.available and l.unavailable_at is None:
            l.unavailable_at = time.time()
        self.db.execute("UPDATE listings SET data=? WHERE key=?",
                        (json.dumps(l.to_dict(), ensure_ascii=False), l.key))
        self.db.commit()

    def stats(self) -> dict:
        rows = self.db.execute("SELECT source, COUNT(*), SUM(dup_of IS NOT NULL) FROM listings "
                               "GROUP BY source").fetchall()
        return {s: {"total": n, "duplicates": int(d or 0)} for s, n, d in rows}

    # ── аналіз LLM ──
    def analysis(self, key) -> Analysis | None:
        r = self.db.execute("SELECT data FROM analyses WHERE key=?", (key,)).fetchone()
        return Analysis.from_dict(json.loads(r[0])) if r else None

    def clear_analysis(self, key):
        self.db.execute("DELETE FROM analyses WHERE key=?", (key,))
        self.db.commit()

    def save_analysis(self, key, a: Analysis, model: str):
        self.db.execute("INSERT OR REPLACE INTO analyses VALUES (?,?,?,?)",
                        (key, json.dumps(a.to_dict(), ensure_ascii=False), model, time.time()))
        self.db.commit()

    # ── користувачі ──
    def set_criteria(self, chat_id: int, c: Criteria, text: str):
        self.db.execute(
            "INSERT INTO users VALUES (?,?,?,1,?) ON CONFLICT(chat_id) DO UPDATE SET "
            "criteria=excluded.criteria, criteria_text=excluded.criteria_text",
            (chat_id, json.dumps(c.to_dict(), ensure_ascii=False), text, time.time()))
        self.db.commit()

    def set_active(self, chat_id: int, active: bool):
        self.db.execute("UPDATE users SET active=? WHERE chat_id=?", (int(active), chat_id))
        self.db.commit()

    def user(self, chat_id: int):
        r = self.db.execute("SELECT criteria, criteria_text, active FROM users WHERE chat_id=?",
                            (chat_id,)).fetchone()
        if not r or not r[0]:
            return None
        return Criteria.from_dict(json.loads(r[0])), r[1], bool(r[2])

    def active_users(self):
        rows = self.db.execute("SELECT chat_id,criteria FROM users WHERE active=1 AND criteria IS NOT NULL").fetchall()
        return iter([(chat_id, Criteria.from_dict(json.loads(c))) for chat_id, c in rows])

    def was_sent(self, chat_id: int, key: str) -> bool:
        return self.db.execute("SELECT 1 FROM sent WHERE chat_id=? AND key=?",
                               (chat_id, key)).fetchone() is not None

    def mark_sent(self, chat_id: int, key: str):
        self.db.execute("INSERT OR IGNORE INTO sent VALUES (?,?,?)", (chat_id, key, time.time()))
        self.db.execute("DELETE FROM notification_queue WHERE chat_id=? AND key=?", (chat_id, key))
        self.db.commit()

    def enqueue(self, chat_id, listings):
        self.db.executemany("INSERT OR IGNORE INTO notification_queue VALUES (?,?)",
                            [(chat_id, l.key) for l in listings if not self.was_sent(chat_id, l.key)])
        self.db.commit()

    def pending(self, chat_id):
        rows = self.db.execute(
            "SELECT l.data,l.first_seen,l.last_seen FROM notification_queue q JOIN listings l ON l.key=q.key "
            "WHERE q.chat_id=? AND l.dup_of IS NULL AND NOT EXISTS "
            "(SELECT 1 FROM sent s WHERE s.chat_id=q.chat_id AND s.key=q.key) "
            "ORDER BY l.first_seen DESC", (chat_id,))
        return [self._listing(row) for row in rows.fetchall()]

    def discard_pending(self, chat_id, key):
        self.db.execute("DELETE FROM notification_queue WHERE chat_id=? AND key=?", (chat_id, key))
        self.db.commit()

    def expire(self, listing):
        listing.available = False
        listing.unavailable_at = listing.unavailable_at or time.time()
        self.update_listing(listing)
        self.db.execute("DELETE FROM notification_queue WHERE key=?", (listing.key,))
        self.db.commit()

    def begin_run(self, kind, chat_id=None):
        run_id = uuid.uuid4().hex
        self.db.execute("INSERT INTO search_runs VALUES (?,?,?,?,?,?,?,?,?)",
                        (run_id, kind, chat_id, time.time(), None, "running", "queued", "{}", "{}"))
        self.db.commit()
        return run_id

    def update_run(self, run_id, phase, progress=None, stats=None, status="running"):
        row = self.db.execute("SELECT progress FROM search_runs WHERE id=?", (run_id,)).fetchone()
        data = json.loads(row[0]) if row else {}
        data.update(progress or {})
        self.db.execute("UPDATE search_runs SET phase=?,progress=?,stats=?,status=?,finished=? WHERE id=?",
                        (phase, json.dumps(data, ensure_ascii=False), json.dumps(stats or {}, ensure_ascii=False),
                         status, None if status == "running" else time.time(), run_id))
        self.db.commit()

    def latest_run(self, chat_id, running=False):
        condition = " AND status='running'" if running else ""
        order = "(chat_id IS NOT NULL) DESC,started DESC" if running else "started DESC"
        row = self.db.execute(
            "SELECT id,kind,started,finished,status,phase,progress,stats FROM search_runs "
            "WHERE (chat_id=? OR chat_id IS NULL)" + condition + " ORDER BY " + order + " LIMIT 1", (chat_id,)).fetchone()
        return self._run_record(row)

    @staticmethod
    def _run_record(row):
        if not row:
            return None
        keys = ["id", "kind", "started", "finished", "status", "phase", "progress", "stats"]
        result = dict(zip(keys, row))
        result["progress"], result["stats"] = json.loads(result["progress"]), json.loads(result["stats"])
        return result

    def get_run(self, run_id):
        row = self.db.execute(
            "SELECT id,kind,started,finished,status,phase,progress,stats FROM search_runs WHERE id=?",
            (run_id,)).fetchone()
        return self._run_record(row)

    def recover_runs(self):
        self.db.execute("UPDATE search_runs SET status='interrupted',phase='interrupted',finished=? WHERE status='running'",
                        (time.time(),))
        self.db.commit()

    def search_summary(self, chat_id):
        from .matcher import match
        listings = self.listings()
        user = self.user(chat_id)
        active = [l for l in listings if l.available]
        direct = [l for l in active if l.url and l.link_kind == "direct"]
        cheap = [l for l in direct if user and match(user[0], l, None).ok]
        checked, matching, rejected = 0, 0, {}
        for listing in cheap:
            analysis = self.analysis(listing.key)
            if analysis is None:
                continue
            checked += 1
            result = match(user[0], listing, analysis)
            matching += int(result.ok)
            for reason in result.reasons:
                rejected[reason] = rejected.get(reason, 0) + 1
        total = self.db.execute("SELECT count(*) FROM listings").fetchone()[0]
        sent = self.db.execute("SELECT count(*) FROM sent WHERE chat_id=?", (chat_id,)).fetchone()[0]
        return {"total": total, "unique": len(listings), "duplicates": total - len(listings),
                "available": len(active), "unavailable": len(listings) - len(active),
                "unresolved_links": len(active) - len(direct), "eligible": len(cheap),
                "analyzed": checked, "remaining": len(cheap) - checked, "matching": matching,
                "sent": sent, "queued": len(self.pending(chat_id)), "rejections": rejected,
                "active": bool(user and user[2]), "has_criteria": user is not None, "sources": self.stats()}

    def close(self):
        self.db.close()


def _meters(lat1, lon1, lat2, lon2):
    k = 111_320
    return math.hypot((lat1 - lat2) * k, (lon1 - lon2) * k * math.cos(math.radians(lat1)))
