"""Район Львова за координатами: голосування k найближчих оголошень з відомим районом.

Джерела rieltor і Flatfy завжди вказують адміністративний район, DOM.RIA - часто лише
мікрорайон («Пасіки»). Межі районів нерегулярні, тому замість полігонів беремо k-NN по
вже зібраних оголошеннях; точність перевіряється leave-one-out у scripts/evaluate.py.
"""
import math
from collections import Counter

from .models import Listing


def _dist(a, b):
    k = 111_320
    return math.hypot((a[0] - b[0]) * k, (a[1] - b[1]) * k * math.cos(math.radians(a[0])))


class DistrictKNN:
    def __init__(self, k=7, max_dist_m=1500):
        self.k, self.max_dist = k, max_dist_m
        self.points: list[tuple[float, float, str]] = []

    def fit(self, listings: list[Listing]):
        self.points = [(l.lat, l.lon, l.district) for l in listings
                       if l.lat and l.lon and l.district]
        return self

    def predict(self, lat, lon, exclude_self=False):
        if lat is None or lon is None or not self.points:
            return None
        d = sorted((_dist((lat, lon), (p[0], p[1])), p[2]) for p in self.points)
        if exclude_self:
            d = d[1:]
        near = [name for dist, name in d[: self.k] if dist <= self.max_dist]
        if not near:
            return None
        name, votes = Counter(near).most_common(1)[0]
        return name if votes > len(near) / 2 else None     # лише явна більшість
