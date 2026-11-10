# Deployment refresh: 2026-08-17
import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import time
import signal
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

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

from settings import TOKEN, DATABASE_URL, GUILD_ID, TEST_MODE

from core.database import (
    init_db, drop_halloween_tables, load_items_to_cache, load_cargos_to_cache,
    load_balance_index, load_member_names, remember_member_name,
    load_collect_config_to_cache, claim_expired_cargos,
    get_cargo_temporal_by_id, mark_cargo_removal_failed,
    release_cargo_claim, delete_cargo_temporal_by_id,
    create_game_config_table, load_game_config, load_dados_config, load_memo_config,
    load_veterano_config_to_cache, load_saboteador_config_to_cache, load_item_role_restrictions_to_cache,
    save_collect_cooldowns,
    recover_pending_wagers, ensure_wager_constraints,
    get_inactive_user_ids, get_user_deletion_blocker,
    get_user_temporary_roles, purge_user_data, touch_user_activity,
    claim_maintenance_job, complete_maintenance_job,
    delete_expired_failed_item_use_logs,
)
from core import cache
from core.game_reminders import init_game_reminders, game_reminder_worker
from core.config import (
    AYUDA_CHANNEL_ID, LOG_CHANNEL_ID, STAFF_ROLE_ID, PUNISHMENT_ROLE_ID,
    PLAYER_COMMAND_ROLE_ID,
    LOG_CLEANUP_INTERVAL_SECONDS, LOG_MESSAGE_RETENTION_SECONDS,
    LOG_RETENTION_CHANNEL_IDS, LOG_DELETE_DELAY_SECONDS,
    LOG_DELETE_BATCH_SIZE, LOG_CLEANUP_BACKLOG_DELAY_SECONDS,
)
def _safe_startup_error(error: Exception) -> str:
    """Evita que un error de conexión imprima credenciales en los registros."""
    message = str(error)
    secrets = [TOKEN, DATABASE_URL]
    try:
        password = urlsplit(DATABASE_URL).password
        if password:
            secrets.append(password)
    except ValueError:
        pass
    for secret in secrets:
        if secret:
            message = message.replace(secret, "[REDACTADO]")
    return message[:700]


async def _startup_step(label: str, operation):
    """Registra esperas prolongadas y fallos de cada etapa sin exponer secretos."""
    started = time.monotonic()
    task = asyncio.create_task(operation, name=f"startup-{label}")
    try:
        while True:
            try:
                return await asyncio.wait_for(asyncio.shield(task), timeout=20)
            except asyncio.TimeoutError:
                logger.warning(
                    "[STARTUP:WAIT] %s sigue esperando tras %.0fs",
                    label, time.monotonic() - started,
                )
    except BaseException as error:
        if not task.done():
            task.cancel()
        if isinstance(error, Exception):
            safe_error = _safe_startup_error(error)
            logger.error(
                "[STARTUP:ERROR] %s: %s: %s",
                label, type(error).__name__, safe_error,
            )
            raise RuntimeError(
                f"Inicio fallido en {label}: {type(error).__name__}: {safe_error}"
            ) from None
        else:
            logger.warning("[STARTUP:CANCELLED] %s", label)
        raise


intents = discord.Intents.default()
intents.message_content = False
intents.members = False
intents.messages = False

async def check_restricted_interaction(interaction: discord.Interaction) -> bool:
    """Bloquea botones y formularios abiertos antes de aplicar /punish."""
    guild_id = getattr(interaction, "guild_id", None)
    if guild_id is None:
        return True
    expires_at = cache.get_active_cargo_expiry(
        interaction.user.id, guild_id, PUNISHMENT_ROLE_ID,
    )
    if not expires_at:
        return True
    if interaction.type is discord.InteractionType.autocomplete:
        await interaction.response.autocomplete([])
    else:
        await interaction.response.send_message(
            "Tu acceso está **Restringido** debido a un uso incorrecto del canal. "
            f"Podrás volver a interactuar en <t:{int(expires_at)}:R>.",
            ephemeral=True,
        )
    return False

async def check_player_access(interaction: discord.Interaction) -> bool:
    """Protege tanto los paneles slash como cada botón que ejecuta acciones."""
    async def deny(message: str) -> bool:
        if interaction.type is discord.InteractionType.autocomplete:
            await interaction.response.autocomplete([])
        else:
            await interaction.response.send_message(message, ephemeral=True)
        return False

    if interaction.guild is None:
        return await deny("Este panel solo está disponible en el servidor.")
    if not await check_restricted_interaction(interaction):
        return False
    if PLAYER_COMMAND_ROLE_ID not in {
        role.id for role in getattr(interaction.user, "roles", ())
    }:
        return await deny(
            "No tienes el rol necesario para usar los comandos de jugador."
        )
    return True


class PurpleJackCommandTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        command_name = (interaction.data or {}).get("name")
        if command_name not in {"ayuda_nave", "eliminar_mis_datos"}:
            if not await check_restricted_interaction(interaction):
                return False
        if command_name in {
            "jugar", "trabajar", "consultar", "work", "crime", "collect",
            "tienda", "rob", "carrera", "retar",
        }:
            return await check_player_access(interaction)
        if command_name in {"ayuda_nave", "eliminar_mis_datos"}:
            return True
        return True

bot = commands.Bot(
    command_prefix=[],
    intents=intents,
    help_command=None,
    member_cache_flags=discord.MemberCacheFlags.none(),
    chunk_guilds_at_startup=False,
    tree_cls=PurpleJackCommandTree,
)


cargos_task = None
wager_recovery_task = None
inactive_purge_task = None
log_cleanup_task = None
game_reminder_task = None
cache_flush_task = None
slash_sync_done = False
_activity_writes = {}
bot._user_activity_writes = _activity_writes

USER_INACTIVITY_RETENTION = 30 * 24 * 60 * 60
USER_ACTIVITY_WRITE_INTERVAL = 60 * 60
INACTIVE_PURGE_INTERVAL = 6 * 60 * 60
LOG_CLEANUP_JOB_NAME = "discord-log-retention"


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


async def record_member_name(user) -> None:
    if not isinstance(user, discord.Member):
        return
    try:
        await remember_member_name(user.id, user.display_name)
    except Exception:
        logger.exception("No se pudo guardar el apodo del usuario %s", user.id)


@bot.listen("on_interaction")
async def track_interaction_activity(interaction: discord.Interaction):
    if interaction.user and not interaction.user.bot:
        await record_member_name(interaction.user)
        await record_user_activity(interaction.user.id)


async def load_modules():
    for extension in (
        "modules.economy", "modules.basic_games", "modules.staff",
        "modules.roulette", "modules.rob", "modules.shop",
        "modules.Empleos", "modules.collect", "modules.dados",
        "modules.duels", "modules.golpear", "modules.carrera",
        "modules.memo", "modules.blackjack", "modules.player_panels",
        "modules.player_commands",
    ):
        await _startup_step(f"Extensión {extension}", bot.load_extension(extension))


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
                    "Purga automática: %s usuario(s) eliminado(s) tras 30 días de inactividad.",
                    purged,
                )
            await asyncio.sleep(60 if len(user_ids) >= 100 else INACTIVE_PURGE_INTERVAL)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Error en la purga automática de usuarios inactivos")
            await asyncio.sleep(300)


async def _delete_expired_bot_logs(
    channel_id: int,
    cutoff: datetime,
    max_deletions: int,
) -> tuple[int, bool]:
    """Elimina mensajes antiguos solo cuando pertenecen a esta cuenta del bot."""
    if max_deletions <= 0:
        return 0, True

    try:
        channel = bot.get_channel(channel_id)
        if channel is None:
            channel = await bot.fetch_channel(channel_id)
        if not hasattr(channel, "history"):
            raise RuntimeError("el destino no permite consultar historial")
    except (discord.Forbidden, discord.NotFound, discord.HTTPException, RuntimeError) as error:
        logger.error(
            "[LOG_CLEANUP:E_CHANNEL] No se pudo consultar el canal %s: %s",
            channel_id,
            error,
        )
        return 0, False

    bot_user_id = bot.user.id if bot.user else None
    if bot_user_id is None:
        return 0, False

    deleted = 0
    batch_limit_reached = False
    try:
        async for message in channel.history(
            limit=None,
            before=cutoff,
            oldest_first=True,
        ):
            if message.author.id != bot_user_id:
                continue
            try:
                await message.delete()
                deleted += 1
                if deleted >= max_deletions:
                    batch_limit_reached = True
                    break
                await asyncio.sleep(LOG_DELETE_DELAY_SECONDS)
            except discord.NotFound:
                continue
            except discord.Forbidden as error:
                logger.error(
                    "[LOG_CLEANUP:E_FORBIDDEN] Sin permiso para eliminar en %s: %s",
                    channel_id,
                    error,
                )
                break
            except discord.HTTPException as error:
                logger.warning(
                    "[LOG_CLEANUP:E_DELETE] No se eliminó el mensaje %s en %s: %s",
                    message.id,
                    channel_id,
                    error,
                )
    except (discord.Forbidden, discord.HTTPException) as error:
        logger.error(
            "[LOG_CLEANUP:E_HISTORY] Falló la lectura del canal %s: %s",
            channel_id,
            error,
        )
    return deleted, batch_limit_reached


async def log_retention_worker():
    """Ejecuta la retención horaria con agenda persistente y reclamo atómico."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            now = time.time()
            schedule = await claim_maintenance_job(
                LOG_CLEANUP_JOB_NAME,
                LOG_CLEANUP_INTERVAL_SECONDS,
                now=now,
            )
            if not schedule["claimed"]:
                wait_for = max(1, schedule["next_run_at"] - now)
                await asyncio.sleep(min(wait_for, LOG_CLEANUP_INTERVAL_SECONDS))
                continue

            cutoff = datetime.now(timezone.utc) - timedelta(
                seconds=LOG_MESSAGE_RETENTION_SECONDS
            )
            deleted_messages = 0
            backlog_pending = False
            for channel_id in LOG_RETENTION_CHANNEL_IDS:
                remaining = LOG_DELETE_BATCH_SIZE - deleted_messages
                if remaining <= 0:
                    backlog_pending = True
                    break
                channel_deleted, reached_limit = await _delete_expired_bot_logs(
                    channel_id,
                    cutoff,
                    remaining,
                )
                deleted_messages += channel_deleted
                if reached_limit:
                    backlog_pending = True
                    break

            deleted_outbox = await delete_expired_failed_item_use_logs()
            completed_at = time.time()
            await complete_maintenance_job(
                LOG_CLEANUP_JOB_NAME,
                completed_at=completed_at,
                next_run_at=(
                    completed_at + LOG_CLEANUP_BACKLOG_DELAY_SECONDS
                    if backlog_pending
                    else None
                ),
            )
            logger.info(
                "[LOG_CLEANUP:OK] Eliminados %s mensaje(s) del bot y %s "
                "registro(s) fallido(s) del outbox.%s",
                deleted_messages,
                deleted_outbox,
                " Próximo lote en 5 minutos." if backlog_pending else "",
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[LOG_CLEANUP:E_WORKER] Falló el worker de retención")
            await asyncio.sleep(60)


async def shutdown():
    logger.warning("Apagando bot — flusheando caché a DB...")
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

_REQUIRED_PLAYER_COMMANDS = {
    "jugar", "consultar", "trabajar", "work", "crime", "collect",
    "tienda", "rob", "carrera", "retar",
}


async def sync_slash_commands(client: commands.Bot) -> None:
    """Publica primero los comandos del servidor; luego limpia globales heredados."""
    guild_id = int(GUILD_ID)
    if client.get_guild(guild_id) is None:
        raise RuntimeError(
            f"GUILD_ID={guild_id} no es un servidor conectado a "
            f"{client.user}. Servidores visibles: {[guild.id for guild in client.guilds]}"
        )

    guild = discord.Object(id=guild_id)
    local_global = client.tree.get_commands()
    local_names = {command.name for command in local_global}
    missing = _REQUIRED_PLAYER_COMMANDS - local_names
    if missing:
        raise RuntimeError(f"Faltan comandos locales antes de sincronizar: {sorted(missing)}")

    client.tree.copy_global_to(guild=guild)
    await client.tree.sync(guild=guild)
    published = await client.tree.fetch_commands(guild=guild)
    published_names = {command.name for command in published}
    missing_remote = local_names - published_names
    if missing_remote or len(published) != len(local_global):
        raise RuntimeError(
            f"Discord no registró todos los comandos en el servidor {guild_id}. "
            f"Esperados: {len(local_global)}, publicados: {len(published)}, "
            f"faltantes: {sorted(missing_remote)}"
        )
    logger.info(
        "[SLASH] Servidor %s: %s comandos verificados; paneles /jugar, "
        "/consultar y /trabajar presentes.", guild_id, len(published),
    )

    # Los comandos globales del repositorio anterior son independientes de los
    # del servidor. Solo se retiran tras verificar que los nuevos están activos.
    old_global = await client.tree.fetch_commands()
    client.tree.clear_commands(guild=None)
    if old_global:
        try:
            await client.tree.sync()
            remaining = await client.tree.fetch_commands()
            if remaining:
                raise RuntimeError(
                    f"Persisten {len(remaining)} comandos globales antiguos en Discord."
                )
        except Exception:
            for command in local_global:
                client.tree.add_command(command)
            raise
    logger.info(
        "[SLASH] Comandos globales anteriores retirados: %s. "
        "Comandos de PurpleJack activos solo en servidor %s.",
        len(old_global), guild_id,
    )


@bot.event
async def on_ready():
    global cargos_task, wager_recovery_task
    global inactive_purge_task, log_cleanup_task, game_reminder_task, slash_sync_done
    global cache_flush_task
    logger.info(f"Bot conectado como {bot.user}")
    logger.info(
        "Caché iniciada | Flush cada %s segundos",
        cache.FLUSH_INTERVAL,
    )
    logger.info(f"Servidores activos: {len(bot.guilds)}")

    if not slash_sync_done:
        try:
            await sync_slash_commands(bot)
            slash_sync_done = True
        except Exception:
            logger.exception(
                "[SLASH:E_SYNC] No se pudo verificar la publicación de comandos. "
                "Revisa DISCORD_TOKEN, GUILD_ID y los permisos de la aplicación."
            )
            await bot.close()
            return

    if cache_flush_task is None or cache_flush_task.done():
        cache_flush_task = asyncio.create_task(cache.flush_loop(), name="economy-cache-flush")
    if not TEST_MODE and (cargos_task is None or cargos_task.done()):
        cargos_task = asyncio.create_task(
            check_cargos_loop(),
            name="cargos-temporales-worker",
        )
        logger.info("Task de cargos temporales iniciada | Revisión persistente cada 30 segundos")
    if wager_recovery_task is None or wager_recovery_task.done():
        wager_recovery_task = asyncio.create_task(
            recover_expired_wagers_loop(),
            name="apuestas-vencidas-worker",
        )
        logger.info("Task de apuestas vencidas iniciada | Revisión cada 30 segundos")
    if not TEST_MODE and (inactive_purge_task is None or inactive_purge_task.done()):
        inactive_purge_task = asyncio.create_task(
            purge_inactive_users_loop(),
            name="purga-usuarios-inactivos-worker",
        )
        logger.info("Task de privacidad iniciada | Purga tras 30 días de inactividad")
    if not TEST_MODE and (log_cleanup_task is None or log_cleanup_task.done()):
        log_cleanup_task = asyncio.create_task(
            log_retention_worker(),
            name="retencion-logs-worker",
        )
        logger.info(
            "Task de retención de logs iniciada | Revisión persistente cada hora"
        )
    if game_reminder_task is None or game_reminder_task.done():
        game_reminder_task = asyncio.create_task(
            game_reminder_worker(bot), name="recordatorios-juegos-worker",
        )
    logger.info("\n⫷ 𝙋𝙐𝙍𝙋𝙇𝙀𝙅𝘼𝘾𝙆 𝙀𝙉 𝙇𝙄𝙉𝙀𝘼 ⫸\n")

def _register_shutdown_signal(loop: asyncio.AbstractEventLoop) -> bool:
    """La señal es opcional en entornos donde el event loop la restringe."""
    try:
        loop.add_signal_handler(
            signal.SIGTERM, lambda: asyncio.create_task(shutdown()),
        )
    except (NotImplementedError, RuntimeError, ValueError) as error:
        logger.warning(
            "[STARTUP:SIGNAL] SIGTERM no disponible: %s. "
            "El arranque continuará.", type(error).__name__,
        )
        return False
    return True


def run_bot():
    async def main():
        loop = asyncio.get_running_loop()
        _register_shutdown_signal(loop)

        await _startup_step("PostgreSQL y esquema", init_db())
        await _startup_step("Retiro de tablas Halloween", drop_halloween_tables())
        await _startup_step("Tabla de recordatorios", init_game_reminders())
        reembolsadas = await _startup_step("Apuestas pendientes", recover_pending_wagers())
        if reembolsadas:
            logger.warning(
                "Se reembolsaron %s apuestas vencidas al iniciar.",
                reembolsadas,
            )
        indexed_balances = await _startup_step("Índice de balances", load_balance_index())
        logger.info(
            "Índice económico en RAM iniciado con %s usuario(s).",
            indexed_balances,
        )
        indexed_names = await _startup_step("Caché de nombres", load_member_names())
        logger.info("Caché ligera de nombres iniciada con %s usuario(s).", indexed_names)
        for label, startup_call in (
            ("Restricciones de apuestas", ensure_wager_constraints),
            ("Tabla de configuración de juegos", create_game_config_table),
            ("Configuración general", load_game_config),
            ("Configuración Dados", load_dados_config),
            ("Configuración Memo", load_memo_config),
            ("Caché de artículos", load_items_to_cache),
            ("Caché de cargos", load_cargos_to_cache),
            ("Restricciones de artículos", load_item_role_restrictions_to_cache),
            ("Configuración Collect", load_collect_config_to_cache),
            ("Configuración Veterano", load_veterano_config_to_cache),
            ("Configuración Saboteador", load_saboteador_config_to_cache),
        ):
            await _startup_step(label, startup_call())
        await _startup_step("Carga de extensiones", load_modules())
        try:
            await bot.start(TOKEN)
        except Exception as error:
            safe_error = _safe_startup_error(error)
            logger.error(
                "[STARTUP:ERROR] Discord: %s: %s",
                type(error).__name__, safe_error,
            )
            raise RuntimeError(
                f"Inicio fallido en Discord: {type(error).__name__}: {safe_error}"
            ) from None
        finally:
            await cache.flush_to_db()
            logger.info("Flush final completado al cerrar.")

    asyncio.run(main())


if __name__ == "__main__":
    run_bot()
