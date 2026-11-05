"""Збір датасету оголошень для моделі ціни й оцінки якості (ввічливо: 1 запит / 3 с на сайт).

python scripts/collect.py --pages 15
"""
import argparse
import json
import sys
import time
from contextlib import ExitStack
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from homefull.http import Blocked, PoliteClient  # noqa: E402
from homefull.sources import ALL  # noqa: E402
from homefull.storage import Store  # noqa: E402
from homefull.config import DATA  # noqa: E402

DATA_LOG = DATA / "collect_log.json"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=15)
    ap.add_argument("--only", choices=[cls.name for cls in ALL], help="лише одне джерело")
    args = ap.parse_args()
    store, client, t0, log = Store(DATA / "homefull.sqlite"), PoliteClient(), time.time(), {}
    prev = DATA_LOG.exists() and json.loads(DATA_LOG.read_text(encoding="utf-8")) or {}
    log.update({k: v for k, v in prev.items() if k in {cls.name for cls in ALL}})
    for cls in ALL:
        if args.only and cls.name != args.only:
            continue
        src = cls(client)
        # Browser sessions must close on success as well as HTTP/parsing failures.
        with ExitStack() as cleanup:
            if hasattr(src, "close"):
                cleanup.callback(src.close)
            log[src.name] = collect_source(src, store, args.pages, t0)
    log["seconds"] = round(time.time() - t0) + (prev.get("seconds", 0) if args.only else 0)
    log["stats"] = store.stats()
    DATA_LOG.write_text(json.dumps(log, ensure_ascii=False, indent=1),
                                                    encoding="utf-8")
    print(json.dumps(log, ensure_ascii=False))


def collect_source(src, store, pages, t0):
    got, new_total, details, p = 0, 0, 0, 0
    errors = []
    try:
        for p in range(1, pages + 1):
            ls = src.parse_catalog(src.http.get(src.page_url(p)))
            if not ls:
                break
            got += len(ls)
            new = store.upsert(ls)
            new_total += len(new)
            if src.needs_details:
                for listing in new:
                    try:
                        src.details(listing)
                        store.update_listing(listing)
                        details += 1
                    except Blocked:
                        raise
                    except Exception as e:  # noqa: BLE001
                        errors.append(f"деталі {listing.key}: {e}")
            print(f"{src.name} стор. {p}: {len(ls)} (нових {len(new)}) {time.time() - t0:.0f} с",
                  flush=True)
    except Exception as e:  # noqa: BLE001 - записати відмову й закрити браузер
        errors.append(str(e))
        print(src.name, "зупинено:", e)
    return {"pages": p, "fetched": got, "new": new_total, "details": details, "errors": errors}


if __name__ == "__main__":
    main()
