import discord
import asyncio
import logging
import random
import time
from discord.ext import commands
from core.database import get_user, update_balance, update_bank
from core.config import COIN

logger = logging.getLogger(__name__)

# ── CONFIG ─────────────────────────────────────────────
DUEL_TIMEOUT = 20  # segundos para aceptar/rechazar
ROUND_TIMEOUT = 5  # segundos que la espada está visible
TOTAL_ROUNDS = 9
GRID_SIZE = 5
RETAL_THUMBNAIL = "https://pub-a09b3609b6b34dfab5c7aa7742cd1a8a.r2.dev/Purple%20jack%20Harcode/Kill.png"
WIN_THUMBNAIL = "https://pub-a09b3609b6b34dfab5c7aa7742cd1a8a.r2.dev/Purple%20jack%20Harcode/win.png"
ESPANOL_ROLE = "Español"

# ── GLOBAL STATE ───────────────────────────────────────
_active_duels = set()  # {guild_id} para evitar múltiples duelos por servidor
_duel_cooldowns = {}  # {guild_id: cooldown_seconds}
DEFAULT_DUEL_COOLDOWN = 600  # 10 minutos
_last_duel_times = {}  # {guild_id: timestamp}


class AcceptDuelView(discord.ui.View):
    def __init__(self, retador_id, retado_id, monto, ctx):
        super().__init__(timeout=DUEL_TIMEOUT)
        self.retador_id = retador_id
        self.retado_id = retado_id
        self.monto = monto
        self.ctx = ctx

    async def on_timeout(self):
        try:
            await self.message.delete()
        except discord.HTTPException:
            pass
        try:
            await self.ctx.send(
                f"⚔️ {self.ctx.author.mention} **Reto anulado (Sin Respuesta)** Intenta retar a otra persona.",
                delete_after=15
            )
        except discord.HTTPException:
            pass
        self.ctx.command.reset_cooldown(self.ctx)

    @discord.ui.button(label="Aceptar Reto", style=discord.ButtonStyle.success)
    async def aceptar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.retado_id:
            return await interaction.response.send_message("❌ Solo el retado puede aceptar.", ephemeral=True)

        # Verificar saldo del retado
        retado_data = await get_user(self.retado_id)
        if retado_data["bank"] < self.monto:
            return await interaction.response.send_message(
                f"❌ No tienes suficiente en banco. Necesitas **{self.monto}** {COIN}.",
                ephemeral=True
            )

        # Verificar saldo del retador nuevamente
        retador_data = await get_user(self.retador_id)
        if retador_data["bank"] < self.monto:
            return await interaction.response.send_message(
                f"❌ El retador ya no tiene suficiente saldo.",
                ephemeral=True
            )

        # Descontar
        await update_bank(self.retador_id, -self.monto)
        await update_bank(self.retado_id, -self.monto)

        # Detener la vista de aceptación para evitar timeouts posteriores
        self.stop()

        # Auto-eliminar mensaje de reto en 3 segundos
        async def delete_message():
            await asyncio.sleep(3)
            try:
                await self.message.delete()
            except discord.HTTPException:
                pass
        
        asyncio.create_task(delete_message())

        # Mensaje anunciando el inicio sin embed
        await interaction.response.defer()
        msg_anuncio = await self.ctx.send(f"⚔️ La batalla entre <@{self.retador_id}> y <@{self.retado_id}> Comenzará en segundos.... ⚔️")
        
        # Cerrar permisos de escritura para el rol Español
        rol_espanol = discord.utils.get(self.ctx.guild.roles, name=ESPANOL_ROLE)
        if rol_espanol:
            await self.ctx.channel.set_permissions(rol_espanol, send_messages=False, view_channel=True, read_message_history=True, add_reactions=True)
        
        await asyncio.sleep(3)
        await msg_anuncio.delete()

        # Iniciar minijuego
        duel_view = DuelGameView(self.retador_id, self.retado_id, self.monto * 2, self.ctx.guild.id, self.ctx.channel, self.ctx.guild)
        embed = discord.Embed(
            title="⚔️ Duelo Iniciado",
            description=f"¡Comienza el duelo entre <@{self.retador_id}> y <@{self.retado_id}>!\n\n**Pozo total:** {self.monto * 2} {COIN}\n\nRonda 1/{TOTAL_ROUNDS}",
            color=discord.Color.blue()
        )
        embed.set_thumbnail(url=RETAL_THUMBNAIL)
        msg_duelo = await self.ctx.send(embed=embed, view=duel_view)
        duel_view.message = msg_duelo
        await duel_view.start_game()

    @discord.ui.button(label="Rechazar Reto", style=discord.ButtonStyle.danger)
    async def rechazar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.retado_id:
            return await interaction.response.send_message("❌ Solo el retado puede rechazar.", ephemeral=True)

        embed = discord.Embed(
            title="❌ Reto Rechazado",
            description=f"<@{self.retado_id}> Se siente indispuesto/a hoy y ha rechazado este duelo...",
            color=discord.Color.red()
        )
        self.stop()
        await interaction.response.edit_message(embed=embed, view=None)
        
        # Resetear cooldown para que otros puedan retar
        self.ctx.command.reset_cooldown(self.ctx)
        
        # Auto-eliminar mensaje de reto en 3 segundos
        async def delete_message():
            await asyncio.sleep(3)
            try:
                await self.message.delete()
            except discord.HTTPException:
                pass
        
        asyncio.create_task(delete_message())


class DuelButton(discord.ui.Button):
    def __init__(self, x, y, duel_view):
        super().__init__(label="⬜", style=discord.ButtonStyle.secondary, row=y)
        self.x = x
        self.y = y
        self.duel_view = duel_view

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id not in [self.duel_view.retador_id, self.duel_view.retado_id]:
            return await interaction.response.send_message("❌ No participas en este duelo.", ephemeral=True)

        if not self.duel_view.sword_visible or self.duel_view.clicked:
            await interaction.response.defer()
            return

        if (self.x, self.y) != self.duel_view.sword_pos:
            await interaction.response.defer()
            return

        # Punto ganado
        self.duel_view.clicked = True
        if interaction.user.id == self.duel_view.retador_id:
            self.duel_view.retador_score += 1
        else:
            self.duel_view.retado_score += 1

        await interaction.response.defer()

        # Ocultar espada inmediatamente
        await self.duel_view.hide_sword()


class DuelGameView(discord.ui.View):
    def __init__(self, retador_id, retado_id, pozo, guild_id, channel=None, guild=None):
        super().__init__(timeout=300)  # 5 min total
        self.retador_id = retador_id
        self.retado_id = retado_id
        self.pozo = pozo
        self.guild_id = guild_id
        self.channel = channel
        self.guild = guild
        self.round = 0
        self.retador_score = 0
        self.retado_score = 0
        self.sword_visible = False
        self.sword_pos = None
        self.clicked = False
        self.game_task = None
        self.message = None

        # Crear cuadrícula 5x5
        for y in range(GRID_SIZE):
            for x in range(GRID_SIZE):
                self.add_item(DuelButton(x, y, self))

    async def start_game(self):
        _active_duels.add(self.guild_id)
        self.game_task = asyncio.create_task(self.run_game())

    async def run_game(self):
        try:
            for round_num in range(1, TOTAL_ROUNDS + 1):
                self.round = round_num
                self.clicked = False
                self.sword_visible = False

                # Actualizar embed
                embed = discord.Embed(
                    title="⚔️ Duelo en Progreso",
                    description=f"<@{self.retador_id}> vs <@{self.retado_id}>\n\n**Pozo:** {self.pozo} {COIN}\n**Ronda:** {round_num}/{TOTAL_ROUNDS}\n\n🧐 Preparate para Atacar!",
                    color=discord.Color.blue()
                )
                await self.message.edit(embed=embed, view=self)

                # Esperar intervalo aleatorio (2-5 seg)
                await asyncio.sleep(random.uniform(2, 5))

                # Mostrar espada
                self.sword_pos = (random.randint(0, GRID_SIZE-1), random.randint(0, GRID_SIZE-1))
                self.sword_visible = True
                self.clicked = False

                # Actualizar botones
                for item in self.children:
                    if isinstance(item, DuelButton):
                        if (item.x, item.y) == self.sword_pos:
                            item.label = "⚔️"
                            item.style = discord.ButtonStyle.danger
                        else:
                            item.label = "⬜"
                            item.style = discord.ButtonStyle.secondary

                embed.description = f"<@{self.retador_id}> vs <@{self.retado_id}>\n\n**Pozo:** {self.pozo} {COIN}\n**Ronda:** {round_num}/{TOTAL_ROUNDS}\n\n😈 Ataca !"
                await self.message.edit(embed=embed, view=self)

                # Esperar que alguien clickee o timeout
                start_time = asyncio.get_event_loop().time()
                while not self.clicked and (asyncio.get_event_loop().time() - start_time) < ROUND_TIMEOUT:
                    await asyncio.sleep(0.1)

                # Ocultar espada
                await self.hide_sword()

                # Pequeña pausa
                await asyncio.sleep(1)

            # Fin del juego
            await self.end_game()

        except Exception as e:
            logger.error(f"Error en duelo: {e}")
            await self.end_game()

    async def hide_sword(self):
        self.sword_visible = False
        for item in self.children:
            if isinstance(item, DuelButton):
                item.label = "⬜"
                item.style = discord.ButtonStyle.secondary
        try:
            await self.message.edit(view=self)
        except discord.HTTPException:
            pass

    async def end_game(self):
        _active_duels.discard(self.guild_id)

        if self.retador_score > self.retado_score:
            winner_id = self.retador_id
            winner_name = f"<@{self.retador_id}>"
        elif self.retado_score > self.retador_score:
            winner_id = self.retado_id
            winner_name = f"<@{self.retado_id}>"
        else:
            # Empate (aunque con 7 rondas es improbable)
            winner_id = None
            winner_name = "Empate"

        if winner_id:
            await update_bank(winner_id, self.pozo)

        embed = discord.Embed(
            title="🏆 Duelo Finalizado",
            description=f"**Ganador:** {winner_name}\n\n**Puntuación:**\n<@{self.retador_id}>: {self.retador_score}\n<@{self.retado_id}>: {self.retado_score}\n\n**Recompensa:** {self.pozo} {COIN} al banco del ganador.",
            color=discord.Color.green() if winner_id else discord.Color.yellow()
        )
        embed.set_thumbnail(url=WIN_THUMBNAIL)

        # Desactivar botones
        for item in self.children:
            item.disabled = True

        try:
            await self.message.edit(embed=embed, view=self)
        except discord.HTTPException:
            pass

        # Esperar 6 segundos y eliminar el mensaje
        await asyncio.sleep(6)
        try:
            await self.message.delete()
        except discord.HTTPException:
            pass
        
        # Abrir permisos de escritura para el rol Español
        if self.channel and self.guild:
            rol_espanol = discord.utils.get(self.guild.roles, name=ESPANOL_ROLE)
            if rol_espanol:
                await self.channel.set_permissions(rol_espanol, send_messages=True, view_channel=True, read_message_history=True, add_reactions=True)
            # Anunciar fin de batalla
            await self.channel.send("Batalla Finalizada, Se Retoma la Actividad!")

        if self.game_task:
            self.game_task.cancel()


class Duels(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="retar")
    async def retar(self, ctx, usuario: discord.Member, monto: int):
        # Check cooldown
        cooldown = _duel_cooldowns.get(ctx.guild.id, DEFAULT_DUEL_COOLDOWN)
        last_time = _last_duel_times.get(ctx.guild.id, 0)
        if time.time() - last_time < cooldown:
            remaining = cooldown - (time.time() - last_time)
            timestamp = int(ctx.message.created_at.timestamp()) + int(remaining)
            await ctx.send(f"🚀 La Arena de combate esta ocupada por Jugadores de otros universos Intenta <t:{timestamp}:R>")
            return
        _last_duel_times[ctx.guild.id] = time.time()

        if ctx.author.id == usuario.id:
            return await ctx.send("❌ No puedes retarte a ti mismo.")

        if ctx.guild.id in _active_duels:
            return await ctx.send("❌ La arena de duelo está ocupada. Espera a que termine el duelo actual.")

        if monto <= 0:
            return await ctx.send("❌ El monto debe ser mayor a 0.")

        # Verificar saldo del retador
        retador_data = await get_user(ctx.author.id)
        if retador_data["bank"] < monto:
            return await ctx.send(f"❌ No tienes suficiente en banco. Necesitas **{monto}** {COIN}.")

        embed = discord.Embed(
            title="⚔️ Reto de Duelo",
            description=f"<@{usuario.id}> Has recibido un duelo por parte de <@{ctx.author.id}> por **{monto}** {COIN}.\n\n¿Aceptas?",
            color=discord.Color.orange()
        )
        embed.set_thumbnail(url=RETAL_THUMBNAIL)

        await ctx.send(f"{usuario.mention} Ha sido retado a un duelo.")
        view = AcceptDuelView(ctx.author.id, usuario.id, monto, ctx)
        message = await ctx.send(embed=embed, view=view)
        view.message = message

    @retar.error
    async def retar_error(self, ctx, error):
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f"{ctx.author.mention} Formato correcto **!retar usuario monto**")
        elif isinstance(error, commands.CommandOnCooldown):
            retry_after = error.retry_after
            timestamp = int(ctx.message.created_at.timestamp()) + int(retry_after)
            await ctx.send(f"🚀 La Arena de combate esta ocupada por Jugadores de otros universos Intenta <t:{timestamp}:R>")
        else:
            raise error


async def setup(bot):
    await bot.add_cog(Duels(bot))