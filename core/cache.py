import asyncio
import logging
import time

logger = logging.getLogger("purplejack.cache")

# ── USUARIOS ───────────────────────────────────────────
_cache = {}
_dirty = set()
_last_activity = {}

FLUSH_INTERVAL = 600   # 10 minutos — mini-juegos se persistirán en este ciclo
CACHE_EXPIRE   = 7200  # 2 horas de inactividad
MAX_BANK       = 200_000  # Límite máximo de almacenamiento en banco


def touch_user(user_id):
    _last_activity[user_id] = time.time()

def get_cached(user_id):
    touch_user(user_id)
    return _cache.get(user_id)

def set_cache(user_id, data):
    _cache[user_id] = {
        "balance":        data.get("balance", 0),
        "bank":           data.get("bank", 0),
        "cooldown_work":  data.get("cooldown_work", 0),
        "cooldown_crime": data.get("cooldown_crime", 0),
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
    """
    Actualiza banco en caché respetando MAX_BANK.
    Si amount es positivo y supera el límite, el excedente se aplica
    automáticamente al balance del mismo usuario.
    Retorna la cantidad efectivamente aplicada al banco.
    """
    if user_id not in _cache:
        return amount  # sin caché no hay nada que hacer, DB lo manejará

    if amount <= 0:
        # Retiros o penalizaciones: siempre se aplican sin límite
        _cache[user_id]["bank"] += amount
        mark_dirty(user_id)
        return amount

    banco_actual = _cache[user_id]["bank"]
    espacio_disponible = max(0, MAX_BANK - banco_actual)

    aplicado_banco    = min(amount, espacio_disponible)
    excedente_balance = amount - aplicado_banco

    _cache[user_id]["bank"] += aplicado_banco

    if excedente_balance > 0:
        _cache[user_id]["balance"] += excedente_balance

    mark_dirty(user_id)
    return aplicado_banco

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

def get_rob_cooldown(user_id):
    return _rob_cooldowns.get(user_id, 0)

def set_rob_cooldown(user_id):
    from core.config import rob_config
    _rob_cooldowns[user_id] = time.time() + rob_config["cooldown"]

def clear_rob_cooldowns_cache():
    _rob_cooldowns.clear()

# ── GAME COOLDOWNS (ruleta / rr / dados) ───────────────
_game_cooldowns = {}  # {(user_id, game): expira_en}

def get_game_cooldown_cache(user_id, game):
    return _game_cooldowns.get((user_id, game), 0)

def set_game_cooldown_cache(user_id, game, expira_en):
    _game_cooldowns[(user_id, game)] = expira_en

def clear_game_cooldowns_cache(game):
    keys = [k for k in _game_cooldowns if k[1] == game]
    for k in keys:
        del _game_cooldowns[k]

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
        entry = dict(item)
        entry.setdefault("limite_uso", 0)
        _inventory_cache[user_id].append(entry)
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
        "rol_id":    rol_id,
        "guild_id":  guild_id,
        "expira_en": expira_en,
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
    _collect_config[rol_id] = {"cantidad": cantidad, "cooldown_horas": cooldown_horas}

def delete_collect_config(rol_id):
    _collect_config.pop(rol_id, None)

# ── COLLECT COOLDOWNS POR USUARIO ──────────────────────
_collect_cooldowns = {}

def get_collect_cooldowns(user_id):
    return _collect_cooldowns.get(user_id)

def get_all_collect_cooldowns() -> dict:
    """Retorna copia de todos los cooldowns de collect en cache — usado en shutdown."""
    return dict(_collect_cooldowns)

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

# ── VETERANO CONFIG ────────────────────────────────────
_veterano_config = {}  # {rol_id: {"monto": int, "msj": str}}

def get_veterano_config() -> dict:
    return _veterano_config

def set_veterano_config(data: dict):
    global _veterano_config
    _veterano_config = data

def upsert_veterano_config(rol_id: int, monto: int, msj: str):
    _veterano_config[rol_id] = {"monto": monto, "msj": msj}

def delete_veterano_config(rol_id: int):
    _veterano_config.pop(rol_id, None)


# ── FLUSH USUARIOS A DB ────────────────────────────────
async def flush_to_db():
    """
    Persiste todos los usuarios marcados como dirty a PostgreSQL.
    El dirty-flag se limpia SOLO después de un write exitoso,
    garantizando que los datos no se pierdan ante un fallo de conexión.
    """
    from core.database import pool
    dirty = get_dirty_users()
    if not dirty:
        return

    flushed = 0
    for user_id in dirty:
        data = _cache.get(user_id)
        if not data:
            clear_dirty(user_id)   # sin datos en caché → nada que persistir
            continue
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """UPDATE users SET balance=$1, bank=$2,
                    cooldown_work=$3, cooldown_crime=$4 WHERE id=$5""",
                    data["balance"], data["bank"],
                    data["cooldown_work"], data["cooldown_crime"],
                    user_id,
                )
            clear_dirty(user_id)   # ✅ solo se limpia tras write exitoso
            flushed += 1
        except Exception as e:
            logger.warning(f"flush_to_db error para {user_id}: {e} — reintentará en próximo ciclo")

    if flushed:
        logger.debug(f"flush_to_db: {flushed} usuario(s) persistidos")


async def flush_loop():
    while True:
        await asyncio.sleep(FLUSH_INTERVAL)
        await flush_to_db()
        cleanup_cache()
