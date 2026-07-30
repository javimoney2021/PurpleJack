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
    recover_pending_wagers, ensure_wager_constraints
)
from core import cache
from core.config import AYUDA_CHANNEL_ID, LOG_CHANNEL_ID, STAFF_ROLE_ID

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

async def get_prefix(bot, message):
    """Normaliza el prefijo '!' aceptando '!comando' y '! comando' indistintamente."""
    return ["! ", "!"]

bot = commands.Bot(command_prefix=get_prefix, intents=intents, help_command=None)
evento_flush_task = None
cargos_task = None


async def load_modules():
    await bot.load_extension("modules.economy")
    await bot.load_extension("modules.basic_games")
    await bot.load_extension("modules.staff")
    await bot.load_extension("modules.roulette")
    await bot.load_extension("modules.russian_roulette")
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
                reason="Expiración de rol temporal otorgado por un item",
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


@bot.event
async def on_command_error(ctx, error):
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

AUTHORIZED_GUILD_ID = 980073134411644939

@bot.event
async def on_ready():
    global evento_flush_task, cargos_task
    logger.info(f"Bot conectado como {bot.user}")
    logger.info("Caché iniciada | Flush cada 5 minutos")
    logger.info(f"Servidores activos: {len(bot.guilds)}")

    # ── Verificar guilds autorizadas al inicio ─────────────
    for guild in bot.guilds:
        if guild.id != AUTHORIZED_GUILD_ID:
            logger.warning(
                f"Guild no autorizada detectada al iniciar | "
                f"Nombre: {guild.name} | ID: {guild.id} | "
                f"Miembros: {guild.member_count} | "
                f"Fecha: {discord.utils.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"
            )
            logger.warning(f"Abandonando guild no autorizada: {guild.name} ({guild.id})")
            await guild.leave()

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
    logger.info("\n⫷ 𝙋𝙐𝙍𝙋𝙇𝙀𝙅𝘼𝘾𝙆 𝙀𝙉 𝙇𝙄𝙉𝙀𝘼 ⫸\n")


@bot.event
async def on_guild_join(guild: discord.Guild):
    if guild.id != AUTHORIZED_GUILD_ID:
        logger.warning(
            f"Intento de instalación no autorizado detectado | "
            f"Nombre: {guild.name} | ID: {guild.id} | "
            f"Miembros: {guild.member_count} | "
            f"Fecha: {discord.utils.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"
        )
        logger.warning(f"Abandonando guild no autorizada: {guild.name} ({guild.id})")
        await guild.leave()


def run_bot():
    async def main():
        # ── Handler de señal SIGTERM (ej: SquareCloud apagando el proceso) ──
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.create_task(shutdown()))

        await init_db()
        reembolsadas = await recover_pending_wagers()
        if reembolsadas:
            logger.warning(
                "Se reembolsaron %s apuestas pendientes de una ejecución anterior.",
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
