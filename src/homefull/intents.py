"""Route short search/status questions without changing rental preferences."""
import re


def is_search_question(text):
    t = re.sub(r"\s+", " ", text.casefold()).strip(" ?!.,…")
    if t in {"знайшов щось", "щось знайшов", "ти щось знайшов", "ти знайшов щось",
             "є щось", "що знайшов", "що є", "щось є", "що нового", "є квартири",
             "покажи квартири", "покажи варіанти", "покажи знайдене", "перевір квартири",
             "знайшов квартиру", "знайшов щось нове", "вже щось знайшов"}:
        return True
    return bool(re.fullmatch(r"(?:чи )?є (?:вже )?(?:нові |якісь )?варіанти", t))


def has_preferences(criteria):
    from .models import Criteria
    return criteria.to_dict() != Criteria().to_dict()
