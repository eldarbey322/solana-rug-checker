"""Кэш на диске: одни и те же кошельки (боты, биржи, снайперы) не проверяем заново каждый раз."""
import json
import os
import time

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache.json")
TTL = {"trace": 30 * 60, "exch": 24 * 3600, "fate": 6 * 3600}
_data = {}
try:
    with open(PATH, encoding="utf-8") as f:
        _data = json.load(f)
except (OSError, ValueError):
    _data = {}


DISABLED = bool(os.getenv("RUGBOT_NO_CACHE"))


def get(ns, key):
    if DISABLED:
        return None
    e = _data.get(ns, {}).get(key)
    if e and time.time() - e["t"] < TTL.get(ns, 1800):
        return e["v"]
    return None


def put(ns, key, value):
    _data.setdefault(ns, {})[key] = {"t": time.time(), "v": value}


def save():
    if DISABLED:
        return
    now = time.time()
    for ns in list(_data):
        ttl = TTL.get(ns, 1800)
        _data[ns] = {k: e for k, e in _data[ns].items() if now - e["t"] < ttl}
    try:
        tmp = PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_data, f)
        os.replace(tmp, PATH)
    except OSError:
        pass
