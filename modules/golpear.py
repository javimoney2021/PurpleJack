import discord
import asyncio
import random
from discord.ext import commands
from discord import app_commands
from core.database import update_balance
from core.config import COIN

# ── CONFIG BASE ────────────────────────────────────────
GOLPEAR_GIF = "https://pub-a09b3609b6b34dfab5c7aa7742cd1a8a.r2.dev/Purple%20jack%20Harcode/cofre1.gif"
MIN_COINS = 150
MAX_COINS = 800
MAX_GOLPES = 3
COFRE_TIMEOUT = 10
STAFF_ROLE = "Equipo de Eventos"

# ── ESTADO GLOBAL ──────────────────────────────────────
_golpear_config = {
    "activo": False,
    "canal_id": None,
    "min_time": 600,
    "max_time": 3600,
}


def is_staff():
    async def predicate(interaction: discord.Interaction):
        role = discord.utils.get(interaction.user.roles, name=STAFF_ROLE)
        if not role:
            await interaction.response.send_message(
                "❌ No tienes permisos para usar este comando.", ephemeral=True
            )
            return False
        return True
    return app_commands.check(predicate)


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

        monto = random.randint(MIN_COINS, MAX_COINS)
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
        description="¡Un cofre misterioso ha aparecido!\n\n¡Sé el primero en golpearlo!",
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

    @app_commands.command(name="golpear_alternar", description="Activa o desactiva el sistema de cofres")
    @is_staff()
    async def golpear_alternar(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)

        if not _golpear_config["canal_id"]:
            return await interaction.followup.send(
                "❌ Primero configura el canal con **/golpear_editar**.", ephemeral=True
            )

        _golpear_config["activo"] = not _golpear_config["activo"]
        estado = "✅ Activado" if _golpear_config["activo"] else "🔴 Desactivado"
        canal = self.bot.get_channel(_golpear_config["canal_id"])
        canal_txt = canal.mention if canal else f"<#{_golpear_config['canal_id']}>"

        await interaction.followup.send(
            f"💥 Sistema de Cofres: **{estado}**\n"
            f"📌 Canal: {canal_txt}\n"
            f"⏱️ Intervalo: **{_golpear_config['min_time'] // 60}m** — **{_golpear_config['max_time'] // 60}m**",
            ephemeral=False
        )

    @app_commands.command(name="golpear_editar", description="Configura canal y tiempos del sistema de cofres")
    @app_commands.describe(
        canal="Canal donde aparecerán los cofres",
        min_time="Tiempo mínimo entre cofres (en minutos)",
        max_time="Tiempo máximo entre cofres (en minutos)"
    )
    @is_staff()
    async def golpear_editar(self, interaction: discord.Interaction,
                              canal: discord.TextChannel,
                              min_time: int,
                              max_time: int):
        await interaction.response.defer(ephemeral=False)

        if min_time <= 0 or max_time <= 0:
            return await interaction.followup.send("❌ Los tiempos deben ser mayores a 0.", ephemeral=True)
        if min_time >= max_time:
            return await interaction.followup.send("❌ El tiempo mínimo debe ser menor al máximo.", ephemeral=True)

        _golpear_config["canal_id"] = canal.id
        _golpear_config["min_time"] = min_time * 60
        _golpear_config["max_time"] = max_time * 60

        await interaction.followup.send(
            f"✅ Sistema de Cofres configurado:\n"
            f"📌 Canal: {canal.mention}\n"
            f"⏱️ Intervalo: **{min_time}m** — **{max_time}m**",
            ephemeral=False
        )

    @app_commands.command(name="golpear_test", description="Spawna un cofre de prueba en un canal")
    @app_commands.describe(canal="Canal donde aparecerá el cofre de prueba")
    @is_staff()
    async def golpear_test(self, interaction: discord.Interaction, canal: discord.TextChannel):
        await interaction.response.defer(ephemeral=True)
        await spawn_cofre(canal)
        await interaction.followup.send(
            f"✅ Cofre de prueba enviado a {canal.mention}.", ephemeral=True
        )


async def setup(bot):
    await bot.add_cog(Golpear(bot))
    asyncio.create_task(golpear_loop(bot))
