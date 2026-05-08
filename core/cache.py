import asyncio
import time

_cache = {}
_dirty = set()
_last_activity = {}

_items_cache = []
_inventory_cache = {}
_collect_cooldowns = {}
_collect_config = {}
_cargos_cache = {}
_top_cooldowns = {}

FLUSH_INTERVAL = 300
CACHE_EXPIRE = 7200

def touch_user(user_id):
    _last_activity[user_id] = time.time()

def get_cached(user_id):
    touch_user(user_id)
    return _cache.get(user_id)

def set_cache(user_id, data):
    _cache[user_id] = {
        "balance": data.get("balance", 0),
        "bank": data.get("bank", 0),
        "cooldown_work": data.get("cooldown_work", 0),
        "cooldown_crime": data.get("cooldown_crime", 0)
    }
    touch_user(user_id)

def mark_dirty(user_id):
    _dirty.add(user_id)
    touch_user(user_id)

def update_cached_balance(user_id, amount):
    if user_id in _cache:
        _cache[user_id]["balance"] += amount
        mark_dirty(user_id)

def update_cached_bank(user_id, amount):
    if user_id in _cache:
        _cache[user_id]["bank"] += amount
        mark_dirty(user_id)

def update_cached_cooldown(user_id, command, timestamp):
    if user_id in _cache:
        _cache[user_id][f"cooldown_{command}"] = timestamp
        mark_dirty(user_id)

def get_dirty_users():
    return list(_dirty)

def clear_dirty(user_id):
    _dirty.discard(user_id)

def get_all_cache():
    return _cache

def set_items_cache(items):
    global _items_cache
    _items_cache = items

def get_items_cache():
    return _items_cache

def get_inventory_cache(user_id):
    touch_user(user_id)
    return _inventory_cache.get(user_id)

def set_inventory_cache(user_id, items):
    _inventory_cache[user_id] = items
    touch_user(user_id)

def get_collect_config():
    return _collect_config

def set_collect_config(data):
    global _collect_config
    _collect_config = data

def get_collect_cooldowns():
    return _collect_cooldowns

def set_collect_cooldown(user_id, role_id, timestamp):
    if user_id not in _collect_cooldowns:
        _collect_cooldowns[user_id] = {}
    _collect_cooldowns[user_id][role_id] = timestamp
    touch_user(user_id)

def get_cargos_cache():
    return _cargos_cache

def set_cargos_cache(data):
    global _cargos_cache
    _cargos_cache = data

def check_top_cooldown(user_id):
    last = _top_cooldowns.get(user_id, 0)
    return time.time() - last < 300

def set_top_cooldown(user_id):
    _top_cooldowns[user_id] = time.time()

def cleanup_cache():
    now = time.time()
    inactive = [
        uid for uid, last in _last_activity.items()
        if now - last > CACHE_EXPIRE
    ]

    for uid in inactive:
        _inventory_cache.pop(uid, None)
        _collect_cooldowns.pop(uid, None)