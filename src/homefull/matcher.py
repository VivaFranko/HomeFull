"""Чи підходить оголошення користувачу: жорсткі умови + пояснення.

Невідоме (null) не відкидає оголошення - воно проходить з приміткою «уточніть», бо
більшість оголошень мовчить про тварин чи комісію, і відсіювати їх означало б пропустити
добрі варіанти. Відкидає лише явна суперечність («без тварин» при pets=True).
"""
from dataclasses import dataclass, field
import re

from .models import Analysis, Criteria, Listing
from .dates import age_text


@dataclass
class Match:
    ok: bool
    reasons: list[str] = field(default_factory=list)   # чому відкинуто
    notes: list[str] = field(default_factory=list)     # що уточнити / попередження


def district_of(l: Listing, a: Analysis | None):
    return l.district or (a.district if a else None)


def match(c: Criteria, l: Listing, a: Analysis | None) -> Match:
    m = Match(ok=True)

    def no(reason):
        m.ok = False
        m.reasons.append(reason)

    if not l.available:
        no("оголошення більше недоступне")
    if not l.url or l.link_kind != "direct":
        no("пряме посилання ще не визначене")

    if l.price_uah is None:
        no("ціна не вказана")
    else:
        if c.max_price_uah and l.price_uah > c.max_price_uah:
            no(f"ціна {l.price_uah:,.0f} > {c.max_price_uah:,.0f} грн".replace(",", " "))
        if c.min_price_uah and l.price_uah < c.min_price_uah:
            no("ціна нижча за мінімальну")
    if c.rooms and l.rooms and l.rooms not in c.rooms:
        no(f"{l.rooms} кімн. не входить у {c.rooms}")
    if c.min_area and l.area and l.area < c.min_area:
        no(f"площа {l.area:g} < {c.min_area:g} м²")
    if c.not_first_floor and l.floor == 1:
        no("перший поверх")
    location_text = " ".join(str(s or "") for s in (
        l.title, l.description, l.street, l.district, a.neighborhood if a else None)).casefold()
    for area in c.excluded_areas:
        pattern = r"леванд[іо]в|levandiv" if area.casefold().startswith("леванд") else re.escape(area.casefold())
        if re.search(pattern, location_text):
            no("виключена місцевість: " + area)
    if c.districts:
        d = district_of(l, a)
        if d is None:
            m.notes.append("район не визначено - перевірте адресу")
        elif d not in c.districts:
            no(f"район {d}")
    if a:
        if c.gas_stove is True and a.gas_stove is not True:
            no("газова плита не підтверджена" if a.gas_stove is None else "плита не газова")
        if c.gas_stove is False and a.gas_stove is True:
            no("газова плита")
        if a.whole_flat is False and not c.room_ok:
            no("здається кімната, а не квартира")
        if c.pets:
            clash = set(c.pet_kinds) & set(a.pets_excluded)
            if a.pets_allowed is False:
                no("без тварин")
            elif a.rules_only and a.pets_allowed is not True:
                no("дозвіл на тварин не підтверджено")
            elif clash:
                no("не можна: " + ", ".join(sorted(clash)))
            elif a.pets_allowed is None:
                m.notes.append("про тварин не сказано - уточніть")
        if c.no_commission:
            if a.commission_pct:
                no(f"комісія {a.commission_pct:g} %")
            elif a.commission_pct is None and a.owner is False:
                m.notes.append("агентство, про комісію не сказано - уточніть")
        if c.owner_only and a.owner is False:
            no("здає агентство")
        for r in a.restrictions:
            m.notes.append("обмеження: " + r)
        for f in a.red_flags:
            m.notes.append("⚠ " + f)
    elif c.pets or c.no_commission or c.owner_only:
        m.notes.append("текст ще не проаналізовано")
    return m


def format_message(l: Listing, a: Analysis | None, m: Match, fair=None) -> str:
    """Текст сповіщення в Telegram (HTML)."""
    from html import escape
    head = f"<b>{l.rooms or '?'}-кімн."
    if l.area:
        head += f", {l.area:g} м²"
    head += f" - {l.price_uah:,.0f} грн/міс</b>".replace(",", " ")
    if l.currency and l.currency != "UAH" and l.price_orig:
        head += f" ({l.price_orig:,.0f} {l.currency})".replace(",", " ")
    lines = [head]
    lines.append(age_text(l))
    if l.title:
        lines.append(escape(l.title[:160]))
    place = ", ".join(x for x in (district_of(l, a), l.street) if x)
    if place:
        lines.append("📍 " + escape(place))
    if l.floor:
        lines.append(f"🏢 поверх {l.floor}" + (f" з {l.floors}" if l.floors else ""))
    if a:
        facts = []
        if a.pets_allowed is not None:
            facts.append("🐾 з тваринами" if a.pets_allowed else "🚫 без тварин")
        if a.commission_pct is not None:
            facts.append("без комісії" if a.commission_pct == 0
                         else f"комісія {a.commission_pct:g} %")
        if a.owner is not None:
            facts.append("власник" if a.owner else "агентство")
        if a.gas_stove is not None:
            facts.append("газова плита" if a.gas_stove else "електрична/індукційна плита")
        if facts:
            lines.append(" · ".join(facts))
        if a.summary:
            lines.append("<i>" + escape(a.summary[:400]) + "</i>")
    if l.description:
        import re
        description = re.sub(r"\s+", " ", l.description).strip()
        lines.append(escape(description[:450]) + ("…" if len(description) > 450 else ""))
    if l.contact_name:
        lines.append("👤 " + escape(l.contact_name[:100]))
    if l.phones:
        lines.append("☎ " + " · ".join(escape(p) for p in l.phones[:3]))
    else:
        lines.append("☎ Телефон не вказаний — контакт через оголошення")
    if fair:
        pred, dev = fair
        word = "нижче" if dev < 0 else "вище"
        lines.append(f"💡 Оцінка моделі: ~{pred:,.0f} грн; ціна на {abs(dev):.0f} % {word}"
                     .replace(",", " ") + (" - вигідно" if dev <= -10 else ""))
    for n in m.notes[:5]:
        n = n[:160]
        lines.append(escape(n) if n.startswith("⚠") else "❔ " + escape(n))
    from urllib.parse import urlsplit
    host = urlsplit(l.url).hostname or l.source
    lines.append(f'<a href="{escape(l.url)}">Відкрити оголошення · {escape(host)}</a>')
    return "\n".join(lines)
