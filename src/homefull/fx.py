"""Офіційний курс НБУ для перерахунку цін у гривні (кеш на добу)."""
import datetime as dt
import time

import requests

API = "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?json"
_cache: dict = {"day": None, "rates": {}, "ts": 0.0}


def rates() -> dict:
    today = dt.date.today()
    if _cache["day"] != today or time.time() - _cache["ts"] > 6 * 3600:
        data = requests.get(API, timeout=20).json()
        _cache.update(day=today, ts=time.time(),
                      rates={row["cc"]: float(row["rate"]) for row in data})
    return _cache["rates"]


def to_uah(amount, currency: str):
    if amount is None:
        return None
    cur = (currency or "UAH").upper()
    if cur in ("UAH", "ГРН"):
        return float(amount)
    return float(amount) * rates()[cur]
