import asyncio
from contextlib import AsyncExitStack
import logging
import time
import uuid
import asyncpg
from settings import DATABASE_URL
from core import cache

logger = logging.getLogger(__name__)

pool = None
_purchase_locks = {}
_economy_locks = {}


def _get_purchase_lock(user_id):
    lock = _purchase_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _purchase_locks[user_id] = lock
    return lock


def _get_economy_lock(user_id):
    """Serializa movimientos económicos críticos de un usuario en este proceso."""
    lock = _economy_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _economy_locks[user_id] = lock
    return lock

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
            limite_uso INTEGER DEFAULT 0,
            log_uso_channel_id BIGINT DEFAULT NULL
        )
        """)

        for col, definition in [
            ("descripcion",          "TEXT DEFAULT ''"),
            ("descripcion_larga",    "TEXT DEFAULT ''"),
            ("cantidad",             "INTEGER DEFAULT 1"),
            ("duracion",             "INTEGER DEFAULT 0"),
            ("limite_por_usuario",   "INTEGER DEFAULT 0"),
            ("limite_uso",           "INTEGER DEFAULT 0"),
            ("log_uso_channel_id",   "BIGINT DEFAULT NULL"),
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
            expira_en DOUBLE PRECISION,
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at DOUBLE PRECISION NOT NULL DEFAULT 0,
            locked_until DOUBLE PRECISION NOT NULL DEFAULT 0,
            last_error TEXT DEFAULT NULL
        )
        """)
        for column, definition in [
            ("attempts", "INTEGER NOT NULL DEFAULT 0"),
            ("next_attempt_at", "DOUBLE PRECISION NOT NULL DEFAULT 0"),
            ("locked_until", "DOUBLE PRECISION NOT NULL DEFAULT 0"),
            ("last_error", "TEXT DEFAULT NULL"),
        ]:
            await conn.execute(
                f"ALTER TABLE cargos_temporales ADD COLUMN IF NOT EXISTS {column} {definition}"
            )
        await conn.execute("""
        DELETE FROM cargos_temporales
        WHERE user_id IS NULL
           OR guild_id IS NULL
           OR rol_id IS NULL
           OR expira_en IS NULL
        """)
        await conn.execute("""
        ALTER TABLE cargos_temporales
            ALTER COLUMN user_id SET NOT NULL,
            ALTER COLUMN guild_id SET NOT NULL,
            ALTER COLUMN rol_id SET NOT NULL,
            ALTER COLUMN expira_en SET NOT NULL
        """)
        await conn.execute("""
        WITH duplicados AS (
            SELECT id,
                   ROW_NUMBER() OVER (
                       PARTITION BY user_id, guild_id, rol_id
                       ORDER BY expira_en DESC, id DESC
                   ) AS posicion
            FROM cargos_temporales
        )
        DELETE FROM cargos_temporales AS cargo
        USING duplicados
        WHERE cargo.id=duplicados.id
          AND duplicados.posicion > 1
        """)
        await conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS cargos_temporales_usuario_rol_idx
        ON cargos_temporales (user_id, guild_id, rol_id)
        """)
        await conn.execute("""
        CREATE INDEX IF NOT EXISTS cargos_temporales_expiracion_idx
        ON cargos_temporales (expira_en, next_attempt_at, locked_until)
        """)

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS item_role_restrictions (
            rol_id BIGINT PRIMARY KEY
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
        CREATE TABLE IF NOT EXISTS item_use_log_outbox (
            id TEXT PRIMARY KEY,
            user_id BIGINT NOT NULL,
            item_id INTEGER NOT NULL,
            guild_id BIGINT NOT NULL,
            channel_id BIGINT NOT NULL,
            content TEXT NOT NULL,
            allow_mentions BOOLEAN NOT NULL DEFAULT FALSE,
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at DOUBLE PRECISION NOT NULL DEFAULT 0,
            locked_until DOUBLE PRECISION NOT NULL DEFAULT 0,
            last_error TEXT DEFAULT NULL,
            created_at DOUBLE PRECISION NOT NULL
        )
        """)
        await conn.execute("""
        CREATE INDEX IF NOT EXISTS item_use_log_outbox_pending_idx
        ON item_use_log_outbox (next_attempt_at, locked_until, created_at)
        """)

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS veterano_config (
            rol_id BIGINT PRIMARY KEY,
            monto_penalizar INTEGER NOT NULL,
            msj_atacante TEXT NOT NULL
        )
        """)

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS saboteador_config (
            rol_id BIGINT PRIMARY KEY
        )
        """)

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS evento_estado (
            id SMALLINT PRIMARY KEY CHECK (id = 1),
            activo BOOLEAN NOT NULL DEFAULT FALSE,
            iniciado_en DOUBLE PRECISION NOT NULL DEFAULT 0
        )
        """)
        await conn.execute("""
        INSERT INTO evento_estado (id, activo, iniciado_en)
        VALUES (1, FALSE, 0)
        ON CONFLICT (id) DO NOTHING
        """)
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS evento_puntos (
            user_id BIGINT PRIMARY KEY,
            puntos BIGINT NOT NULL DEFAULT 0
        )
        """)

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS command_cooldowns (
            scope_type TEXT NOT NULL,
            scope_id BIGINT NOT NULL,
            command TEXT NOT NULL,
            expira_en DOUBLE PRECISION NOT NULL,
            PRIMARY KEY (scope_type, scope_id, command)
        )
        """)

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS duel_config (
            guild_id BIGINT PRIMARY KEY,
            cooldown INTEGER NOT NULL,
            activa BOOLEAN NOT NULL DEFAULT TRUE
        )
        """)
        await conn.execute(
            "ALTER TABLE duel_config ADD COLUMN IF NOT EXISTS activa BOOLEAN NOT NULL DEFAULT TRUE"
        )

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS wagers (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            game TEXT NOT NULL,
            user_id BIGINT NOT NULL,
            amount INTEGER NOT NULL CHECK (amount > 0),
            source TEXT NOT NULL CHECK (source IN ('balance', 'bank')),
            status TEXT NOT NULL DEFAULT 'pending',
            payout INTEGER NOT NULL DEFAULT 0,
            track_event BOOLEAN NOT NULL DEFAULT TRUE,
            created_at DOUBLE PRECISION NOT NULL,
            expires_at DOUBLE PRECISION NOT NULL
        )
        """)
        await conn.execute("""
        CREATE INDEX IF NOT EXISTS wagers_pending_session_idx
        ON wagers (session_id, status)
        """)
        await conn.execute("""
        CREATE INDEX IF NOT EXISTS wagers_pending_user_idx
        ON wagers (user_id, game, status)
        """)

    logger.info("Base de datos conectada y tablas verificadas.")


# ── EVENTO PURPLE COINS ───────────────────────────────

async def load_evento_to_cache():
    """Restaura el estado y los puntos del evento al iniciar el bot."""
    async with pool.acquire() as conn:
        estado = await conn.fetchrow(
            "SELECT activo, iniciado_en FROM evento_estado WHERE id=1"
        )
        puntos = await conn.fetch("SELECT user_id, puntos FROM evento_puntos WHERE puntos > 0")

    cache.restore_evento(
        bool(estado["activo"]) if estado else False,
        float(estado["iniciado_en"]) if estado else 0,
        {row["user_id"]: row["puntos"] for row in puntos},
    )


async def activar_evento():
    """Inicia un evento nuevo y elimina de forma atómica el ranking anterior."""
    iniciado_en = time.time()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM evento_puntos")
            await conn.execute(
                "UPDATE evento_estado SET activo=TRUE, iniciado_en=$1 WHERE id=1",
                iniciado_en,
            )
    cache.start_evento(iniciado_en)
    return iniciado_en


async def cerrar_evento():
    """Detiene el conteo antes de persistir el cierre para no registrar cambios tardíos."""
    cache.stop_evento()
    try:
        async with pool.acquire() as conn:
            await conn.execute("UPDATE evento_estado SET activo=FALSE WHERE id=1")
    except Exception:
        cache.resume_evento()
        raise


async def flush_evento_puntos():
    """Guarda el snapshot pendiente sin perder cambios producidos durante el flush."""
    snapshot = cache.get_evento_dirty_snapshot()
    if not snapshot:
        return

    positivos = [(user_id, puntos) for user_id, puntos in snapshot.items() if puntos > 0]
    eliminados = [user_id for user_id, puntos in snapshot.items() if puntos <= 0]

    async with pool.acquire() as conn:
        async with conn.transaction():
            if positivos:
                await conn.executemany(
                    """
                    INSERT INTO evento_puntos (user_id, puntos)
                    VALUES ($1, $2)
                    ON CONFLICT (user_id) DO UPDATE SET puntos=EXCLUDED.puntos
                    """,
                    positivos,
                )
            if eliminados:
                await conn.execute(
                    "DELETE FROM evento_puntos WHERE user_id = ANY($1::BIGINT[])",
                    eliminados,
                )
    cache.clear_evento_dirty_if_unchanged(snapshot)


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


async def _flush_user_to_db_unlocked(user_id):
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
        return True
    except Exception as e:
        logger.warning(f"flush_user_to_db error para {user_id}: {e}")
        return False


async def flush_user_to_db(user_id):
    """Persiste un usuario sin competir con otro movimiento económico."""
    async with _get_economy_lock(user_id):
        return await _flush_user_to_db_unlocked(user_id)


# ── ESCRITURAS INMEDIATAS (shop, collect, rob, duels) ──

async def update_balance(user_id, amount, track_event=True):
    """Actualiza balance en RAM y persiste a DB de inmediato."""
    await get_user(user_id)
    async with _get_economy_lock(user_id):
        cache.update_cached_balance(user_id, amount, track_event=track_event)
        await _flush_user_to_db_unlocked(user_id)


async def update_bank(user_id, amount, track_event=True):
    """
    Actualiza banco en RAM y persiste a DB de inmediato.
    Si amount es positivo y el banco supera MAX_BANK, el excedente
    se redirige al balance automáticamente. Garantiza flush en ambos casos.
    """
    await get_user(user_id)
    async with _get_economy_lock(user_id):
        aplicado_banco = cache.update_cached_bank(
            user_id,
            amount,
            track_event=track_event,
        )
        await _flush_user_to_db_unlocked(user_id)
        return aplicado_banco


# ── ESCRITURAS EN RAM (mini-juegos: ruleta, rr, dados) ─

async def cache_balance(user_id, amount, track_event=True):
    """
    Actualiza balance solo en RAM.
    La persistencia ocurre en el flush_loop (cada 10 min).
    Usar en mini-juegos donde no hay transferencia entre usuarios.
    """
    await get_user(user_id)  # garantiza fila en DB y usuario en caché
    async with _get_economy_lock(user_id):
        cache.update_cached_balance(user_id, amount, track_event=track_event)


async def cache_bank(user_id, amount, track_event=True):
    """
    Actualiza banco solo en RAM respetando MAX_BANK.
    El excedente se redirige al balance automáticamente.
    La persistencia ocurre en el flush_loop (cada 10 min).
    """
    await get_user(user_id)
    async with _get_economy_lock(user_id):
        return cache.update_cached_bank(user_id, amount, track_event=track_event)


async def transfer_balance(
    sender_id: int,
    recipient_id: int,
    amount: int,
    *,
    track_sender_event: bool = True,
    track_recipient_event: bool = True,
):
    """Transfiere balance entre dos usuarios de forma atómica."""
    if amount <= 0 or sender_id == recipient_id:
        return {"ok": False, "reason": "invalid_transfer"}

    await get_user(sender_id)
    await get_user(recipient_id)
    user_ids = sorted((sender_id, recipient_id))

    async with AsyncExitStack() as stack:
        for user_id in user_ids:
            await stack.enter_async_context(_get_economy_lock(user_id))

        sender = cache.get_cached(sender_id)
        recipient = cache.get_cached(recipient_id)
        if not sender or sender["balance"] < amount:
            return {"ok": False, "reason": "insufficient_balance"}
        if not recipient:
            return {"ok": False, "reason": "recipient_not_found"}

        sender_balance = sender["balance"] - amount
        recipient_balance = recipient["balance"] + amount
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.fetch(
                    """
                    SELECT id FROM users
                    WHERE id = ANY($1::BIGINT[])
                    ORDER BY id
                    FOR UPDATE
                    """,
                    user_ids,
                )
                await conn.execute(
                    """
                    UPDATE users SET balance=$1, bank=$2,
                        cooldown_work=$3, cooldown_crime=$4
                    WHERE id=$5
                    """,
                    sender_balance,
                    sender["bank"],
                    sender["cooldown_work"],
                    sender["cooldown_crime"],
                    sender_id,
                )
                await conn.execute(
                    """
                    UPDATE users SET balance=$1, bank=$2,
                        cooldown_work=$3, cooldown_crime=$4
                    WHERE id=$5
                    """,
                    recipient_balance,
                    recipient["bank"],
                    recipient["cooldown_work"],
                    recipient["cooldown_crime"],
                    recipient_id,
                )

        cache.update_cached_balance(
            sender_id,
            -amount,
            track_event=track_sender_event,
        )
        cache.update_cached_balance(
            recipient_id,
            amount,
            track_event=track_recipient_event,
        )
        return {"ok": True, "amount": amount}


async def update_cooldown(user_id, command, timestamp):
    await get_user(user_id)
    async with _get_economy_lock(user_id):
        cache.update_cached_cooldown(user_id, command, timestamp)


# ── COOLDOWNS PERSISTENTES ─────────────────────────────

async def get_command_cooldown(scope_type: str, scope_id: int, command: str) -> float:
    async with pool.acquire() as conn:
        value = await conn.fetchval(
            """
            SELECT expira_en FROM command_cooldowns
            WHERE scope_type=$1 AND scope_id=$2 AND command=$3
            """,
            scope_type,
            scope_id,
            command,
        )
    return float(value or 0)


async def set_command_cooldown(
    scope_type: str,
    scope_id: int,
    command: str,
    expira_en: float,
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO command_cooldowns (scope_type, scope_id, command, expira_en)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (scope_type, scope_id, command)
            DO UPDATE SET expira_en=EXCLUDED.expira_en
            """,
            scope_type,
            scope_id,
            command,
            expira_en,
        )


async def clear_command_cooldown(scope_type: str, scope_id: int, command: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            DELETE FROM command_cooldowns
            WHERE scope_type=$1 AND scope_id=$2 AND command=$3
            """,
            scope_type,
            scope_id,
            command,
        )


async def clear_command_cooldowns(command: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM command_cooldowns WHERE command=$1",
            command,
        )


async def get_duel_cooldown_config(guild_id: int, default: int) -> int:
    async with pool.acquire() as conn:
        value = await conn.fetchval(
            "SELECT cooldown FROM duel_config WHERE guild_id=$1",
            guild_id,
        )
    return int(value) if value is not None else default


async def set_duel_cooldown_config(guild_id: int, cooldown: int) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO duel_config (guild_id, cooldown)
            VALUES ($1, $2)
            ON CONFLICT (guild_id) DO UPDATE SET cooldown=EXCLUDED.cooldown
            """,
            guild_id,
            cooldown,
        )


async def get_duel_active_config(guild_id: int, default: bool = True) -> bool:
    async with pool.acquire() as conn:
        value = await conn.fetchval(
            "SELECT activa FROM duel_config WHERE guild_id=$1",
            guild_id,
        )
    return bool(value) if value is not None else default


async def set_duel_active_config(
    guild_id: int,
    activa: bool,
    default_cooldown: int,
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO duel_config (guild_id, cooldown, activa)
            VALUES ($1, $2, $3)
            ON CONFLICT (guild_id) DO UPDATE SET activa=EXCLUDED.activa
            """,
            guild_id,
            default_cooldown,
            activa,
        )


# ── APUESTAS TRANSACCIONALES ───────────────────────────

async def reserve_wager(
    user_id: int,
    game: str,
    amount: int,
    *,
    source: str = "balance",
    session_id: str | None = None,
    expires_in: int = 600,
    track_event: bool = True,
    idempotency_key: str | None = None,
    exclusive_pending: bool = False,
    enforce_cooldown: bool = False,
):
    """
    Descuenta y registra una apuesta pendiente en una sola transacción.

    Las opciones de exclusividad se validan después de bloquear la fila del
    usuario en PostgreSQL. Así, dos instancias del bot no pueden reservar a la
    vez una misma solicitud ni crear dos partidas exclusivas para el usuario.
    """
    if amount <= 0 or source not in {"balance", "bank"}:
        return {"ok": False, "reason": "invalid_wager"}

    await get_user(user_id)
    async with _get_economy_lock(user_id):
        user = cache.get_cached(user_id)
        wager_id = str(uuid.uuid4())
        session_id = idempotency_key or session_id or wager_id
        created_at = time.time()
        expires_at = created_at + max(60, expires_in)

        async with pool.acquire() as conn:
            async with conn.transaction():
                # El lock de asyncio solo coordina esta instancia. Este bloqueo
                # de fila serializa también reservas hechas por otro proceso.
                await conn.fetchval(
                    "SELECT id FROM users WHERE id=$1 FOR UPDATE",
                    user_id,
                )

                if idempotency_key:
                    existing = await conn.fetchrow(
                        """
                        SELECT id, status FROM wagers
                        WHERE session_id=$1 AND game=$2 AND user_id=$3
                        ORDER BY created_at DESC
                        LIMIT 1
                        """,
                        idempotency_key,
                        game,
                        user_id,
                    )
                    if existing:
                        return {
                            "ok": False,
                            "reason": "duplicate_request",
                            "id": existing["id"],
                            "status": existing["status"],
                        }

                if exclusive_pending:
                    pending = await conn.fetchrow(
                        """
                        SELECT id FROM wagers
                        WHERE user_id=$1 AND game=$2 AND status='pending'
                        ORDER BY created_at DESC
                        LIMIT 1
                        """,
                        user_id,
                        game,
                    )
                    if pending:
                        return {
                            "ok": False,
                            "reason": "already_pending",
                            "id": pending["id"],
                        }

                if enforce_cooldown:
                    cooldown_until = await conn.fetchval(
                        """
                        SELECT expira_en FROM game_cooldowns
                        WHERE user_id=$1 AND game=$2
                        """,
                        user_id,
                        game,
                    )
                    if cooldown_until and cooldown_until > created_at:
                        return {
                            "ok": False,
                            "reason": "cooldown",
                            "expires_at": float(cooldown_until),
                        }

                if not user or user[source] < amount:
                    return {
                        "ok": False,
                        "reason": f"insufficient_{source}",
                        "available": user[source] if user else 0,
                    }

                new_balance = (
                    user["balance"] - amount
                    if source == "balance"
                    else user["balance"]
                )
                new_bank = (
                    user["bank"] - amount
                    if source == "bank"
                    else user["bank"]
                )
                await conn.execute(
                    """
                    UPDATE users SET balance=$1, bank=$2,
                        cooldown_work=$3, cooldown_crime=$4
                    WHERE id=$5
                    """,
                    new_balance,
                    new_bank,
                    user["cooldown_work"],
                    user["cooldown_crime"],
                    user_id,
                )
                await conn.execute(
                    """
                    INSERT INTO wagers (
                        id, session_id, game, user_id, amount, source,
                        status, payout, track_event, created_at, expires_at
                    )
                    VALUES ($1,$2,$3,$4,$5,$6,'pending',0,$7,$8,$9)
                    """,
                    wager_id,
                    session_id,
                    game,
                    user_id,
                    amount,
                    source,
                    track_event,
                    created_at,
                    expires_at,
                )

        if source == "balance":
            cache.update_cached_balance(user_id, -amount, track_event=False)
        else:
            cache.update_cached_bank(user_id, -amount, track_event=False)

        return {
            "ok": True,
            "id": wager_id,
            "session_id": session_id,
            "amount": amount,
            "source": source,
        }


async def increase_wager(wager_id: str, extra_amount: int):
    """Amplía el riesgo de una apuesta pendiente, validando fondos primero."""
    if extra_amount <= 0:
        return {"ok": True, "added": 0}

    async with pool.acquire() as conn:
        preliminary = await conn.fetchrow(
            "SELECT user_id FROM wagers WHERE id=$1",
            wager_id,
        )
    if not preliminary:
        return {"ok": False, "reason": "not_found"}

    user_id = preliminary["user_id"]
    await get_user(user_id)
    async with _get_economy_lock(user_id):
        user = cache.get_cached(user_id)
        async with pool.acquire() as conn:
            async with conn.transaction():
                wager = await conn.fetchrow(
                    "SELECT * FROM wagers WHERE id=$1 FOR UPDATE",
                    wager_id,
                )
                if not wager or wager["status"] != "pending":
                    return {"ok": False, "reason": "not_pending"}

                source = wager["source"]
                if not user or user[source] < extra_amount:
                    return {
                        "ok": False,
                        "reason": f"insufficient_{source}",
                        "available": user[source] if user else 0,
                    }

                new_balance = (
                    user["balance"] - extra_amount
                    if source == "balance"
                    else user["balance"]
                )
                new_bank = (
                    user["bank"] - extra_amount
                    if source == "bank"
                    else user["bank"]
                )
                await conn.execute(
                    """
                    UPDATE users SET balance=$1, bank=$2,
                        cooldown_work=$3, cooldown_crime=$4
                    WHERE id=$5
                    """,
                    new_balance,
                    new_bank,
                    user["cooldown_work"],
                    user["cooldown_crime"],
                    user_id,
                )
                await conn.execute(
                    "UPDATE wagers SET amount=amount+$1 WHERE id=$2",
                    extra_amount,
                    wager_id,
                )

        if source == "balance":
            cache.update_cached_balance(user_id, -extra_amount, track_event=False)
        else:
            cache.update_cached_bank(user_id, -extra_amount, track_event=False)
        return {"ok": True, "added": extra_amount}


async def extend_wager_expiry(wager_id: str, expires_in: int = 300) -> bool:
    """Renueva el vencimiento de una apuesta interactiva aún pendiente."""
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE wagers
            SET expires_at=GREATEST(expires_at, $2)
            WHERE id=$1 AND status='pending'
            """,
            wager_id,
            time.time() + max(60, expires_in),
        )
    return result.endswith("1")


async def finalize_wager(
    wager_id: str,
    *,
    payout: int,
    status: str,
    require_expired: bool = False,
):
    """Liquida una apuesta una sola vez y sincroniza DB, caché y evento."""
    if payout < 0 or status not in {"settled", "lost", "refunded"}:
        return {"ok": False, "reason": "invalid_settlement"}

    async with pool.acquire() as conn:
        preliminary = await conn.fetchrow(
            "SELECT user_id FROM wagers WHERE id=$1",
            wager_id,
        )
    if not preliminary:
        return {"ok": False, "reason": "not_found"}

    user_id = preliminary["user_id"]
    await get_user(user_id)
    async with _get_economy_lock(user_id):
        user = cache.get_cached(user_id)
        async with pool.acquire() as conn:
            async with conn.transaction():
                wager = await conn.fetchrow(
                    "SELECT * FROM wagers WHERE id=$1 FOR UPDATE",
                    wager_id,
                )
                if not wager:
                    return {"ok": False, "reason": "not_found"}
                if wager["status"] != "pending":
                    return {
                        "ok": False,
                        "reason": "already_finalized",
                        "status": wager["status"],
                    }
                if require_expired and wager["expires_at"] > time.time():
                    return {
                        "ok": False,
                        "reason": "not_expired",
                        "expires_at": wager["expires_at"],
                    }

                source = wager["source"]
                if source == "balance":
                    new_balance = user["balance"] + payout
                    new_bank = user["bank"]
                else:
                    espacio = max(0, cache.MAX_BANK - user["bank"])
                    banco_aplicado = min(payout, espacio)
                    new_bank = user["bank"] + banco_aplicado
                    new_balance = user["balance"] + (payout - banco_aplicado)

                await conn.execute(
                    """
                    UPDATE users SET balance=$1, bank=$2,
                        cooldown_work=$3, cooldown_crime=$4
                    WHERE id=$5
                    """,
                    new_balance,
                    new_bank,
                    user["cooldown_work"],
                    user["cooldown_crime"],
                    user_id,
                )
                await conn.execute(
                    """
                    UPDATE wagers SET status=$1, payout=$2
                    WHERE id=$3
                    """,
                    status,
                    payout,
                    wager_id,
                )

        if payout:
            if source == "balance":
                cache.update_cached_balance(user_id, payout, track_event=False)
            else:
                cache.update_cached_bank(user_id, payout, track_event=False)

        if wager["track_event"] and source == "balance":
            if status == "refunded":
                event_delta = 0
            elif status == "lost":
                event_delta = -wager["amount"]
            else:
                event_delta = payout - wager["amount"]
            if event_delta:
                cache.record_evento_balance_delta(user_id, event_delta)

        return {
            "ok": True,
            "user_id": user_id,
            "amount": wager["amount"],
            "payout": payout,
            "status": status,
        }


async def settle_wager(wager_id: str, payout: int):
    return await finalize_wager(wager_id, payout=payout, status="settled")


async def lose_wager(wager_id: str):
    return await finalize_wager(wager_id, payout=0, status="lost")


async def refund_wager(wager_id: str, *, only_if_expired: bool = False):
    async with pool.acquire() as conn:
        amount = await conn.fetchval(
            "SELECT amount FROM wagers WHERE id=$1 AND status='pending'",
            wager_id,
        )
    if amount is None:
        return {"ok": False, "reason": "not_pending"}
    return await finalize_wager(
        wager_id,
        payout=amount,
        status="refunded",
        require_expired=only_if_expired,
    )


async def refund_wager_session(session_id: str):
    result = await finalize_wager_session(session_id, refund=True)
    return result.get("count", 0) if result.get("ok") else 0


async def finalize_wager_session(
    session_id: str,
    payouts: dict[str, int] | None = None,
    *,
    refund: bool = False,
):
    """Liquida en una única transacción todas las apuestas pendientes de una sesión."""
    payouts = payouts or {}
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM wagers
            WHERE session_id=$1 AND status='pending'
            ORDER BY user_id, id
            """,
            session_id,
        )
    if not rows:
        return {"ok": False, "reason": "not_pending", "count": 0}

    user_ids = sorted({row["user_id"] for row in rows})
    for user_id in user_ids:
        await get_user(user_id)

    async with AsyncExitStack() as stack:
        for user_id in user_ids:
            await stack.enter_async_context(_get_economy_lock(user_id))

        states = {
            user_id: dict(cache.get_cached(user_id))
            for user_id in user_ids
        }
        settlements = []

        async with pool.acquire() as conn:
            async with conn.transaction():
                locked_rows = await conn.fetch(
                    """
                    SELECT * FROM wagers
                    WHERE session_id=$1 AND status='pending'
                    ORDER BY user_id, id
                    FOR UPDATE
                    """,
                    session_id,
                )
                if not locked_rows:
                    return {"ok": False, "reason": "not_pending", "count": 0}

                for wager in locked_rows:
                    payout = (
                        wager["amount"]
                        if refund
                        else max(0, int(payouts.get(wager["id"], 0)))
                    )
                    status = (
                        "refunded"
                        if refund
                        else ("settled" if payout > 0 else "lost")
                    )
                    state = states[wager["user_id"]]

                    if wager["source"] == "balance":
                        state["balance"] += payout
                    else:
                        espacio = max(0, cache.MAX_BANK - state["bank"])
                        banco_aplicado = min(payout, espacio)
                        state["bank"] += banco_aplicado
                        state["balance"] += payout - banco_aplicado

                    await conn.execute(
                        """
                        UPDATE wagers SET status=$1, payout=$2
                        WHERE id=$3
                        """,
                        status,
                        payout,
                        wager["id"],
                    )
                    settlements.append((dict(wager), payout, status))

                for user_id, state in states.items():
                    await conn.execute(
                        """
                        UPDATE users SET balance=$1, bank=$2,
                            cooldown_work=$3, cooldown_crime=$4
                        WHERE id=$5
                        """,
                        state["balance"],
                        state["bank"],
                        state["cooldown_work"],
                        state["cooldown_crime"],
                        user_id,
                    )

        for wager, payout, status in settlements:
            if payout:
                if wager["source"] == "balance":
                    cache.update_cached_balance(
                        wager["user_id"],
                        payout,
                        track_event=False,
                    )
                else:
                    cache.update_cached_bank(
                        wager["user_id"],
                        payout,
                        track_event=False,
                    )

            if wager["track_event"] and wager["source"] == "balance":
                event_delta = (
                    0
                    if status == "refunded"
                    else payout - wager["amount"]
                )
                if event_delta:
                    cache.record_evento_balance_delta(
                        wager["user_id"],
                        event_delta,
                    )

    return {"ok": True, "count": len(settlements)}


async def settle_wager_session(session_id: str, payouts: dict[str, int]):
    return await finalize_wager_session(session_id, payouts)


async def recover_pending_wagers():
    """
    Reembolsa únicamente apuestas realmente vencidas.

    Una instancia nueva no debe tocar apuestas frescas que todavía puede estar
    procesando la instancia anterior durante un despliegue de SquareCloud.
    """
    async with pool.acquire() as conn:
        wager_ids = await conn.fetch(
            """
            SELECT id FROM wagers
            WHERE status='pending' AND expires_at <= $1
            ORDER BY expires_at
            """,
            time.time(),
        )

    recovered = 0
    for row in wager_ids:
        result = await refund_wager(row["id"], only_if_expired=True)
        if result.get("ok"):
            recovered += 1
    return recovered


async def ensure_wager_constraints():
    """
    Instala las garantías de unicidad después de recuperar apuestas huérfanas.

    Los índices viven en PostgreSQL, por lo que siguen protegiendo incluso
    durante el breve solapamiento entre la instancia vieja y la nueva al
    desplegar en SquareCloud.
    """
    async with pool.acquire() as conn:
        duplicates = await conn.fetch(
            """
            SELECT id FROM (
                SELECT id,
                    ROW_NUMBER() OVER (
                        PARTITION BY user_id, game
                        ORDER BY created_at, id
                    ) AS position
                FROM wagers
                WHERE status='pending' AND game IN ('dados', 'rr', 'bj')
            ) pending
            WHERE position > 1
            """
        )
    for duplicate in duplicates:
        await refund_wager(duplicate["id"])

    async with pool.acquire() as conn:
        await conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS wagers_unique_dados_request_idx
            ON wagers (session_id)
            WHERE game='dados'
            """
        )
        await conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS wagers_one_pending_dados_idx
            ON wagers (user_id)
            WHERE game='dados' AND status='pending'
            """
        )
        await conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS wagers_unique_rr_request_idx
            ON wagers (session_id)
            WHERE game='rr'
            """
        )
        await conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS wagers_one_pending_rr_idx
            ON wagers (user_id)
            WHERE game='rr' AND status='pending'
            """
        )
        await conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS wagers_unique_bj_request_idx
            ON wagers (session_id)
            WHERE game='bj'
            """
        )
        await conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS wagers_one_pending_bj_idx
            ON wagers (user_id)
            WHERE game='bj' AND status='pending'
            """
        )


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

async def edit_item(
    item_id, nombre=None, precio=None, stock=None, descripcion=None, mensaje_uso=None,
    limite_por_usuario=None, limite_uso=None,
):
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
        if limite_por_usuario is not None:
            await conn.execute("UPDATE items SET limite_por_usuario=$1 WHERE id=$2", limite_por_usuario, item_id)
        if limite_uso is not None:
            await conn.execute("UPDATE items SET limite_uso=$1 WHERE id=$2", limite_uso, item_id)
    await load_items_to_cache()


async def set_item_log_uso_channel(item_id, channel_id):
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE items SET log_uso_channel_id=$1 WHERE id=$2",
            channel_id,
            item_id,
        )
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

async def reduce_stock(item_id, cantidad=1):
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE items
            SET stock = stock - $2
            WHERE id=$1 AND stock >= $2
            RETURNING stock
            """,
            item_id, cantidad
        )
    if row:
        await load_items_to_cache()
        return True
    return False

async def add_stock(item_id, cantidad):
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE items SET stock = stock + $1 WHERE id=$2", cantidad, item_id
        )
    await load_items_to_cache()


# ── INVENTARIO ─────────────────────────────────────────

async def add_to_inventory(user_id, item_id, cantidad=1):
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO inventario (user_id, item_id, cantidad)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id, item_id)
            DO UPDATE SET cantidad = inventario.cantidad + EXCLUDED.cantidad
            """,
            user_id, item_id, cantidad
        )
    cache.invalidate_inventory_cache(user_id)


async def purchase_item(user_id, item_id, unidades=1, use_bank=False):
    """
    Compra de tienda en una sola operación: valida saldo, stock, límite por usuario,
    actualiza inventario y persiste el saldo resultante.
    """
    if unidades <= 0:
        return {"ok": False, "reason": "invalid_units"}

    lock = _get_purchase_lock(user_id)
    async with lock:
        user = await get_user(user_id)

        async with pool.acquire() as conn:
            async with conn.transaction():
                item = await conn.fetchrow(
                    "SELECT * FROM items WHERE id=$1 FOR UPDATE",
                    item_id
                )
                if not item:
                    return {"ok": False, "reason": "not_found"}

                item_data = dict(item)
                stock = item_data["stock"]
                if stock == 0:
                    return {"ok": False, "reason": "out_of_stock", "item": item_data}
                if stock != -1 and stock < unidades:
                    return {
                        "ok": False,
                        "reason": "insufficient_stock",
                        "item": item_data,
                        "available": stock,
                    }

                inv_row = await conn.fetchrow(
                    """
                    SELECT cantidad FROM inventario
                    WHERE user_id=$1 AND item_id=$2
                    FOR UPDATE
                    """,
                    user_id, item_id
                )
                poseidos = inv_row["cantidad"] if inv_row else 0
                limite = item_data.get("limite_por_usuario", 0) or 0
                if limite > 0 and poseidos + unidades > limite:
                    return {
                        "ok": False,
                        "reason": "limit",
                        "item": item_data,
                        "limit": limite,
                        "owned": poseidos,
                        "available": max(0, limite - poseidos),
                    }

                precio_unitario = item_data["precio"]
                total = precio_unitario * unidades
                balance = user["balance"]
                bank = user["bank"]
                if use_bank:
                    if bank < total:
                        return {
                            "ok": False,
                            "reason": "insufficient_bank",
                            "item": item_data,
                            "total": total,
                            "precio_unitario": precio_unitario,
                        }
                    bank -= total
                else:
                    if balance < total:
                        return {
                            "ok": False,
                            "reason": "insufficient_balance",
                            "item": item_data,
                            "total": total,
                            "precio_unitario": precio_unitario,
                        }
                    balance -= total

                cantidad_compra = item_data.get("cantidad", 1) * unidades
                await conn.execute(
                    """
                    UPDATE users
                    SET balance=$1, bank=$2, cooldown_work=$3, cooldown_crime=$4
                    WHERE id=$5
                    """,
                    balance, bank, user["cooldown_work"], user["cooldown_crime"], user_id
                )
                await conn.execute(
                    """
                    INSERT INTO inventario (user_id, item_id, cantidad)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (user_id, item_id)
                    DO UPDATE SET cantidad = inventario.cantidad + EXCLUDED.cantidad
                    """,
                    user_id, item_id, cantidad_compra
                )
                if stock != -1:
                    await conn.execute(
                        "UPDATE items SET stock = stock - $1 WHERE id=$2",
                        unidades, item_id
                    )

        cache.set_cache(user_id, {
            "balance": balance,
            "bank": bank,
            "cooldown_work": user["cooldown_work"],
            "cooldown_crime": user["cooldown_crime"],
        })
        if not use_bank:
            cache.record_evento_balance_delta(user_id, balance - user["balance"])
        cache.clear_dirty(user_id)
        cache.invalidate_inventory_cache(user_id)
        if stock != -1:
            item_data["stock"] = stock - unidades
            await load_items_to_cache()

        return {
            "ok": True,
            "item": item_data,
            "total": total,
            "precio_unitario": precio_unitario,
            "cantidad_compra": cantidad_compra,
        }

async def get_inventory_from_db(user_id):
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT i.id, i.nombre, i.icono, i.utilizable, i.mensaje_uso,
                   i.rol_id, i.duracion, i.limite_uso, i.log_uso_channel_id,
                   inv.cantidad
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


async def remove_inventory_quantity(user_id: int, item_id: int, cantidad: int):
    """Retira una cantidad exacta de un inventario y persiste el cambio de inmediato."""
    if cantidad <= 0:
        return {"ok": False, "reason": "invalid_quantity"}

    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT cantidad FROM inventario
                WHERE user_id=$1 AND item_id=$2
                FOR UPDATE
                """,
                user_id,
                item_id,
            )
            disponible = row["cantidad"] if row else 0
            if disponible < cantidad:
                return {
                    "ok": False,
                    "reason": "insufficient_quantity",
                    "available": disponible,
                }

            restante = disponible - cantidad
            if restante:
                await conn.execute(
                    "UPDATE inventario SET cantidad=$1 WHERE user_id=$2 AND item_id=$3",
                    restante,
                    user_id,
                    item_id,
                )
            else:
                await conn.execute(
                    "DELETE FROM inventario WHERE user_id=$1 AND item_id=$2",
                    user_id,
                    item_id,
                )

    cache.invalidate_inventory_cache(user_id)
    return {"ok": True, "remaining": restante}


async def consume_inventory_item(
    user_id: int,
    item_id: int,
    *,
    daily_limit: int = 0,
    guild_id: int | None = None,
    role_id: int | None = None,
    role_duration: int = 0,
    log_guild_id: int | None = None,
    general_log_channel_id: int | None = None,
    log_user_display_name: str = "",
    log_user_mention: str = "",
    staff_role_id: int | None = None,
):
    """
    Consume una unidad y registra límite, rol temporal y logs pendientes
    en una sola transacción.
    Debe invocarse únicamente después de verificar la entrega del rol en Discord.
    """
    from datetime import date

    today = date.today()
    expires_at = None
    log_event_ids = []
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT cantidad FROM inventario
                WHERE user_id=$1 AND item_id=$2
                FOR UPDATE
                """,
                user_id,
                item_id,
            )
            if not row or row["cantidad"] <= 0:
                return {"ok": False, "reason": "not_owned"}

            if daily_limit > 0:
                await conn.execute(
                    """
                    INSERT INTO item_uso_diario (user_id, item_id, fecha, usos)
                    VALUES ($1, $2, $3, 0)
                    ON CONFLICT (user_id, item_id, fecha) DO NOTHING
                    """,
                    user_id,
                    item_id,
                    today,
                )
                usage = await conn.fetchrow(
                    """
                    SELECT usos FROM item_uso_diario
                    WHERE user_id=$1 AND item_id=$2 AND fecha=$3
                    FOR UPDATE
                    """,
                    user_id,
                    item_id,
                    today,
                )
                uses_today = usage["usos"] if usage else 0
                if uses_today >= daily_limit:
                    return {
                        "ok": False,
                        "reason": "daily_limit",
                        "uses": uses_today,
                    }

            remaining = row["cantidad"] - 1
            if remaining:
                await conn.execute(
                    """
                    UPDATE inventario SET cantidad=$1
                    WHERE user_id=$2 AND item_id=$3
                    """,
                    remaining,
                    user_id,
                    item_id,
                )
            else:
                await conn.execute(
                    "DELETE FROM inventario WHERE user_id=$1 AND item_id=$2",
                    user_id,
                    item_id,
                )

            if daily_limit > 0:
                await conn.execute(
                    """
                    UPDATE item_uso_diario SET usos=usos+1
                    WHERE user_id=$1 AND item_id=$2 AND fecha=$3
                    """,
                    user_id,
                    item_id,
                    today,
                )

            if guild_id is not None and role_id is not None:
                existing_rows = await conn.fetch(
                    """
                    SELECT id, expira_en FROM cargos_temporales
                    WHERE user_id=$1 AND guild_id=$2 AND rol_id=$3
                    FOR UPDATE
                    """,
                    user_id,
                    guild_id,
                    role_id,
                )
                await conn.execute(
                    """
                    DELETE FROM cargos_temporales
                    WHERE user_id=$1 AND guild_id=$2 AND rol_id=$3
                    """,
                    user_id,
                    guild_id,
                    role_id,
                )

                if role_duration > 0:
                    now = time.time()
                    previous_expiry = max(
                        (record["expira_en"] for record in existing_rows),
                        default=0,
                    )
                    expires_at = max(now, previous_expiry) + role_duration
                    await conn.execute(
                        """
                        INSERT INTO cargos_temporales
                            (user_id, guild_id, rol_id, expira_en)
                        VALUES ($1, $2, $3, $4)
                        """,
                        user_id,
                        guild_id,
                        role_id,
                        expires_at,
                    )

            if log_guild_id is not None and general_log_channel_id is not None:
                item_log = await conn.fetchrow(
                    """
                    SELECT nombre, icono, log_uso_channel_id
                    FROM items
                    WHERE id=$1
                    """,
                    item_id,
                )
                if not item_log:
                    raise RuntimeError(
                        f"No se encontró configuración del item {item_id} para registrar su uso"
                    )

                icono = item_log["icono"] or "🔹"
                nombre = item_log["nombre"]
                created_at = time.time()
                log_rows = []

                general_event_id = str(uuid.uuid4())
                log_event_ids.append(general_event_id)
                log_rows.append((
                    general_event_id,
                    user_id,
                    item_id,
                    log_guild_id,
                    general_log_channel_id,
                    f"✨ **{log_user_display_name}** usó {icono} **{nombre}**",
                    False,
                    created_at,
                ))

                special_channel_id = item_log["log_uso_channel_id"]
                if (
                    special_channel_id
                    and special_channel_id != general_log_channel_id
                    and staff_role_id is not None
                ):
                    special_event_id = str(uuid.uuid4())
                    log_event_ids.append(special_event_id)
                    log_rows.append((
                        special_event_id,
                        user_id,
                        item_id,
                        log_guild_id,
                        special_channel_id,
                        (
                            f"<@&{staff_role_id}> ✨ {log_user_mention} "
                            f"usó {icono} **{nombre}**"
                        ),
                        True,
                        created_at,
                    ))

                await conn.executemany(
                    """
                    INSERT INTO item_use_log_outbox (
                        id, user_id, item_id, guild_id, channel_id,
                        content, allow_mentions, created_at
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    """,
                    log_rows,
                )

    cache.invalidate_inventory_cache(user_id)
    if guild_id is not None and role_id is not None:
        if expires_at is not None:
            cache.upsert_cargo_cache(
                user_id,
                guild_id,
                role_id,
                expires_at,
            )
        else:
            cache.remove_cargo_cache(user_id, role_id, guild_id)
    return {
        "ok": True,
        "remaining": remaining,
        "expires_at": expires_at,
        "log_event_ids": log_event_ids,
    }


async def claim_pending_item_use_logs(limit: int = 25, event_ids=None):
    """Reserva logs pendientes para evitar que dos procesos los envíen a la vez."""
    limit = max(1, min(int(limit), 100))
    now = time.time()
    async with pool.acquire() as conn:
        async with conn.transaction():
            if event_ids:
                rows = await conn.fetch(
                    """
                    SELECT id, user_id, item_id, guild_id, channel_id, content,
                           allow_mentions, attempts, created_at
                    FROM item_use_log_outbox
                    WHERE id = ANY($1::TEXT[])
                      AND next_attempt_at <= $2
                      AND locked_until <= $2
                    ORDER BY created_at ASC
                    LIMIT $3
                    FOR UPDATE SKIP LOCKED
                    """,
                    list(event_ids),
                    now,
                    limit,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT id, user_id, item_id, guild_id, channel_id, content,
                           allow_mentions, attempts, created_at
                    FROM item_use_log_outbox
                    WHERE next_attempt_at <= $1
                      AND locked_until <= $1
                    ORDER BY created_at ASC
                    LIMIT $2
                    FOR UPDATE SKIP LOCKED
                    """,
                    now,
                    limit,
                )

            claimed_ids = [row["id"] for row in rows]
            if claimed_ids:
                await conn.execute(
                    """
                    UPDATE item_use_log_outbox
                    SET locked_until=$1
                    WHERE id = ANY($2::TEXT[])
                    """,
                    now + 60,
                    claimed_ids,
                )
    return [dict(row) for row in rows]


async def mark_item_use_log_sent(event_id: str):
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM item_use_log_outbox WHERE id=$1",
            event_id,
        )


async def mark_item_use_log_failed(
    event_id: str,
    error: str,
    retry_after: float,
):
    now = time.time()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE item_use_log_outbox
            SET attempts=attempts+1,
                next_attempt_at=$1,
                locked_until=0,
                last_error=$2
            WHERE id=$3
            """,
            now + max(1, retry_after),
            error[:1000],
            event_id,
        )


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
            """
            SELECT user_id, guild_id, rol_id, MAX(expira_en) AS expira_en
            FROM cargos_temporales
            WHERE expira_en > $1
            GROUP BY user_id, guild_id, rol_id
            """,
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


async def claim_expired_cargos(limit: int = 50):
    """
    Reserva cargos vencidos directamente desde Aiven.
    La reserva expira sola para que otro proceso pueda recuperarla tras una caída.
    """
    limit = max(1, min(int(limit), 200))
    now = time.time()
    async with pool.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(
                """
                SELECT id, user_id, guild_id, rol_id, expira_en,
                       attempts, last_error
                FROM cargos_temporales
                WHERE expira_en <= $1
                  AND next_attempt_at <= $1
                  AND locked_until <= $1
                ORDER BY expira_en ASC
                LIMIT $2
                FOR UPDATE SKIP LOCKED
                """,
                now,
                limit,
            )
            cargo_ids = [row["id"] for row in rows]
            if cargo_ids:
                await conn.execute(
                    """
                    UPDATE cargos_temporales
                    SET locked_until=$1
                    WHERE id = ANY($2::INTEGER[])
                    """,
                    now + 90,
                    cargo_ids,
                )
    return [dict(row) for row in rows]


async def get_cargo_temporal_by_id(cargo_id: int):
    """Revalida una reserva antes de alterar Discord."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, user_id, guild_id, rol_id, expira_en,
                   attempts, next_attempt_at, locked_until, last_error
            FROM cargos_temporales
            WHERE id=$1
            """,
            cargo_id,
        )
    return dict(row) if row else None


async def mark_cargo_removal_failed(
    cargo_id: int,
    error: str,
    retry_after: float,
):
    """Libera la reserva y conserva el fallo para reintentarlo tras reinicios."""
    now = time.time()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE cargos_temporales
            SET attempts=attempts+1,
                next_attempt_at=$1,
                locked_until=0,
                last_error=$2
            WHERE id=$3
            """,
            now + max(1, retry_after),
            error[:1000],
            cargo_id,
        )


async def release_cargo_claim(cargo_id: int):
    """Libera una reserva que dejó de estar vencida antes de procesarse."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE cargos_temporales
            SET locked_until=0
            WHERE id=$1
            """,
            cargo_id,
        )


async def delete_cargo_temporal_by_id(cargo_id: int):
    """Elimina exactamente la expiración procesada y sincroniza la caché."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            DELETE FROM cargos_temporales
            WHERE id=$1
            RETURNING user_id, guild_id, rol_id
            """,
            cargo_id,
        )
    if row:
        cache.remove_cargo_cache(
            row["user_id"],
            row["rol_id"],
            row["guild_id"],
        )
        return True
    return False


async def add_cargo_temporal(user_id, guild_id, rol_id, expira_en):
    async with pool.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(
                """
                SELECT id, expira_en FROM cargos_temporales
                WHERE user_id=$1 AND guild_id=$2 AND rol_id=$3
                FOR UPDATE
                """,
                user_id,
                guild_id,
                rol_id,
            )
            expira_en = max(
                expira_en,
                max((row["expira_en"] for row in rows), default=0),
            )
            await conn.execute(
                """
                DELETE FROM cargos_temporales
                WHERE user_id=$1 AND guild_id=$2 AND rol_id=$3
                """,
                user_id,
                guild_id,
                rol_id,
            )
            await conn.execute(
                """
                INSERT INTO cargos_temporales
                    (user_id, guild_id, rol_id, expira_en)
                VALUES ($1, $2, $3, $4)
                """,
                user_id,
                guild_id,
                rol_id,
                expira_en,
            )
    cache.upsert_cargo_cache(user_id, guild_id, rol_id, expira_en)

async def delete_cargo_temporal(user_id, rol_id, guild_id=None):
    async with pool.acquire() as conn:
        if guild_id is None:
            await conn.execute(
                "DELETE FROM cargos_temporales WHERE user_id=$1 AND rol_id=$2",
                user_id,
                rol_id,
            )
        else:
            await conn.execute(
                """
                DELETE FROM cargos_temporales
                WHERE user_id=$1 AND rol_id=$2 AND guild_id=$3
                """,
                user_id,
                rol_id,
                guild_id,
            )
    cache.remove_cargo_cache(user_id, rol_id, guild_id)


async def load_item_role_restrictions_to_cache():
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT rol_id FROM item_role_restrictions")
    cache.set_restricted_item_role_ids(row["rol_id"] for row in rows)


async def set_item_role_restrictions_db(role_ids):
    role_ids = list(set(role_ids))
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM item_role_restrictions")
            await conn.executemany(
                "INSERT INTO item_role_restrictions (rol_id) VALUES ($1)",
                [(role_id,) for role_id in role_ids],
            )
    cache.set_restricted_item_role_ids(role_ids)


async def remove_item_role_restriction_db(role_id):
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM item_role_restrictions WHERE rol_id=$1", role_id)
    cache.remove_restricted_item_role_id(role_id)


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


async def delete_orphan_collect_configs_db(role_ids):
    """Elimina configuraciones collect huérfanas y sus cooldowns asociados."""
    if not role_ids:
        return 0

    async with pool.acquire() as conn:
        async with conn.transaction():
            deleted = await conn.fetch(
                "DELETE FROM collect_config WHERE rol_id = ANY($1::BIGINT[]) RETURNING rol_id",
                role_ids,
            )
            if deleted:
                await conn.execute(
                    "DELETE FROM collect_cooldowns WHERE rol_id = ANY($1::BIGINT[])",
                    role_ids,
                )

    for role_id in role_ids:
        cache.delete_collect_config(role_id)
    return len(deleted)


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
    from core.config import (
        game_config, rr_config, ruleta_config, rob_config, dados_config,
        memo_config, blackjack_config,
    )
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
        CREATE TABLE IF NOT EXISTS memo_config_db (
            id SERIAL PRIMARY KEY,
            max_apuesta INTEGER NOT NULL,
            cooldown INTEGER NOT NULL,
            activa BOOLEAN DEFAULT TRUE
        )
        """)
        memo_exists = await conn.fetchrow("SELECT * FROM memo_config_db LIMIT 1")
        if not memo_exists:
            await conn.execute("""
            INSERT INTO memo_config_db (max_apuesta, cooldown, activa)
            VALUES ($1, $2, $3)
            """,
            memo_config["max_apuesta"],
            memo_config["cooldown"],
            memo_config["activa"],
        )

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS blackjack_config_db (
            id SMALLINT PRIMARY KEY CHECK (id = 1),
            max_apuesta INTEGER NOT NULL DEFAULT 100,
            cooldown INTEGER NOT NULL,
            activa BOOLEAN NOT NULL DEFAULT TRUE
        )
        """)
        await conn.execute(
            """
            ALTER TABLE blackjack_config_db
            ADD COLUMN IF NOT EXISTS max_apuesta INTEGER NOT NULL DEFAULT 100
            """
        )
        await conn.execute(
            """
            ALTER TABLE blackjack_config_db
            DROP COLUMN IF EXISTS ganancia_pct,
            DROP COLUMN IF EXISTS perdida_pct
            """
        )
        await conn.execute(
            """
            INSERT INTO blackjack_config_db (
                id, max_apuesta, cooldown, activa
            ) VALUES (1, $1, $2, $3)
            ON CONFLICT (id) DO NOTHING
            """,
            blackjack_config["max_apuesta"],
            blackjack_config["cooldown"],
            blackjack_config["activa"],
        )

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS golpear_config (
            id SERIAL PRIMARY KEY,
            canal_id BIGINT,
            min_time INTEGER DEFAULT 600,
            max_time INTEGER DEFAULT 3600,
            min_ganancia INTEGER DEFAULT 150,
            max_ganancia INTEGER DEFAULT 800,
            activo BOOLEAN DEFAULT FALSE
        )
        """)
        golpear_exists = await conn.fetchrow("SELECT * FROM golpear_config LIMIT 1")
        if not golpear_exists:
            await conn.execute("""
            INSERT INTO golpear_config (
                canal_id, min_time, max_time, min_ganancia, max_ganancia, activo
            ) VALUES ($1, $2, $3, $4, $5, $6)
            """,
            None, 600, 3600, 150, 800, False,
        )

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS nave_config (
            id INTEGER PRIMARY KEY DEFAULT 1,
            contenido TEXT
        )
        """)

async def load_golpear_config_to_cache():
    """Devuelve un dict con los valores de la DB. NO importa modules.golpear."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM golpear_config LIMIT 1")
    if row:
        return {
            "canal_id":     row["canal_id"],
            "min_time":     row["min_time"],
            "max_time":     row["max_time"],
            "min_ganancia": row["min_ganancia"],
            "max_ganancia": row["max_ganancia"],
            "activo":       row["activo"],
        }
    return None

async def save_golpear_config(canal_id, min_time, max_time, min_ganancia, max_ganancia, activo):
    """Guarda la config en la DB. Recibe los valores explicitamente, NO importa modules.golpear."""
    async with pool.acquire() as conn:
        await conn.execute("""
        UPDATE golpear_config SET
            canal_id=$1, min_time=$2, max_time=$3,
            min_ganancia=$4, max_ganancia=$5, activo=$6
        """,
        canal_id, min_time, max_time, min_ganancia, max_ganancia, activo,
        )

async def load_game_config():
    from core.config import (
        game_config, rr_config, ruleta_config, rob_config, dados_config,
        blackjack_config,
    )
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

        blackjack_row = await conn.fetchrow(
            "SELECT * FROM blackjack_config_db WHERE id=1"
        )
        if blackjack_row:
            blackjack_config["max_apuesta"] = blackjack_row["max_apuesta"]
            blackjack_config["cooldown"] = blackjack_row["cooldown"]
            blackjack_config["activa"] = blackjack_row["activa"]

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


async def save_memo_config():
    from core.config import memo_config
    async with pool.acquire() as conn:
        await conn.execute("""
        UPDATE memo_config_db SET max_apuesta=$1, cooldown=$2, activa=$3
        """,
        memo_config["max_apuesta"],
        memo_config["cooldown"],
        memo_config["activa"],
        )


async def save_blackjack_config():
    from core.config import blackjack_config
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO blackjack_config_db (
                id, max_apuesta, cooldown, activa
            ) VALUES (1, $1, $2, $3)
            ON CONFLICT (id) DO UPDATE SET
                max_apuesta=EXCLUDED.max_apuesta,
                cooldown=EXCLUDED.cooldown,
                activa=EXCLUDED.activa
            """,
            blackjack_config["max_apuesta"],
            blackjack_config["cooldown"],
            blackjack_config["activa"],
        )


async def load_memo_config():
    from core.config import memo_config
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM memo_config_db LIMIT 1")
        if not row:
            return
        memo_config["max_apuesta"] = row["max_apuesta"]
        memo_config["cooldown"] = row["cooldown"]
        memo_config["activa"] = row["activa"]

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


# ── SABOTEADOR CONFIG ──────────────────────────────────

async def load_saboteador_config_to_cache():
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT rol_id FROM saboteador_config")
    cache.set_saboteador_role_ids(row["rol_id"] for row in rows)


async def add_saboteador_role_db(rol_id: int):
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO saboteador_config (rol_id)
            VALUES ($1)
            ON CONFLICT (rol_id) DO NOTHING
        """, rol_id)
    cache.add_saboteador_role(rol_id)


async def delete_saboteador_role_db(rol_id: int):
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM saboteador_config WHERE rol_id=$1", rol_id)
    cache.delete_saboteador_role(rol_id)
