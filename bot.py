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
    load_collect_config_to_cache, delete_cargo_temporal,
    create_game_config_table, load_game_config, load_dados_config
)
from core import cache
from core.config import AYUDA_CHANNEL_ID

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


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
    await bot.load_extension("modules.registro_ruleta")
    await bot.load_extension("modules.memo")


async def check_cargos_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        await asyncio.sleep(300)
        now = time.time()
        cargos = cache.get_cargos_cache()
        vencidos = []

        for user_id, lista in list(cargos.items()):
            for cargo in lista:
                if cargo["expira_en"] <= now:
                    vencidos.append((user_id, cargo["guild_id"], cargo["rol_id"]))

        for user_id, guild_id, rol_id in vencidos:
            try:
                guild = bot.get_guild(guild_id)
                if not guild:
                    continue
                member = guild.get_member(user_id)
                if not member:
                    member = await guild.fetch_member(user_id)
                role = guild.get_role(rol_id)
                if role and role in member.roles:
                    await member.remove_roles(role)
            except Exception as e:
                logger.warning(f"Error removiendo cargo {rol_id} a {user_id}: {e}")
            finally:
                await delete_cargo_temporal(user_id, rol_id)

        if vencidos:
            logger.info(f"Cargos temporales vencidos removidos: {len(vencidos)}")


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        await ctx.send(
            f"❌ Este comando no es válido, consulta **/ayuda_nave** en el canal <#{AYUDA_CHANNEL_ID}>",
            delete_after=10
        )
    elif isinstance(error, commands.CommandOnCooldown):
        return  # deja que el handler del módulo lo maneje
    else:
        raise error

async def shutdown():
    logger.warning("Apagando bot — flusheando caché a DB...")
    await cache.flush_to_db()
    logger.info("Caché flusheada correctamente.")
    await bot.close()

@bot.event
async def on_ready():
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
    asyncio.create_task(check_cargos_loop())
    logger.info("Task de cargos temporales iniciada | Revisión cada 5 minutos")
    logger.info("\n⫷ 𝙋𝙐𝙍𝙋𝙇𝙀𝙅𝘼𝘾𝙆 𝙀𝙉 𝙇𝙄𝙉𝙀𝘼 ⫸\n")


def run_bot():
    async def main():
        # ── Handler de señal SIGTERM (ej: SquareCloud apagando el proceso) ──
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.create_task(shutdown()))

        await init_db()
        await create_game_config_table()
        await load_game_config()
        await load_dados_config()
        await load_items_to_cache()
        await load_cargos_to_cache()
        await load_collect_config_to_cache()
        await load_modules()
        try:
            await bot.start(TOKEN)
        finally:
            await cache.flush_to_db()
            logger.info("Flush final completado al cerrar.")

    asyncio.run(main())
