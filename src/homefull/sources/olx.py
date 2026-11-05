"""Публічні HTML-сторінки OLX: каталог і опис, без контактів та приватного API."""
import re
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from ..fx import to_uah
from ..models import Listing
from ..dates import enrich_dates, timestamp, set_published
from ..contacts import enrich_public, web_url
from .base import currency_of, norm_district, num, extract_assigned_value

CATALOG = "https://www.olx.ua/uk/nedvizhimost/kvartiry/dolgosrochnaya-arenda-kvartir/lvov/"


def offer_url(href):
    url = urlsplit(urljoin(CATALOG, href or ""))
    if url.scheme != "https" or url.hostname not in {"www.olx.ua", "olx.ua"}:
        return None
    if not re.search(r"/obyavlenie/.*-ID[\w]+\.html$", url.path):
        return None
    return urlunsplit((url.scheme, url.netloc, url.path, "", ""))


def room_count(text):
    m = re.search(r"(?<!\d)(\d+)\s*[-–]?\s*(?:кімн|к[.\s-]|комн)", text, re.I)
    return int(m[1]) if m else None


class Olx:
    name = "olx"
    needs_details = True

    def __init__(self, client):
        self._owns_client = hasattr(client, "browser")
        self.http = client.browser() if self._owns_client else client

    def close(self):
        if self._owns_client:
            self.http.close()

    def page_url(self, page: int) -> str:
        # Свіжі оголошення мають потрапляти в першу сторінку кожного циклу.
        params = {"search[order]": "created_at:desc"}
        if page > 1:
            params["page"] = page
        return f"{CATALOG}?{urlencode(params)}"

    def parse_catalog(self, html: str) -> list[Listing]:
        soup = BeautifulSoup(html, "html.parser")
        cards = soup.select('[data-cy="l-card"], [data-testid="l-card"]')
        if not cards:
            if soup.select_one('[data-testid="no-results"], [data-testid="listing-no-results"]'):
                return []
            raise ValueError("OLX: не знайдено карток; можлива зміна розмітки або сторінка захисту")
        out, seen = [], set()
        for card in cards:
            link = card.select_one('a[href*="/obyavlenie/"]')
            url = offer_url(link.get("href")) if link else None
            if not url:
                continue
            # ID з URL стабільний навіть після підняття оголошення чи зміни ціни.
            sid = re.search(r"-ID([\w]+)\.html$", urlsplit(url).path)[1]
            if sid in seen:
                continue
            seen.add(sid)
            title = card.select_one("h4, h6, h3")
            title = title.get_text(" ", strip=True) if title else link.get_text(" ", strip=True)
            price_el = card.select_one('[data-testid="ad-price"]')
            price_text = price_el.get_text(" ", strip=True) if price_el else ""
            price, cur = num(price_text), currency_of(price_text)
            location = card.select_one('[data-testid="location-date"]')
            location = location.get_text(" ", strip=True) if location else ""
            area = re.search(r"(\d+(?:[.,]\d+)?)\s*м[²2]", card.get_text(" ", strip=True))
            image = card.select_one("img[src], img[data-src]")
            photo = web_url(image.get("data-src") or image.get("src"), url) if image else None
            out.append(Listing(
                source=self.name, source_id=sid, url=url, title=title,
                price_orig=price, currency=cur, price_uah=to_uah(price, cur) if price else None,
                rooms=room_count(title), area=float(area[1].replace(",", ".")) if area else None,
                district=norm_district(location), photo_url=photo))
            out[-1].refreshed_at, _ = timestamp(location)
        if not out:
            raise ValueError("OLX: картки є, але посилання оголошень не розпізнано")
        return out

    def parse_details(self, html: str, listing: Listing) -> Listing:
        soup = BeautifulSoup(html, "html.parser")
        state = extract_assigned_value(html, "window.__PRERENDERED_STATE__") or {}
        own = (state.get("ad") or {}).get("ad") or {}
        set_published(listing, own.get("createdTime"), authoritative=True)
        refreshed, _ = timestamp(own.get("lastRefreshTime"))
        if refreshed is not None:
            listing.refreshed_at = refreshed
        desc = soup.select_one('[data-testid="ad_description"], [data-cy="ad_description"]')
        if desc is None:
            raise ValueError("OLX: не знайдено опис оголошення")
        container = soup.select_one('[data-testid="ad-parameters-container"], [data-testid="ad-params-container"]')
        params = container.select("li") if container else []
        if not params and container:
            params = container.select("p")
        texts = [p.get_text(" ", strip=True) for p in params]
        listing.description = "\n".join([desc.get_text(" ", strip=True), *texts])
        for text in texts:
            label, sep, value = text.partition(":")
            if not sep:
                continue
            label = label.strip().lower()
            value = num(value)
            if value is None:
                continue
            if label in {"кількість кімнат", "количество комнат"}:
                listing.rooms = int(value)
            elif label in {"загальна площа", "общая площадь"}:
                listing.area = value
            elif label in {"поверх", "этаж"}:
                listing.floor = int(value)
            elif label in {"поверховість", "этажность"}:
                listing.floors = int(value)
        enrich_dates(soup, listing)
        return enrich_public(soup, listing)

    def fetch(self, pages=1) -> list[Listing]:
        out, seen = [], set()
        for page in range(1, pages + 1):
            listings = self.parse_catalog(self.http.get(self.page_url(page)))
            if not listings:
                break
            for listing in listings:
                if listing.key not in seen:
                    seen.add(listing.key)
                    out.append(listing)
        return out

    def details(self, listing: Listing) -> Listing:
        self.parse_details(self.http.get(listing.url), listing)
        reveal = getattr(self.http, "reveal_phone", None)
        if not listing.phones and reveal:
            html = reveal(listing.url)
            if html:
                enrich_public(BeautifulSoup(html, "html.parser"), listing)
        listing.details_version = 1
        return listing
