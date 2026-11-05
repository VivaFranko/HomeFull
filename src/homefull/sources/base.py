"""Спільне для джерел: вилучення JSON зі сторінки, розбір чисел."""
import json
import re


def extract_assigned_value(html, marker):
    """Read an object or JSON-encoded string assignment without running JavaScript."""
    start = html.find(marker)
    if start < 0:
        return None
    assignment = html.find("=", start + len(marker))
    if assignment < 0:
        return None
    raw = html[assignment + 1:].lstrip()
    if raw.startswith("JSON.parse("):
        raw = raw[len("JSON.parse("):].lstrip()
    try:
        value, _ = json.JSONDecoder().raw_decode(raw)
        return json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError):
        return None


def extract_assigned_json(html: str, marker: str):
    """JSON-об'єкт після `marker =` (наприклад, window.__INITIAL_STATE__ = {...})."""
    i = html.find(marker)
    if i < 0:
        return None
    s = html[html.index("{", i):]
    depth, in_str, esc = 0, False, False
    for k, ch in enumerate(s):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                raw = s[:k + 1]
                # A public hydration object may contain JS undefined, never execute it.
                raw = re.sub(r'"(?:\\.|[^"\\])*"|\bundefined\b',
                             lambda m: "null" if m[0] == "undefined" else m[0], raw)
                return json.loads(raw)
    return None


def find_dicts(obj, pred, limit=500):
    """Усі вкладені dict, для яких pred(d) істинне (обхід у глибину)."""
    out, stack = [], [obj]
    while stack and len(out) < limit:
        o = stack.pop()
        if isinstance(o, dict):
            if pred(o):
                out.append(o)
                continue
            stack.extend(o.values())
        elif isinstance(o, list):
            stack.extend(o)
    return out


def num(text):
    """«1 200 $» -> 1200.0; «110 / 55 / 18 м²» -> 110.0 (перше число)."""
    if text is None:
        return None
    m = re.search(r"\d[\d\s  ]*(?:[.,]\d+)?", str(text))
    if not m:
        return None
    return float(re.sub(r"[\s  ]", "", m.group(0)).replace(",", "."))


def currency_of(text):
    t = str(text or "")
    if "$" in t or "USD" in t or "дол" in t:
        return "USD"
    if "€" in t or "EUR" in t:
        return "EUR"
    return "UAH"


def norm_district(name):
    """«Галицький р-н», «галицкий», «Halytskyi» -> «Галицький» (або None)."""
    if not name:
        return None
    from ..models import LVIV_DISTRICTS
    n = str(name).lower()
    stems = {"галиц": "Галицький", "залізнич": "Залізничний", "железнодор": "Залізничний",
             "личак": "Личаківський", "сихів": "Сихівський", "сыхов": "Сихівський",
             "франків": "Франківський", "франков": "Франківський",
             "шевчен": "Шевченківський"}
    for stem, full in stems.items():
        if stem in n:
            return full
    return name if name in LVIV_DISTRICTS else None
