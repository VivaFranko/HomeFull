"""Publication age is separate from refresh dates and the bot's observation period."""
import json
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .config import TIMEZONE

MONTHS = {name: number for number, name in enumerate(
    ["січня", "лютого", "березня", "квітня", "травня", "червня", "липня", "серпня",
     "вересня", "жовтня", "листопада", "грудня"], 1)}


def timestamp(value, now=None):
    """Return (UTC timestamp, precision). Never infer publication from refresh time."""
    if value is None or value == "":
        return None, None
    if isinstance(value, (int, float)):
        number = float(value)
        return (number / 1000 if number > 1e12 else number), "time"
    text = str(value).strip()
    zone = ZoneInfo(TIMEZONE)
    current = datetime.fromtimestamp(now if now is not None else time.time(), zone)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=zone)
        return parsed.timestamp(), "date" if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text) else "time"
    except ValueError:
        pass
    lower = text.casefold()
    relative = re.search(r"(сьогодні|вчора|сегодня|вчера).*?(\d{1,2}):(\d{2})", lower)
    if relative:
        day = current - timedelta(days=1 if relative[1] in {"вчора", "вчера"} else 0)
        try:
            return day.replace(hour=int(relative[2]), minute=int(relative[3]), second=0, microsecond=0).timestamp(), "time"
        except ValueError:
            return None, None
    date = re.search(r"(\d{1,2})\s+(" + "|".join(MONTHS) + r")\s+(\d{4})", lower)
    if date:
        clock = re.search(r"(\d{1,2}):(\d{2})", lower)
        try:
            parsed = datetime(int(date[3]), MONTHS[date[2]], int(date[1]),
                              int(clock[1]) if clock else 0, int(clock[2]) if clock else 0, tzinfo=zone)
            return parsed.timestamp(), "time" if clock else "date"
        except ValueError:
            pass
    return None, None


def set_published(listing, value, basis="source", now=None, authoritative=False):
    parsed, precision = timestamp(value, now)
    if parsed is None:
        return
    # Prefer a source publication over the aggregator's insertion date.
    if authoritative or listing.published_at is None or (listing.publication_basis != "source" and basis == "source"):
        listing.published = str(value)
        listing.published_at = parsed
        listing.published_precision = precision
        listing.publication_basis = basis


def enrich_dates(soup, listing, now=None):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            state = json.loads(script.get_text())
        except (ValueError, TypeError):
            continue
        stack = [state]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, dict):
                # Ignore unrelated recommendations: only the advert/product schema.
                kind = item.get("@type", [])
                kinds = [kind] if isinstance(kind, str) else kind
                if set(kinds or []) & {"Product", "Apartment", "RealEstateListing", "Offer"}:
                    set_published(listing, item.get("datePosted") or item.get("datePublished") or item.get("dateCreated"), now=now)
                    changed, _ = timestamp(item.get("dateModified"), now)
                    if changed:
                        listing.refreshed_at = changed
                stack.extend(value for key, value in item.items() if key in {"@graph", "offers", "mainEntity"})
    node = soup.select_one('[data-testid="ad-posted-at"], [itemprop="datePosted"], [itemprop="datePublished"]')
    if node:
        basis = "source_display" if node.get("data-testid") == "ad-posted-at" else "source"
        set_published(listing, node.get("datetime") or node.get("content") or node.get_text(" ", strip=True), basis=basis, now=now)


def duration(seconds, date_only=False):
    seconds = max(0, int(seconds))
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    if date_only:
        return f"{days} дн." if days else "сьогодні"
    if days:
        return f"{days} дн. {hours} год."
    if hours:
        return f"{hours} год. {remainder // 60} хв."
    return f"{seconds // 60} хв." if seconds >= 60 else "менше хвилини"


def age_text(listing, now=None):
    current = now if now is not None else time.time()
    end = listing.unavailable_at or current
    if listing.published_at is not None:
        date_only = listing.published_precision == "date"
        published = datetime.fromtimestamp(listing.published_at, ZoneInfo(TIMEZONE))
        date = published.strftime("%d.%m.%Y" if date_only else "%d.%m.%Y %H:%M")
        if listing.publication_basis == "source_display":
            return f"🕒 Дата на сайті: {date} (може бути датою оновлення)"
        label = "У ЛУН" if listing.publication_basis == "aggregator" else "Оголошенню"
        if date_only:
            days = max(0, (datetime.fromtimestamp(end, ZoneInfo(TIMEZONE)).date() - published.date()).days)
            elapsed = f"{days} дн." if days else "сьогодні"
        else:
            elapsed = duration(end - listing.published_at)
        return f"🕒 {label}: {elapsed} · дата на сайті: {date}"
    if listing.first_seen_at is not None:
        return f"🕒 Дата публікації невідома · спостерігаю {duration(end - listing.first_seen_at)}"
    return "🕒 Дату публікації сайт не вказав"
