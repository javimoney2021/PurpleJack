"""Comandos slash independientes que reutilizan los flujos públicos existentes."""

import asyncio
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

from core import cache
from core.rankings import get_balance_ranking
from modules.player_panels import PanelContext

logger = logging.getLogger(__name__)


class RobTargetSelect(discord.ui.Select):
    def __init__(self, candidates: list[tuple[int, int, int]]):
        options = []
        for position, user_id, balance in candidates:
            name = " ".join((cache.get_member_name(user_id) or f"Usuario {user_id}").split())[:80]
            options.append(discord.SelectOption(
                label=f"{position}. {name}",
                value=str(user_id),
                description=f"Balance: {balance:,} PurpleCoins",
            ))
        super().__init__(
            placeholder="Selecciona a quién intentarás robar",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        view: RobTargetView = self.view
        if interaction.user.id != view.owner_id:
            await interaction.response.send_message("❌ Este selector no te pertenece.", ephemeral=True)
            return
        from bot import check_player_access
        if not await check_player_access(interaction):
            return
        async with view.lock:
            if view.used:
                await interaction.response.send_message(
                    "Ya seleccionaste un objetivo. Usa /rob para abrir otro panel.", ephemeral=True,
                )
                return
            target_id = int(self.values[0])
            current_top = {
                user_id for user_id, balance in get_balance_ranking(limit=10)
                if user_id != view.owner_id and balance >= 5000
            }
            if target_id not in view.candidate_ids or target_id not in current_top:
                await interaction.response.edit_message(
                    content="El Top cambió. Usa /rob para elegir un objetivo actualizado.",
                    embed=None,
                    view=None,
                )
                view.stop()
                return
            view.used = True
            await interaction.response.edit_message(
                content="🎯 Objetivo seleccionado. Preparando el robo…",
                embed=None,
                view=None,
            )
            view.stop()

        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("Solo puedes robar en el servidor.", ephemeral=True)
            return
        target = guild.get_member(target_id)
        if target is None:
            try:
                target = await guild.fetch_member(target_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                await interaction.followup.send(
                    "Ese usuario ya no está disponible en el servidor. Abre /rob de nuevo.",
                    ephemeral=True,
                )
                return
        if target.bot:
            await interaction.followup.send("No puedes robar a un bot.", ephemeral=True)
            return
        cog = interaction.client.get_cog("PlayerCommands")
        if cog is None:
            await interaction.followup.send("El sistema de robos no está disponible.", ephemeral=True)
            return
        await cog._execute(interaction, "Rob", "rob", target)


class RobTargetView(discord.ui.View):
    def __init__(self, owner_id: int, candidates: list[tuple[int, int, int]]):
        super().__init__(timeout=60)
        self.owner_id = owner_id
        self.candidate_ids = {user_id for _, user_id, _ in candidates}
        self.used = False
        self.lock = asyncio.Lock()
        self.message = None
        self.add_item(RobTargetSelect(candidates))

    async def on_timeout(self):
        if self.used:
            return
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except (discord.HTTPException, discord.NotFound):
                pass


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

    @app_commands.command(name="rob", description="Elige un objetivo del Top para intentar robar")
    async def rob(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("Solo puedes robar en el servidor.", ephemeral=True)
            return
        candidates = [
            (position, user_id, balance)
            for position, (user_id, balance) in enumerate(get_balance_ranking(limit=10), start=1)
            if user_id != interaction.user.id and balance >= 5000
        ]
        if not candidates:
            await interaction.response.send_message(
                "No hay objetivos elegibles en el Top 10 ahora mismo (mínimo 5.000 PurpleCoins).",
                ephemeral=True,
            )
            return
        view = RobTargetView(interaction.user.id, candidates)
        await interaction.response.defer(ephemeral=True)
        view.message = await interaction.followup.send(
            embed=discord.Embed(
                title="🔫 Objetivos del Top 10",
                description=(
                    "Elige a un jugador para intentar robarle. "
                    "El saldo y la protección se comprobarán al ejecutar el robo. "
                    "Este selector vence en 60 segundos."
                ),
                color=discord.Color.dark_purple(),
            ),
            view=view,
            ephemeral=True,
            wait=True,
        )

    @app_commands.command(name="carrera", description="Convoca una carrera en el canal")
    @app_commands.describe(apuesta="PurpleCoins que apuestas en la carrera")
    async def carrera(self, interaction: discord.Interaction, apuesta: int):
        await self._execute(interaction, "Carrera", "carrera", apuesta)

    @app_commands.command(name="retar", description="Reta a otro miembro a un duelo")
    @app_commands.describe(usuario="Miembro al que retar", apuesta="PurpleCoins en el banco")
    async def retar(self, interaction: discord.Interaction,
                    usuario: discord.Member, apuesta: int):
        await self._execute(interaction, "Duels", "retar", usuario, str(apuesta))


async def setup(bot):
    await bot.add_cog(PlayerCommands(bot))
