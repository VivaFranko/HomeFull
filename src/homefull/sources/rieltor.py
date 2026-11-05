"""rieltor.ua: картки каталогу оренди квартир у Львові (ціна, адреса, район, параметри,
опис і координати є прямо в картці - окремі сторінки не потрібні)."""
import re

from bs4 import BeautifulSoup

from ..fx import to_uah
from ..models import Listing
from ..dates import enrich_dates
from ..contacts import enrich_public, web_url
from .base import currency_of, norm_district, num

CATALOG = "https://rieltor.ua/lvov/flats-rent/"


class Rieltor:
    name = "rieltor"
    needs_details = True

    def __init__(self, client):
        self.http = client

    def page_url(self, page: int) -> str:
        return CATALOG if page <= 1 else f"{CATALOG}?page={page}"

    def parse_catalog(self, html: str) -> list[Listing]:
        soup = BeautifulSoup(html, "html.parser")
        out = []
        for c in soup.select("div.catalog-card"):
            cid = c.get("data-catalog-item-id")
            if not cid:
                continue
            price_text = (c.select_one(".catalog-card-price-title") or c).get_text(" ", strip=True)
            cur = currency_of(price_text)
            price = num(c.get("data-label") or price_text)
            details = (c.select_one(".catalog-card-details") or c).get_text(" ", strip=True)
            rooms = re.search(r"(\d+)\s*кімнат", details)
            area = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:/|м²)", details)
            fl = re.search(r"поверх\s*(\d+)\s*з\s*(\d+)", details)
            region = (c.select_one(".catalog-card-region") or c).get_text(" ", strip=True)
            addr = c.select_one(".catalog-card-address")
            desc = c.select_one(".catalog-card-description")
            link = c.select_one('a[href*="/flats-rent/view/"]')
            image = c.select_one("img.offer-photo-slider-slide-image, img[src]")
            out.append(Listing(
                source=self.name, source_id=cid,
                url=link.get("href") if link else f"https://rieltor.ua/flats-rent/view/{cid}/",
                price_orig=price, currency=cur, price_uah=to_uah(price, cur) if price else None,
                rooms=int(rooms.group(1)) if rooms else None,
                area=float(area.group(1).replace(",", ".")) if area else None,
                floor=int(fl.group(1)) if fl else None, floors=int(fl.group(2)) if fl else None,
                district=norm_district(region), street=addr.get_text(" ", strip=True) if addr else None,
                lat=_f(c.get("data-latitude")), lon=_f(c.get("data-longitude")),
                title=details[:80], description=desc.get_text(" ", strip=True) if desc else "",
                photo_url=web_url(image.get("data-src") or image.get("src")) if image else None))
            enrich_public(c, out[-1])
        return out

    def fetch(self, pages=1) -> list[Listing]:
        out = []
        for p in range(1, pages + 1):
            out += self.parse_catalog(self.http.get(self.page_url(p)))
        return out

    def details(self, listing: Listing) -> Listing:
        return self.parse_details(self.http.get(listing.url), listing)

    def parse_details(self, html: str, listing: Listing) -> Listing:
        soup = BeautifulSoup(html, "html.parser")
        description = None
        for section in soup.select(".offer-view-section"):
            title = section.select_one(".offer-view-section-title")
            if title and title.get_text(strip=True).casefold() == "опис":
                description = section.select_one(".offer-view-section-text")
                break
        attributes = [e.get_text(" ", strip=True)
                      for e in soup.select(".offer-view-communication-text")]
        if description is None and not attributes:
            raise ValueError("Rieltor: не знайдено опису та характеристик квартири")
        base = description.get_text(" ", strip=True) if description else listing.description
        listing.description = base + ("\nХарактеристики:\n" + "\n".join(attributes) if attributes else "")
        listing.details_version = 1
        enrich_dates(soup, listing)
        return enrich_public(soup, listing)


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None
