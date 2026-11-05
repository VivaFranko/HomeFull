"""Public advert media and Ukrainian phone numbers; no account data."""
import re
from urllib.parse import urljoin, urlsplit


def web_url(value, base=""):
    url = urljoin(base, str(value or "").strip())
    parts = urlsplit(url)
    return url if parts.scheme in {"https", "http"} and parts.hostname else None


def phone_numbers(text):
    out = []
    for match in re.finditer(r"(?<!\d)(?:\+?380|0)(?:[\s().-]*\d){9}(?!\d)", text or ""):
        digits = re.sub(r"\D", "", match[0])
        normalized = "+38" + digits if len(digits) == 10 else "+" + digits
        if normalized not in out:
            out.append(normalized)
    return out


def enrich_public(soup, listing):
    listing.phones = list(dict.fromkeys([
        *listing.phones, *phone_numbers(listing.description),
        *(phone for a in soup.select('a[href^="tel:"]')
          for phone in phone_numbers(a.get("href", "")))]))[:3]
    image = soup.select_one('meta[property="og:image"]')
    if image:
        listing.photo_url = web_url(image.get("content"), listing.url) or listing.photo_url
    name = soup.select_one('[data-testid="user-profile-user-name"], [itemprop="seller"] [itemprop="name"]')
    if name:
        listing.contact_name = name.get_text(" ", strip=True)[:100]
    return listing
