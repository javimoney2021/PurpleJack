# Deployment refresh: 2026-08-17
import discord
from discord.ext import commands
import asyncio
import time
import signal
import logging

# ── LOGGING ────────────────────────────────────────────

class _PurpleFormatter(logging.Formatter):
    ICONS = {
        logging.DEBUG:    "🔍",
        logging.INFO:     "✔️ ",
        logging.WARNING:  "❌",
        logging.ERROR:    "❌",
        logging.CRITICAL: "❌",
    }

    def format(self, record: logging.LogRecord) -> str:
        icon = self.ICONS.get(record.levelno, "  ")
        base = super().format(record)
        return f"{icon} {base}"


class _VoiceWarningFilter(logging.Filter):
    """Suprime los warnings de PyNaCl / davey (voz no usada)."""
    _BLOCKED = {"PyNaCl is not installed", "davey is not installed"}

    def filter(self, record: logging.LogRecord) -> bool:
        return not any(msg in record.getMessage() for msg in self._BLOCKED)


_handler = logging.StreamHandler()
_handler.setFormatter(_PurpleFormatter(
    fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
))
_handler.addFilter(_VoiceWarningFilter())

logging.basicConfig(level=logging.INFO, handlers=[_handler])

# Aplicar filtro también al logger raíz de discord
logging.getLogger("discord").addFilter(_VoiceWarningFilter())

logger = logging.getLogger("purplejack")

logger.info(f"discord.py version: {discord.__version__}")

from settings import TOKEN
try:
    from settings import GUILD_ID
except ImportError:
    GUILD_ID = None

from core.database import (
    init_db, load_items_to_cache, load_cargos_to_cache,
    load_collect_config_to_cache, claim_expired_cargos,
    get_cargo_temporal_by_id, mark_cargo_removal_failed,
    release_cargo_claim, delete_cargo_temporal_by_id,
    create_game_config_table, load_game_config, load_dados_config, load_memo_config,
    load_veterano_config_to_cache, load_saboteador_config_to_cache, load_item_role_restrictions_to_cache,
    save_collect_cooldowns, load_evento_to_cache, flush_evento_puntos,
    recover_pending_wagers, ensure_wager_constraints,
    get_inactive_user_ids, get_user_deletion_blocker,
    get_user_temporary_roles, purge_user_data, touch_user_activity,
)
from core import cache
from core.config import (
    AYUDA_CHANNEL_ID, LOG_CHANNEL_ID, STAFF_ROLE_ID, PUNISHMENT_ROLE_ID,
)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

async def get_prefix(bot, message):
    """Normaliza el prefijo '!' aceptando '!comando' y '! comando' indistintamente."""
    return ["! ", "!"]

bot = commands.Bot(command_prefix=get_prefix, intents=intents, help_command=None)
evento_flush_task = None
cargos_task = None
wager_recovery_task = None
inactive_purge_task = None
_activity_writes = {}
bot._user_activity_writes = _activity_writes

USER_INACTIVITY_RETENTION = 60 * 24 * 60 * 60
USER_ACTIVITY_WRITE_INTERVAL = 60 * 60
INACTIVE_PURGE_INTERVAL = 6 * 60 * 60


class CommandAccessRestricted(commands.CheckFailure):
    """Evita que un usuario castigado ejecute comandos con prefijo."""


@bot.check
async def block_restricted_prefix_commands(ctx: commands.Context) -> bool:
    if ctx.guild is None:
        return True

    expires_at = cache.get_active_cargo_expiry(
        ctx.author.id,
        ctx.guild.id,
        PUNISHMENT_ROLE_ID,
    )
    if not expires_at:
        return True

    await ctx.reply(
        "Tu acceso está **Restringido** debido a un uso incorrecto del canal. "
        f"Podrás volver a interactuar en <t:{int(expires_at)}:R>",
        mention_author=False,
        delete_after=3,
    )
    raise CommandAccessRestricted()


async def record_user_activity(user_id: int) -> None:
    now = time.time()
    if now - _activity_writes.get(user_id, 0) < USER_ACTIVITY_WRITE_INTERVAL:
        return
    _activity_writes[user_id] = now
    try:
        await touch_user_activity(user_id, now)
    except Exception:
        _activity_writes.pop(user_id, None)
        logger.exception("No se pudo registrar actividad para el usuario %s", user_id)


@bot.listen("on_command")
async def track_prefix_command_activity(ctx):
    if not ctx.author.bot:
        await record_user_activity(ctx.author.id)


@bot.listen("on_interaction")
async def track_interaction_activity(interaction: discord.Interaction):
    if interaction.user and not interaction.user.bot:
        await record_user_activity(interaction.user.id)


async def load_modules():
    await bot.load_extension("modules.economy")
    await bot.load_extension("modules.basic_games")
    await bot.load_extension("modules.staff")
    await bot.load_extension("modules.roulette")
    await bot.load_extension("modules.rob")
    await bot.load_extension("modules.shop")
    await bot.load_extension("modules.Empleos")
    await bot.load_extension("modules.collect")
    await bot.load_extension("modules.dados")
    await bot.load_extension("modules.duels")
    await bot.load_extension("modules.golpear")
    await bot.load_extension("modules.carrera")
    await bot.load_extension("modules.memo")
    await bot.load_extension("modules.adivinar")
    await bot.load_extension("modules.blackjack")


async def _alertar_fallo_retiro_cargo(cargo: dict, error: Exception, intento: int):
    """Avisa al Staff sin interrumpir los reintentos persistentes."""
    try:
        channel = bot.get_channel(LOG_CHANNEL_ID)
        if channel is None:
            channel = await bot.fetch_channel(LOG_CHANNEL_ID)
        await channel.send(
            (
                f"<@&{STAFF_ROLE_ID}> ⚠️ No pude retirar un rol temporal vencido.\n"
                f"Usuario: <@{cargo['user_id']}> (`{cargo['user_id']}`)\n"
                f"Rol: <@&{cargo['rol_id']}> (`{cargo['rol_id']}`)\n"
                f"Intento: **{intento}**\n"
                f"Error: `{type(error).__name__}: {str(error)[:500]}`"
            ),
            allowed_mentions=discord.AllowedMentions(
                everyone=False,
                roles=True,
                users=False,
                replied_user=False,
            ),
        )
    except Exception:
        logger.exception(
            "No se pudo alertar al Staff sobre el fallo del cargo temporal %s",
            cargo["id"],
        )


async def _retirar_cargo_vencido(cargo: dict) -> str:
    """
    Retira y verifica un rol. El registro solo se elimina cuando Discord
    confirma que el miembro ya no lo posee o cuando el recurso dejó de existir.
    """
    user_id = cargo["user_id"]
    guild_id = cargo["guild_id"]
    role_id = cargo["rol_id"]
    role_lock = cache.get_role_assignment_lock(user_id, guild_id, role_id)

    async with role_lock:
        latest = await get_cargo_temporal_by_id(cargo["id"])
        if latest is None:
            return "reemplazado"
        if latest["expira_en"] > time.time():
            await release_cargo_claim(cargo["id"])
            cache.upsert_cargo_cache(
                user_id,
                guild_id,
                role_id,
                latest["expira_en"],
            )
            return "extendido"

        guild = bot.get_guild(guild_id)
        if guild is None:
            try:
                guild = await bot.fetch_guild(guild_id)
            except discord.NotFound:
                await delete_cargo_temporal_by_id(cargo["id"])
                return "servidor_inexistente"

        role = guild.get_role(role_id)
        if role is None:
            roles = await guild.fetch_roles()
            role = discord.utils.get(roles, id=role_id)
        if role is None:
            await delete_cargo_temporal_by_id(cargo["id"])
            return "rol_inexistente"

        try:
            member = await guild.fetch_member(user_id)
        except discord.NotFound:
            await delete_cargo_temporal_by_id(cargo["id"])
            return "miembro_fuera"

        if role_id in {member_role.id for member_role in member.roles}:
            await member.remove_roles(
                role,
                reason=(
                    "Finalización de restricción temporal de comandos"
                    if role_id == PUNISHMENT_ROLE_ID
                    else "Expiración de rol temporal otorgado por un item"
                ),
            )

        for intento in range(3):
            try:
                member_check = await guild.fetch_member(user_id)
            except discord.NotFound:
                member_check = None
                break
            if role_id not in {member_role.id for member_role in member_check.roles}:
                break
            if intento < 2:
                await asyncio.sleep(0.75)
        else:
            raise RuntimeError("Discord todavía reporta el rol después de retirarlo")

        await delete_cargo_temporal_by_id(cargo["id"])
        return "retirado"


async def check_cargos_loop():
    """Procesa expiraciones desde Aiven y recupera pendientes tras reinicios."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            cargos = await claim_expired_cargos(limit=50)
            if not cargos:
                await asyncio.sleep(30)
                continue

            retirados = 0
            limpiados = 0
            for cargo in cargos:
                try:
                    resultado = await _retirar_cargo_vencido(cargo)
                    if resultado == "retirado":
                        retirados += 1
                    elif resultado in {
                        "servidor_inexistente",
                        "rol_inexistente",
                        "miembro_fuera",
                    }:
                        limpiados += 1
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    intento = cargo.get("attempts", 0) + 1
                    retry_after = min(3600, 30 * (2 ** min(cargo.get("attempts", 0), 7)))
                    try:
                        await mark_cargo_removal_failed(
                            cargo["id"],
                            f"{type(error).__name__}: {error}",
                            retry_after,
                        )
                    except Exception:
                        logger.exception(
                            "No se pudo persistir el reintento del cargo temporal %s",
                            cargo["id"],
                        )
                    logger.warning(
                        "Cargo temporal %s no retirado (usuario=%s, rol=%s, "
                        "intento=%s); reintento en %ss: %s",
                        cargo["id"],
                        cargo["user_id"],
                        cargo["rol_id"],
                        intento,
                        retry_after,
                        error,
                    )
                    if intento in {3, 10} or (
                        isinstance(error, discord.Forbidden) and intento == 1
                    ):
                        await _alertar_fallo_retiro_cargo(cargo, error, intento)

            if retirados or limpiados:
                logger.info(
                    "Cargos vencidos procesados: %s retirado(s), %s registro(s) obsoleto(s).",
                    retirados,
                    limpiados,
                )
            await asyncio.sleep(1 if len(cargos) >= 50 else 5)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Error general procesando cargos temporales vencidos")
            await asyncio.sleep(30)


async def recover_expired_wagers_loop():
    """Reembolsa apuestas vencidas sin interferir con despliegues activos."""
    while not bot.is_closed():
        try:
            await asyncio.sleep(30)
            recovered = await recover_pending_wagers()
            if recovered:
                logger.warning(
                    "Se reembolsaron %s apuestas vencidas.",
                    recovered,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Error recuperando apuestas vencidas")
            await asyncio.sleep(30)


async def _remove_temporary_roles_before_purge(user_id: int) -> bool:
    for cargo in await get_user_temporary_roles(user_id):
        guild = bot.get_guild(cargo["guild_id"])
        if guild is None:
            continue
        role = guild.get_role(cargo["rol_id"])
        if role is None:
            continue
        try:
            member = guild.get_member(user_id) or await guild.fetch_member(user_id)
        except discord.NotFound:
            continue
        try:
            if role in member.roles:
                await member.remove_roles(
                    role,
                    reason="Purga de datos por inactividad o solicitud del usuario",
                )
        except (discord.Forbidden, discord.HTTPException):
            logger.exception(
                "No se pudo retirar el rol %s antes de purgar al usuario %s",
                cargo["rol_id"],
                user_id,
            )
            return False
    return True


async def purge_inactive_users_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            cutoff = time.time() - USER_INACTIVITY_RETENTION
            user_ids = await get_inactive_user_ids(cutoff, limit=100)
            purged = 0
            for user_id in user_ids:
                if await get_user_deletion_blocker(user_id):
                    continue
                if not await _remove_temporary_roles_before_purge(user_id):
                    continue
                if await purge_user_data(user_id, inactive_before=cutoff):
                    _activity_writes.pop(user_id, None)
                    purged += 1
            if purged:
                logger.info(
                    "Purga automática: %s usuario(s) eliminado(s) tras 60 días de inactividad.",
                    purged,
                )
            await asyncio.sleep(60 if len(user_ids) >= 100 else INACTIVE_PURGE_INTERVAL)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Error en la purga automática de usuarios inactivos")
            await asyncio.sleep(300)


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, CommandAccessRestricted):
        return
    if isinstance(error, commands.CommandNotFound):
        await ctx.reply(
            "❌ Este comando no es válido, Consulta la Guia de la Nave en <#1505296434076057752>",
            delete_after=10
        )
    elif isinstance(error, commands.CommandOnCooldown):
        return  # deja que el handler del módulo lo maneje
    else:
        raise error

async def shutdown():
    logger.warning("Apagando bot — flusheando caché a DB...")
    await flush_evento_puntos()
    await cache.flush_to_db()
    # ── Persistir cooldowns de collect pendientes en cache ──
    all_cooldowns = cache.get_all_collect_cooldowns()
    for user_id, cobros in all_cooldowns.items():
        if cobros:
            try:
                await save_collect_cooldowns(user_id, cobros)
            except Exception as e:
                logger.warning(f"shutdown: error persistiendo collect cooldowns [{user_id}]: {e}")
    logger.info("Caché flusheada correctamente.")
    await bot.close()

@bot.event
async def on_ready():
    global evento_flush_task, cargos_task, wager_recovery_task, inactive_purge_task
    logger.info(f"Bot conectado como {bot.user}")
    logger.info("Caché iniciada | Flush cada 5 minutos")
    logger.info(f"Servidores activos: {len(bot.guilds)}")

    try:
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            synced = await bot.tree.sync(guild=guild)
            logger.info(f"Comandos Slash/Staff sincronizados en el servidor {GUILD_ID}: {len(synced)} comandos.")
        else:
            synced = await bot.tree.sync()
            logger.info(f"Comandos Slash/Staff sincronizados globalmente: {len(synced)} comandos.")
    except Exception as e:
        logger.warning(f"Error sincronizando comandos slash: {e}")

    asyncio.create_task(cache.flush_loop())
    if cargos_task is None or cargos_task.done():
        cargos_task = asyncio.create_task(
            check_cargos_loop(),
            name="cargos-temporales-worker",
        )
        logger.info("Task de cargos temporales iniciada | Revisión persistente cada 30 segundos")
    if evento_flush_task is None or evento_flush_task.done():
        evento_flush_task = asyncio.create_task(cache.evento_flush_loop())
        logger.info("Task de evento iniciada | Flush de ranking cada 10 minutos")
    if wager_recovery_task is None or wager_recovery_task.done():
        wager_recovery_task = asyncio.create_task(
            recover_expired_wagers_loop(),
            name="apuestas-vencidas-worker",
        )
        logger.info("Task de apuestas vencidas iniciada | Revisión cada 30 segundos")
    if inactive_purge_task is None or inactive_purge_task.done():
        inactive_purge_task = asyncio.create_task(
            purge_inactive_users_loop(),
            name="purga-usuarios-inactivos-worker",
        )
        logger.info("Task de privacidad iniciada | Purga tras 60 días de inactividad")
    logger.info("\n⫷ 𝙋𝙐𝙍𝙋𝙇𝙀𝙅𝘼𝘾𝙆 𝙀𝙉 𝙇𝙄𝙉𝙀𝘼 ⫸\n")

def run_bot():
    async def main():
        # ── Handler de señal SIGTERM (ej: SquareCloud apagando el proceso) ──
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.create_task(shutdown()))

        await init_db()
        reembolsadas = await recover_pending_wagers()
        if reembolsadas:
            logger.warning(
                "Se reembolsaron %s apuestas vencidas al iniciar.",
                reembolsadas,
            )
        await ensure_wager_constraints()
        await create_game_config_table()
        await load_game_config()
        await load_dados_config()
        await load_memo_config()
        await load_items_to_cache()
        await load_cargos_to_cache()
        await load_item_role_restrictions_to_cache()
        await load_collect_config_to_cache()
        await load_veterano_config_to_cache()
        await load_saboteador_config_to_cache()
        await load_evento_to_cache()
        await load_modules()
        try:
            await bot.start(TOKEN)
        finally:
            await flush_evento_puntos()
            await cache.flush_to_db()
            logger.info("Flush final completado al cerrar.")

    asyncio.run(main())
