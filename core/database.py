import asyncpg
from settings import DATABASE_URL
from core import cache
from core.config import game_config

pool = None

async def init_db():
    global pool

    # ── DEBUG: verificar que la URL está cargada ──────────
    if not DATABASE_URL:
        print("❌ ERROR: DATABASE_URL no está definida. Revisa tus variables de entorno.")
        raise ValueError("DATABASE_URL vacía")

    # Muestra la URL ocultando la contraseña para no exponerla en logs
    try:
        from urllib.parse import urlparse
        parsed = urlparse(DATABASE_URL)
        safe_url = f"{parsed.scheme}://{parsed.username}:***@{parsed.hostname}:{parsed.port}{parsed.path}"
        print(f"🔗 Intentando conectar a: {safe_url}")
    except Exception:
        print("🔗 Intentando conectar a la base de datos...")

    # ── DEBUG: intentar crear el pool ─────────────────────
    try:
        pool = await asyncpg.create_pool(DATABASE_URL)
        print("✅ Pool de conexiones creado correctamente.")
    except Exception as e:
        print(f"❌ ERROR al crear el pool: {type(e).__name__}: {e}")
        raise

    # ── DEBUG: verificar conexión real con una query simple ─
    try:
        async with pool.acquire() as conn:
            version = await conn.fetchval("SELECT version()")
            print(f"✅ Conexión exitosa. PostgreSQL: {version[:40]}...")
            ssl_info = await conn.fetchval("SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()")
            print(f"🔒 SSL activo en esta conexión: {ssl_info}")
    except Exception as e:
        print(f"❌ ERROR al verificar conexión: {type(e).__name__}: {e}")
        raise

    # ── TABLAS ─────────────────────────────────────────────
    try:
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
                duracion INTEGER DEFAULT 0
            )
            """)

            for col, definition in [
                ("descripcion",       "TEXT DEFAULT ''"),
                ("descripcion_larga", "TEXT DEFAULT ''"),
                ("cantidad",          "INTEGER DEFAULT 1"),
                ("duracion",          "INTEGER DEFAULT 0"),
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

        print("✅ Base de datos conectada y tablas verificadas.")

    except Exception as e:
        print(f"❌ ERROR al crear tablas: {type(e).__name__}: {e}")
        raise


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
                "balance": user["balance"],
                "bank": user["bank"],
                "cooldown_work": user["cooldown_work"],
                "cooldown_crime": user["cooldown_crime"]
            }
        cache.set_cache(user_id, data)
        return data

async def update_balance(user_id, amount):
    await get_user(user_id)
    cache.update_cached_balance(user_id, amount)

async def update_bank(user_id, amount):
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
                   stock, icono, utilizable, mensaje_uso, rol_id, duracion):
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO items
                (nombre, descripcion, descripcion_larga, precio, cantidad,
                 stock, icono, utilizable, mensaje_uso, rol_id, duracion)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
        """, nombre, descripcion, descripcion_larga, precio, cantidad,
             stock, icono, utilizable, mensaje_uso, rol_id, duracion)
    await load_items_to_cache()

async def edit_item(item_id, nombre=None, precio=None, stock=None):
    async with pool.acquire() as conn:
        if nombre:
            await conn.execute("UPDATE items SET nombre=$1 WHERE id=$2", nombre, item_id)
        if precio is not None:
            await conn.execute("UPDATE items SET precio=$1 WHERE id=$2", precio, item_id)
        if stock is not None:
            await conn.execute("UPDATE items SET stock=$1 WHERE id=$2", stock, item_id)
    await load_items_to_cache()

async def delete_item(item_id):
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM items WHERE id=$1", item_id)
        await conn.execute("DELETE FROM inventario WHERE item_id=$1", item_id)
    await load_items_to_cache()

async def reduce_stock(item_id):
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE items SET stock = stock - 1 WHERE id=$1 AND stock > 0", item_id
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
                   i.rol_id, i.duracion, inv.cantidad
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
            "rol_id": r["rol_id"],
            "guild_id": r["guild_id"],
            "expira_en": r["expira_en"]
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
    """Guarda en DB solo los roles que fueron cobrados. cobros = {rol_id: timestamp}"""
    async with pool.acquire() as conn:
        for rol_id, ultima_vez in cobros.items():
            await conn.execute("""
                INSERT INTO collect_cooldowns (user_id, rol_id, ultima_vez)
                VALUES ($1, $2, $3)
                ON CONFLICT (user_id, rol_id) DO UPDATE SET ultima_vez=$3
            """, user_id, rol_id, ultima_vez)


async def create_game_config_table():
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
            ruleta_activa BOOLEAN,
            ruleta_max_apuesta INTEGER
        )
        """)

        exists = await conn.fetchrow("SELECT * FROM game_config LIMIT 1")

        if not exists:
            await conn.execute("""
            INSERT INTO game_config (
                work_min, work_max, work_cooldown,
                crime_min, crime_max, crime_cooldown,
                ruleta_activa, ruleta_max_apuesta
            )
            VALUES (100, 500, 3600, 100, 500, 3600, TRUE, 100)
            """)

async def load_game_config():
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM game_config LIMIT 1")
        if not row:
            return

        game_config["work"]["min"] = row["work_min"]
        game_config["work"]["max"] = row["work_max"]
        game_config["work"]["cooldown"] = row["work_cooldown"]

        game_config["crime"]["min"] = row["crime_min"]
        game_config["crime"]["max"] = row["crime_max"]
        game_config["crime"]["cooldown"] = row["crime_cooldown"]

        game_config["ruleta"]["activa"] = row["ruleta_activa"]
        game_config["ruleta"]["max_apuesta"] = row["ruleta_max_apuesta"]

async def save_game_config():
    async with pool.acquire() as conn:
        await conn.execute("""
        UPDATE game_config
        SET
            work_min=$1,
            work_max=$2,
            work_cooldown=$3,
            crime_min=$4,
            crime_max=$5,
            crime_cooldown=$6,
            ruleta_activa=$7,
            ruleta_max_apuesta=$8
        """,
        game_config["work"]["min"],
        game_config["work"]["max"],
        game_config["work"]["cooldown"],
        game_config["crime"]["min"],
        game_config["crime"]["max"],
        game_config["crime"]["cooldown"],
        game_config["ruleta"]["activa"],
        game_config["ruleta"]["max_apuesta"])
