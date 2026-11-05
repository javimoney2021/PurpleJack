"""Comandos slash independientes que reutilizan los flujos públicos existentes."""

import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

from modules.player_panels import PanelContext

logger = logging.getLogger(__name__)


class PlayerCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._tienda_last_used: dict[int, float] = {}

    async def _execute(self, interaction: discord.Interaction, cog_name: str,
                       command_name: str, *args):
        if not interaction.response.is_done():
            await interaction.response.defer(thinking=True)
        context = PanelContext(interaction, private=False, first_followup=True)
        cog = self.bot.get_cog(cog_name)
        if cog is None:
            await interaction.followup.send("Esta función no está disponible.", ephemeral=True)
            return
        try:
            await getattr(cog, command_name)(context, *args)
        except Exception:
            logger.exception("Falló /%s para %s", command_name, interaction.user.id)
            await interaction.followup.send(
                "❌ No se pudo completar esta acción. Inténtalo más tarde.", ephemeral=True,
            )
            return
        if not context.replied:
            await interaction.followup.send(
                "La solicitud ya estaba en curso o fue procesada.", ephemeral=True,
            )

    @app_commands.command(name="work", description="Realiza una tarea y gana PurpleCoins")
    async def work(self, interaction: discord.Interaction):
        await self._execute(interaction, "BasicGames", "work")

    @app_commands.command(name="crime", description="Intenta un crimen económico")
    async def crime(self, interaction: discord.Interaction):
        await self._execute(interaction, "BasicGames", "crime")

    @app_commands.command(name="collect", description="Reclama las ganancias de tus roles")
    async def collect(self, interaction: discord.Interaction):
        await self._execute(interaction, "Collect", "collect")

    @app_commands.command(name="tienda", description="Abre la tienda de artículos")
    async def tienda(self, interaction: discord.Interaction):
        remaining = 30 - (time.monotonic() - self._tienda_last_used.get(interaction.user.id, 0))
        if remaining > 0:
            await interaction.response.send_message(
                f"⏳ Podrás abrir la tienda de nuevo en **{int(remaining) + 1}s**.",
                ephemeral=True,
            )
            return
        self._tienda_last_used[interaction.user.id] = time.monotonic()
        await interaction.response.defer(ephemeral=True)
        from core.database import get_all_items
        from modules.shop import TiendaLayout
        try:
            items = await get_all_items()
            if not items:
                await interaction.followup.send("🛒 La tienda está vacía por ahora.", ephemeral=True)
                return
            await interaction.followup.send(
                view=TiendaLayout(sorted(items, key=lambda item: item["precio"]),
                                  interaction.user.id, self.bot),
                ephemeral=True,
            )
        except Exception:
            logger.exception("No se pudo abrir /tienda para %s", interaction.user.id)
            self._tienda_last_used.pop(interaction.user.id, None)
            await interaction.followup.send("❌ No pude abrir la tienda.", ephemeral=True)

    @app_commands.command(name="rob", description="Intenta robar a otro miembro")
    @app_commands.describe(usuario="Miembro al que intentarás robar")
    async def rob(self, interaction: discord.Interaction,
                  usuario: discord.Member | None = None):
        if usuario is None:
            await interaction.response.send_message(
                "Indica **/rob usuario**. Para robar al autor de un mensaje, "
                "usa **Apps → Robar** sobre ese mensaje.", ephemeral=True,
            )
            return
        await self._execute(interaction, "Rob", "rob", usuario)

    @app_commands.command(name="carrera", description="Convoca una carrera en el canal")
    @app_commands.describe(apuesta="PurpleCoins que apuestas en la carrera")
    async def carrera(self, interaction: discord.Interaction, apuesta: int):
        await self._execute(interaction, "Carrera", "carrera", apuesta)

    @app_commands.command(name="retar", description="Reta a otro miembro a un duelo")
    @app_commands.describe(usuario="Miembro al que retar", apuesta="PurpleCoins en el banco")
    async def retar(self, interaction: discord.Interaction,
                    usuario: discord.Member, apuesta: int):
        await self._execute(interaction, "Duels", "retar", usuario, str(apuesta))


@app_commands.context_menu(name="Robar")
async def robar_mensaje(interaction: discord.Interaction, message: discord.Message):
    if interaction.guild is None:
        await interaction.response.send_message(
            "Solo puedes robar en el servidor.", ephemeral=True,
        )
        return
    await interaction.response.defer(thinking=True)
    target = message.author
    if not isinstance(target, discord.Member):
        try:
            target = await interaction.guild.fetch_member(target.id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            await interaction.followup.send(
                "No pude identificar al autor de ese mensaje en este servidor.",
                ephemeral=True,
            )
            return
    cog = interaction.client.get_cog("PlayerCommands")
    await cog._execute(interaction, "Rob", "rob", target)


async def setup(bot):
    await bot.add_cog(PlayerCommands(bot))
    bot.tree.add_command(robar_mensaje)


async def teardown(bot):
    bot.tree.remove_command("Robar", type=discord.AppCommandType.message)
