"""Модель даних: оголошення, критерії користувача, результат аналізу LLM."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional


@dataclass
class Listing:
    source: str                     # domria | flatfy | rieltor | olx
    source_id: str
    url: str
    price_uah: Optional[float] = None
    price_orig: Optional[float] = None
    currency: Optional[str] = None  # UAH | USD | EUR
    rooms: Optional[int] = None
    area: Optional[float] = None
    floor: Optional[int] = None
    floors: Optional[int] = None
    district: Optional[str] = None
    street: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    title: str = ""
    description: str = ""
    published: Optional[str] = None
    photo_url: Optional[str] = None
    contact_name: Optional[str] = None
    phones: list[str] = field(default_factory=list)
    details_version: int = 0
    available: bool = True
    published_at: Optional[float] = None
    published_precision: Optional[str] = None
    publication_basis: Optional[str] = None
    refreshed_at: Optional[float] = None
    first_seen_at: Optional[float] = None
    last_seen_at: Optional[float] = None
    unavailable_at: Optional[float] = None
    link_kind: str = "direct"

    @property
    def key(self) -> str:
        return f"{self.source}:{self.source_id}"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Criteria:
    """Критерії користувача. Поля None - «не важливо»."""
    max_price_uah: Optional[float] = None
    min_price_uah: Optional[float] = None
    rooms: list[int] = field(default_factory=list)          # [] - будь-яка кількість
    min_area: Optional[float] = None
    districts: list[str] = field(default_factory=list)      # райони Львова, [] - будь-який
    excluded_areas: list[str] = field(default_factory=list)
    gas_stove: Optional[bool] = None
    pets: Optional[bool] = None                             # True - потрібно з тваринами
    pet_kinds: list[str] = field(default_factory=list)      # які тварини є: «кіт», «собака»
    room_ok: Optional[bool] = None                          # True - згоден і на кімнату
    no_commission: Optional[bool] = None                    # True - лише без комісії
    owner_only: Optional[bool] = None
    not_first_floor: Optional[bool] = None
    extra: str = ""                                         # побажання вільним текстом

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Criteria":
        known = {k: d.get(k) for k in cls.__dataclass_fields__ if k in d}
        c = cls(**known)
        c.rooms = [int(r) for r in (c.rooms or [])]
        c.districts = list(c.districts or [])
        c.excluded_areas = list(c.excluded_areas or [])
        c.pet_kinds = [k.lower() for k in (c.pet_kinds or [])]
        return c


@dataclass
class Analysis:
    """Те, що LLM витягує з тексту оголошення (немає у фільтрах сайтів)."""
    pets_allowed: Optional[bool] = None
    pets_excluded: list[str] = field(default_factory=list)  # «кіт», «собака» - прямо заборонені
    whole_flat: Optional[bool] = None           # False - здається кімната / ліжко-місце
    commission_pct: Optional[float] = None      # 0 - без комісії
    owner: Optional[bool] = None                # True - власник, False - рієлтор
    district: Optional[str] = None              # нормалізований район Львова
    neighborhood: Optional[str] = None
    gas_stove: Optional[bool] = None
    furnished: Optional[bool] = None
    utilities_included: Optional[bool] = None
    deposit: Optional[bool] = None
    restrictions: list[str] = field(default_factory=list)   # «лише для сім'ї», «без дітей»...
    red_flags: list[str] = field(default_factory=list)      # «передоплата до огляду» тощо
    summary: str = ""
    rules_only: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Analysis":
        a = cls(**{k: d.get(k) for k in cls.__dataclass_fields__ if k in d})
        a.restrictions = list(a.restrictions or [])
        a.red_flags = list(a.red_flags or [])
        a.pets_excluded = [k.lower() for k in (a.pets_excluded or [])]
        return a


LVIV_DISTRICTS = ["Галицький", "Залізничний", "Личаківський", "Сихівський", "Франківський",
                  "Шевченківський"]
