"""DOM.RIA: каталог оренди квартир у Львові (JSON-стан сторінки) + сторінка оголошення
(повний опис і перелік зручностей, наприклад «Можна з тваринами»).

Офіційний API (developers.ria.com) потребує ключа; коли ключ з'явиться, цей клас можна
замінити на API-клієнт без змін у решті бота.
"""
import re

from bs4 import BeautifulSoup

from ..fx import to_uah
from ..models import Listing
from ..dates import set_published, enrich_dates
from ..contacts import enrich_public, web_url, phone_numbers
from .base import extract_assigned_json, find_dicts, norm_district

BASE = "https://dom.ria.com"
CATALOG = BASE + "/uk/arenda-kvartir/lvov/"


class DomRia:
    name = "domria"
    needs_details = True

    def __init__(self, client):
        self.http = client

    def page_url(self, page: int) -> str:
        return CATALOG if page <= 1 else f"{CATALOG}?page={page}"

    def parse_catalog(self, html: str) -> list[Listing]:
        state = extract_assigned_json(html, "window.__INITIAL_STATE__")
        items = find_dicts(state or {}, lambda d: "realty_id" in d and "priceArr" in d)
        out = []
        for d in items:
            uah = d.get("priceArr", {}).get("3")
            price_uah = float(str(uah).replace(" ", "")) if uah else None
            url = BASE + "/uk/" + d["beautiful_url"] if d.get("beautiful_url") else None
            out.append(Listing(
                source=self.name, source_id=str(d["realty_id"]), url=url,
                price_uah=price_uah, price_orig=d.get("price"),
                currency={"грн": "UAH", "$": "USD", "€": "EUR"}.get(d.get("currency_type"), "UAH"),
                rooms=d.get("rooms_count"), area=d.get("total_square_meters"),
                floor=d.get("floor"), floors=d.get("floors_count"),
                district=norm_district(d.get("district_name_uk") or d.get("district_name")),
                street=d.get("street_name_uk") or d.get("streetName"),
                lat=_f(d.get("latitude")), lon=_f(d.get("longitude")),
                title=f"{d.get('rooms_count', '?')}-кімн., {d.get('total_square_meters', '?')} м²",
                description=(d.get("description_uk") or d.get("description") or ""),
                published=d.get("publishing_date"), photo_url=web_url(d.get("mainPhoto")),
                phones=phone_numbers(d.get("description_uk") or d.get("description") or "")))
            set_published(out[-1], d.get("publishing_date"))
        return out

    def parse_details(self, html: str, listing: Listing) -> Listing:
        state = extract_assigned_json(html, "window.__INITIAL_STATE__") or {}
        own = find_dicts(state, lambda d: str(d.get("realty_id")) == listing.source_id
                         and ("description_uk" in d or "description" in d))
        desc = ""
        for d in own:
            desc = d.get("description_uk") or d.get("description") or desc
            if desc:
                break
        soup = BeautifulSoup(html, "html.parser")
        comfort = [x.get_text(" ", strip=True) for x in soup.select(".comfort-list-text")]
        # опис зі сторінки, а якщо там порожньо - з каталогу (без рядка «Зручності» минулого разу)
        base = desc or listing.description.split("\nЗручності:")[0]
        parts = [re.sub(r"\s+", " ", base).strip()]
        if comfort:
            parts.append("Зручності: " + "; ".join(dict.fromkeys(comfort)))
        listing.description = "\n".join(p for p in parts if p)
        listing.details_version = 1
        enrich_dates(soup, listing)
        return enrich_public(soup, listing)

    def fetch(self, pages=1) -> list[Listing]:
        out = []
        for p in range(1, pages + 1):
            out += self.parse_catalog(self.http.get(self.page_url(p)))
        return out

    def details(self, listing: Listing) -> Listing:
        if listing.url:
            self.parse_details(self.http.get(listing.url), listing)
        return listing


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


__all__ = ["DomRia", "to_uah"]
