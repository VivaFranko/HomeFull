"""Public LUN state supplies original advert URLs and full text.

Legacy source/photo IDs preserve the existing database and delivery history.
"""
import hashlib
import json
import re
from dataclasses import fields, replace
from urllib.parse import quote, urlsplit

from bs4 import BeautifulSoup

from ..contacts import phone_numbers, web_url
from ..dates import set_published, enrich_dates
from ..fx import to_uah
from ..models import Listing
from .base import extract_assigned_json, norm_district

CATALOG = "https://flatfy.ua/uk/" + quote("оренда-квартир-львів")


def property_link(value):
    url = web_url(value)
    if not url:
        return None
    parsed = urlsplit(url)
    if parsed.username or parsed.password:
        return None
    host = (parsed.hostname or "").casefold()
    if host not in {"lun.ua", "www.lun.ua", "flatfy.ua", "www.flatfy.ua", "rieltor.ua",
                     "www.rieltor.ua", "dom.ria.com", "olx.ua", "www.olx.ua"}:
        return None
    path = parsed.path
    if (re.search(r"/(?:uk/)?realty/\d+", path)
            or re.search(r"/flats-rent/view/\d+", path)
            or re.search(r"/obyavlenie/.+-ID\w+\.html", path)
            or re.search(r"/realty-.+\d+\.html", path)):
        return url
    return None


class Flatfy:
    name = "flatfy"
    needs_details = True

    def __init__(self, client):
        self.http = client

    def close(self):
        pass

    def page_url(self, page):
        return CATALOG if page <= 1 else f"{CATALOG}?page={page}"

    def parse_catalog(self, html):
        state = (extract_assigned_json(html, "window.INITIAL_STATE")
                 or extract_assigned_json(html, "window.__INITIAL_STATE__") or {})
        items = (state.get("search", {}).get("realties", {}) or {}).get("list", [])
        if items:
            photos = {}
            soup = BeautifulSoup(html, "html.parser")
            for script in soup.select('script[type="application/ld+json"]'):
                try:
                    schema = json.loads(script.get_text())
                except ValueError:
                    continue
                if not isinstance(schema, dict):
                    continue
                for element in schema.get("itemListElement", []):
                    images = (element.get("item") or {}).get("image") or []
                    for image in [images] if isinstance(images, str) else images:
                        if not isinstance(image, str):
                            continue
                        match = re.search(r"/(\d+)\.(?:webp|jpg|jpeg|png)", image)
                        if match:
                            photos[match[1]] = image
            return [self._from_state(item, photos) for item in items]
        return self._from_schema(html)

    def _from_state(self, item, photos=None):
        images = item.get("images") or []
        first_image = str(images[0].get("image_id", "")) if images else ""
        sid = first_image.rsplit("/", 1)[-1] or str(item["id"])
        url = property_link(item.get("url_raw")) or f"https://lun.ua/uk/realty/{int(item['id'])}"
        photo = f"https://market-images.lunstatic.net/lun-ua/960/720/images/{first_image}.jpg" if first_image else None
        photo = (photos or {}).get(sid) or photo
        entities = item.get("geo_entities") or []
        district = next((e.get("name") for e in entities if e.get("type") == "district"), None)
        micro = next((e.get("name") for e in entities if e.get("subtype") == "microdistrict"), None)
        position = item.get("location") or []
        price, currency = item.get("price"), item.get("currency") or "UAH"
        description = item.get("text") or ""
        if micro:
            description += "\nМікрорайон: " + micro
        pets = item.get("pets_allowed_full")
        if pets in {"yes", "no"}:
            description += "\nДомашні улюбленці: " + ("Так" if pets == "yes" else "Ні")
        listing = Listing(
            source=self.name, source_id=sid, url=url,
            price_orig=price, currency=currency, price_uah=to_uah(price, currency) if price else None,
            rooms=item.get("room_count"), area=item.get("area_total"), floor=item.get("floor"),
            floors=item.get("floor_count"), street=item.get("header"), district=norm_district(district),
            lat=position[1] if len(position) == 2 else None,
            lon=position[0] if len(position) == 2 else None,
            title=item.get("header") or "", description=description, photo_url=photo,
            phones=phone_numbers(description), available=item.get("group_page_status", "active") == "active")
        set_published(listing, item.get("insert_time"), basis="aggregator")
        return listing

    def _from_schema(self, html):
        out = []
        soup = BeautifulSoup(html, "html.parser")
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                data = json.loads(script.get_text())
            except ValueError:
                continue
            if not isinstance(data, dict) or not data.get("numberOfItems"):
                continue
            for element in data.get("itemListElement", []):
                item = element.get("item") or {}
                offer, geo = item.get("offers") or {}, item.get("geo") or {}
                street = item.get("name") or (item.get("address") or {}).get("streetAddress")
                images = item.get("image") or []
                image = images if isinstance(images, str) else (images[0] if images else "")
                sid_match = re.search(r"/(\d+)\.\w+$", image)
                sid = sid_match[1] if sid_match else hashlib.sha256(
                    f"{street}|{offer.get('price')}|{item.get('numberOfRooms')}".encode()).hexdigest()[:12]
                url = property_link(item.get("url") or offer.get("url"))
                price, currency = offer.get("price"), offer.get("priceCurrency", "UAH")
                listing = Listing(
                    source=self.name, source_id=sid, url=url or "", link_kind="direct" if url else "unresolved",
                    price_orig=price, currency=currency, price_uah=to_uah(price, currency) if price else None,
                    rooms=item.get("numberOfRooms"), street=street,
                    district=norm_district((offer.get("areaServed") or {}).get("name")),
                    lat=geo.get("latitude"), lon=geo.get("longitude"), title=street or "",
                    description=item.get("description") or "", photo_url=web_url(image),
                    phones=phone_numbers(item.get("description") or ""))
                set_published(listing, offer.get("availabilityStarts"), basis="aggregator")
                out.append(listing)
        if not out and not soup.select_one('[data-testid="no-results"]'):
            raise ValueError("LUN: змінилась розмітка або каталог недоступний")
        return out

    def fetch(self, pages=1):
        out = {}
        for page in range(1, pages + 1):
            listings = self.parse_catalog(self.http.get(self.page_url(page)))
            if not listings:
                break
            out.update({listing.key: listing for listing in listings})
        return list(out.values())

    def details(self, listing):
        from .olx import Olx
        from .rieltor import Rieltor
        from .domria import DomRia
        host = (urlsplit(listing.url).hostname or "").removeprefix("www.")
        cls = {"olx.ua": Olx, "rieltor.ua": Rieltor, "dom.ria.com": DomRia}.get(host)
        if cls:
            provider = cls(self.http)
            native_id = listing.source_id
            if cls is DomRia:
                match = re.search(r"-(\d+)\.html", listing.url)
                native_id = match[1] if match else native_id
            native = replace(listing, source=provider.name, source_id=native_id)
            try:
                provider.details(native)
                for field in fields(Listing):
                    if field.name not in {"source", "source_id", "url"}:
                        setattr(listing, field.name, getattr(native, field.name))
            finally:
                close = getattr(provider, "close", None)
                if close:
                    close()
        else:
            # A LUN-owned advert has its own public detail page.
            html = self.http.get(listing.url)
            enrich_dates(BeautifulSoup(html, "html.parser"), listing)
        listing.details_version = 1
        return listing
