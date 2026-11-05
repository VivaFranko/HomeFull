"""Джерела оголошень про довгострокову оренду квартир у Львові.

OLX читається з публічного HTML, якщо сайт дозволяє доступ. Відмова 403/429
зупиняє збір цього джерела; решта джерел продовжують працювати.
"""
from .domria import DomRia
from .flatfy import Flatfy
from .rieltor import Rieltor
from .olx import Olx

ALL = [DomRia, Rieltor, Flatfy, Olx]
