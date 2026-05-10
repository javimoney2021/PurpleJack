import asyncio
import time

# ── USUARIOS ───────────────────────────────────────────
_cache = {}
_dirty = set()
_last_activity = {}

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

# ── ROB ────────────────────────────────────────────────
_rob_cooldowns = {}      # {user_id: timestamp}
_rob_protection = {}     # {user_id: timestamp}

def get_rob_cooldown(user_id):
    return _rob_cooldowns.get(user_id, 0)

def set_rob_cooldown(user_id):
    from core.config import rob_config
    _rob_cooldowns[user_id] = time.time() + rob_config["cooldown"]

def get_rob_protection(user_id):
    return _rob_protection.get(user_id, 0)

def set_rob_protection(user_id):
    _rob_protection[user_id] = time.time() + 3600  # 1 hora fija
    
    # ── GAME COOLDOWNS (ruleta / rr) ───────────────────────
_game_cooldowns = {}  # {(user_id, game): expira_en}

def get_game_cooldown_cache(user_id, game):
    return _game_cooldowns.get((user_id, game), 0)

def set_game_cooldown_cache(user_id, game, expira_en):
    _game_cooldowns[(user_id, game)] = expira_en

# ── ITEMS ──────────────────────────────────────────────
_items_cache = []

def set_items_cache(items):
    global _items_cache
    _items_cache = items

def get_items_cache():
    return _items_cache

# ── INVENTARIO POR USUARIO ─────────────────────────────
_inventory_cache = {}

def get_inventory_cache(user_id):
    touch_user(user_id)
    return _inventory_cache.get(user_id)

def set_inventory_cache(user_id, items):
    _inventory_cache[user_id] = items
    touch_user(user_id)

def add_to_inventory_cache(user_id, item):
    if user_id not in _inventory_cache:
        _inventory_cache[user_id] = []
    inv = _inventory_cache[user_id]
    existing = next((i for i in inv if i["nombre"].lower() == item["nombre"].lower()), None)
    if existing:
        existing["cantidad"] += item.get("cantidad", 1)
    else:
        _inventory_cache[user_id].append(dict(item))
    touch_user(user_id)

def remove_from_inventory_cache(user_id, nombre):
    if user_id not in _inventory_cache:
        return
    inv = _inventory_cache[user_id]
    existing = next((i for i in inv if i["nombre"].lower() == nombre.lower()), None)
    if existing:
        existing["cantidad"] -= 1
        if existing["cantidad"] <= 0:
            _inventory_cache[user_id] = [i for i in inv if i["nombre"].lower() != nombre.lower()]

def invalidate_inventory_cache(user_id):
    _inventory_cache.pop(user_id, None)

# ── CARGOS TEMPORALES ──────────────────────────────────
_cargos_cache = {}

def get_cargos_cache():
    return _cargos_cache

def set_cargos_cache(data: dict):
    global _cargos_cache
    _cargos_cache = data

def add_cargo_cache(user_id, guild_id, rol_id, expira_en):
    if user_id not in _cargos_cache:
        _cargos_cache[user_id] = []
    _cargos_cache[user_id].append({
        "rol_id": rol_id,
        "guild_id": guild_id,
        "expira_en": expira_en
    })

def remove_cargo_cache(user_id, rol_id):
    if user_id in _cargos_cache:
        _cargos_cache[user_id] = [
            c for c in _cargos_cache[user_id]
            if c["rol_id"] != rol_id
        ]
        if not _cargos_cache[user_id]:
            del _cargos_cache[user_id]

# ── COLLECT CONFIG ─────────────────────────────────────
_collect_config = {}

def get_collect_config():
    return _collect_config

def set_collect_config(data: dict):
    global _collect_config
    _collect_config = data

def upsert_collect_config(rol_id, cantidad, cooldown_horas):
    _collect_config[rol_id] = {
        "cantidad": cantidad,
        "cooldown_horas": cooldown_horas
    }

def delete_collect_config(rol_id):
    _collect_config.pop(rol_id, None)

# ── COLLECT COOLDOWNS POR USUARIO ──────────────────────
_collect_cooldowns = {}

def get_collect_cooldowns(user_id):
    return _collect_cooldowns.get(user_id)

def set_collect_cooldowns(user_id, data: dict):
    _collect_cooldowns[user_id] = data
    touch_user(user_id)

def update_collect_cooldown(user_id, rol_id, timestamp):
    if user_id not in _collect_cooldowns:
        _collect_cooldowns[user_id] = {}
    _collect_cooldowns[user_id][rol_id] = timestamp
    touch_user(user_id)

# ── TOP COOLDOWN ───────────────────────────────────────
_top_cooldowns = {}

def check_top_cooldown(user_id):
    last = _top_cooldowns.get(user_id, 0)
    return time.time() - last < 300

def set_top_cooldown(user_id):
    _top_cooldowns[user_id] = time.time()

# ── LIMPIEZA DE CACHÉ INACTIVA ─────────────────────────
def cleanup_cache():
    now = time.time()
    inactive = [
        uid for uid, last in _last_activity.items()
        if now - last > CACHE_EXPIRE
    ]
    for uid in inactive:
        _inventory_cache.pop(uid, None)
        _collect_cooldowns.pop(uid, None)

# ── FLUSH USUARIOS A DB ────────────────────────────────
async def flush_to_db():
    from core.database import pool
    dirty = get_dirty_users()
    if not dirty:
        return
    for user_id in dirty:
        clear_dirty(user_id)
        data = _cache.get(user_id)
        if data:
            async with pool.acquire() as conn:
                await conn.execute(
                    """UPDATE users SET balance=$1, bank=$2,
                    cooldown_work=$3, cooldown_crime=$4 WHERE id=$5""",
                    data["balance"], data["bank"],
                    data["cooldown_work"], data["cooldown_crime"],
                    user_id
                )

async def flush_loop():
    while True:
        await asyncio.sleep(FLUSH_INTERVAL)
        await flush_to_db()
        cleanup_cache()
