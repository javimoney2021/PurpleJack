import ast
import asyncio
import logging
import random
import time
import traceback
import uuid

import discord
from discord import ButtonStyle, Interaction, ui
from discord.ext import commands

from core.config import COIN, COORDINADOR_ROLE_ID
from core.database import (
    pool, _get_economy_lock, _flush_user_to_db_unlocked, get_system_toggle,
)
from core import cache as economy_cache

logger = logging.getLogger("purplejack.empleos")

EMPLEOS = {
    "limpiador": {
        "salario_min": 1000,
        "salario_max": 1500,
        "dificultad": "Fácil",
        "xp_requisito": 0,
        "xp_ganada": 2,
        "duracion_horas": 3,
        "penalizacion": -500,
        "prob_fallo": 0.15,
        "mensajes_exito": [
            "Has dejado la nave limpia y sin rastros, recibes {monto} {COIN}.",
            "Todo limpio nadie sospecha de ti, ganas {monto} {COIN}.",
            "Ganas la partida con 3 limpiezas exitosas, ganas {monto} {COIN} ."
        ],
        "mensajes_fallo": [
            "Te pillaron limpiando en cafeteria, eso te hace perder {monto} {COIN}.",
            "Revivieron el jugador antes de limpiarlo, pierdes {monto} {COIN}.",
            "Como vas a limpiar delante de varios tripulantes ? pierdes {monto} {COIN}."
        ]
    },
    "ingeniero": {
        "salario_min": 1700,
        "salario_max": 2300,
        "dificultad": "Media",
        "xp_requisito": 30,
        "xp_ganada": 4,
        "duracion_horas": 3,
        "penalizacion": -700,
        "mensajes_exito": [
            "Has reparado con éxito los cables de la nave... {monto} {COIN}.",
            "Las comunicaciones fueron reactivadas {monto} {COIN} .",
            "Corriges problema en el sistema electrico... {monto} {COIN}."
        ],
        "mensajes_fallo": [
            "No lograste reparar el reactor pierdes {monto} {COIN}.",
            "El sistema electrico no fue reparado pierdes {monto} {COIN}.",
            "Ningun tripulantes puede ver por las camaras! pierdes {monto} {COIN}."
        ]
    },
    "plomero": {
        "salario_min": 3000,
        "salario_max": 3800,
        "dificultad": "Difícil",
        "xp_requisito": 50,
        "xp_ganada": 6,
        "duracion_horas": 3,
        "penalizacion": -900,
        "prob_fallo": 0.30,
        "mensajes_exito": [
            "Habia un gloton en los ductos, ganas {monto} {COIN}.",
            "Descubres a un sus saliendo de la alcantarilla, ganas {monto} {COIN}.",
            "Ganas la partida al sellar varios ductos, ganas {monto} {COIN}."
        ],
        "mensajes_fallo": [
            "Ductos sin sellar generó una pérdida de {monto} {COIN}.",
            "No se encontró a nadie en las alcantarillas, pierdes {monto} {COIN}.",
            "No usaste tu habilidad como se debe y recibes {monto} {COIN} de penalización."
        ]
    }
}

# ── OFICINA Y MAESTRÍAS ──────────────────────────────────
OFICINA_XP_MINIMA = 30
MAESTRIA_XP_COSTO = 150
OFICINA_PANEL_SEGUNDOS = 300
JORNADA_PERSISTENTE_SEGUNDOS = 10 * 365 * 24 * 3600
MAESTRIA_THUMBNAIL_URL = "https://pub-a09b3609b6b34dfab5c7aa7742cd1a8a.r2.dev/Purple%20jack%20Harcode/MaestriaPJ.png"

EMPLEOS_MAESTRIA = {
    "chantajista": {
        "nombre": "Chantajista",
        "maestrias_requeridas": 1,
        "dificultad": "Maestría",
        "salario_min": 5000,
        "salario_max": 5000,
        "xp_ganada": 15,
        "duracion_horas": 3,
        "desarrollado": True,
    },
    "cazador": {
        "nombre": "Cazador",
        "maestrias_requeridas": 2,
        "dificultad": "Maestría",
        "salario_min": 7000,
        "salario_max": 7000,
        "xp_ganada": 20,
        "duracion_horas": 3,
        "penalizacion": -1000,
        "desarrollado": True,
    },
    "piromano": {
        "nombre": "Píromano",
        "maestrias_requeridas": 3,
        "dificultad": "Maestría",
        "salario_min": 9000,
        "salario_max": 9000,
        "xp_ganada": 25,
        "duracion_horas": 3,
        "penalizacion": -2500,
        "xp_penalizacion": -30,
        "desarrollado": True,
    },
}

# ── BONOS DE RACHA ──────────────────────────────────────
RACHA_BONUS_XP    = 5     # XP extra al completar racha de 5 éxitos
RACHA_BONUS_COINS = 0.10  # 10 % del pago como bono de coins por racha

# ── STAFF BYPASS ─────────────────────────────────────────
STAFF_BYPASS_ROL  = "Coordinador-ES"   # Rol con cooldowns liberados en empleos
COOLDOWN_RENUNCIA_SEGUNDOS = 3 * 3600

def _es_coordinador(member: discord.Member) -> bool:
    """True si el miembro posee el rol de bypass de cooldowns."""
    return any(
        r.id == COORDINADOR_ROLE_ID or r.name == STAFF_BYPASS_ROL
        for r in member.roles
    )


def _puede_acceder_oficina(data: dict | None, member: discord.Member) -> bool:
    if not data:
        return False
    empleo_actual = normalizar_empleo(data.get("empleo_actual") or "")
    return (
        data.get("exp_laboral", 0) >= OFICINA_XP_MINIMA
        or _es_coordinador(member)
        or empleo_actual in EMPLEOS_MAESTRIA
    )


# ── DEBUG DE INTERACCIONES ───────────────────────────────
def _log_trabajar_error(empleo: str, error: Exception, usuario: str = "?", contexto: str = ""):
    """Log estandarizado para errores en tableros de !trabajar."""
    tipo   = type(error).__name__
    lineas = traceback.extract_tb(error.__traceback__)
    ultima = lineas[-1] if lineas else None
    linea  = f"L.{ultima.lineno} en {ultima.filename.split('/')[-1]}" if ultima else "sin traceback"
    logger.error(
        f"[TRABAJAR/{empleo.upper()}] {contexto} — {tipo}: {error} | "
        f"Usuario: {usuario} | {linea}"
    )


async def _notificar_error_interaccion(interaction: Interaction):
    """Da una respuesta válida a Discord cuando un tablero falla de forma inesperada."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(
                "❌ Ocurrió un error en la jornada. Consulta **!trabajar** nuevamente.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "❌ Ocurrió un error en la jornada. Consulta **!trabajar** nuevamente.",
                ephemeral=True,
            )
    except (discord.HTTPException, discord.NotFound):
        pass


async def _caducar_tablero(view: ui.View):
    """Deshabilita un tablero vencido sin alterar pagos ni cooldowns."""
    view.terminado = True
    for child in view.children:
        child.disabled = True

    if not getattr(view, "message", None):
        return

    try:
        embed = view.build_embed()
        embed.color = discord.Color.dark_grey()
        embed.set_footer(text="Jornada caducada. Consulta !trabajar para iniciar otra.")
        await view.message.edit(embed=embed, view=view)
    except (discord.HTTPException, discord.NotFound):
        pass


async def _editar_tablero_seguro(
    interaction: Interaction,
    view: ui.View,
    *,
    embed: discord.Embed,
    remove_view: bool = False,
):
    """Actualiza un tablero con reintento y fallback al mensaje almacenado."""
    target_view = None if remove_view else view
    last_error = None
    for intento in range(3):
        try:
            await interaction.edit_original_response(embed=embed, view=target_view)
            return
        except (discord.HTTPException, discord.NotFound) as error:
            last_error = error
            if getattr(view, "message", None) is not None:
                try:
                    await view.message.edit(embed=embed, view=target_view)
                    return
                except (discord.HTTPException, discord.NotFound) as message_error:
                    last_error = message_error
            if intento < 2:
                await asyncio.sleep(0.5 * (intento + 1))
    if last_error:
        raise last_error


async def _responder_jornada(
    interaction: Interaction,
    mensaje: str,
    *,
    ephemeral: bool = True,
):
    """Responde correctamente aunque el clic ya se haya diferido."""
    if interaction.response.is_done():
        return await interaction.followup.send(mensaje, ephemeral=ephemeral)
    return await interaction.response.send_message(mensaje, ephemeral=ephemeral)


async def _renovar_jornada_tolerante(session_id: str, timeout: int):
    """Renueva la sesión sin destruir la partida por un fallo transitorio de Aiven."""
    for intento in range(2):
        try:
            return await asyncio.wait_for(
                renovar_jornada(session_id, timeout),
                timeout=4,
            )
        except Exception as error:
            if intento == 0:
                await asyncio.sleep(0.25)
                continue
            logger.warning(
                "No se pudo renovar temporalmente la jornada %s: %s",
                session_id,
                error,
            )
    # La liquidación atómica continúa siendo la autoridad final. Un fallo
    # transitorio de renovación no debe cerrar un tablero que sigue en RAM.
    return None


async def _cerrar_tablero_con_error(
    view: ui.View,
    empleo: str,
    *,
    interaction: Interaction | None = None,
    liquidada: bool = False,
):
    """Retira siempre los controles tras un error de cierre o liquidación."""
    view.terminado = True
    if hasattr(view, "bloqueado"):
        view.bloqueado = True
    for child in view.children:
        child.disabled = True
    view.stop()

    cancelada = True
    if not liquidada:
        cancelada = await cancelar_jornada_segura(view.session_id, "error")

    descripcion = (
        "La jornada fue procesada, pero no fue posible mostrar el resultado. "
        "Consulta **!exp** para verificar tu progreso."
        if liquidada
        else (
            "No fue posible liquidar la jornada. Puedes consultar **!trabajar** nuevamente."
            if cancelada
            else (
                "No fue posible liquidar ni cerrar la jornada en Aiven. "
                "Espera unos minutos antes de consultar **!trabajar** nuevamente."
            )
        )
    )
    embed = discord.Embed(
        title=f"Jornada {empleo} - Error",
        description=descripcion,
        color=discord.Color.red(),
    )
    try:
        if interaction is not None:
            await _editar_tablero_seguro(
                interaction,
                view,
                embed=embed,
                remove_view=True,
            )
        elif getattr(view, "message", None) is not None:
            await view.message.edit(embed=embed, view=None)
    except Exception:
        logger.exception("No se pudo retirar el tablero con error de %s.", empleo)
        if interaction is not None:
            try:
                await _responder_jornada(interaction, descripcion)
            except (discord.HTTPException, discord.NotFound):
                pass


async def _mostrar_tablero_bloqueado(view: ui.View):
    """Refleja visualmente que el tablero dejó de aceptar jugadas."""
    for child in view.children:
        child.disabled = True
    if getattr(view, "message", None) is None:
        return
    try:
        await view.message.edit(embed=view.build_embed(), view=view)
    except (discord.HTTPException, discord.NotFound):
        logger.warning(
            "No se pudo mostrar el bloqueo visual de la jornada %s.",
            getattr(view, "session_id", "?"),
        )


def _programar_eliminacion(view: ui.View, delay: int = 180):
    async def cleanup():
        await asyncio.sleep(delay)
        try:
            if getattr(view, "message", None):
                await view.message.delete()
        except (discord.HTTPException, discord.NotFound):
            pass

    asyncio.create_task(cleanup())


# ── CONFIG DESPIDOS ─────────────────────────────────────
_despidos_config = {"activo": False}

_EMPLEOS_CACHE = {}
_CONFIRMACION_EMPLEO_LOCKS = {}
def _get_confirmacion_empleo_lock(user_id: int) -> asyncio.Lock:
    lock = _CONFIRMACION_EMPLEO_LOCKS.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _CONFIRMACION_EMPLEO_LOCKS[user_id] = lock
    return lock


def format_relative_time(unix_ts: float) -> str:
    return f"<t:{int(unix_ts)}:R>"


def normalizar_empleo(nombre: str) -> str:
    texto = nombre.lower().strip()
    texto = texto.replace("í", "i").replace("á", "a").replace("é", "e").replace("ó", "o").replace("ú", "u")
    texto = texto.replace("limpador", "limpiador")
    return texto


async def init_empleos_tables():
    if not pool:
        return
    async with pool.acquire() as conn:
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS empleos_users (
            user_id BIGINT PRIMARY KEY,
            empleo_actual TEXT DEFAULT NULL,
            dificultad TEXT DEFAULT NULL,
            fecha_contratacion DOUBLE PRECISION DEFAULT 0,
            ultimo_trabajo DOUBLE PRECISION DEFAULT 0,
            historial_reciente_de_jornadas TEXT DEFAULT '[]',
            cooldown_renuncia DOUBLE PRECISION DEFAULT 0,
            progreso_permanencia DOUBLE PRECISION DEFAULT 0,
            ultimo_empleo TEXT DEFAULT NULL,
            progreso_requisito DOUBLE PRECISION DEFAULT 0,
            despedido_inactividad BOOLEAN DEFAULT FALSE,
            exp_laboral INTEGER DEFAULT 0,
            maestrias INTEGER DEFAULT 0,
            trabajos_exitosos INTEGER DEFAULT 0,
            trabajos_fallidos INTEGER DEFAULT 0,
            total_generado INTEGER DEFAULT 0,
            racha_exitos INTEGER DEFAULT 0,
            ingresos_empleo_actual INTEGER DEFAULT 0,
            exitosos_empleo_actual INTEGER DEFAULT 0,
            fallidos_empleo_actual INTEGER DEFAULT 0
        )
        """)
        for column, definition in [
            ("exp_laboral", "INTEGER DEFAULT 0"),
            ("maestrias", "INTEGER DEFAULT 0"),
            ("trabajos_exitosos", "INTEGER DEFAULT 0"),
            ("trabajos_fallidos", "INTEGER DEFAULT 0"),
            ("total_generado", "INTEGER DEFAULT 0"),
            ("racha_exitos", "INTEGER DEFAULT 0"),
            ("ingresos_empleo_actual", "INTEGER DEFAULT 0"),
            ("exitosos_empleo_actual", "INTEGER DEFAULT 0"),
            ("fallidos_empleo_actual", "INTEGER DEFAULT 0"),
        ]:
            await conn.execute(f"ALTER TABLE empleos_users ADD COLUMN IF NOT EXISTS {column} {definition}")
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS empleos_historial (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            empleo TEXT NOT NULL,
            timestamp DOUBLE PRECISION NOT NULL,
            exito BOOLEAN NOT NULL,
            pago INTEGER NOT NULL,
            motivo TEXT NOT NULL
        )
        """)
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS empleos_jornadas (
            session_id TEXT PRIMARY KEY,
            request_id TEXT NOT NULL UNIQUE,
            user_id BIGINT NOT NULL,
            empleo TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            guild_id BIGINT,
            channel_id BIGINT,
            message_id BIGINT,
            created_at DOUBLE PRECISION NOT NULL,
            expires_at DOUBLE PRECISION NOT NULL,
            finalized_at DOUBLE PRECISION,
            exito BOOLEAN,
            pago INTEGER NOT NULL DEFAULT 0
        )
        """)
        await conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS empleos_una_jornada_activa_idx
        ON empleos_jornadas (user_id)
        WHERE status='active'
        """)
        await conn.execute("""
        CREATE INDEX IF NOT EXISTS empleos_jornadas_estado_idx
        ON empleos_jornadas (status, expires_at)
        """)


async def get_jornada_activa(user_id: int):
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT * FROM empleos_jornadas
            WHERE user_id=$1 AND status='active'
            """,
            user_id,
        )
    return dict(row) if row else None


async def crear_jornada(
    user_id: int,
    empleo: str,
    request_id: str,
    *,
    guild_id: int | None,
    channel_id: int,
    timeout: int,
    cooldown_seconds: int,
    bypass_cooldown: bool,
):
    """Crea una única jornada por usuario, también entre varias instancias."""
    session_id = str(uuid.uuid4())
    now = time.time()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # La fila laboral es el punto de serialización entre procesos.
            empleo_actual = await conn.fetchrow(
                """
                SELECT user_id, empleo_actual, ultimo_trabajo
                FROM empleos_users WHERE user_id=$1 FOR UPDATE
                """,
                user_id,
            )
            if not empleo_actual or normalizar_empleo(
                empleo_actual["empleo_actual"] or ""
            ) != empleo:
                return {"ok": False, "reason": "employment_changed"}
            duplicate = await conn.fetchrow(
                "SELECT session_id, status FROM empleos_jornadas WHERE request_id=$1",
                request_id,
            )
            if duplicate:
                return {
                    "ok": False,
                    "reason": "duplicate_request",
                    "session_id": duplicate["session_id"],
                }

            disponible_en = (
                float(empleo_actual["ultimo_trabajo"] or 0) + cooldown_seconds
            )
            if not bypass_cooldown and disponible_en > now:
                return {
                    "ok": False,
                    "reason": "cooldown",
                    "expires_at": disponible_en,
                }

            active = await conn.fetchrow(
                """
                SELECT session_id, expires_at FROM empleos_jornadas
                WHERE user_id=$1 AND status='active'
                FOR UPDATE
                """,
                user_id,
            )
            if active and active["expires_at"] <= now:
                await conn.execute(
                    """
                    UPDATE empleos_jornadas
                    SET status='cancelled', finalized_at=$2
                    WHERE session_id=$1 AND status='active'
                    """,
                    active["session_id"],
                    now,
                )
                active = None
            if active:
                return {
                    "ok": False,
                    "reason": "already_active",
                    "session_id": active["session_id"],
                }

            await conn.execute(
                """
                INSERT INTO empleos_jornadas (
                    session_id, request_id, user_id, empleo, status,
                    guild_id, channel_id, created_at, expires_at
                )
                VALUES ($1,$2,$3,$4,'active',$5,$6,$7,$8)
                """,
                session_id,
                request_id,
                user_id,
                empleo,
                guild_id,
                channel_id,
                now,
                now + timeout + 30,
            )
    return {"ok": True, "session_id": session_id}


async def asociar_mensaje_jornada(session_id: str, message_id: int):
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE empleos_jornadas SET message_id=$2
            WHERE session_id=$1 AND status='active'
            """,
            session_id,
            message_id,
        )


async def renovar_jornada(session_id: str, timeout: int) -> bool:
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE empleos_jornadas
            SET expires_at=$2
            WHERE session_id=$1 AND status='active'
            """,
            session_id,
            time.time() + timeout + 30,
        )
    return result.endswith("1")


async def cancelar_jornada(session_id: str, status: str = "cancelled") -> bool:
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE empleos_jornadas
            SET status=$2, finalized_at=$3
            WHERE session_id=$1 AND status='active'
            """,
            session_id,
            status,
            time.time(),
        )
    return result.endswith("1")


async def cancelar_jornada_segura(
    session_id: str,
    status: str = "cancelled",
) -> bool:
    try:
        return await cancelar_jornada(session_id, status)
    except Exception:
        logger.exception(
            "No se pudo cerrar la jornada %s con estado %s.",
            session_id,
            status,
        )
        return False


async def cancelar_jornadas_de_ejecucion_anterior():
    """Cierra sesiones RAM huérfanas y devuelve sus mensajes para limpiarlos."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            UPDATE empleos_jornadas
            SET status='cancelled', finalized_at=$1
            WHERE status='active'
            RETURNING user_id, empleo, guild_id, channel_id, message_id
            """,
            time.time(),
        )
    return [dict(row) for row in rows]


def _parse_historial_jornadas(value) -> list:
    if isinstance(value, list):
        return value
    try:
        return ast.literal_eval(value or "[]")
    except Exception:
        return []


async def _finalizar_jornada_atomica_unlocked(
    session_id: str,
    user_id: int,
    empleo: str,
    exito: bool,
    pago: int,
    motivo: str,
    *,
    xp_ganada: int = 0,
    penalizacion_desde_balance: bool = False,
):
    """Liquida banco, EXP, historial y sesión exactamente una vez."""
    now = time.time()
    bonus = {"coins": 0, "xp": 0}
    overflow_balance = 0

    async with pool.acquire() as conn:
        async with conn.transaction():
            # Mismo orden de locks que crear_jornada: empleo -> sesión -> economía.
            # Evita deadlocks entre un clic final y otro !trabajar simultáneo.
            empleo_row = await conn.fetchrow(
                "SELECT * FROM empleos_users WHERE user_id=$1 FOR UPDATE",
                user_id,
            )
            if not empleo_row:
                raise RuntimeError("Usuario laboral inexistente")
            session = await conn.fetchrow(
                "SELECT * FROM empleos_jornadas WHERE session_id=$1 FOR UPDATE",
                session_id,
            )
            if not session:
                return {"ok": False, "reason": "session_not_found", "bonus": bonus}
            if session["status"] != "active":
                return {
                    "ok": False,
                    "reason": "already_finalized",
                    "status": session["status"],
                    "bonus": bonus,
                }
            if session["user_id"] != user_id:
                return {"ok": False, "reason": "invalid_owner", "bonus": bonus}
            if normalizar_empleo(empleo_row["empleo_actual"] or "") != normalizar_empleo(empleo):
                return {
                    "ok": False,
                    "reason": "employment_changed",
                    "bonus": bonus,
                }

            user = await conn.fetchrow(
                "SELECT * FROM users WHERE id=$1 FOR UPDATE",
                user_id,
            )
            if not user:
                raise RuntimeError("Usuario económico inexistente")

            historial = _parse_historial_jornadas(
                empleo_row["historial_reciente_de_jornadas"]
            )
            historial = (historial + [now])[-10:]
            progreso = max(
                empleo_row["progreso_permanencia"] or 0,
                now - (empleo_row["fecha_contratacion"] or now),
            )

            racha = empleo_row["racha_exitos"] or 0
            nueva_xp = empleo_row["exp_laboral"] or 0
            if exito:
                racha += 1
                nueva_xp += max(0, xp_ganada)
                if racha % 5 == 0:
                    bonus = {
                        "coins": int(pago * RACHA_BONUS_COINS) if pago > 0 else 0,
                        "xp": RACHA_BONUS_XP,
                    }
                    nueva_xp += RACHA_BONUS_XP
                    racha = 0
            else:
                racha = 0
                nueva_xp += min(0, xp_ganada)

            coin_delta = pago + bonus["coins"]
            new_balance = user["balance"]
            new_bank = user["bank"]
            if coin_delta > 0:
                espacio = max(0, economy_cache.MAX_BANK - new_bank)
                aplicado_banco = min(coin_delta, espacio)
                new_bank += aplicado_banco
                overflow_balance = coin_delta - aplicado_banco
                new_balance += overflow_balance
            elif coin_delta < 0:
                if penalizacion_desde_balance:
                    new_balance += coin_delta
                else:
                    new_bank += coin_delta

            user_row = await conn.fetchrow(
                """
                UPDATE users SET balance=$2, bank=$3
                WHERE id=$1
                RETURNING balance, bank, cooldown_work, cooldown_crime
                """,
                user_id,
                new_balance,
                new_bank,
            )
            empleo_actualizado = await conn.fetchrow(
                """
                UPDATE empleos_users SET
                    ultimo_trabajo=$2,
                    progreso_permanencia=$3,
                    historial_reciente_de_jornadas=$4,
                    exp_laboral=$5,
                    racha_exitos=$6,
                    trabajos_exitosos=COALESCE(trabajos_exitosos, 0)+$7,
                    exitosos_empleo_actual=COALESCE(exitosos_empleo_actual, 0)+$7,
                    trabajos_fallidos=COALESCE(trabajos_fallidos, 0)+$8,
                    fallidos_empleo_actual=COALESCE(fallidos_empleo_actual, 0)+$8,
                    total_generado=COALESCE(total_generado, 0)+$9,
                    ingresos_empleo_actual=COALESCE(ingresos_empleo_actual, 0)+$9
                WHERE user_id=$1
                RETURNING *
                """,
                user_id,
                now,
                progreso,
                repr(historial),
                nueva_xp,
                racha,
                1 if exito else 0,
                0 if exito else 1,
                max(0, pago) if exito else 0,
            )
            await conn.execute(
                """
                INSERT INTO empleos_historial (
                    user_id, empleo, timestamp, exito, pago, motivo
                ) VALUES ($1,$2,$3,$4,$5,$6)
                """,
                user_id,
                empleo,
                now,
                exito,
                pago,
                motivo,
            )
            await conn.execute(
                """
                UPDATE empleos_jornadas SET
                    status=$2, finalized_at=$3, exito=$4, pago=$5
                WHERE session_id=$1
                """,
                session_id,
                "completed" if exito else "failed",
                now,
                exito,
                pago,
            )

    if user_row:
        economy_cache.set_cache(user_id, dict(user_row))
    if overflow_balance:
        economy_cache.record_evento_balance_delta(user_id, overflow_balance)
    if empleo_actualizado:
        data = dict(empleo_actualizado)
        data["historial_reciente_de_jornadas"] = _parse_historial_jornadas(
            data["historial_reciente_de_jornadas"]
        )
        _EMPLEOS_CACHE[user_id] = data
    return {"ok": True, "bonus": bonus}


async def finalizar_jornada_atomica(
    session_id: str,
    user_id: int,
    empleo: str,
    exito: bool,
    pago: int,
    motivo: str,
    *,
    xp_ganada: int = 0,
    penalizacion_desde_balance: bool = False,
):
    async with _get_economy_lock(user_id):
        # Conserva cualquier movimiento legítimo que aún estuviera dirty en RAM
        # antes de calcular la liquidación sobre el saldo real de PostgreSQL.
        flushed = await _flush_user_to_db_unlocked(user_id)
        if flushed is False:
            raise RuntimeError("No se pudo sincronizar el saldo antes de la jornada")
        return await _finalizar_jornada_atomica_unlocked(
            session_id,
            user_id,
            empleo,
            exito,
            pago,
            motivo,
            xp_ganada=xp_ganada,
            penalizacion_desde_balance=penalizacion_desde_balance,
        )


async def get_empleo_user(user_id, force_refresh=False):
    if not force_refresh:
        cached = _EMPLEOS_CACHE.get(user_id)
        if cached is not None:
            return cached
    if not pool:
        return None
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM empleos_users WHERE user_id=$1", user_id)
    if not row:
        data = {
            "user_id": user_id,
            "empleo_actual": None,
            "dificultad": None,
            "fecha_contratacion": 0,
            "ultimo_trabajo": 0,
            "historial_reciente_de_jornadas": [],
            "cooldown_renuncia": 0,
            "progreso_permanencia": 0,
            "ultimo_empleo": None,
            "progreso_requisito": 0,
            "despedido_inactividad": False,
            "exp_laboral": 0,
            "maestrias": 0,
            "trabajos_exitosos": 0,
            "trabajos_fallidos": 0,
            "total_generado": 0,
            "racha_exitos": 0,
            "ingresos_empleo_actual": 0,
            "exitosos_empleo_actual": 0,
            "fallidos_empleo_actual": 0,
        }
    else:
        data = dict(row)
        try:
            data["historial_reciente_de_jornadas"] = ast.literal_eval(data["historial_reciente_de_jornadas"])
        except Exception:
            data["historial_reciente_de_jornadas"] = []
    _EMPLEOS_CACHE[user_id] = data
    return data


async def save_empleo_user(data):
    if not pool:
        return
    hist = data.get("historial_reciente_de_jornadas", [])
    hist_json = repr(hist)
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO empleos_users (
                user_id, empleo_actual, dificultad, fecha_contratacion,
                ultimo_trabajo, historial_reciente_de_jornadas,
                cooldown_renuncia, progreso_permanencia,
                ultimo_empleo, progreso_requisito, despedido_inactividad,
                exp_laboral, maestrias, trabajos_exitosos, trabajos_fallidos,
                total_generado, racha_exitos,
                ingresos_empleo_actual, exitosos_empleo_actual, fallidos_empleo_actual
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20)
            ON CONFLICT (user_id) DO UPDATE SET
                empleo_actual=EXCLUDED.empleo_actual,
                dificultad=EXCLUDED.dificultad,
                fecha_contratacion=EXCLUDED.fecha_contratacion,
                ultimo_trabajo=EXCLUDED.ultimo_trabajo,
                historial_reciente_de_jornadas=EXCLUDED.historial_reciente_de_jornadas,
                cooldown_renuncia=EXCLUDED.cooldown_renuncia,
                progreso_permanencia=EXCLUDED.progreso_permanencia,
                ultimo_empleo=EXCLUDED.ultimo_empleo,
                progreso_requisito=EXCLUDED.progreso_requisito,
                despedido_inactividad=EXCLUDED.despedido_inactividad,
                exp_laboral=EXCLUDED.exp_laboral,
                maestrias=EXCLUDED.maestrias,
                trabajos_exitosos=EXCLUDED.trabajos_exitosos,
                trabajos_fallidos=EXCLUDED.trabajos_fallidos,
                total_generado=EXCLUDED.total_generado,
                racha_exitos=EXCLUDED.racha_exitos,
                ingresos_empleo_actual=EXCLUDED.ingresos_empleo_actual,
                exitosos_empleo_actual=EXCLUDED.exitosos_empleo_actual,
                fallidos_empleo_actual=EXCLUDED.fallidos_empleo_actual
        """, data["user_id"], data.get("empleo_actual"), data.get("dificultad"), data.get("fecha_contratacion", 0),
             data.get("ultimo_trabajo", 0), hist_json, data.get("cooldown_renuncia", 0),
             data.get("progreso_permanencia", 0), data.get("ultimo_empleo"),
             data.get("progreso_requisito", 0), data.get("despedido_inactividad", False),
             data.get("exp_laboral", 0), data.get("maestrias", 0), data.get("trabajos_exitosos", 0), data.get("trabajos_fallidos", 0),
             data.get("total_generado", 0), data.get("racha_exitos", 0),
             data.get("ingresos_empleo_actual", 0), data.get("exitosos_empleo_actual", 0), data.get("fallidos_empleo_actual", 0))
    _EMPLEOS_CACHE[data["user_id"]] = data


async def contratar_empleo_maestria_atomica(data: dict, maestrias_consumidas: int):
    """Contrata y consume maestrías en una única operación de base de datos."""
    if not pool or maestrias_consumidas <= 0:
        return None

    hist_json = repr(data.get("historial_reciente_de_jornadas", []))
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE empleos_users
            SET empleo_actual=$3,
                dificultad=$4,
                fecha_contratacion=$5,
                ultimo_trabajo=$6,
                historial_reciente_de_jornadas=$7,
                cooldown_renuncia=$8,
                progreso_permanencia=$9,
                despedido_inactividad=$10,
                ingresos_empleo_actual=$11,
                exitosos_empleo_actual=$12,
                fallidos_empleo_actual=$13,
                maestrias=maestrias-$2
            WHERE user_id=$1 AND maestrias >= $2
            RETURNING *
            """,
            data["user_id"],
            maestrias_consumidas,
            data.get("empleo_actual"),
            data.get("dificultad"),
            data.get("fecha_contratacion", 0),
            data.get("ultimo_trabajo", 0),
            hist_json,
            data.get("cooldown_renuncia", 0),
            data.get("progreso_permanencia", 0),
            data.get("despedido_inactividad", False),
            data.get("ingresos_empleo_actual", 0),
            data.get("exitosos_empleo_actual", 0),
            data.get("fallidos_empleo_actual", 0),
        )
    if not row:
        return None

    saved_data = dict(row)
    try:
        saved_data["historial_reciente_de_jornadas"] = ast.literal_eval(
            saved_data["historial_reciente_de_jornadas"]
        )
    except Exception:
        saved_data["historial_reciente_de_jornadas"] = []
    _EMPLEOS_CACHE[data["user_id"]] = saved_data
    return saved_data


async def append_historial(user_id, empleo, exito, pago, motivo):
    if not pool:
        return
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO empleos_historial (user_id, empleo, timestamp, exito, pago, motivo)
            VALUES ($1, $2, $3, $4, $5, $6)
        """, user_id, empleo, time.time(), exito, pago, motivo)


async def get_all_empleos_activos():
    """Devuelve todos los registros de empleos_users con empleo_actual activo."""
    if not pool:
        return []
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM empleos_users WHERE empleo_actual IS NOT NULL"
        )
    result = []
    for row in rows:
        data = dict(row)
        try:
            data["historial_reciente_de_jornadas"] = ast.literal_eval(data["historial_reciente_de_jornadas"])
        except Exception:
            data["historial_reciente_de_jornadas"] = []
        result.append(data)
    return result


async def get_all_experiencia_laboral():
    """Devuelve el ranking completo de EXP laboral directamente desde la DB."""
    if not pool:
        return []
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT user_id, empleo_actual, exp_laboral
            FROM empleos_users
            ORDER BY exp_laboral DESC, user_id ASC
        """)
    return [dict(row) for row in rows]


async def limpiar_progreso(user_id):
    data = await get_empleo_user(user_id)
    if not data:
        return
    data["progreso_permanencia"] = 0
    data["progreso_requisito"] = 0
    data["fecha_contratacion"] = 0
    data["ultimo_trabajo"] = 0
    data["historial_reciente_de_jornadas"] = []
    data["empleo_actual"] = None
    data["dificultad"] = None
    await save_empleo_user(data)


async def reset_empleo_user(user_id):
    """Reinicia completamente el estado de empleo del usuario en DB y RAM."""
    data = await get_empleo_user(user_id) or {
        "user_id": user_id,
        "empleo_actual": None,
        "dificultad": None,
        "fecha_contratacion": 0,
        "ultimo_trabajo": 0,
        "historial_reciente_de_jornadas": [],
        "cooldown_renuncia": 0,
        "progreso_permanencia": 0,
        "ultimo_empleo": None,
        "progreso_requisito": 0,
        "despedido_inactividad": False,
        "exp_laboral": 0,
        "maestrias": 0,
        "trabajos_exitosos": 0,
        "trabajos_fallidos": 0,
        "total_generado": 0,
        "racha_exitos": 0,
        "ingresos_empleo_actual": 0,
        "exitosos_empleo_actual": 0,
        "fallidos_empleo_actual": 0,
    }

    data.update({
        "empleo_actual": None,
        "dificultad": None,
        "fecha_contratacion": 0,
        "ultimo_trabajo": 0,
        "historial_reciente_de_jornadas": [],
        "cooldown_renuncia": 0,
        "progreso_permanencia": 0,
        "ultimo_empleo": None,
        "progreso_requisito": 0,
        "despedido_inactividad": False,
        "exp_laboral": 0,
        "maestrias": 0,
        "trabajos_exitosos": 0,
        "trabajos_fallidos": 0,
        "total_generado": 0,
        "racha_exitos": 0,
        "ingresos_empleo_actual": 0,
        "exitosos_empleo_actual": 0,
        "fallidos_empleo_actual": 0,
    })

    await save_empleo_user(data)
    _EMPLEOS_CACHE[user_id] = data
    return data


async def registrar_resultado(user_id, empleo, exito, pago, motivo, xp_ganada=0):
    data = await get_empleo_user(user_id)
    if not data:
        return {"coins": 0, "xp": 0}
    ahora = time.time()
    data["ultimo_trabajo"] = ahora
    data["progreso_permanencia"] = max(data.get("progreso_permanencia", 0), ahora - data.get("fecha_contratacion", ahora))
    data["historial_reciente_de_jornadas"] = (data.get("historial_reciente_de_jornadas", []) + [ahora])[-10:]

    bonus = {"coins": 0, "xp": 0}

    if exito:
        data["trabajos_exitosos"]       = data.get("trabajos_exitosos", 0) + 1
        data["exitosos_empleo_actual"]  = data.get("exitosos_empleo_actual", 0) + 1
        data["exp_laboral"]             = data.get("exp_laboral", 0) + max(0, xp_ganada)
        data["racha_exitos"]            = data.get("racha_exitos", 0) + 1
        # ── Bono cada 5 éxitos consecutivos ───────────────
        if data["racha_exitos"] % 5 == 0:
            bono_coins = int(pago * RACHA_BONUS_COINS) if pago > 0 else 0
            data["exp_laboral"] += RACHA_BONUS_XP
            data["racha_exitos"] = 0   # reset tras el bono
            bonus = {"coins": bono_coins, "xp": RACHA_BONUS_XP}
        data["total_generado"]          = data.get("total_generado", 0) + max(0, pago)
        data["ingresos_empleo_actual"]  = data.get("ingresos_empleo_actual", 0) + max(0, pago)
    else:
        data["trabajos_fallidos"]       = data.get("trabajos_fallidos", 0) + 1
        data["fallidos_empleo_actual"]  = data.get("fallidos_empleo_actual", 0) + 1
        data["racha_exitos"] = 0

    await save_empleo_user(data)
    await append_historial(user_id, empleo, exito, pago, motivo)
    return bonus


async def despedir_por_inactividad(user_id: int, data: dict):
    """Despide al usuario por inactividad: limpia empleo en DB y RAM."""
    data["ultimo_empleo"]               = data.get("empleo_actual")
    data["empleo_actual"]               = None
    data["dificultad"]                  = None
    data["fecha_contratacion"]          = 0
    data["ultimo_trabajo"]              = 0
    data["historial_reciente_de_jornadas"] = []
    data["despedido_inactividad"]       = True
    data["cooldown_renuncia"]           = 0   # sin cooldown tras ser despedido
    await save_empleo_user(data)
    _EMPLEOS_CACHE[user_id] = data


def nombre_empleo(empleo: str | None) -> str:
    if not empleo:
        return "Sin empleo"
    empleo_normalizado = normalizar_empleo(empleo)
    return EMPLEOS_MAESTRIA.get(empleo_normalizado, {}).get("nombre", empleo.title())


def build_exp_embed(member: discord.Member, data: dict) -> discord.Embed:
    """Construye el mismo resumen laboral que muestra el comando !exp."""
    empleo = data.get("empleo_actual") or "Sin empleo"
    embed = discord.Embed(
        title=f"📊 Experiencia Laboral - {member.display_name}",
        color=discord.Color.gold(),
    )
    embed.add_field(
        name="Empleo actual",
        value=nombre_empleo(empleo) if empleo != "Sin empleo" else empleo,
        inline=False,
    )
    embed.add_field(name="XP Laboral", value=str(data.get("exp_laboral", 0)), inline=True)
    embed.add_field(name="Trabajos exitosos", value=str(data.get("trabajos_exitosos", 0)), inline=True)
    embed.add_field(name="Trabajos fallidos", value=str(data.get("trabajos_fallidos", 0)), inline=True)
    embed.add_field(name="Total generado", value=f"{data.get('total_generado', 0)} {COIN}", inline=False)
    embed.set_footer(text="Tu progreso laboral se actualiza al terminar cada jornada.")
    embed.set_thumbnail(url="https://pub-a09b3609b6b34dfab5c7aa7742cd1a8a.r2.dev/Purple%20jack%20Harcode/ExpPJ.png")
    return embed


def build_oficina_embed(member: discord.Member, data: dict) -> discord.Embed:
    empleo = nombre_empleo(data.get("empleo_actual"))
    nombre = member.nick or member.display_name
    embed = discord.Embed(
        title=f"{nombre} - {empleo}",
        description="Gerencia tu actividad laboral realizando acciones y consultas en los botones abajo...",
        color=discord.Color.green(),
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text="Gestiona tu situación laboral.")
    return embed


def build_maestrias_embed(data: dict) -> discord.Embed:
    embed = discord.Embed(
        title="Especialización en Maestría",
        description=(
            "Realiza tu maestría para acceder a Empleos especializados.\n\n"
            f"**Maestrías:** {data.get('maestrias', 0)}"
        ),
        color=discord.Color.green(),
    )
    embed.set_thumbnail(url=MAESTRIA_THUMBNAIL_URL)
    return embed


def build_empleos_maestria_embed() -> discord.Embed:
    lineas = [
        f"• **{info['nombre']}** - Consume {info['maestrias_requeridas']} maestría(s)"
        for info in EMPLEOS_MAESTRIA.values()
    ]
    embed = discord.Embed(
        title="Empleos Disponibles",
        description="\n".join(lineas),
        color=discord.Color.green(),
    )
    embed.set_thumbnail(url=MAESTRIA_THUMBNAIL_URL)
    return embed


async def renunciar_empleo(member: discord.Member) -> tuple[bool, str]:
    """Aplica la misma baja y cooldown que el comando !renunciar."""
    if await get_jornada_activa(member.id):
        return False, "⌛ Finaliza primero tu jornada laboral activa."

    data = await get_empleo_user(member.id)
    if not data or not data.get("empleo_actual"):
        return False, "❌ No posees un empleo activo."

    bypass = _es_coordinador(member)
    data["ultimo_empleo"] = data.get("empleo_actual")
    data["progreso_requisito"] = data.get("progreso_permanencia", 0)
    data["cooldown_renuncia"] = 0 if bypass else time.time() + COOLDOWN_RENUNCIA_SEGUNDOS
    data["empleo_actual"] = None
    data["dificultad"] = None
    data["fecha_contratacion"] = 0
    data["ultimo_trabajo"] = 0
    data["historial_reciente_de_jornadas"] = []
    data["despedido_inactividad"] = False
    await save_empleo_user(data)
    if bypass:
        return True, "🛑 Has renunciado a tu empleo. Puedes aplicar inmediatamente a un nuevo trabajo."
    return True, (
        "🛑 Has renunciado a tu empleo. Podrás aplicar a un empleo común dentro de 3 horas. "
        "(No necesitas esperar para aplicar a empleos de Maestría.)"
    )


class ConfirmarEmpleoView(ui.View):
    def __init__(self, bot, user_id, empleo):
        super().__init__(timeout=60)
        self.bot = bot
        self.user_id = user_id
        self.empleo = empleo
        self.procesado = False
        self._interaction_lock = _get_confirmacion_empleo_lock(user_id)

    @ui.button(label="Aceptar empleo", style=ButtonStyle.green)
    async def aceptar(self, interaction: Interaction, button: ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ No es tu confirmación.", ephemeral=True)
        if self._interaction_lock.locked() or self.procesado:
            return await interaction.response.send_message(
                "⌛ Esta solicitud ya se está procesando.",
                ephemeral=True,
            )

        try:
            await interaction.response.defer(thinking=True)
        except discord.NotFound:
            logger.info("La confirmación de empleo de %s venció antes de ser procesada.", self.user_id)
            return

        async with self._interaction_lock:
            if await get_jornada_activa(self.user_id):
                return await interaction.followup.send(
                    "⌛ Finaliza primero tu jornada laboral activa.",
                    ephemeral=True,
                )
            data = await get_empleo_user(self.user_id, force_refresh=True)
            if not data:
                return await interaction.followup.send(
                    "❌ No fue posible consultar tu información laboral. Inténtalo nuevamente.",
                    ephemeral=True,
                )
            bypass = _es_coordinador(interaction.user)
            es_maestria = self.empleo in EMPLEOS_MAESTRIA
            cooldown_until = data.get("cooldown_renuncia", 0)
            if (
                cooldown_until
                and time.time() < cooldown_until
                and not bypass
                and not es_maestria
            ):
                return await interaction.followup.send(
                    f"⏳ Podras Aplicar a otro empleo {format_relative_time(cooldown_until)} Regresa luego.",
                    ephemeral=True,
                )

            if normalizar_empleo(data.get("empleo_actual") or "") == self.empleo:
                return await interaction.followup.send(
                    f"❌ Ya trabajas como **{data['empleo_actual'].title()}**. Elige un empleo distinto.",
                    ephemeral=True,
                )

            info = EMPLEOS_MAESTRIA[self.empleo] if es_maestria else EMPLEOS[self.empleo]
            if es_maestria:
                if not info.get("desarrollado", False):
                    return await interaction.followup.send(
                        f"🚧 **{info['nombre']}**: Trabajo Pendiente de desarrollo.",
                        ephemeral=True,
                    )
                requeridas = info["maestrias_requeridas"]
                if data.get("maestrias", 0) < requeridas and not bypass:
                    return await interaction.followup.send(
                        f"❌ **{info['nombre']}** requiere {requeridas} maestría(s).",
                        ephemeral=True,
                    )
                xp_consumida = 0
                maestrias_consumidas = 0 if bypass else requeridas
            else:
                xp_requerida = info["xp_requisito"]
                if data.get("exp_laboral", 0) < xp_requerida and not bypass:
                    return await interaction.followup.send(
                        f"❌ {interaction.user.mention} necesitas **{xp_requerida}** puntos de Experiencia Laboral para aplicar a **{self.empleo.title()}**.",
                        ephemeral=True,
                    )
                xp_consumida = 0 if bypass else xp_requerida
                maestrias_consumidas = 0

            now = time.time()
            nuevo_data = dict(data)
            nuevo_data["exp_laboral"] -= xp_consumida
            nuevo_data.update({
                "empleo_actual": self.empleo,
                "dificultad": info["dificultad"],
                "cooldown_renuncia": 0,
                "fecha_contratacion": now,
                "ultimo_trabajo": 0,
                "historial_reciente_de_jornadas": [],
                "progreso_permanencia": 0,
                "despedido_inactividad": False,
                "ingresos_empleo_actual": 0,
                "exitosos_empleo_actual": 0,
                "fallidos_empleo_actual": 0,
            })
            if maestrias_consumidas:
                data_guardada = await contratar_empleo_maestria_atomica(
                    nuevo_data,
                    maestrias_consumidas,
                )
                if data_guardada is None:
                    return await interaction.followup.send(
                        f"❌ Ya no tienes las **{requeridas}** maestría(s) necesarias. "
                        "Actualiza el panel e inténtalo nuevamente.",
                        ephemeral=True,
                    )
                nuevo_data = data_guardada
            else:
                await save_empleo_user(nuevo_data)
            self.procesado = True
            self.stop()

        mensaje = (
            f"🎉 {interaction.user.mention} ahora eres **{info['nombre'] if es_maestria else self.empleo.title()}**. "
            "Usa `!trabajar` para iniciar tu jornada de 3 horas."
        )
        if xp_consumida:
            mensaje += f"\n📊 Se consumieron **{xp_consumida}** XP Laboral."
        if maestrias_consumidas:
            mensaje += f"\n🎓 Se consumieron **{maestrias_consumidas}** maestría(s)."
        await interaction.followup.send(mensaje, ephemeral=False)
        try:
            await interaction.message.delete(delay=1)
        except Exception:
            pass

    @ui.button(label="Rechazar empleo", style=ButtonStyle.red)
    async def rechazar(self, interaction: Interaction, button: ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ No es tu confirmación.", ephemeral=True)
        if self._interaction_lock.locked() or self.procesado:
            return await interaction.response.send_message(
                "⌛ Esta solicitud ya se está procesando.",
                ephemeral=True,
            )
        self.procesado = True
        self.stop()
        await interaction.response.send_message("❌ Has rechazado aplicar a este empleo.", ephemeral=True)
        try:
            await interaction.message.delete(delay=1)
        except Exception:
            pass


class OficinaBaseView(ui.View):
    """Base para mantener la Oficina privada y con vencimiento uniforme."""

    def __init__(self, owner_id: int, expires_at: float):
        # Se conserva activa un margen adicional para poder informar el vencimiento.
        super().__init__(timeout=180)
        self.owner_id = owner_id
        self.expires_at = expires_at

    async def validar_interaccion(self, interaction: Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("❌ Este panel no es tuyo.", ephemeral=True)
            return False
        if time.time() >= self.expires_at:
            await interaction.response.send_message(
                "Panel Vencido consulta nuevamente. **!oficina**",
                ephemeral=True,
            )
            return False
        return True


class AbrirOficinaView(ui.View):
    def __init__(self, owner_id: int):
        super().__init__(timeout=120)
        self.owner_id = owner_id

    @ui.button(label="Abrir Oficina", style=ButtonStyle.green)
    async def abrir_oficina(self, interaction: Interaction, button: ui.Button):
        if interaction.user.id != self.owner_id:
            return await interaction.response.send_message("❌ Este panel no es tuyo.", ephemeral=True)

        data = await get_empleo_user(self.owner_id, force_refresh=True)
        if not _puede_acceder_oficina(data, interaction.user):
            return await interaction.response.send_message(
                "Para acceder a la oficina necesitas tener 30 de exp laboral, consulta **!exp** continua trabajando.",
                ephemeral=True,
            )

        expires_at = time.time() + OFICINA_PANEL_SEGUNDOS
        await interaction.response.send_message(
            embed=build_oficina_embed(interaction.user, data),
            view=OficinaView(self.owner_id, expires_at),
            ephemeral=True,
        )
        try:
            await interaction.message.delete(delay=3)
        except discord.HTTPException:
            logger.warning("No se pudo eliminar el acceso temporal a Oficina de %s.", self.owner_id)


class OficinaView(OficinaBaseView):
    @ui.button(label="Info General", style=ButtonStyle.blurple, row=0)
    async def info_general(self, interaction: Interaction, button: ui.Button):
        if not await self.validar_interaccion(interaction):
            return
        data = await get_empleo_user(self.owner_id, force_refresh=True)
        await interaction.response.edit_message(
            embed=build_exp_embed(interaction.user, data),
            view=OficinaInfoView(self.owner_id, self.expires_at),
        )

    @ui.button(label="Maestría", style=ButtonStyle.green, row=0)
    async def maestria(self, interaction: Interaction, button: ui.Button):
        if not await self.validar_interaccion(interaction):
            return
        data = await get_empleo_user(self.owner_id, force_refresh=True)
        await interaction.response.edit_message(
            embed=build_maestrias_embed(data),
            view=MaestriasView(self.owner_id, self.expires_at),
        )

    @ui.button(label="Renunciar", style=ButtonStyle.red, row=0)
    async def renunciar(self, interaction: Interaction, button: ui.Button):
        if not await self.validar_interaccion(interaction):
            return
        _, mensaje = await renunciar_empleo(interaction.user)
        data = await get_empleo_user(self.owner_id, force_refresh=True)
        await interaction.response.edit_message(
            embed=build_oficina_embed(interaction.user, data),
            view=OficinaView(self.owner_id, self.expires_at),
        )
        await interaction.followup.send(mensaje, ephemeral=True)


class OficinaInfoView(OficinaBaseView):
    @ui.button(label="Atrás", style=ButtonStyle.secondary)
    async def atras(self, interaction: Interaction, button: ui.Button):
        if not await self.validar_interaccion(interaction):
            return
        data = await get_empleo_user(self.owner_id, force_refresh=True)
        await interaction.response.edit_message(
            embed=build_oficina_embed(interaction.user, data),
            view=OficinaView(self.owner_id, self.expires_at),
        )


class MaestriaModal(ui.Modal, title="Adquirir Maestrías"):
    cantidad = ui.TextInput(
        label="¿Cuántas maestrías deseas obtener?",
        placeholder="150 XP laboral por maestría. Ej: 3",
        max_length=4,
    )

    def __init__(self, owner_id: int, expires_at: float, panel_message):
        super().__init__()
        self.owner_id = owner_id
        self.expires_at = expires_at
        self.panel_message = panel_message

    async def on_submit(self, interaction: Interaction):
        if interaction.user.id != self.owner_id:
            return await interaction.response.send_message("❌ Este panel no es tuyo.", ephemeral=True)
        if time.time() >= self.expires_at:
            return await interaction.response.send_message(
                "Panel Vencido consulta nuevamente. **!oficina**",
                ephemeral=True,
            )

        try:
            cantidad = int(self.cantidad.value.strip())
        except ValueError:
            return await interaction.response.send_message(
                "❌ Indica una cantidad válida de maestrías.",
                ephemeral=True,
            )

        if cantidad <= 0:
            return await interaction.response.send_message(
                "❌ Debes adquirir al menos una maestría.",
                ephemeral=True,
            )

        data = await get_empleo_user(self.owner_id, force_refresh=True)
        costo = cantidad * MAESTRIA_XP_COSTO
        if data.get("exp_laboral", 0) < costo:
            return await interaction.response.send_message(
                f"❌ Necesitas **{costo}** XP laboral para adquirir {cantidad} maestría(s).",
                ephemeral=True,
            )

        data["exp_laboral"] -= costo
        data["maestrias"] = data.get("maestrias", 0) + cantidad
        await save_empleo_user(data)

        await interaction.response.send_message(
            f"✅ Adquiriste **{cantidad}** maestría(s) por **{costo}** XP laboral.",
            ephemeral=True,
        )
        try:
            await self.panel_message.edit(
                embed=build_maestrias_embed(data),
                view=MaestriasView(self.owner_id, self.expires_at),
            )
        except discord.HTTPException:
            logger.warning("No se pudo actualizar el panel de maestrías de %s.", self.owner_id)


class MaestriasView(OficinaBaseView):
    @ui.button(label="Empleos de Maestrías", style=ButtonStyle.blurple, row=0)
    async def empleos_maestrias(self, interaction: Interaction, button: ui.Button):
        if not await self.validar_interaccion(interaction):
            return
        await interaction.response.edit_message(
            embed=build_empleos_maestria_embed(),
            view=EmpleosMaestriaView(self.owner_id, self.expires_at),
        )

    @ui.button(label="Realizar Maestría", style=ButtonStyle.green, row=0)
    async def realizar_maestria(self, interaction: Interaction, button: ui.Button):
        if not await self.validar_interaccion(interaction):
            return
        await interaction.response.send_modal(
            MaestriaModal(self.owner_id, self.expires_at, interaction.message)
        )

    @ui.button(label="Atrás", style=ButtonStyle.secondary, row=1)
    async def atras(self, interaction: Interaction, button: ui.Button):
        if not await self.validar_interaccion(interaction):
            return
        data = await get_empleo_user(self.owner_id, force_refresh=True)
        await interaction.response.edit_message(
            embed=build_oficina_embed(interaction.user, data),
            view=OficinaView(self.owner_id, self.expires_at),
        )


class EmpleosMaestriaSelect(ui.Select):
    def __init__(self, owner_id: int, expires_at: float):
        opciones = [
            discord.SelectOption(
                label=info["nombre"],
                value=clave,
                description=f"Consume {info['maestrias_requeridas']} maestría(s)",
            )
            for clave, info in EMPLEOS_MAESTRIA.items()
        ]
        super().__init__(placeholder="Selecciona un empleo especializado", options=opciones)
        self.owner_id = owner_id
        self.expires_at = expires_at

    async def callback(self, interaction: Interaction):
        if interaction.user.id != self.owner_id:
            return await interaction.response.send_message("❌ Este panel no es tuyo.", ephemeral=True)
        if time.time() >= self.expires_at:
            return await interaction.response.send_message(
                "Panel Vencido consulta nuevamente. **!oficina**",
                ephemeral=True,
            )

        empleo = self.values[0]
        info = EMPLEOS_MAESTRIA[empleo]
        data = await get_empleo_user(self.owner_id, force_refresh=True)
        if not data:
            return await interaction.response.send_message(
                "❌ No fue posible consultar tu información laboral. Inténtalo nuevamente.",
                ephemeral=True,
            )
        bypass = _es_coordinador(interaction.user)
        if data.get("maestrias", 0) < info["maestrias_requeridas"] and not bypass:
            return await interaction.response.send_message(
                f"❌ **{info['nombre']}** requiere {info['maestrias_requeridas']} maestría(s).",
                ephemeral=True,
            )
        if not info.get("desarrollado", False):
            return await interaction.response.send_message(
                f"🚧 **{info['nombre']}**: Trabajo Pendiente de desarrollo.",
                ephemeral=True,
            )

        if normalizar_empleo(data.get("empleo_actual") or "") == empleo:
            return await interaction.response.send_message(
                f"❌ Ya trabajas como **{info['nombre']}**.",
                ephemeral=True,
            )

        embed = discord.Embed(
            title=f"¿Deseas aplicar como {info['nombre']}?",
            description=(
                f"Requiere **{info['maestrias_requeridas']} maestría(s)**\n"
                f"Al contratar se consumen **{info['maestrias_requeridas']} maestría(s)**\n"
                f"Recompensa: **{info['salario_min']} {COIN} + {info['xp_ganada']} XP Laboral**\n"
                f"Jornada: **{info['duracion_horas']} horas**"
            ),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(
            embed=embed,
            view=ConfirmarEmpleoView(None, self.owner_id, empleo),
            ephemeral=True,
        )


class EmpleosMaestriaView(OficinaBaseView):
    def __init__(self, owner_id: int, expires_at: float):
        super().__init__(owner_id, expires_at)
        self.add_item(EmpleosMaestriaSelect(owner_id, expires_at))

    @ui.button(label="Atrás", style=ButtonStyle.secondary, row=1)
    async def atras(self, interaction: Interaction, button: ui.Button):
        if not await self.validar_interaccion(interaction):
            return
        data = await get_empleo_user(self.owner_id, force_refresh=True)
        await interaction.response.edit_message(
            embed=build_maestrias_embed(data),
            view=MaestriasView(self.owner_id, self.expires_at),
        )


class Empleos(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._jornadas_huerfanas = []
        self._jornadas_huerfanas_limpiadas = False

    async def cog_load(self):
        await init_empleos_tables()
        _despidos_config["activo"] = await get_system_toggle("despidos", False)
        self._jornadas_huerfanas = await cancelar_jornadas_de_ejecucion_anterior()

    @commands.Cog.listener()
    async def on_ready(self):
        if self._jornadas_huerfanas_limpiadas:
            return
        self._jornadas_huerfanas_limpiadas = True
        for jornada in self._jornadas_huerfanas:
            if not jornada.get("channel_id") or not jornada.get("message_id"):
                continue
            try:
                channel = self.bot.get_channel(jornada["channel_id"])
                if channel is None:
                    channel = await self.bot.fetch_channel(jornada["channel_id"])
                message = await channel.fetch_message(jornada["message_id"])
                embed = discord.Embed(
                    title="Jornada interrumpida",
                    description=(
                        "Esta jornada fue cerrada de forma segura durante un reinicio. "
                        "Puedes consultar **!trabajar** para iniciar una nueva."
                    ),
                    color=discord.Color.dark_grey(),
                )
                await message.edit(embed=embed, view=None)
            except (discord.HTTPException, discord.NotFound, discord.Forbidden):
                logger.warning(
                    "No se pudo cerrar el tablero laboral huérfano %s/%s.",
                    jornada.get("channel_id"),
                    jornada.get("message_id"),
                )
        self._jornadas_huerfanas.clear()

    @commands.command(name="empleos", aliases=["trabajos"])
    async def empleos(self, ctx):
        embed = discord.Embed(title="💼 Empleos Disponibles", color=discord.Color.blue())
        for nombre, info in EMPLEOS.items():
            embed.add_field(
                name=f"{nombre.title()}",
                value=(
                    f"• Recompensa: {info['salario_min']} - {info['salario_max']} {COIN} + {info.get('xp_ganada', 0)} XP\n"
                    f"• Dificultad: {info['dificultad']}\n"
                    f"• XP requerida: {info['xp_requisito']}\n"
                    f"• Jornada: {info['duracion_horas']} horas"
                ),
                inline=False,
            )
        embed.set_thumbnail(url="https://pub-a09b3609b6b34dfab5c7aa7742cd1a8a.r2.dev/Purple%20jack%20Harcode/EmpleosPJ.png")
        embed.set_footer(text="Aplica con: !aplicar <empleo>")
        await ctx.send(embed=embed)

    @commands.command(name="aplicar")
    async def aplicar(self, ctx, empleo: str = None):
        if not empleo:
            return await ctx.send("❌ Usa: `!aplicar <empleo>`")
        empleo = normalizar_empleo(empleo)
        if empleo not in EMPLEOS:
            return await ctx.send("⭕ Empleo **Invalido** Consulta **!empleos.**")

        data = await get_empleo_user(ctx.author.id)
        bypass = _es_coordinador(ctx.author)
        cooldown_until = data.get("cooldown_renuncia", 0) if data else 0
        if cooldown_until and time.time() < cooldown_until and not bypass:
            return await ctx.send(f"⏳ Debes esperar {format_relative_time(cooldown_until)} para aplicar a un nuevo empleo.")

        if data and normalizar_empleo(data.get("empleo_actual") or "") == empleo:
            return await ctx.send(f"❌ Ya trabajas como **{data['empleo_actual'].title()}**. Elige un empleo distinto.")

        info = EMPLEOS[empleo]
        if data and data.get("exp_laboral", 0) < info["xp_requisito"] and not bypass:
            return await ctx.send(f"❌ {ctx.author.mention} necesitas **{info['xp_requisito']}** puntos de Experiencia Laboral para aplicar a **{empleo.title()}**.")

        embed = discord.Embed(
            title=f"¿Deseas aplicar como {empleo.title()}?",
            description=(
                f"Requiere **{info['xp_requisito']} XP Laboral**\n"
                f"Pago estimado: **{info['salario_min']} - {info['salario_max']} {COIN}** por jornada de 3 horas"
            ),
            color=discord.Color.blurple(),
        )
        await ctx.send(embed=embed, view=ConfirmarEmpleoView(self.bot, ctx.author.id, empleo))

    @commands.command(name="renunciar")
    async def renunciar(self, ctx):
        _, mensaje = await renunciar_empleo(ctx.author)
        await ctx.send(mensaje)

    @commands.command(name="exp")
    async def exp(self, ctx):
        data = await get_empleo_user(ctx.author.id, force_refresh=True)
        await ctx.send(embed=build_exp_embed(ctx.author, data))

    @commands.command(name="oficina")
    async def oficina(self, ctx):
        data = await get_empleo_user(ctx.author.id, force_refresh=True)
        if not _puede_acceder_oficina(data, ctx.author):
            return await ctx.reply(
                "Para acceder a la oficina necesitas tener 30 de exp laboral, consulta **!exp** continua trabajando.",
                mention_author=False,
            )
        await ctx.reply(view=AbrirOficinaView(ctx.author.id), mention_author=False)

    @commands.command(name="trabajar")
    async def trabajar(self, ctx):
        data = await get_empleo_user(ctx.author.id)
        if not data or not data.get("empleo_actual"):
            return await ctx.send(f"❌ {ctx.author.mention} No tienes un empleo activo. Consulta **!empleos.**")

        empleo = normalizar_empleo(data["empleo_actual"])
        if empleo in EMPLEOS_MAESTRIA:
            info = EMPLEOS_MAESTRIA[empleo]
            if not info.get("desarrollado", False):
                return await ctx.send("🚧 Trabajo Pendiente de desarrollo.")
        elif empleo in EMPLEOS:
            info = EMPLEOS[empleo]
        else:
            logger.error("Empleo desconocido en DB para %s: %r", ctx.author.id, data["empleo_actual"])
            return await ctx.send("❌ Tu empleo actual no es válido. Contacta a un administrador.")

        now = time.time()
        bypass = _es_coordinador(ctx.author)

        if not bypass:
            if data.get("ultimo_trabajo", 0) and (now - data["ultimo_trabajo"]) < info["duracion_horas"] * 3600:
                disponible_en = int(data["ultimo_trabajo"] + info["duracion_horas"] * 3600)
                return await ctx.reply(f"⏳ Podrás volver a trabajar <t:{disponible_en}:R>")

            # ── Despido por inactividad (solo si el sistema está activo) ──
            if _despidos_config["activo"] and data.get("ultimo_trabajo", 0) > 0:
                if (now - data["ultimo_trabajo"]) >= 86400:
                    await despedir_por_inactividad(ctx.author.id, data)
                    return await ctx.send(
                        f"⚠️ {ctx.author.mention} Has sido **despedido** por inactividad "
                        f"(más de 24h sin trabajar). Usa **!aplicar** para conseguir un nuevo empleo."
                    )

        timeout = (
            JORNADA_PERSISTENTE_SEGUNDOS
            if empleo == "piromano"
            else (60 if empleo == "chantajista" else 180)
        )
        jornada = await crear_jornada(
            ctx.author.id,
            empleo,
            f"trabajar:message:{ctx.message.id}",
            guild_id=ctx.guild.id if ctx.guild else None,
            channel_id=ctx.channel.id,
            timeout=timeout,
            cooldown_seconds=info["duracion_horas"] * 3600,
            bypass_cooldown=bypass,
        )
        if not jornada["ok"]:
            if jornada["reason"] == "duplicate_request":
                return
            if jornada["reason"] == "cooldown":
                return await ctx.reply(
                    f"⏳ Podrás volver a trabajar <t:{int(jornada['expires_at'])}:R>"
                )
            if jornada["reason"] == "employment_changed":
                _EMPLEOS_CACHE.pop(ctx.author.id, None)
                return await ctx.reply(
                    "❌ Tu empleo cambió mientras se iniciaba la jornada. Inténtalo nuevamente.",
                    mention_author=False,
                )
            return await ctx.reply(
                "⌛ Ya tienes una jornada laboral activa. Finalízala antes de comenzar otra.",
                mention_author=False,
            )

        session_id = jornada["session_id"]
        if empleo == "limpiador":
            view = LimpiadorView(self.bot, ctx.author, info, session_id)
        elif empleo == "ingeniero":
            view = IngenieroView(self.bot, ctx.author, info, session_id)
        elif empleo == "plomero":
            view = PlomeroView(self.bot, ctx.author, info, session_id)
        elif empleo == "chantajista":
            view = ChantajistaView(self.bot, ctx.author, info, session_id)
        elif empleo == "cazador":
            view = CazadorView(self.bot, ctx.author, info, session_id)
        elif empleo == "piromano":
            view = PiromanoView(self.bot, ctx.author, info, session_id)
        else:
            await cancelar_jornada_segura(session_id)
            return await ctx.send("🚧 Trabajo Pendiente de desarrollo.")

        try:
            if empleo == "chantajista":
                msg = await ctx.reply(embed=view.build_embed(), view=view, mention_author=False)
            else:
                msg = await ctx.send(embed=view.build_embed(), view=view)
        except Exception:
            await cancelar_jornada_segura(session_id)
            raise
        view.message = msg
        try:
            await asociar_mensaje_jornada(session_id, msg.id)
        except Exception:
            await cancelar_jornada_segura(session_id)
            try:
                await msg.edit(
                    content="❌ No se pudo registrar la jornada. Inténtalo nuevamente.",
                    embed=None,
                    view=None,
                )
            except Exception:
                pass
            raise
        if empleo == "cazador":
            view.iniciar_preparacion()
        elif empleo == "piromano":
            view.iniciar_preparacion()


class JornadaView(ui.View):
    async def interaction_check(self, interaction: Interaction) -> bool:
        if interaction.user.id != self.author.id:
            await interaction.response.send_message(
                "❌ Este tablero no es tuyo.",
                ephemeral=True,
            )
            return False
        try:
            await interaction.response.defer()
        except discord.NotFound:
            return False

        renovada = await _renovar_jornada_tolerante(
            self.session_id,
            int(getattr(self, "session_timeout", self.timeout or 180)),
        )
        if renovada is False:
            await _responder_jornada(
                interaction,
                "⌛ Esta jornada ya fue cerrada. Consulta **!trabajar** nuevamente.",
            )
            return False
        return True


class CazadorView(JornadaView):
    TRIPULANTE = discord.PartialEmoji.from_str("<:t_justiceiro:1410760055090970805>")
    SUS = (
        discord.PartialEmoji.from_str("<:i_Marionetista:1403884981108871281>"),
        discord.PartialEmoji.from_str("<:i_apostador:1410272686004768789>"),
        discord.PartialEmoji.from_str("<:n_JokerCoin:1287140438276571146>"),
    )
    REVELAR_SEGUNDOS = 3
    MEZCLAR_SEGUNDOS = 3

    def __init__(self, bot, author, info, session_id: str):
        super().__init__(timeout=180)
        self.bot = bot
        self.author = author
        self.info = info
        self.session_id = session_id
        self.message = None
        self.fase = "revelando"
        self.terminado = False
        self.bloqueado = True
        self.casilla_elegida = None
        self.resultado_exitoso = None
        self.resultado_mensaje = None
        self.resultado_color = discord.Color.dark_grey()
        self._interaction_lock = asyncio.Lock()
        self._preparacion_task = None

        guild = getattr(author, "guild", None)
        self.tripulante_emoji = (
            guild.get_emoji(self.TRIPULANTE.id) if guild else None
        ) or "🧑‍🚀"
        sus_fallback = ("🎭", "🎰", "🪙")
        self.sus_emojis = [
            (guild.get_emoji(emoji.id) if guild else None) or fallback
            for emoji, fallback in zip(self.SUS, sus_fallback)
        ]
        self.tablero = [self.tripulante_emoji] * 7 + self.sus_emojis.copy()
        random.shuffle(self.tablero)
        self._build_buttons()

    def _build_buttons(self):
        self.clear_items()
        for idx, emoji in enumerate(self.tablero):
            if self.fase in {"revelando", "resultado"}:
                if self.fase == "resultado" and idx == self.casilla_elegida:
                    estilo = (
                        ButtonStyle.success
                        if self.resultado_exitoso
                        else ButtonStyle.danger
                    )
                else:
                    estilo = ButtonStyle.secondary
                button = ui.Button(
                    emoji=emoji,
                    style=estilo,
                    row=idx // 5,
                    custom_id=f"caz_{self.session_id}_{idx}",
                    disabled=True,
                )
            else:
                button = ui.Button(
                    label="⬜",
                    style=ButtonStyle.secondary,
                    row=idx // 5,
                    custom_id=f"caz_{self.session_id}_{idx}",
                    disabled=self.terminado or self.bloqueado,
                )
            button.callback = self._make_callback(idx)
            self.add_item(button)

    def build_embed(self):
        estado = {
            "revelando": "\n\n👁️ Preparate para llevarte a uno.",
            "preparando": "\n\n⏳ **Preparando Habilidad...**",
            "activo": "\n\n🎯 Elige una casilla para atacar.",
            "resultado": f"\n\n{self.author.mention} {self.resultado_mensaje or ''}",
        }.get(self.fase, "")
        embed = discord.Embed(
            title=f"Jornada Cazador - {self.author.nick or self.author.display_name}",
            description=(
                "Usa tu habilidad de Cazador para llevarte contigo a uno de los SUS, "
                "Perderas si atacas a un Tripulante."
                f"{estado}"
            ),
            color=self.resultado_color if self.fase == "resultado" else discord.Color.dark_grey(),
        )
        embed.set_footer(text="Las casillas se mezclaran aleatoriamente...")
        return embed

    def _mensaje_resultado(self, exito: bool, *, por_timeout: bool = False) -> str:
        if exito:
            return (
                "Buena jugada has acertado llevandote a uno de los SUS, "
                f"Ganas {self.info['salario_max']} {COIN} + "
                f"{self.info['xp_ganada']} Exp Laboral."
            )
        if por_timeout:
            return "Tiempo agotado. La jornada de Cazador finalizó sin recompensa."
        return (
            "Has fallado llevandote a un Tripulante, "
            f"pierdes {abs(self.info['penalizacion'])} {COIN}."
        )

    def iniciar_preparacion(self):
        if self._preparacion_task is None:
            self._preparacion_task = asyncio.create_task(
                self._ejecutar_preparacion(),
                name=f"cazador-preparacion-{self.session_id}",
            )

    async def _actualizar_preparacion(self):
        if self.message is not None:
            await self.message.edit(embed=self.build_embed(), view=self)

    async def _ejecutar_preparacion(self):
        try:
            await asyncio.sleep(self.REVELAR_SEGUNDOS)
            async with self._interaction_lock:
                if self.terminado:
                    return
                self.fase = "preparando"
                self.bloqueado = True
                random.shuffle(self.tablero)
                self._build_buttons()
                await self._actualizar_preparacion()

            await asyncio.sleep(self.MEZCLAR_SEGUNDOS)
            async with self._interaction_lock:
                if self.terminado:
                    return
                self.fase = "activo"
                self.bloqueado = False
                self._build_buttons()
                await self._actualizar_preparacion()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _log_trabajar_error("Cazador", error, self.author.name, "_ejecutar_preparacion")
            await _cerrar_tablero_con_error(self, "Cazador")

    def _make_callback(self, idx: int):
        async def callback(interaction: Interaction):
            if interaction.user.id != self.author.id:
                return await _responder_jornada(interaction, "❌ Este tablero no es tuyo.")
            if self._interaction_lock.locked():
                return await _responder_jornada(
                    interaction,
                    "⏳ El tablero está actualizando la jugada anterior.",
                )

            async with self._interaction_lock:
                if self.terminado:
                    return await _responder_jornada(
                        interaction,
                        "⌛ Esta jornada ya finalizó.",
                    )
                if self.bloqueado or self.fase != "activo":
                    return await _responder_jornada(
                        interaction,
                        "⏳ La habilidad de Cazador se está preparando.",
                    )

                self.terminado = True
                self.bloqueado = True
                self.stop()
                exito = self.tablero[idx] in self.sus_emojis
                self.fase = "resultado"
                self.casilla_elegida = idx
                self.resultado_exitoso = exito
                self.resultado_mensaje = self._mensaje_resultado(exito)
                self.resultado_color = (
                    discord.Color.green() if exito else discord.Color.red()
                )
                self._build_buttons()
                await _editar_tablero_seguro(
                    interaction,
                    self,
                    embed=self.build_embed(),
                )

            await self._finalizar(exito, interaction=interaction)

        return callback

    async def _finalizar(self, exito: bool, *, interaction=None, por_timeout=False):
        liquidada = False
        try:
            if exito:
                pago = self.info["salario_max"]
                xp_ganada = self.info["xp_ganada"]
                mensaje = self._mensaje_resultado(True)
                settlement = await finalizar_jornada_atomica(
                    self.session_id,
                    self.author.id,
                    "cazador",
                    True,
                    pago,
                    mensaje,
                    xp_ganada=xp_ganada,
                )
                color = discord.Color.green()
            else:
                pago = self.info["penalizacion"] if not por_timeout else 0
                mensaje = self._mensaje_resultado(False, por_timeout=por_timeout)
                settlement = await finalizar_jornada_atomica(
                    self.session_id,
                    self.author.id,
                    "cazador",
                    False,
                    pago,
                    mensaje,
                    penalizacion_desde_balance=not por_timeout,
                )
                color = discord.Color.red()

            if not settlement["ok"]:
                raise RuntimeError(settlement.get("reason", "settlement_failed"))
            liquidada = True

            if exito:
                bonus = settlement["bonus"]
                if bonus["coins"] > 0:
                    mensaje += (
                        f"\n🌟 **¡Racha de 5!** Bono: **+{bonus['coins']}** "
                        f"{COIN} y **+{bonus['xp']} XP Laboral**"
                    )
            self.fase = "resultado"
            self.resultado_exitoso = exito
            self.resultado_mensaje = mensaje
            self.resultado_color = color
            self._build_buttons()
            embed = self.build_embed()
            if interaction is not None:
                await _editar_tablero_seguro(
                    interaction,
                    self,
                    embed=embed,
                )
            elif self.message is not None:
                await self.message.edit(embed=embed, view=self)
        except Exception as error:
            _log_trabajar_error("Cazador", error, self.author.name, "_finalizar")
            await _cerrar_tablero_con_error(
                self,
                "Cazador",
                interaction=interaction,
                liquidada=liquidada,
            )
        finally:
            _programar_eliminacion(self)

    async def on_timeout(self):
        async with self._interaction_lock:
            if self.terminado:
                return
            self.terminado = True
            self.bloqueado = True
            self.stop()
            self.fase = "resultado"
            self.resultado_exitoso = False
            self.resultado_mensaje = self._mensaje_resultado(False, por_timeout=True)
            self.resultado_color = discord.Color.red()
            self._build_buttons()
        await _mostrar_tablero_bloqueado(self)
        await self._finalizar(False, por_timeout=True)

    async def on_error(self, interaction: Interaction, error: Exception, item):
        self.terminado = True
        self.bloqueado = True
        self.stop()
        await cancelar_jornada_segura(self.session_id, "error")
        _log_trabajar_error("Cazador", error, interaction.user.name, f"on_error — Item: {item}")
        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except (discord.HTTPException, discord.NotFound):
                pass
        await _notificar_error_interaccion(interaction)


class PiromanoView(JornadaView):
    REVELAR_SEGUNDOS = 4
    MEZCLAR_SEGUNDOS = 2
    GASOLINAS_OBJETIVO = 10

    def __init__(self, bot, author, info, session_id: str):
        super().__init__(timeout=None)
        self.bot = bot
        self.author = author
        self.info = info
        self.session_id = session_id
        self.session_timeout = JORNADA_PERSISTENTE_SEGUNDOS
        self.message = None
        self.fase = "revelando"
        self.terminado = False
        self.bloqueado = True
        self.reveladas_gasolina = set()
        self.gasolinas_encontradas = 0
        self.casilla_final = None
        self.resultado_exitoso = None
        self.resultado_mensaje = None
        self._interaction_lock = asyncio.Lock()
        self._preparacion_task = None

        self.tablero_gasolina = ["⛽"] * self.GASOLINAS_OBJETIVO + ["💧"] * 5
        random.shuffle(self.tablero_gasolina)
        self.tablero_fuego = ["🔥"] * 6 + ["💧"] * 3
        random.shuffle(self.tablero_fuego)
        self._build_buttons()

    async def interaction_check(self, interaction: Interaction) -> bool:
        """Valida al propietario sin interponer llamadas antes del cambio visual."""
        if interaction.user.id != self.author.id:
            await interaction.response.send_message(
                "❌ Este tablero no es tuyo.",
                ephemeral=True,
            )
            return False
        return True

    def _build_buttons(self):
        self.clear_items()

        if self.fase in {"revelando", "mezclando", "gasolina"}:
            for idx, emoji in enumerate(self.tablero_gasolina):
                revelada = idx in self.reveladas_gasolina
                if self.fase == "revelando":
                    button = ui.Button(
                        emoji=emoji,
                        style=ButtonStyle.secondary,
                        row=idx // 5,
                        disabled=True,
                        custom_id=f"piro_gas_{self.session_id}_{idx}",
                    )
                elif self.fase == "gasolina" and revelada:
                    button = ui.Button(
                        emoji=emoji,
                        style=(
                            ButtonStyle.success if emoji == "⛽" else ButtonStyle.danger
                        ),
                        row=idx // 5,
                        disabled=True,
                        custom_id=f"piro_gas_{self.session_id}_{idx}",
                    )
                else:
                    button = ui.Button(
                        label="⬜",
                        style=ButtonStyle.secondary,
                        row=idx // 5,
                        disabled=self.fase != "gasolina" or self.bloqueado,
                        custom_id=f"piro_gas_{self.session_id}_{idx}",
                    )
                button.callback = self._make_callback(idx)
                self.add_item(button)
            return

        for idx, emoji in enumerate(self.tablero_fuego):
            if self.fase == "resultado":
                estilo = ButtonStyle.secondary
                if idx == self.casilla_final:
                    estilo = (
                        ButtonStyle.success
                        if self.resultado_exitoso
                        else ButtonStyle.danger
                    )
                button = ui.Button(
                    emoji=emoji,
                    style=estilo,
                    row=idx // 3,
                    disabled=True,
                    custom_id=f"piro_fuego_{self.session_id}_{idx}",
                )
            else:
                button = ui.Button(
                    label="⬜",
                    style=ButtonStyle.secondary,
                    row=idx // 3,
                    disabled=self.bloqueado or self.terminado,
                    custom_id=f"piro_fuego_{self.session_id}_{idx}",
                )
            button.callback = self._make_callback(idx)
            self.add_item(button)

    def build_embed(self):
        if self.fase == "revelando":
            descripcion = "Encuentra todos los botes de gasolina..."
        elif self.fase == "mezclando":
            descripcion = (
                "Encuentra todos los botes de gasolina...\n\n"
                "⏳ **Mezclando las casillas...**"
            )
        elif self.fase == "gasolina":
            descripcion = (
                "Encuentra todos los botes de gasolina...\n\n"
                f"⛽ Gasolina encontrada: **{self.gasolinas_encontradas}/"
                f"{self.GASOLINAS_OBJETIVO}**"
            )
        elif self.fase == "fuego":
            descripcion = "Ahora encuentra el 🔥 para Ganar, evita el 💧 o Perderas..."
        else:
            descripcion = self.resultado_mensaje or "Jornada finalizada."

        color = discord.Color.orange()
        if self.fase == "resultado":
            color = (
                discord.Color.green()
                if self.resultado_exitoso
                else discord.Color.red()
            )
        embed = discord.Embed(
            title=f"Jornada Píromano - {self.author.nick or self.author.display_name}",
            description=descripcion,
            color=color,
        )
        footer = {
            "revelando": "Las casillas se ocultarán en 4 segundos...",
            "mezclando": "Las casillas se están mezclando...",
            "gasolina": "Las casillas descubiertas permanecerán abiertas.",
            "fuego": "Solo tienes un intento para encontrar el fuego.",
            "resultado": "Jornada Píromano finalizada.",
        }.get(self.fase)
        if footer:
            embed.set_footer(text=footer)
        return embed

    def iniciar_preparacion(self):
        if self._preparacion_task is None:
            self._preparacion_task = asyncio.create_task(
                self._ejecutar_preparacion(),
                name=f"piromano-preparacion-{self.session_id}",
            )

    async def _actualizar_tablero(self):
        if self.message is not None:
            await self.message.edit(embed=self.build_embed(), view=self)

    async def _editar_interaccion_inmediata(self, interaction: Interaction):
        embed = self.build_embed()
        if not interaction.response.is_done():
            try:
                await interaction.response.edit_message(embed=embed, view=self)
                return
            except (discord.HTTPException, discord.NotFound):
                pass
        await _editar_tablero_seguro(interaction, self, embed=embed)

    async def _ejecutar_preparacion(self):
        try:
            await asyncio.sleep(self.REVELAR_SEGUNDOS)
            async with self._interaction_lock:
                if self.terminado:
                    return
                self.fase = "mezclando"
                self.bloqueado = True
                random.shuffle(self.tablero_gasolina)
                self._build_buttons()
                await self._actualizar_tablero()

            await asyncio.sleep(self.MEZCLAR_SEGUNDOS)
            async with self._interaction_lock:
                if self.terminado:
                    return
                self.fase = "gasolina"
                self.bloqueado = False
                self._build_buttons()
                await self._actualizar_tablero()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _log_trabajar_error(
                "Píromano",
                error,
                self.author.name,
                "_ejecutar_preparacion",
            )
            await _cerrar_tablero_con_error(self, "Píromano")

    def _make_callback(self, idx: int):
        async def callback(interaction: Interaction):
            if interaction.user.id != self.author.id:
                return await _responder_jornada(interaction, "❌ Este tablero no es tuyo.")
            if self._interaction_lock.locked():
                return await _responder_jornada(
                    interaction,
                    "⏳ El tablero está actualizando la jugada anterior.",
                )

            resultado = None
            async with self._interaction_lock:
                if self.terminado:
                    return await _responder_jornada(
                        interaction,
                        "⌛ Esta jornada ya finalizó.",
                    )
                if self.bloqueado or self.fase not in {"gasolina", "fuego"}:
                    return await _responder_jornada(
                        interaction,
                        "⏳ La jornada de Píromano se está preparando.",
                    )

                if self.fase == "gasolina":
                    if idx in self.reveladas_gasolina:
                        return await _responder_jornada(
                            interaction,
                            "✅ Esa casilla ya está abierta.",
                        )
                    self.reveladas_gasolina.add(idx)
                    if self.tablero_gasolina[idx] == "⛽":
                        self.gasolinas_encontradas += 1

                    if self.gasolinas_encontradas >= self.GASOLINAS_OBJETIVO:
                        self.fase = "fuego"
                        self.bloqueado = False
                    self._build_buttons()
                    await self._editar_interaccion_inmediata(interaction)
                    return

                self.terminado = True
                self.bloqueado = True
                self.casilla_final = idx
                self.resultado_exitoso = self.tablero_fuego[idx] == "🔥"
                resultado = self.resultado_exitoso
                self.stop()
                self.fase = "resultado"
                self.resultado_mensaje = (
                    "🔥 Encontraste el fuego. Procesando recompensa..."
                    if resultado
                    else "💧 Encontraste agua. Procesando penalización..."
                )
                self._build_buttons()
                await self._editar_interaccion_inmediata(interaction)

            await self._finalizar(resultado, interaction)

        return callback

    async def _finalizar(self, exito: bool, interaction: Interaction):
        liquidada = False
        try:
            if exito:
                pago = self.info["salario_max"]
                xp_cambio = self.info["xp_ganada"]
                mensaje = (
                    "Felicidades eres un estupendo Píromano, "
                    f"Ganas {pago} {COIN} + {xp_cambio} Exp Laboral"
                )
            else:
                pago = self.info["penalizacion"]
                xp_cambio = self.info["xp_penalizacion"]
                mensaje = (
                    "El agua no es compatible con el fuego, "
                    f"pierdes {abs(pago)} {COIN} y {xp_cambio} Exp Laboral"
                )

            settlement = await finalizar_jornada_atomica(
                self.session_id,
                self.author.id,
                "piromano",
                exito,
                pago,
                mensaje,
                xp_ganada=xp_cambio,
                penalizacion_desde_balance=not exito,
            )
            if not settlement["ok"]:
                raise RuntimeError(settlement.get("reason", "settlement_failed"))
            liquidada = True

            if exito:
                bonus = settlement["bonus"]
                if bonus["coins"] > 0:
                    mensaje += (
                        f"\n🌟 **¡Racha de 5!** Bono: **+{bonus['coins']}** "
                        f"{COIN} y **+{bonus['xp']} XP Laboral**"
                    )
            self.fase = "resultado"
            self.resultado_mensaje = f"{self.author.mention} {mensaje}"
            self._build_buttons()
            await _editar_tablero_seguro(
                interaction,
                self,
                embed=self.build_embed(),
            )
        except Exception as error:
            _log_trabajar_error("Píromano", error, self.author.name, "_finalizar")
            await _cerrar_tablero_con_error(
                self,
                "Píromano",
                interaction=interaction,
                liquidada=liquidada,
            )

    async def on_error(self, interaction: Interaction, error: Exception, item):
        self.terminado = True
        self.bloqueado = True
        self.stop()
        await cancelar_jornada_segura(self.session_id, "error")
        _log_trabajar_error(
            "Píromano",
            error,
            interaction.user.name,
            f"on_error — Item: {item}",
        )
        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except (discord.HTTPException, discord.NotFound):
                pass
        await _notificar_error_interaccion(interaction)


class ChantajistaView(JornadaView):
    TRIPULANTE = discord.PartialEmoji.from_str("<:t_Enginer:1288581004914589756>")
    INCORRECTO = discord.PartialEmoji.from_str("<:n_JokerCoin:1287140438276571146>")
    MAX_VIDAS = 3

    def __init__(self, bot, author, info, session_id: str):
        super().__init__(timeout=60)
        self.bot = bot
        self.author = author
        self.info = info
        self.session_id = session_id
        self.message = None
        self.ganador = random.randrange(6)
        self.vidas = self.MAX_VIDAS
        self.descartadas = set()
        self.incorrecta_visible = None
        self.mostrar_ganador = False
        self.bloqueado = False
        self.terminado = False
        self._cleanup_programado = False
        self._interaction_lock = asyncio.Lock()
        guild = getattr(author, "guild", None)
        self.tripulante_emoji = (
            guild.get_emoji(self.TRIPULANTE.id) if guild else None
        ) or "🧑‍🚀"
        self.incorrecto_emoji = (
            guild.get_emoji(self.INCORRECTO.id) if guild else None
        ) or "❌"
        self._build_buttons()

    def _build_buttons(self):
        self.clear_items()
        for idx in range(6):
            es_ganador_visible = idx == self.ganador and self.mostrar_ganador
            es_incorrecta_visible = idx == self.incorrecta_visible

            if es_ganador_visible:
                button = ui.Button(
                    emoji=self.tripulante_emoji,
                    style=ButtonStyle.success,
                    row=idx // 3,
                    custom_id=f"chant_{self.session_id}_{idx}",
                    disabled=True,
                )
            elif es_incorrecta_visible:
                button = ui.Button(
                    emoji=self.incorrecto_emoji,
                    style=ButtonStyle.danger,
                    row=idx // 3,
                    custom_id=f"chant_{self.session_id}_{idx}",
                    disabled=True,
                )
            else:
                button = ui.Button(
                    label="⬜",
                    style=ButtonStyle.secondary,
                    row=idx // 3,
                    custom_id=f"chant_{self.session_id}_{idx}",
                    disabled=self.terminado or self.bloqueado or idx in self.descartadas,
                )

            button.callback = self._make_callback(idx)
            self.add_item(button)

    def build_embed(self):
        corazones = "❤️" * self.vidas + "🖤" * (self.MAX_VIDAS - self.vidas)
        embed = discord.Embed(
            title="Jornada Chantajista",
            description="Adivina en cual casilla se encuentra el Tripulante",
            color=discord.Color.purple(),
        )
        embed.add_field(name="Vidas restantes", value=corazones, inline=False)
        embed.set_footer(text="Tienes 60 segundos para encontrar al Tripulante.")
        return embed

    def _make_callback(self, idx):
        async def callback(interaction: Interaction):
            if interaction.user.id != self.author.id:
                return await _responder_jornada(
                    interaction,
                    "❌ Este tablero no es tuyo.",
                )
            if self._interaction_lock.locked():
                return await _responder_jornada(
                    interaction,
                    "⏳ El tablero está actualizando la jugada anterior.",
                )

            resultado = None
            async with self._interaction_lock:
                if self.terminado:
                    return await _responder_jornada(
                        interaction,
                        "⌛ Esta jornada ya finalizó.",
                    )
                if self.bloqueado or idx in self.descartadas:
                    return await _responder_jornada(
                        interaction,
                        "⏳ Esa casilla no está disponible.",
                    )

                if idx == self.ganador:
                    self.terminado = True
                    self.bloqueado = True
                    self.mostrar_ganador = True
                    self.stop()
                    self._build_buttons()
                    await _editar_tablero_seguro(
                        interaction,
                        self,
                        embed=self.build_embed(),
                    )
                    resultado = True
                else:
                    self.vidas -= 1
                    self.descartadas.add(idx)
                    self.incorrecta_visible = idx
                    self.bloqueado = True
                    self._build_buttons()
                    try:
                        await _editar_tablero_seguro(
                            interaction,
                            self,
                            embed=self.build_embed(),
                        )
                        await asyncio.sleep(1)
                    finally:
                        self.incorrecta_visible = None
                        if self.vidas <= 0:
                            self.terminado = True
                            self.mostrar_ganador = True
                            self.stop()
                            resultado = False
                        else:
                            self.bloqueado = False
                        self._build_buttons()

                    await _editar_tablero_seguro(
                        interaction,
                        self,
                        embed=self.build_embed(),
                    )

            if resultado is not None:
                await self._finalizar(resultado, interaction=interaction)

        return callback

    async def _finalizar(self, exito, interaction=None, por_timeout=False):
        liquidada = False
        try:
            if exito:
                pago = self.info["salario_max"]
                xp_ganada = self.info["xp_ganada"]
                mensaje = (
                    f"Felicidades has encontrado al Tripulante Ganas "
                    f"{pago} {COIN} + {xp_ganada} Exp Laboral"
                )
                settlement = await finalizar_jornada_atomica(
                    self.session_id,
                    self.author.id,
                    "chantajista",
                    True,
                    pago,
                    mensaje,
                    xp_ganada=xp_ganada,
                )
                if not settlement["ok"]:
                    raise RuntimeError(
                        f"Jornada ya liquidada: {settlement.get('reason')}"
                    )
                liquidada = True
                bonus = settlement["bonus"]
                if bonus["coins"] > 0:
                    mensaje += (
                        f"\n🌟 **¡Racha de 5!** Bono: **+{bonus['coins']}** "
                        f"{COIN} y **+{bonus['xp']} XP Laboral**"
                    )
                embed = discord.Embed(
                    title="Jornada Chantajista",
                    description=f"{self.author.mention} {mensaje}",
                    color=discord.Color.green(),
                )
            else:
                if por_timeout:
                    motivo = "No encontraste al Tripulante antes de finalizar el tiempo."
                else:
                    motivo = "Perdiste tus 3 vidas sin encontrar al Tripulante."
                settlement = await finalizar_jornada_atomica(
                    self.session_id,
                    self.author.id,
                    "chantajista",
                    False,
                    0,
                    motivo,
                )
                if not settlement["ok"]:
                    raise RuntimeError(
                        f"Jornada ya liquidada: {settlement.get('reason')}"
                    )
                liquidada = True
                embed = discord.Embed(
                    title="Jornada Chantajista - Derrota",
                    description=(
                        f"{self.author.mention} {motivo}\n"
                        f"El Tripulante estaba en la casilla **{self.ganador + 1}** "
                        f"{self.tripulante_emoji}"
                    ),
                    color=discord.Color.red(),
                )

            if interaction is not None:
                await _editar_tablero_seguro(
                    interaction,
                    self,
                    embed=embed,
                    remove_view=True,
                )
            elif self.message is not None:
                await self.message.edit(embed=embed, view=None)
        except Exception as error:
            _log_trabajar_error("Chantajista", error, self.author.name, "_finalizar")
            await _cerrar_tablero_con_error(
                self,
                "Chantajista",
                interaction=interaction,
                liquidada=liquidada,
            )
        finally:
            if not self._cleanup_programado:
                self._cleanup_programado = True
                asyncio.create_task(self._cleanup())

    async def on_timeout(self):
        async with self._interaction_lock:
            if self.terminado:
                return
            self.terminado = True
            self.bloqueado = True
            self.mostrar_ganador = True
            self._build_buttons()
        await _mostrar_tablero_bloqueado(self)
        logger.info("[TRABAJAR/CHANTAJISTA] Jornada vencida — Usuario: %s", self.author.name)
        await self._finalizar(False, por_timeout=True)

    async def on_error(self, interaction: Interaction, error: Exception, item):
        self.terminado = True
        self.bloqueado = True
        self.stop()
        await cancelar_jornada_segura(self.session_id, "error")
        _log_trabajar_error(
            "Chantajista",
            error,
            interaction.user.name,
            f"on_error — Item: {item}",
        )
        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except (discord.HTTPException, discord.NotFound):
                pass
        await _notificar_error_interaccion(interaction)
        if not self._cleanup_programado:
            self._cleanup_programado = True
            asyncio.create_task(self._cleanup())

    async def _cleanup(self):
        await asyncio.sleep(180)
        try:
            if self.message:
                await self.message.delete()
        except (discord.HTTPException, discord.NotFound):
            pass


class LimpiadorView(JornadaView):
    def __init__(self, bot, author, info, session_id: str):
        super().__init__(timeout=180)
        self.bot = bot
        self.author = author
        self.info = info
        self.session_id = session_id
        self.start_time = time.time()
        self.pago_base = random.randint(
            self.info["salario_min"],
            self.info["salario_max"],
        )
        self.revelados = [False] * 16
        self.basura = 3
        self.celdas_erroneas = set()
        self.message = None
        self.puntos = 0
        self.terminado = False
        self._interaction_lock = asyncio.Lock()
        self._generar_tablero()
        self._build_buttons()

    def _generar_tablero(self):
        # Mantiene exactamente 3 celdas de reciclaje, pero las mezcla al azar en cada invocación.
        emojis = ["🗑️"] * 3 + ["🧹", "🧺", "🧼", "🪴", "📚", "🪟", "📦", "🧻", "🧽", "🫧", "🪣", "🖥️", "🪙"]
        random.shuffle(emojis)
        self.tablero = emojis

    def _build_buttons(self):
        self.clear_items()
        for i, emoji in enumerate(self.tablero):
            row = i // 4
            if self.revelados[i]:
                style = ButtonStyle.success if emoji == "🗑️" else ButtonStyle.danger
                btn = ui.Button(
                    label=emoji,
                    style=style,
                    row=row,
                    custom_id=f"limp_{i}",
                    disabled=True,
                )
            else:
                btn = ui.Button(
                    label="⬜",
                    style=ButtonStyle.secondary,
                    row=row,
                    custom_id=f"limp_{i}",
                    disabled=self.terminado,
                )
            btn.custom_id = f"limp_{self.session_id}_{i}"
            btn.callback = self._make_callback(i)
            self.add_item(btn)

    def _make_callback(self, idx):
        async def callback(interaction: Interaction):
            if interaction.user.id != self.author.id:
                return await _responder_jornada(interaction, "❌ Este tablero no es tuyo.")
            if self._interaction_lock.locked():
                return await _responder_jornada(
                    interaction,
                    "⏳ El tablero está actualizando la jugada anterior.",
                )

            terminar_ahora = False
            async with self._interaction_lock:
                if self.terminado:
                    return await _responder_jornada(interaction, "⌛ Esta jornada ya finalizó.")
                if self.revelados[idx]:
                    return await _responder_jornada(interaction, "✅ Esa casilla ya está descubierta.")
                self.revelados[idx] = True
                self.puntos += 1
                if self.tablero[idx] == "🗑️":
                    self.basura -= 1
                else:
                    self.celdas_erroneas.add(idx)
                terminar_ahora = self.basura == 0
                if terminar_ahora:
                    self.terminado = True
                    self.stop()
                self._build_buttons()
                await _editar_tablero_seguro(
                    interaction,
                    self,
                    embed=self.build_embed(),
                )
            if terminar_ahora:
                await self._terminar(interaction, exito=True)
        return callback

    def build_embed(self):
        tiempo = int(time.time() - self.start_time)
        ratio = 1.0 + max(0.0, 45 - tiempo) / 45.0 * 0.35
        pago_actual = min(int(self.pago_base * ratio), self.info['salario_max'])

        embed = discord.Embed(title=f"🧹 Jornada Limpiador - {self.author.nick or self.author.display_name}", color=discord.Color.yellow())
        embed.add_field(name="Objetivo", value="Descubre los 3 símbolos de reciclaje para completar la tarea.", inline=False)
        embed.add_field(name="Símbolos de 🗑️ restantes", value=str(self.basura), inline=True)
        embed.add_field(name="Pago estimado actual", value=f"{pago_actual} {COIN}", inline=False)
        embed.set_thumbnail(url="https://pub-a09b3609b6b34dfab5c7aa7742cd1a8a.r2.dev/Purple%20jack%20Harcode/LimpiadorPJ.png")
        embed.set_footer(text="Trabajo de Limpiador en progreso......")
        return embed

    async def _terminar(self, interaction, exito, *, por_timeout=False):
        liquidada = False
        try:
            tiempo = int(time.time() - self.start_time)
            ratio = 1.0 + max(0.0, 45 - tiempo) / 45.0 * 0.35
            pago = min(int(self.pago_base * ratio), self.info['salario_max'])
            xp_ganada = self.info.get('xp_ganada', 0)
            if not exito or random.random() < self.info['prob_fallo']:
                pago = self.info['penalizacion']
                if por_timeout:
                    mensaje = (
                        f"La jornada terminó por inactividad; pierdes "
                        f"{abs(self.info['penalizacion'])} {COIN}."
                    )
                else:
                    mensaje = random.choice(self.info['mensajes_fallo']).format(monto=abs(self.info['penalizacion']), COIN=COIN)
                settlement = await finalizar_jornada_atomica(
                    self.session_id, self.author.id, "limpiador", False,
                    self.info['penalizacion'], mensaje,
                )
                if not settlement["ok"]:
                    raise RuntimeError(settlement.get("reason"))
                liquidada = True
                result_embed = discord.Embed(title="🧹 Resultado", description=f"{self.author.mention} {mensaje}", color=discord.Color.red())
                if interaction is not None:
                    await _editar_tablero_seguro(
                        interaction,
                        self,
                        embed=result_embed,
                        remove_view=True,
                    )
                elif self.message is not None:
                    await self.message.edit(embed=result_embed, view=None)
                _programar_eliminacion(self)
                return
            mensaje = random.choice(self.info['mensajes_exito']).format(monto=pago, COIN=COIN)
            mensaje = f"{mensaje} (+{xp_ganada} XP Laboral)"
            settlement = await finalizar_jornada_atomica(
                self.session_id, self.author.id, "limpiador", True,
                pago, mensaje, xp_ganada=xp_ganada,
            )
            if not settlement["ok"]:
                raise RuntimeError(settlement.get("reason"))
            liquidada = True
            bonus = settlement["bonus"]
            if bonus["coins"] > 0:
                mensaje += f"\n🌟 **¡Racha de 5!** Bono: **+{bonus['coins']}** {COIN} y **+{bonus['xp']} XP Laboral**"
            result_embed = discord.Embed(title="🧹 Resultado", description=f"{self.author.mention} {mensaje}", color=discord.Color.green())
            if interaction is not None:
                await _editar_tablero_seguro(
                    interaction,
                    self,
                    embed=result_embed,
                    remove_view=True,
                )
            elif self.message is not None:
                await self.message.edit(embed=result_embed, view=None)
            _programar_eliminacion(self)
        except Exception as e:
            _log_trabajar_error("Limpiador", e, self.author.name, "_terminar")
            await _cerrar_tablero_con_error(
                self,
                "Limpiador",
                interaction=interaction,
                liquidada=liquidada,
            )
            _programar_eliminacion(self)

    async def on_error(self, interaction: Interaction, error: Exception, item):
        self.terminado = True
        self.stop()
        await cancelar_jornada_segura(self.session_id, "error")
        _log_trabajar_error("Limpiador", error, interaction.user.name, f"on_error — Item: {item}")
        if self.message:
            try:
                await self.message.edit(view=None)
            except (discord.HTTPException, discord.NotFound):
                pass
        await _notificar_error_interaccion(interaction)

    async def on_timeout(self):
        logger.warning(f"[TRABAJAR/LIMPIADOR] on_timeout — Usuario: {self.author.name}")
        async with self._interaction_lock:
            if self.terminado:
                return
            self.terminado = True
            self.stop()
            self._build_buttons()
        await _mostrar_tablero_bloqueado(self)
        await self._terminar(None, exito=False, por_timeout=True)

    async def _cleanup(self):
        await asyncio.sleep(180)
        try:
            if self.message:
                await self.message.delete()
        except Exception:
            pass


class IngenieroView(JornadaView):
    MAX_VIDAS = 4

    def __init__(self, bot, author, info, session_id: str):
        super().__init__(timeout=180)
        self.bot = bot
        self.author = author
        self.info = info
        self.session_id = session_id
        self.start_time = time.time()
        self.pago_base = random.randint(
            self.info["salario_min"],
            self.info["salario_max"],
        )
        self.message = None
        self.revelados = [False] * 8
        self.seleccion = []
        self.pares = 0
        self.vidas = self.MAX_VIDAS
        self.bloqueado = False
        self.erroneas = set()
        self.terminado = False
        self._interaction_lock = asyncio.Lock()
        self._generar_tablero()
        self._build_buttons()

    def _generar_tablero(self):
        emojis = ["📡", "💡", "🔌", "🔧"] * 2
        random.shuffle(emojis)
        self.tablero = emojis

    def _build_buttons(self):
        self.clear_items()
        for i, emoji in enumerate(self.tablero):
            row = i // 4
            if i in self.seleccion or self.revelados[i]:
                if i in self.erroneas:
                    style = ButtonStyle.danger
                elif i in self.seleccion:
                    style = ButtonStyle.primary
                else:
                    style = ButtonStyle.success
                btn = ui.Button(
                    label=emoji,
                    style=style,
                    row=row,
                    custom_id=f"ing_{i}",
                    disabled=(
                        self.terminado
                        or self.bloqueado
                        or self.revelados[i]
                        or i in self.seleccion
                    ),
                )
            else:
                btn = ui.Button(
                    label="⬜",
                    style=ButtonStyle.secondary,
                    row=row,
                    custom_id=f"ing_{i}",
                    disabled=self.terminado or self.bloqueado,
                )
            btn.custom_id = f"ing_{self.session_id}_{i}"
            btn.callback = self._make_callback(i)
            self.add_item(btn)

    def _make_callback(self, idx):
        async def callback(interaction: Interaction):
            if interaction.user.id != self.author.id:
                return await _responder_jornada(interaction, "❌ Este tablero no es tuyo.")
            if self._interaction_lock.locked():
                return await _responder_jornada(
                    interaction,
                    "⏳ El tablero está actualizando la jugada anterior.",
                )

            resultado = None
            async with self._interaction_lock:
                if self.bloqueado or self.terminado or self.revelados[idx] or idx in self.seleccion:
                    return await _responder_jornada(
                        interaction,
                        "⏳ Esa casilla no está disponible.",
                    )
                self.seleccion.append(idx)
                self._build_buttons()
                await _editar_tablero_seguro(
                    interaction,
                    self,
                    embed=self.build_embed(),
                )
                if len(self.seleccion) == 2:
                    self.bloqueado = True
                    i1, i2 = self.seleccion
                    if self.tablero[i1] == self.tablero[i2]:
                        self.revelados[i1] = True
                        self.revelados[i2] = True
                        self.pares += 1
                        self.seleccion = []
                        if self.pares == 4:
                            self.terminado = True
                            self.bloqueado = True
                            self.stop()
                            resultado = True
                        else:
                            self.bloqueado = False
                        self._build_buttons()
                        await interaction.edit_original_response(embed=self.build_embed(), view=self)
                    else:
                        self.vidas -= 1
                        self.erroneas = {i1, i2}
                        if self.vidas <= 0:
                            self.terminado = True
                            self.stop()
                        self._build_buttons()
                        await _editar_tablero_seguro(
                            interaction,
                            self,
                            embed=self.build_embed(),
                        )
                        try:
                            await asyncio.sleep(2)
                        finally:
                            self.erroneas.clear()
                            self.seleccion = []
                            if self.terminado:
                                resultado = False
                            else:
                                self.bloqueado = False
                            self._build_buttons()
                        await _editar_tablero_seguro(
                            interaction,
                            self,
                            embed=self.build_embed(),
                        )
            if resultado is not None:
                await self._terminar(interaction, exito=resultado)
        return callback

    def build_embed(self):
        tiempo = int(time.time() - self.start_time)
        ratio = 1.0 + max(0.0, 45 - tiempo) / 45.0 * 0.35
        pago_actual = min(int(self.pago_base * ratio), self.info['salario_max'])
        corazones = "❤️" * self.vidas + "🖤" * (self.MAX_VIDAS - self.vidas)
        embed = discord.Embed(title=f"🛠️ Jornada Ingeniero - {self.author.nick or self.author.display_name}", color=discord.Color.blurple())
        embed.add_field(
            name="Objetivo",
            value="Encuentra los 4 pares para reparar la Nave antes de perder tus 4 vidas.",
            inline=False,
        )
        embed.add_field(name="Vidas restantes", value=corazones, inline=False)
        embed.add_field(name="Pares encontrados", value=str(self.pares), inline=True)
        embed.add_field(name="Pago estimado actual", value=f"{pago_actual} {COIN}", inline=True)
        embed.set_thumbnail(url="https://pub-a09b3609b6b34dfab5c7aa7742cd1a8a.r2.dev/Purple%20jack%20Harcode/IngePJ.png")
        embed.set_footer(text="Trabajo de Ingeniero en progreso......")
        return embed

    async def _terminar(self, interaction, exito, *, por_timeout=False):
        liquidada = False
        try:
            tiempo = int(time.time() - self.start_time)
            ratio = 1.0 + max(0.0, 45 - tiempo) / 45.0 * 0.35
            pago = min(int(self.pago_base * ratio), self.info['salario_max'])
            xp_ganada = self.info.get('xp_ganada', 0)
            if not exito:
                if por_timeout:
                    mensaje = (
                        f"La jornada terminó por inactividad; pierdes "
                        f"{abs(self.info['penalizacion'])} {COIN}."
                    )
                else:
                    mensaje = random.choice(self.info['mensajes_fallo']).format(monto=abs(self.info['penalizacion']), COIN=COIN)
                settlement = await finalizar_jornada_atomica(
                    self.session_id, self.author.id, "ingeniero", False,
                    self.info['penalizacion'], mensaje,
                )
                if not settlement["ok"]:
                    raise RuntimeError(settlement.get("reason"))
                liquidada = True
                result_embed = discord.Embed(title="🔧 Resultado", description=f"{self.author.mention} {mensaje}", color=discord.Color.red())
            else:
                mensaje = random.choice(self.info['mensajes_exito']).format(monto=pago, COIN=COIN)
                mensaje = f"{mensaje} (+{xp_ganada} XP Laboral)"
                settlement = await finalizar_jornada_atomica(
                    self.session_id, self.author.id, "ingeniero", True,
                    pago, mensaje, xp_ganada=xp_ganada,
                )
                if not settlement["ok"]:
                    raise RuntimeError(settlement.get("reason"))
                liquidada = True
                bonus = settlement["bonus"]
                if bonus["coins"] > 0:
                    mensaje += f"\n🌟 **¡Racha de 5!** Bono: **+{bonus['coins']}** {COIN} y **+{bonus['xp']} XP Laboral**"
                result_embed = discord.Embed(title="🔧 Resultado", description=f"{self.author.mention} {mensaje}", color=discord.Color.green())
            if interaction is not None:
                await _editar_tablero_seguro(
                    interaction,
                    self,
                    embed=result_embed,
                    remove_view=True,
                )
            elif self.message is not None:
                await self.message.edit(embed=result_embed, view=None)
            _programar_eliminacion(self)
        except Exception as e:
            _log_trabajar_error("Ingeniero", e, self.author.name, "_terminar")
            await _cerrar_tablero_con_error(
                self,
                "Ingeniero",
                interaction=interaction,
                liquidada=liquidada,
            )
            _programar_eliminacion(self)

    async def on_error(self, interaction: Interaction, error: Exception, item):
        self.terminado = True
        self.bloqueado = True
        self.stop()
        await cancelar_jornada_segura(self.session_id, "error")
        _log_trabajar_error("Ingeniero", error, interaction.user.name, f"on_error — Item: {item}")
        if self.message:
            try:
                await self.message.edit(view=None)
            except (discord.HTTPException, discord.NotFound):
                pass
        await _notificar_error_interaccion(interaction)

    async def on_timeout(self):
        logger.warning(f"[TRABAJAR/INGENIERO] on_timeout — Usuario: {self.author.name}")
        async with self._interaction_lock:
            if self.terminado:
                return
            self.terminado = True
            self.bloqueado = True
            self.stop()
            self._build_buttons()
        await _mostrar_tablero_bloqueado(self)
        await self._terminar(None, exito=False, por_timeout=True)


class PlomeroView(JornadaView):
    def __init__(self, bot, author, info, session_id: str):
        super().__init__(timeout=180)
        self.bot = bot
        self.author = author
        self.info = info
        self.session_id = session_id
        self.start_time = time.time()
        self.pago_base = random.randint(
            self.info["salario_min"],
            self.info["salario_max"],
        )
        self.message = None
        self.revelados = [False] * 9
        self.intentos = 0
        self.hallazgos = 0
        self.max_intentos = 6
        self.terminado = False
        self._interaction_lock = asyncio.Lock()
        self._generar_tablero()
        self._build_buttons()

    def _generar_tablero(self):
        emojis = ["💣"] * 3 + ["🪨"] * 6
        random.shuffle(emojis)
        self.tablero = emojis

    def _build_buttons(self):
        self.clear_items()
        for i, emoji in enumerate(self.tablero):
            row = i // 3
            if self.revelados[i]:
                btn = ui.Button(
                    label=emoji,
                    style=ButtonStyle.success if emoji == "💣" else ButtonStyle.danger,
                    row=row,
                    custom_id=f"plo_{i}",
                    disabled=True,
                )
            else:
                btn = ui.Button(
                    label="⬜",
                    style=ButtonStyle.secondary,
                    row=row,
                    custom_id=f"plo_{i}",
                    disabled=self.terminado,
                )
            btn.custom_id = f"plo_{self.session_id}_{i}"
            btn.callback = self._make_callback(i)
            self.add_item(btn)

    def _make_callback(self, idx):
        async def callback(interaction: Interaction):
            if interaction.user.id != self.author.id:
                return await _responder_jornada(interaction, "❌ Este tablero no es tuyo.")
            if self._interaction_lock.locked():
                return await _responder_jornada(
                    interaction,
                    "⏳ El tablero está actualizando la jugada anterior.",
                )

            terminar_exito = None
            async with self._interaction_lock:
                if self.terminado:
                    return await _responder_jornada(interaction, "⌛ Esta jornada ya finalizó.")
                if self.revelados[idx]:
                    return await _responder_jornada(interaction, "✅ Esa casilla ya está abierta.")
                self.revelados[idx] = True
                if self.tablero[idx] == "💣":
                    self.hallazgos += 1
                else:
                    self.intentos += 1
                if self.hallazgos >= 3:
                    terminar_exito = True
                elif self.intentos >= self.max_intentos:
                    terminar_exito = False
                if terminar_exito is not None:
                    self.terminado = True
                    self.stop()
                self._build_buttons()
                await _editar_tablero_seguro(
                    interaction,
                    self,
                    embed=self.build_embed(),
                )
            if terminar_exito is not None:
                await self._terminar(interaction, exito=terminar_exito)
        return callback

    def build_embed(self):
        embed = discord.Embed(title=f"💣 Busqueda de Impostores - {self.author.nick or self.author.display_name}", color=discord.Color.orange())
        intentos_restantes = self.max_intentos - self.intentos
        corazones = "❤️" * intentos_restantes + "🖤" * self.intentos
        embed.add_field(
            name="Objetivo",
            value="Encuentra los 3 elementos ideales para sellar ductos. Tienes 6 oportunidades para fallar antes de fracasar.",
            inline=False,
        )
        embed.add_field(name="Vidas restantes", value=corazones, inline=False)
        embed.add_field(name="Ductos Sellados", value=str(self.hallazgos), inline=True)
        embed.set_thumbnail(url="https://pub-a09b3609b6b34dfab5c7aa7742cd1a8a.r2.dev/Purple%20jack%20Harcode/PlomeroPJ.png")
        embed.set_footer(text="Trabajo de Plomero en progreso......")
        return embed

    async def _terminar(self, interaction, exito, *, por_timeout=False):
        liquidada = False
        try:
            tiempo = int(time.time() - self.start_time)
            ratio = 1.0 + max(0.0, 45 - tiempo) / 45.0 * 0.35
            pago = min(int(self.pago_base * ratio), self.info['salario_max'])
            xp_ganada = self.info.get('xp_ganada', 0)
            if not exito:
                if por_timeout:
                    mensaje = (
                        f"La jornada terminó por inactividad; pierdes "
                        f"{abs(self.info['penalizacion'])} {COIN}."
                    )
                else:
                    mensaje = random.choice(self.info['mensajes_fallo']).format(monto=abs(self.info['penalizacion']), COIN=COIN)
                settlement = await finalizar_jornada_atomica(
                    self.session_id, self.author.id, "plomero", False,
                    self.info['penalizacion'], mensaje,
                )
                if not settlement["ok"]:
                    raise RuntimeError(settlement.get("reason"))
                liquidada = True
                result_embed = discord.Embed(title="🛠️ Resultado", description=f"{self.author.mention} {mensaje}", color=discord.Color.red())
            else:
                mensaje = random.choice(self.info['mensajes_exito']).format(monto=pago, COIN=COIN)
                mensaje = f"{mensaje} (+{xp_ganada} XP Laboral)"
                settlement = await finalizar_jornada_atomica(
                    self.session_id, self.author.id, "plomero", True,
                    pago, mensaje, xp_ganada=xp_ganada,
                )
                if not settlement["ok"]:
                    raise RuntimeError(settlement.get("reason"))
                liquidada = True
                bonus = settlement["bonus"]
                if bonus["coins"] > 0:
                    mensaje += f"\n🌟 **¡Racha de 5!** Bono: **+{bonus['coins']}** {COIN} y **+{bonus['xp']} XP Laboral**"
                result_embed = discord.Embed(title="🛠️ Resultado", description=f"{self.author.mention} {mensaje}", color=discord.Color.green())
            if interaction is not None:
                await _editar_tablero_seguro(
                    interaction,
                    self,
                    embed=result_embed,
                    remove_view=True,
                )
            elif self.message is not None:
                await self.message.edit(embed=result_embed, view=None)
            _programar_eliminacion(self)
        except Exception as e:
            _log_trabajar_error("Plomero", e, self.author.name, "_terminar")
            await _cerrar_tablero_con_error(
                self,
                "Plomero",
                interaction=interaction,
                liquidada=liquidada,
            )
            _programar_eliminacion(self)

    async def on_error(self, interaction: Interaction, error: Exception, item):
        self.terminado = True
        self.stop()
        await cancelar_jornada_segura(self.session_id, "error")
        _log_trabajar_error("Plomero", error, interaction.user.name, f"on_error — Item: {item}")
        if self.message:
            try:
                await self.message.edit(view=None)
            except (discord.HTTPException, discord.NotFound):
                pass
        await _notificar_error_interaccion(interaction)

    async def on_timeout(self):
        logger.warning(f"[TRABAJAR/PLOMERO] on_timeout — Usuario: {self.author.name}")
        async with self._interaction_lock:
            if self.terminado:
                return
            self.terminado = True
            self.stop()
            self._build_buttons()
        await _mostrar_tablero_bloqueado(self)
        await self._terminar(None, exito=False, por_timeout=True)


async def setup(bot):
    await bot.add_cog(Empleos(bot))
