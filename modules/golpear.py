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

        await asyncio.sleep(15)
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
        embed.set_footer(text="Este mensaje se eliminará en 15 segundos.")

        try:
            await message.edit(embed=embed, view=self)
        except Exception:
            pass

        await asyncio.sleep(15)
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
    while True:
        if not _golpear_config["activo"] or not _golpear_config["canal_id"]:
            await asyncio.sleep(30)
            continue

        wait = random.randint(_golpear_config["min_time"], _golpear_config["max_time"])
        await asyncio.sleep(wait)

        if not _golpear_config["activo"]:
            continue

        canal = bot.get_channel(_golpear_config["canal_id"])
        if not canal:
            continue

        await spawn_cofre(canal)


# ── COG ────────────────────────────────────────────────
class Golpear(commands.Cog):
    def __init__(self, bot):
        self.bot = bot


async def setup(bot):
    await bot.add_cog(Golpear(bot))
    asyncio.create_task(golpear_loop(bot))
