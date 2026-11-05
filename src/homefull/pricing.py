"""Необов'язкова модель оцінки ціни оренди.

Той самий підхід: градієнтний бустинг на деревах (LightGBM), ціль - ln(ціна), оцінка на
крос-валідації. Ознаки: кімнати, площа, поверх, поверховість, район, координати.
Для оголошення бот показує, наскільки ціна нижча чи вища за прогноз моделі.
"""
import json
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from .models import LVIV_DISTRICTS, Listing

FEATURES = ["rooms", "area", "floor", "floors", "lat", "lon", "district_code", "src_code"]
SOURCES = ["domria", "rieltor", "flatfy"]


def frame(listings: list[Listing]) -> pd.DataFrame:
    df = pd.DataFrame([l.to_dict() for l in listings])
    df["district_code"] = df["district"].map({d: i for i, d in enumerate(LVIV_DISTRICTS)})
    df["src_code"] = df["source"].map({s: i for i, s in enumerate(SOURCES)})
    for c in FEATURES:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def model():
    import lightgbm as lgb
    return lgb.LGBMRegressor(n_estimators=400, learning_rate=0.04, num_leaves=15,
                             min_child_samples=10, subsample=0.8, subsample_freq=1,
                             colsample_bytree=0.8, reg_lambda=1.0, random_state=42, verbose=-1)


def clean(df: pd.DataFrame) -> pd.DataFrame:
    """Без явних помилок введення: ціна 3-300 тис. грн, площа 12-400 м²."""
    ok = df["price_uah"].between(3000, 300000) & (df["area"].isna() | df["area"].between(12, 400))
    return df[ok & df["rooms"].notna()]


class FairPrice:
    def __init__(self, path=None):
        self.m = None
        self.meta = {}
        if path and Path(path).exists():
            self.m, self.meta = joblib.load(path)

    def fit(self, listings: list[Listing], path=None):
        from sklearn.model_selection import KFold
        df = clean(frame(listings))
        X, y = df[FEATURES], np.log(df["price_uah"])
        oof = np.zeros(len(df))
        for tr, va in KFold(5, shuffle=True, random_state=42).split(X):
            oof[va] = model().fit(X.iloc[tr], y.iloc[tr]).predict(X.iloc[va])
        err = np.exp(oof) / df["price_uah"] - 1
        base = np.exp(np.log(df["price_uah"]).groupby(df["rooms"].clip(upper=4)).transform(
            "median"))
        self.meta = {
            "n": int(len(df)),
            "mape": float(np.mean(np.abs(err)) * 100),
            "median_ape": float(np.median(np.abs(err)) * 100),
            "baseline_rooms_mape": float(np.mean(np.abs(base / df["price_uah"] - 1)) * 100),
            "within_15pct": float(np.mean(np.abs(err) <= 0.15) * 100),
            "q10": float(np.quantile(df["price_uah"] / np.exp(oof), 0.10)),
            "q90": float(np.quantile(df["price_uah"] / np.exp(oof), 0.90)),
        }
        self.m = model().fit(X, y)
        imp = self.m.booster_.feature_importance("gain")
        self.meta["importance"] = dict(zip(FEATURES, (imp / imp.sum() * 100).round(1).tolist()))
        if path:
            joblib.dump((self.m, self.meta), path)
        self.oof = pd.DataFrame({"actual": df["price_uah"].values, "pred": np.exp(oof),
                                 "rooms": df["rooms"].values, "source": df["source"].values})
        return self

    def predict(self, l: Listing):
        if self.m is None or not l.rooms:
            return None
        return float(np.exp(self.m.predict(frame([l])[FEATURES])[0]))

    def verdict(self, l: Listing):
        """(прогноз, відхилення ціни від прогнозу у %) або None."""
        p = self.predict(l)
        if p is None or not l.price_uah:
            return None
        return p, (l.price_uah / p - 1) * 100

    def save_meta(self, path):
        Path(path).write_text(json.dumps(self.meta, ensure_ascii=False, indent=1),
                              encoding="utf-8")
