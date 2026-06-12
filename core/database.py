import logging
import asyncpg
from settings import DATABASE_URL
from core import cache

logger = logging.getLogger(__name__)

pool = None

async def init_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL)
    async with pool.acquire() as conn:
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id BIGINT PRIMARY KEY,
            balance INTEGER DEFAULT 0,
            bank INTEGER DEFAULT 0,
            cooldown_work DOUBLE PRECISION DEFAULT 0,
            cooldown_crime DOUBLE PRECISION DEFAULT 0
        )
        """)
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS cooldown_work DOUBLE PRECISION DEFAULT 0")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS cooldown_crime DOUBLE PRECISION DEFAULT 0")

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS items (
            id SERIAL PRIMARY KEY,
            nombre TEXT UNIQUE,
            descripcion TEXT DEFAULT '',
            descripcion_larga TEXT DEFAULT '',
            precio INTEGER DEFAULT 0,
            cantidad INTEGER DEFAULT 1,
            stock INTEGER DEFAULT -1,
            icono TEXT DEFAULT '',
            utilizable BOOLEAN DEFAULT FALSE,
            mensaje_uso TEXT DEFAULT '',
            rol_id BIGINT DEFAULT NULL,
            duracion INTEGER DEFAULT 0,
            limite_por_usuario INTEGER DEFAULT 0,
            limite_uso INTEGER DEFAULT 0
        )
        """)

        for col, definition in [
            ("descripcion",          "TEXT DEFAULT ''"),
            ("descripcion_larga",    "TEXT DEFAULT ''"),
            ("cantidad",             "INTEGER DEFAULT 1"),
            ("duracion",             "INTEGER DEFAULT 0"),
            ("limite_por_usuario",   "INTEGER DEFAULT 0"),
            ("limite_uso",           "INTEGER DEFAULT 0"),
        ]:
            await conn.execute(
                f"ALTER TABLE items ADD COLUMN IF NOT EXISTS {col} {definition}"
            )

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS inventario (
            user_id BIGINT,
            item_id INTEGER,
            cantidad INTEGER DEFAULT 1,
            PRIMARY KEY (user_id, item_id)
        )
        """)

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS cargos_temporales (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            guild_id BIGINT,
            rol_id BIGINT,
            expira_en DOUBLE PRECISION
        )
        """)

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS collect_config (
            rol_id BIGINT PRIMARY KEY,
            cantidad INTEGER NOT NULL,
            cooldown_horas INTEGER NOT NULL
        )
        """)

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS collect_cooldowns (
            user_id BIGINT,
            rol_id BIGINT,
            ultima_vez DOUBLE PRECISION NOT NULL,
            PRIMARY KEY (user_id, rol_id)
        )
        """)

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS game_cooldowns (
            user_id BIGINT,
            game TEXT,
            expira_en DOUBLE PRECISION,
            PRIMARY KEY (user_id, game)
        )
        """)

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS item_uso_diario (
            user_id BIGINT,
            item_id INTEGER,
            fecha DATE NOT NULL,
            usos INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, item_id, fecha)
        )
        """)

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS veterano_config (
            rol_id BIGINT PRIMARY KEY,
            monto_penalizar INTEGER NOT NULL,
            msj_atacante TEXT NOT NULL
        )
        """)

    logger.info("Base de datos conectada y tablas verificadas.")


# ── USUARIOS ───────────────────────────────────────────

async def get_user(user_id):
    cached = cache.get_cached(user_id)
    if cached:
        return cached
    async with pool.acquire() as conn:
        user = await conn.fetchrow("SELECT * FROM users WHERE id = $1", user_id)
        if not user:
            await conn.execute("INSERT INTO users (id) VALUES ($1)", user_id)
            data = {"balance": 0, "bank": 0, "cooldown_work": 0, "cooldown_crime": 0}
        else:
            data = {
                "balance":        user["balance"],
                "bank":           user["bank"],
                "cooldown_work":  user["cooldown_work"],
                "cooldown_crime": user["cooldown_crime"],
            }
        cache.set_cache(user_id, data)
        return data


async def flush_user_to_db(user_id):
    """
    Persiste un usuario específico a DB de forma inmediata.
    Se usa para operaciones críticas (collect, shop, rob).
    El dirty-flag se limpia solo tras write exitoso.
    """
    data = cache.get_cached(user_id)
    if not data:
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """UPDATE users SET balance=$1, bank=$2,
                cooldown_work=$3, cooldown_crime=$4 WHERE id=$5""",
                data["balance"], data["bank"],
                data["cooldown_work"], data["cooldown_crime"],
                user_id,
            )
        cache.clear_dirty(user_id)
    except Exception as e:
        logger.warning(f"flush_user_to_db error para {user_id}: {e}")


# ── ESCRITURAS INMEDIATAS (shop, collect, rob, duels) ──

async def update_balance(user_id, amount):
    """Actualiza balance en RAM y persiste a DB de inmediato."""
    await get_user(user_id)
    cache.update_cached_balance(user_id, amount)
    await flush_user_to_db(user_id)


async def update_bank(user_id, amount):
    """Actualiza banco en RAM y persiste a DB de inmediato."""
    await get_user(user_id)
    cache.update_cached_bank(user_id, amount)
    await flush_user_to_db(user_id)


# ── ESCRITURAS EN RAM (mini-juegos: ruleta, rr, dados) ─

async def cache_balance(user_id, amount):
    """
    Actualiza balance solo en RAM.
    La persistencia ocurre en el flush_loop (cada 10 min).
    Usar en mini-juegos donde no hay transferencia entre usuarios.
    """
    await get_user(user_id)  # garantiza fila en DB y usuario en caché
    cache.update_cached_balance(user_id, amount)


async def cache_bank(user_id, amount):
    """
    Actualiza banco solo en RAM.
    La persistencia ocurre en el flush_loop (cada 10 min).
    """
    await get_user(user_id)
    cache.update_cached_bank(user_id, amount)


async def update_cooldown(user_id, command, timestamp):
    await get_user(user_id)
    cache.update_cached_cooldown(user_id, command, timestamp)


# ── ITEMS ──────────────────────────────────────────────

async def load_items_to_cache():
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM items")
    cache.set_items_cache([dict(r) for r in rows])

async def get_all_items():
    items = cache.get_items_cache()
    if items:
        return items
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM items")
    items = [dict(r) for r in rows]
    cache.set_items_cache(items)
    return items

async def get_item_by_name(nombre):
    items = await get_all_items()
    nombre = nombre.lower().strip()
    return next((i for i in items if i["nombre"].lower() == nombre), None)

async def add_item(nombre, descripcion, descripcion_larga, precio, cantidad,
                   stock, icono, utilizable, mensaje_uso, rol_id, duracion,
                   limite_por_usuario=0, limite_uso=0):
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO items
                (nombre, descripcion, descripcion_larga, precio, cantidad,
                 stock, icono, utilizable, mensaje_uso, rol_id, duracion,
                 limite_por_usuario, limite_uso)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
        """, nombre, descripcion, descripcion_larga, precio, cantidad,
             stock, icono, utilizable, mensaje_uso, rol_id, duracion,
             limite_por_usuario, limite_uso)
    await load_items_to_cache()

async def edit_item(item_id, nombre=None, precio=None, stock=None, descripcion=None, mensaje_uso=None):
    async with pool.acquire() as conn:
        if nombre:
            await conn.execute("UPDATE items SET nombre=$1 WHERE id=$2", nombre, item_id)
        if precio is not None:
            await conn.execute("UPDATE items SET precio=$1 WHERE id=$2", precio, item_id)
        if stock is not None:
            await conn.execute("UPDATE items SET stock=$1 WHERE id=$2", stock, item_id)
        if descripcion:
            await conn.execute("UPDATE items SET descripcion=$1 WHERE id=$2", descripcion, item_id)
        if mensaje_uso is not None:
            await conn.execute("UPDATE items SET mensaje_uso=$1 WHERE id=$2", mensaje_uso, item_id)
    await load_items_to_cache()

async def delete_item(item_id):
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM items WHERE id=$1", item_id)
        rows = await conn.fetch("SELECT user_id FROM inventario WHERE item_id=$1", item_id)
        affected_users = [r["user_id"] for r in rows]
        await conn.execute("DELETE FROM inventario WHERE item_id=$1", item_id)
    for user_id in affected_users:
        cache.invalidate_inventory_cache(user_id)
    await load_items_to_cache()

async def reduce_stock(item_id):
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE items SET stock = stock - 1 WHERE id=$1 AND stock > 0", item_id
        )
    await load_items_to_cache()

async def add_stock(item_id, cantidad):
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE items SET stock = stock + $1 WHERE id=$2", cantidad, item_id
        )
    await load_items_to_cache()


# ── INVENTARIO ─────────────────────────────────────────

async def add_to_inventory(user_id, item_id, cantidad=1):
    async with pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT cantidad FROM inventario WHERE user_id=$1 AND item_id=$2",
            user_id, item_id
        )
        if existing:
            await conn.execute(
                "UPDATE inventario SET cantidad=cantidad+$1 WHERE user_id=$2 AND item_id=$3",
                cantidad, user_id, item_id
            )
        else:
            await conn.execute(
                "INSERT INTO inventario (user_id, item_id, cantidad) VALUES ($1, $2, $3)",
                user_id, item_id, cantidad
            )

async def get_inventory_from_db(user_id):
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT i.id, i.nombre, i.icono, i.utilizable, i.mensaje_uso,
                   i.rol_id, i.duracion, i.limite_uso, inv.cantidad
            FROM inventario inv
            JOIN items i ON inv.item_id = i.id
            WHERE inv.user_id = $1
        """, user_id)
    return [dict(r) for r in rows]

async def get_inventory(user_id):
    cached = cache.get_inventory_cache(user_id)
    if cached is not None:
        return cached
    items = await get_inventory_from_db(user_id)
    cache.set_inventory_cache(user_id, items)
    return items

async def remove_from_inventory(user_id, item_nombre):
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT inv.item_id, inv.cantidad FROM inventario inv
            JOIN items i ON inv.item_id = i.id
            WHERE inv.user_id=$1 AND LOWER(i.nombre)=$2
        """, user_id, item_nombre.lower())
        if not row:
            return False
        if row["cantidad"] > 1:
            await conn.execute(
                "UPDATE inventario SET cantidad=cantidad-1 WHERE user_id=$1 AND item_id=$2",
                user_id, row["item_id"]
            )
        else:
            await conn.execute(
                "DELETE FROM inventario WHERE user_id=$1 AND item_id=$2",
                user_id, row["item_id"]
            )
    cache.remove_from_inventory_cache(user_id, item_nombre)
    return True


async def get_usos_diarios(user_id: int, item_id: int) -> int:
    from datetime import date
    today = date.today()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT usos FROM item_uso_diario WHERE user_id=$1 AND item_id=$2 AND fecha=$3",
            user_id, item_id, today
        )
    return row["usos"] if row else 0


async def registrar_uso_diario(user_id: int, item_id: int) -> int:
    from datetime import date
    today = date.today()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO item_uso_diario (user_id, item_id, fecha, usos)
            VALUES ($1, $2, $3, 1)
            ON CONFLICT (user_id, item_id, fecha)
            DO UPDATE SET usos = item_uso_diario.usos + 1
        """, user_id, item_id, today)
        row = await conn.fetchrow(
            "SELECT usos FROM item_uso_diario WHERE user_id=$1 AND item_id=$2 AND fecha=$3",
            user_id, item_id, today
        )
    return row["usos"] if row else 1


async def get_all_users_net_worth(minimum=0):
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, balance, bank FROM users WHERE (balance + bank) >= $1",
            minimum
        )
    return [dict(r) for r in rows]


async def get_all_inventarios():
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT inv.user_id, i.nombre, inv.cantidad
            FROM inventario inv
            JOIN items i ON inv.item_id = i.id
            ORDER BY inv.user_id, i.nombre
        """)
    return [dict(r) for r in rows]


# ── CARGOS TEMPORALES ──────────────────────────────────

async def load_cargos_to_cache():
    import time
    now = time.time()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT user_id, guild_id, rol_id, expira_en FROM cargos_temporales WHERE expira_en > $1",
            now
        )
    data = {}
    for r in rows:
        uid = r["user_id"]
        if uid not in data:
            data[uid] = []
        data[uid].append({
            "rol_id":    r["rol_id"],
            "guild_id":  r["guild_id"],
            "expira_en": r["expira_en"],
        })
    cache.set_cargos_cache(data)

async def add_cargo_temporal(user_id, guild_id, rol_id, expira_en):
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO cargos_temporales (user_id, guild_id, rol_id, expira_en)
            VALUES ($1, $2, $3, $4)
        """, user_id, guild_id, rol_id, expira_en)
    cache.add_cargo_cache(user_id, guild_id, rol_id, expira_en)

async def delete_cargo_temporal(user_id, rol_id):
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM cargos_temporales WHERE user_id=$1 AND rol_id=$2",
            user_id, rol_id
        )
    cache.remove_cargo_cache(user_id, rol_id)


# ── COLLECT ────────────────────────────────────────────

async def load_collect_config_to_cache():
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT rol_id, cantidad, cooldown_horas FROM collect_config")
    data = {r["rol_id"]: {"cantidad": r["cantidad"], "cooldown_horas": r["cooldown_horas"]} for r in rows}
    cache.set_collect_config(data)

async def upsert_collect_config_db(rol_id, cantidad, cooldown_horas):
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO collect_config (rol_id, cantidad, cooldown_horas)
            VALUES ($1, $2, $3)
            ON CONFLICT (rol_id) DO UPDATE
            SET cantidad=$2, cooldown_horas=$3
        """, rol_id, cantidad, cooldown_horas)
    cache.upsert_collect_config(rol_id, cantidad, cooldown_horas)

async def delete_collect_config_db(rol_id):
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM collect_config WHERE rol_id=$1", rol_id)
    cache.delete_collect_config(rol_id)

async def load_collect_cooldowns_for_user(user_id):
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT rol_id, ultima_vez FROM collect_cooldowns WHERE user_id=$1", user_id
        )
    data = {r["rol_id"]: r["ultima_vez"] for r in rows}
    cache.set_collect_cooldowns(user_id, data)
    return data

async def save_collect_cooldowns(user_id, cobros: dict):
    rows = [(user_id, rol_id, ultima_vez) for rol_id, ultima_vez in cobros.items()]
    async with pool.acquire() as conn:
        await conn.executemany("""
            INSERT INTO collect_cooldowns (user_id, rol_id, ultima_vez)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id, rol_id) DO UPDATE SET ultima_vez=$3
        """, rows)


# ── GAME CONFIG ────────────────────────────────────────

async def create_game_config_table():
    from core.config import game_config, rr_config, ruleta_config, rob_config, dados_config
    async with pool.acquire() as conn:
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS game_config (
            id SERIAL PRIMARY KEY,
            work_min INTEGER,
            work_max INTEGER,
            work_cooldown INTEGER,
            crime_min INTEGER,
            crime_max INTEGER,
            crime_cooldown INTEGER,
            crime_ganar_prob DOUBLE PRECISION DEFAULT 1.0,
            crime_perder_prob DOUBLE PRECISION DEFAULT 0.0
        )
        """)
        exists = await conn.fetchrow("SELECT * FROM game_config LIMIT 1")
        if not exists:
            await conn.execute("""
            INSERT INTO game_config (
                work_min, work_max, work_cooldown,
                crime_min, crime_max, crime_cooldown,
                crime_ganar_prob, crime_perder_prob
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            game_config["work"]["min"],
            game_config["work"]["max"],
            game_config["work"]["cooldown"],
            game_config["crime"]["min"],
            game_config["crime"]["max"],
            game_config["crime"]["cooldown"],
            game_config["crime"]["ganar_prob"],
            game_config["crime"]["perder_prob"],
        )

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS rr_config (
            id SERIAL PRIMARY KEY,
            max_apuesta INTEGER,
            cooldown INTEGER,
            ganar_prob DOUBLE PRECISION,
            perder_prob DOUBLE PRECISION,
            activa BOOLEAN DEFAULT TRUE
        )
        """)
        rr_exists = await conn.fetchrow("SELECT * FROM rr_config LIMIT 1")
        if not rr_exists:
            await conn.execute("""
            INSERT INTO rr_config (
                max_apuesta, cooldown, ganar_prob, perder_prob, activa
            ) VALUES ($1, $2, $3, $4, $5)
            """,
            rr_config["max_apuesta"],
            rr_config["cooldown"],
            rr_config["ganar_prob"],
            rr_config["perder_prob"],
            rr_config["activa"],
        )

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS ruleta_config (
            id SERIAL PRIMARY KEY,
            max_apuesta INTEGER,
            cooldown INTEGER,
            activa BOOLEAN DEFAULT TRUE
        )
        """)
        ruleta_exists = await conn.fetchrow("SELECT * FROM ruleta_config LIMIT 1")
        if not ruleta_exists:
            await conn.execute("""
            INSERT INTO ruleta_config (
                max_apuesta, cooldown, activa
            ) VALUES ($1, $2, $3)
            """,
            ruleta_config["max_apuesta"],
            ruleta_config["cooldown"],
            ruleta_config["activa"],
        )

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS rob_config_db (
            id SERIAL PRIMARY KEY,
            activa BOOLEAN DEFAULT TRUE,
            cooldown INTEGER,
            exito_prob DOUBLE PRECISION DEFAULT 0.5,
            fallo_prob DOUBLE PRECISION DEFAULT 0.5
        )
        """)
        rob_exists = await conn.fetchrow("SELECT * FROM rob_config_db LIMIT 1")
        if not rob_exists:
            await conn.execute("""
            INSERT INTO rob_config_db (activa, cooldown, exito_prob, fallo_prob)
            VALUES ($1, $2, $3, $4)
            """,
            rob_config["activa"],
            rob_config["cooldown"],
            rob_config["exito_prob"],
            rob_config["fallo_prob"],
        )

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS dados_config (
            id SERIAL PRIMARY KEY,
            max_apuesta INTEGER DEFAULT 100,
            cooldown INTEGER DEFAULT 60,
            exito_prob DOUBLE PRECISION DEFAULT 0.5,
            fallo_prob DOUBLE PRECISION DEFAULT 0.5,
            activa BOOLEAN DEFAULT TRUE
        )
        """)
        dados_exists = await conn.fetchrow("SELECT * FROM dados_config LIMIT 1")
        if not dados_exists:
            await conn.execute("""
            INSERT INTO dados_config (
                max_apuesta, cooldown, exito_prob, fallo_prob, activa
            ) VALUES ($1, $2, $3, $4, $5)
            """,
            dados_config["max_apuesta"],
            dados_config["cooldown"],
            dados_config["exito_prob"],
            dados_config["fallo_prob"],
            dados_config["activa"],
        )

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS nave_config (
            id INTEGER PRIMARY KEY DEFAULT 1,
            contenido TEXT
        )
        """)

async def load_game_config():
    from core.config import game_config, rr_config, ruleta_config, rob_config, dados_config
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM game_config LIMIT 1")
        if not row:
            return
        game_config["work"]["min"]          = row["work_min"]
        game_config["work"]["max"]          = row["work_max"]
        game_config["work"]["cooldown"]     = row["work_cooldown"]
        game_config["crime"]["min"]         = row["crime_min"]
        game_config["crime"]["max"]         = row["crime_max"]
        game_config["crime"]["cooldown"]    = row["crime_cooldown"]
        game_config["crime"]["ganar_prob"]  = row["crime_ganar_prob"]
        game_config["crime"]["perder_prob"] = row["crime_perder_prob"]

        rr_row = await conn.fetchrow("SELECT * FROM rr_config LIMIT 1")
        if rr_row:
            rr_config["max_apuesta"] = rr_row["max_apuesta"]
            rr_config["cooldown"]    = rr_row["cooldown"]
            rr_config["ganar_prob"]  = rr_row["ganar_prob"]
            rr_config["perder_prob"] = rr_row["perder_prob"]
            rr_config["activa"]      = rr_row["activa"]

        ruleta_row = await conn.fetchrow("SELECT * FROM ruleta_config LIMIT 1")
        if ruleta_row:
            ruleta_config["max_apuesta"] = ruleta_row["max_apuesta"]
            ruleta_config["cooldown"]    = ruleta_row["cooldown"]
            ruleta_config["activa"]      = ruleta_row["activa"]

        rob_row = await conn.fetchrow("SELECT * FROM rob_config_db LIMIT 1")
        if rob_row:
            rob_config["activa"]      = rob_row["activa"]
            rob_config["cooldown"]    = rob_row["cooldown"]
            rob_config["exito_prob"]  = rob_row["exito_prob"]
            rob_config["fallo_prob"]  = rob_row["fallo_prob"]

        dados_row = await conn.fetchrow("SELECT * FROM dados_config LIMIT 1")
        if dados_row:
            dados_config["max_apuesta"] = dados_row["max_apuesta"]
            dados_config["cooldown"]    = dados_row["cooldown"]
            dados_config["exito_prob"]  = dados_row["exito_prob"]
            dados_config["fallo_prob"]  = dados_row["fallo_prob"]
            dados_config["activa"]      = dados_row["activa"]

async def save_game_config():
    from core.config import game_config
    async with pool.acquire() as conn:
        await conn.execute("""
        UPDATE game_config SET
            work_min=$1, work_max=$2, work_cooldown=$3,
            crime_min=$4, crime_max=$5, crime_cooldown=$6,
            crime_ganar_prob=$7, crime_perder_prob=$8
        """,
        game_config["work"]["min"],
        game_config["work"]["max"],
        game_config["work"]["cooldown"],
        game_config["crime"]["min"],
        game_config["crime"]["max"],
        game_config["crime"]["cooldown"],
        game_config["crime"]["ganar_prob"],
        game_config["crime"]["perder_prob"],
        )

async def save_rr_config():
    from core.config import rr_config
    async with pool.acquire() as conn:
        await conn.execute("""
        UPDATE rr_config SET
            max_apuesta=$1, cooldown=$2, ganar_prob=$3,
            perder_prob=$4, activa=$5
        """,
        rr_config["max_apuesta"],
        rr_config["cooldown"],
        rr_config["ganar_prob"],
        rr_config["perder_prob"],
        rr_config["activa"],
        )

async def save_ruleta_config():
    from core.config import ruleta_config
    async with pool.acquire() as conn:
        await conn.execute("""
        UPDATE ruleta_config SET
            max_apuesta=$1, cooldown=$2, activa=$3
        """,
        ruleta_config["max_apuesta"],
        ruleta_config["cooldown"],
        ruleta_config["activa"],
        )

async def save_rob_config():
    from core.config import rob_config
    async with pool.acquire() as conn:
        await conn.execute("""
        UPDATE rob_config_db SET activa=$1, cooldown=$2, exito_prob=$3, fallo_prob=$4
        """,
        rob_config["activa"],
        rob_config["cooldown"],
        rob_config["exito_prob"],
        rob_config["fallo_prob"],
        )

async def save_dados_config():
    from core.config import dados_config
    async with pool.acquire() as conn:
        await conn.execute("""
        UPDATE dados_config SET
            max_apuesta=$1, cooldown=$2, exito_prob=$3,
            fallo_prob=$4, activa=$5
        """,
        dados_config["max_apuesta"],
        dados_config["cooldown"],
        dados_config["exito_prob"],
        dados_config["fallo_prob"],
        dados_config["activa"],
        )

async def load_dados_config():
    from core.config import dados_config
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM dados_config LIMIT 1")
        if not row:
            return
        dados_config["max_apuesta"] = row["max_apuesta"]
        dados_config["cooldown"]    = row["cooldown"]
        dados_config["exito_prob"]  = row["exito_prob"]
        dados_config["fallo_prob"]  = row["fallo_prob"]
        dados_config["activa"]      = row["activa"]

async def get_nave_contenido():
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT contenido FROM nave_config WHERE id=1")
    return row["contenido"] if row else None

async def save_nave_contenido(contenido: str):
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO nave_config (id, contenido)
            VALUES (1, $1)
            ON CONFLICT (id) DO UPDATE SET contenido=$1
        """, contenido)

async def get_game_cooldown(user_id, game):
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT expira_en FROM game_cooldowns WHERE user_id=$1 AND game=$2",
            user_id, game
        )
    return row["expira_en"] if row else 0

async def set_game_cooldown(user_id, game, expira_en):
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO game_cooldowns (user_id, game, expira_en)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id, game) DO UPDATE SET expira_en=$3
        """, user_id, game, expira_en)

async def clear_game_cooldowns(game):
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM game_cooldowns WHERE game=$1", game
        )


# ── VETERANO CONFIG ────────────────────────────────────

async def load_veterano_config_to_cache():
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT rol_id, monto_penalizar, msj_atacante FROM veterano_config"
        )
    data = {
        r["rol_id"]: {"monto": r["monto_penalizar"], "msj": r["msj_atacante"]}
        for r in rows
    }
    cache.set_veterano_config(data)

async def upsert_veterano_config_db(rol_id: int, monto: int, msj: str):
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO veterano_config (rol_id, monto_penalizar, msj_atacante)
            VALUES ($1, $2, $3)
            ON CONFLICT (rol_id) DO UPDATE
            SET monto_penalizar=$2, msj_atacante=$3
        """, rol_id, monto, msj)
    cache.upsert_veterano_config(rol_id, monto, msj)

async def delete_veterano_config_db(rol_id: int):
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM veterano_config WHERE rol_id=$1", rol_id
        )
    cache.delete_veterano_config(rol_id)
