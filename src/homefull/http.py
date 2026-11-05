"""«Ввічливий» HTTP-клієнт для збору оголошень.

- перевіряє robots.txt кожного сайту і не завантажує заборонені шляхи;
- не частіше одного запиту на сайт кожні MIN_INTERVAL секунд;
- не обходить захист від ботів: відповідь 403/429 - сигнал зупинитись, а не міняти
  повторювати запит із проксі. Для OLX використовується нативний Chromium transport.
"""
import logging
import time
import urllib.parse
import urllib.robotparser

import requests

log = logging.getLogger(__name__)
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/130.0 Safari/537.36 HomeFull/0.1 (+https://github.com/VivaFranko/HomeFull)")
MIN_INTERVAL = 3.0


class Blocked(RuntimeError):
    """Сайт відмовив (403/429) або шлях заборонено robots.txt."""


class PoliteClient:
    def __init__(self, min_interval=MIN_INTERVAL, session=None):
        self.min_interval = min_interval
        self.s = session or requests.Session()
        self.s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "uk,en;q=0.8"})
        self._last: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser] = {}

    def browser(self):
        from .browser import ChromiumSession
        return PoliteClient(self.min_interval, session=ChromiumSession())

    def close(self):
        self.s.close()

    def reveal_phone(self, url):
        reveal = getattr(self.s, "reveal_phone", None)
        if reveal is None:
            return ""
        host = urllib.parse.urlsplit(url).netloc
        wait = self._last.get(host, 0) + self.min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            return reveal()
        finally:
            self._last[host] = time.monotonic()

    def allowed(self, url: str) -> bool:
        host = urllib.parse.urlsplit(url)
        base = f"{host.scheme}://{host.netloc}"
        rp = self._robots.get(base)
        if rp is None:
            rp = urllib.robotparser.RobotFileParser()
            try:
                r = self.s.get(base + "/robots.txt", timeout=15)
                self._last[host.netloc] = time.monotonic()
                if r.status_code in (401, 403, 429):
                    raise Blocked(f"{host.netloc} robots.txt відповів {r.status_code} - зупиняємось")
                if r.status_code not in (404, 410):
                    r.raise_for_status()
                rp.parse(r.text.splitlines() if r.ok else [])
            except requests.RequestException as e:
                raise Blocked(f"Не вдалося перевірити robots.txt для {base}") from e
            self._robots[base] = rp
        return rp.can_fetch("*", url)

    def get(self, url: str) -> str:
        if not self.allowed(url):
            raise Blocked(f"robots.txt забороняє {url}")
        host = urllib.parse.urlsplit(url).netloc
        wait = self._last.get(host, 0) + self.min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        r = self.s.get(url, timeout=30)
        self._last[host] = time.monotonic()
        if r.status_code in (403, 429):
            raise Blocked(f"{host} відповів {r.status_code} - зупиняємось")
        r.raise_for_status()
        # Якщо сервер не вказав charset, requests читає текст як ISO-8859-1 і кирилиця
        # перетворюється на «Ð¡Ð¸…» (так було з Flatfy). Сайти українські - беремо UTF-8.
        if "charset" not in r.headers.get("Content-Type", "").lower():
            r.encoding = "utf-8"
        return r.text
