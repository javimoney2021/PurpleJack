import discord
import asyncio
import logging
import random
from discord.ext import commands
from core.database import update_balance
from core.config import COIN, STAFF_ROLE

logger = logging.getLogger(__name__)

# ── CONFIG BASE ────────────────────────────────────────
GOLPEAR_GIF = "https://pub-a09b3609b6b34dfab5c7aa7742cd1a8a.r2.dev/Purple%20jack%20Harcode/cofre1.gif"
MAX_GOLPES = 3
COFRE_TIMEOUT = 6

# ── ESTADO GLOBAL ──────────────────────────────────────
_golpear_config = {
    "activo": False,
    "canal_id": None,
    "min_time": 600,
    "max_time": 3600,
    "min_ganancia": 150,
    "max_ganancia": 800,
}


# ── VIEW ───────────────────────────────────────────────
class GolpearView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=COFRE_TIMEOUT)
        self.golpeadores = []
        self.terminado = False
        self.message = None

    async def on_timeout(self):
        if self.terminado:
            return
        self.terminado = True
        for item in self.children:
            item.disabled = True

        if self.golpeadores:
            await asyncio.sleep(3)
            lineas = "\n".join(
                f"**{u.display_name}** obtuvo **{m}** {COIN}"
                for u, m in self.golpeadores
            )
            embed = discord.Embed(
                title="💥 ¡Cofre Destruido!",
                description=f"Los aventureros que golpearon primero:\n\n{lineas}",
                color=discord.Color.gold()
            )
            embed.set_image(url=GOLPEAR_GIF)
            embed.set_footer(text="Este mensaje se eliminará en breve.")
        else:
            embed = discord.Embed(
                title="💨 Cofre Vencido",
                description="El cofre desapareció... Nadie golpeó a tiempo.",
                color=discord.Color.dark_gray()
            )
            embed.set_image(url=GOLPEAR_GIF)

        try:
            await self.message.edit(embed=embed, view=self)
        except Exception:
            pass

        await asyncio.sleep(240)
        try:
            await self.message.delete()
        except Exception:
            pass

    @discord.ui.button(label="💥 Golpear", style=discord.ButtonStyle.danger)
    async def golpear(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.terminado:
            return await interaction.response.send_message(
                "❌ El cofre ya fue reclamado.", ephemeral=True
            )

        if any(u.id == interaction.user.id for u, _ in self.golpeadores):
            return await interaction.response.send_message(
                "❌ Ya golpeaste este cofre.", ephemeral=True
            )

        monto = random.randint(_golpear_config["min_ganancia"], _golpear_config["max_ganancia"])
        self.golpeadores.append((interaction.user, monto))
        await update_balance(interaction.user.id, monto)
        await interaction.response.defer()

        if len(self.golpeadores) >= MAX_GOLPES:
            await self._cerrar(interaction.message)

    async def _cerrar(self, message):
        if self.terminado:
            return
        self.terminado = True
        self.stop()

        for item in self.children:
            item.disabled = True

        await asyncio.sleep(3)

        lineas = "\n".join(
            f"**{u.display_name}** obtuvo **{m}** {COIN}"
            for u, m in self.golpeadores
        )

        embed = discord.Embed(
            title="💥 ¡Cofre Destruido!",
            description=f"Los aventureros que golpearon primero:\n\n{lineas}",
            color=discord.Color.gold()
        )
        embed.set_image(url=GOLPEAR_GIF)
        embed.set_footer(text="Ganancias entregadas....")

        try:
            await message.edit(embed=embed, view=self)
        except Exception:
            pass

        await asyncio.sleep(240)
        try:
            await message.delete()
        except Exception:
            pass


# ── SPAWN ──────────────────────────────────────────────
async def spawn_cofre(canal: discord.TextChannel):
    embed = discord.Embed(
        title="💥 ¡Cofre Misterioso!",
        description="¡Un cofre misterioso ha aparecido!\n\n¡Sé de los primeros en golpearlo!",
        color=discord.Color.purple()
    )
    embed.set_image(url=GOLPEAR_GIF)
    embed.set_footer(text="¡Date prisa antes de que desaparezca!")

    view = GolpearView()
    msg = await canal.send(embed=embed, view=view)
    view.message = msg


# ── TASK ───────────────────────────────────────────────
async def golpear_loop(bot):
    await bot.wait_until_ready()
    logger.info("golpear_loop iniciado y listo.")

    # Bug #1 fix: cargar config directamente desde la DB al arrancar el loop,
    # evitando el problema del dict huérfano causado por el orden de imports en bot.py
    from core.database import load_golpear_config_to_cache
    await load_golpear_config_to_cache()
    logger.info("golpear_loop: config cargada desde DB al iniciar.")

    while True:
        try:
            # ── Esperar mientras esté inactivo o sin canal ─────────
            if not _golpear_config["activo"] or not _golpear_config["canal_id"]:
                await asyncio.sleep(30)
                continue

            # ── Dormir el intervalo aleatorio configurado ──────────
            wait = random.randint(_golpear_config["min_time"], _golpear_config["max_time"])
            logger.info(f"golpear_loop: próximo cofre en {wait}s.")
            await asyncio.sleep(wait)

            # ── Re-verificar que sigue activo tras el sleep ────────
            if not _golpear_config["activo"]:
                logger.info("golpear_loop: sistema desactivado durante el intervalo, saltando spawn.")
                continue

            # ── Resolver el canal (cache primero, fetch como fallback) ─
            canal_id = _golpear_config["canal_id"]
            canal = bot.get_channel(canal_id)
            if canal is None:
                try:
                    canal = await bot.fetch_channel(canal_id)
                    logger.info(f"golpear_loop: canal {canal_id} no estaba en cache, obtenido via fetch.")
                except Exception as e:
                    logger.warning(f"golpear_loop: no se pudo obtener el canal {canal_id}: {e} — reintentando en 60s.")
                    await asyncio.sleep(60)
                    continue

            # ── Spawn ──────────────────────────────────────────────
            logger.info(f"golpear_loop: spawneando cofre en #{canal.name} ({canal_id}).")
            await spawn_cofre(canal)
            logger.info("golpear_loop: cofre enviado correctamente.")

        except asyncio.CancelledError:
            # La tarea fue cancelada intencionalmente (cog_unload) — salir limpio
            logger.info("golpear_loop: tarea cancelada, cerrando loop.")
            raise

        except Exception as e:
            # Cualquier otro error (permisos, API, etc.) — loguear y reintentar
            logger.error(f"golpear_loop: error inesperado: {e} — reintentando en 60s.", exc_info=True)
            await asyncio.sleep(60)


# ── COG ────────────────────────────────────────────────
_loop_task: asyncio.Task | None = None  # Variable a nivel de módulo para evitar tareas duplicadas en recargas


class Golpear(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def cog_unload(self):
        global _loop_task
        if _loop_task and not _loop_task.done():
            _loop_task.cancel()
            logger.info("golpear_loop: tarea cancelada por cog_unload.")


async def setup(bot):
    global _loop_task
    await bot.add_cog(Golpear(bot))
    # Bug #2 fix: crear la tarea DESPUÉS de add_cog, siguiendo el patrón del proyecto
    if _loop_task is None or _loop_task.done():
        _loop_task = asyncio.create_task(golpear_loop(bot))
