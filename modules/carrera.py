import discord
import asyncio
import random
import uuid
from discord.ext import commands
from discord import app_commands
from core.database import (
    get_user,
    reserve_wager,
    refund_wager,
    settle_wager_session,
    refund_wager_session,
    get_system_toggle,
    set_system_toggle,
)
from core.config import COIN, STAFF_ROLE

# ── CONFIG ─────────────────────────────────────────────
JOIN_TIMEOUT  = 12
MAX_PLAYERS   = 5
MAX_BET       = 5_000
GIF_URL       = "https://pub-a09b3609b6b34dfab5c7aa7742cd1a8a.r2.dev/Purple%20jack%20Harcode/carrera.gif"
GIF_DURATION  = 12    # segundos que se muestra el gif antes del resultado
RESULT_DELETE = 60   # segundos antes de borrar el embed final

# Nombre del bot de relleno
BOT_NAME = "Jack"
JACK_PLAYER_WIN_PROBABILITY = 0.30

# ── ESTADO GLOBAL ──────────────────────────────────────
_active_races   = set()
_carrera_activa = True


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


# ── JOIN VIEW ──────────────────────────────────────────
class JoinRaceView(discord.ui.View):
    def __init__(self, author, monto, session_id, author_wager_id):
        super().__init__(timeout=JOIN_TIMEOUT)
        self.author  = author
        self.monto   = monto
        self.players = [author]   # solo jugadores reales
        self.wagers = {author.id: author_wager_id}
        self.session_id = session_id
        self.message = None
        self.started = False
        self.join_lock = asyncio.Lock()

    def build_embed(self, countdown=None):
        inscritos = "\n".join(
            f"🏎️ {p.display_name}"
            for p in self.players
        )
        desc = (
            f"{self.author.mention} ha convocado una carrera por **{self.monto}** {COIN}\n\n"
            f"Presiona el botón para participar con la misma apuesta.\n\n"
            f"**Inscritos ({len(self.players)}/{MAX_PLAYERS}):**\n{inscritos}\n\n"
        )
        if countdown is not None and countdown > 0:
            desc += f"⏳ Inscripciones abiertas por **{countdown}s**"
        elif countdown == 0:
            desc += "🏁 ¡Cerrando inscripciones…!"
        else:
            desc += "🏁 ¡Arrancando!"

        return discord.Embed(
            title="🏎️ ¡Carrera de Autos!",
            description=desc,
            color=discord.Color.blurple()
        )

    @discord.ui.button(label="🏎️ Unirse", style=discord.ButtonStyle.primary)
    async def unirse(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self.join_lock:
            if self.started:
                return await interaction.response.send_message("❌ La carrera ya comenzó.", ephemeral=True)
            if interaction.user.id in self.wagers:
                return await interaction.response.send_message("❌ Ya estás inscrito.", ephemeral=True)
            if len(self.players) >= MAX_PLAYERS:
                return await interaction.response.send_message("❌ La carrera está llena.", ephemeral=True)

            wager = await reserve_wager(
                interaction.user.id,
                "carrera",
                self.monto,
                session_id=self.session_id,
                expires_in=180,
            )
            if not wager["ok"]:
                return await interaction.response.send_message(
                    f"❌ No tienes suficiente balance. Necesitas **{self.monto}** {COIN}.",
                    ephemeral=True,
                )

            self.players.append(interaction.user)
            self.wagers[interaction.user.id] = wager["id"]
            try:
                await interaction.response.edit_message(
                    embed=self.build_embed(countdown=None),
                    view=self,
                )
            except Exception:
                self.players.remove(interaction.user)
                self.wagers.pop(interaction.user.id, None)
                await refund_wager(wager["id"])
                raise

    async def on_timeout(self):
        async with self.join_lock:
            if self.started:
                return
            self.started = True
            for item in self.children:
                item.disabled = True
        if self.message:
            try:
                await self.message.edit(embed=self.build_embed(countdown=0), view=self)
            except Exception:
                pass


def build_jack_confirmation_embed(author, monto):
    return discord.Embed(
        title="🏎️ ¡Carrera de Autos!",
        description=(
            f"{author.mention} ha convocado una carrera por **{monto}** {COIN}\n\n"
            f"**Inscritos (1/{MAX_PLAYERS}):**\n🏎️ {author.display_name}\n\n"
            "**Sin participantes inscritos!**\n"
            "⚠️ Correr contra una maquina puede ser peligroso, deseas continuar?"
        ),
        color=discord.Color.blurple(),
    )


class SoloVsJackView(discord.ui.View):
    def __init__(self, author, monto, channel_id, session_id, wagers):
        super().__init__(timeout=30)
        self.author = author
        self.monto = monto
        self.channel_id = channel_id
        self.session_id = session_id
        self.wagers = wagers
        self.message = None
        self.resolved = False

    async def _reject_other_user(self, interaction):
        await interaction.response.send_message(
            "❌ Esta confirmación no te pertenece.", ephemeral=True
        )

    @discord.ui.button(label="SI", style=discord.ButtonStyle.success)
    async def continuar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author.id:
            return await self._reject_other_user(interaction)
        if self.resolved:
            return await interaction.response.send_message(
                "❌ Esta carrera ya fue resuelta.", ephemeral=True
            )

        self.resolved = True
        for item in self.children:
            item.disabled = True
        try:
            await interaction.response.defer()
            asyncio.create_task(
                run_race(
                    interaction.message,
                    [self.author],
                    self.monto,
                    self.channel_id,
                    self.session_id,
                    self.wagers,
                )
            )
        except Exception:
            await refund_wager_session(self.session_id)
            _active_races.discard(self.channel_id)
            raise

    @discord.ui.button(label="NO", style=discord.ButtonStyle.danger)
    async def cancelar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author.id:
            return await self._reject_other_user(interaction)
        if self.resolved:
            return await interaction.response.send_message(
                "❌ Esta carrera ya fue resuelta.", ephemeral=True
            )

        self.resolved = True
        await refund_wager_session(self.session_id)
        _active_races.discard(self.channel_id)
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="🏎️ Carrera cancelada",
                description="La carrera contra Jack fue cancelada y la apuesta fue reembolsada.",
                color=discord.Color.red(),
            ),
            view=self,
        )

    async def on_timeout(self):
        if self.resolved:
            return
        self.resolved = True
        await refund_wager_session(self.session_id)
        _active_races.discard(self.channel_id)
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(
                    embed=discord.Embed(
                        title="🏎️ Carrera cancelada",
                        description=(
                            "No se confirmó la carrera contra Jack a tiempo. "
                            "La apuesta fue reembolsada."
                        ),
                        color=discord.Color.red(),
                    ),
                    view=self,
                )
            except discord.HTTPException:
                pass


# ── RESULT EMBED ───────────────────────────────────────
def build_result_embed(winner_id, monto, players, has_bot: bool):
    """
    players: lista de Member reales.
    has_bot: si Jack (bot) participó.
    """
    n_total  = len(players) + (1 if has_bot else 0)
    pot_others  = monto * (n_total - 1)
    ganancia_net = int(pot_others * 1.5)

    lines = []
    for player in players:
        if player.id == winner_id:
            lines.append(
                f"🥇 {player.mention} **¡GANÓ!** **+{ganancia_net}** {COIN} "
                f"*(apuesta devuelta + 150% de las apuestas rivales)*"
            )
        else:
            lines.append(f"💀 {player.mention} perdió **-{monto}** {COIN}")

    # Si Jack ganó (bot), nadie recibe nada extra — solo se muestra
    if has_bot and winner_id is None:
        lines.insert(0, f"🥇 **{BOT_NAME}** (Bot) se llevó la carrera — nadie ganó el pozo.")

    return discord.Embed(
        title="🏁 ¡CARRERA FINALIZADA!",
        description="\n\n".join(lines),
        color=discord.Color.green()
    )


# ── RACE LOGIC ─────────────────────────────────────────
async def run_race(
    message,
    real_players,
    monto,
    channel_id,
    session_id,
    wagers,
):
    has_bot  = len(real_players) == 1

    # Jack conserva ventaja en carreras con un único jugador real.
    if has_bot:
        winner_id = (
            real_players[0].id
            if random.random() < JACK_PLAYER_WIN_PROBABILITY
            else None
        )
    else:
        winner_id = random.choice(real_players).id

    # ── Mostrar embed con GIF mientras "corre" la carrera ─────────
    gif_embed = discord.Embed(
        title="🏎️ ¡La carrera ha comenzado!",
        color=discord.Color.gold()
    )
    gif_embed.set_image(url=GIF_URL)
    n_total = len(real_players) + (1 if has_bot else 0)
    pot = monto * n_total
    gif_embed.set_footer(text=f"Apuesta: {monto} • Pozo Total {pot}.")

    try:
        await message.edit(embed=gif_embed, view=None)
    except Exception:
        pass

    try:
        await asyncio.sleep(GIF_DURATION)

        # El ganador recupera su apuesta y recibe 150% de las apuestas rivales.
        payouts = {}
        if winner_id is not None:
            pot_others = monto * (n_total - 1)
            ganancia_neta = int(pot_others * 1.5)
            payouts[wagers[winner_id]] = monto + ganancia_neta
        settlement = await settle_wager_session(session_id, payouts)
        if not settlement["ok"]:
            raise RuntimeError("No se pudo liquidar el pozo de la carrera.")
    except Exception:
        await refund_wager_session(session_id)
        _active_races.discard(channel_id)
        try:
            await message.edit(
                embed=discord.Embed(
                    title="🏎️ Carrera cancelada",
                    description="Ocurrió un error y todas las apuestas fueron reembolsadas.",
                    color=discord.Color.red(),
                ),
                view=None,
            )
        except Exception:
            pass
        return

    # ── Embed de resultado final ───────────────────────────────────
    result_embed = build_result_embed(winner_id, monto, real_players, has_bot)
    try:
        await message.edit(embed=result_embed)
    except Exception:
        pass

    await asyncio.sleep(RESULT_DELETE)
    try:
        await message.delete()
    except Exception:
        pass

    _active_races.discard(channel_id)


# ── COG ────────────────────────────────────────────────
class Carrera(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="carrera")
    async def carrera(self, ctx, monto: int = None):
        if not _carrera_activa:
            return await ctx.send(f"❌ {ctx.author.mention} El sistema de carreras está desactivado.")

        if monto is None:
            return await ctx.send(f"❌ {ctx.author.mention} Formato correcto: `!carrera {{monto}}`")

        if monto <= 0:
            return await ctx.send(f"❌ {ctx.author.mention} El monto debe ser mayor a 0.")

        if monto > MAX_BET:
            return await ctx.message.reply(f"No puedes apostar mas de {MAX_BET} {COIN}")

        if ctx.channel.id in _active_races:
            return await ctx.send(f"❌ {ctx.author.mention} Ya hay una carrera activa en este canal.")

        await get_user(ctx.author.id)
        session_id = str(uuid.uuid4())
        author_wager = await reserve_wager(
            ctx.author.id,
            "carrera",
            monto,
            session_id=session_id,
            expires_in=180,
        )
        if not author_wager["ok"]:
            return await ctx.send(
                f"❌ {ctx.author.mention} No tienes suficiente balance. Necesitas **{monto}** {COIN}."
            )

        _active_races.add(ctx.channel.id)

        view = JoinRaceView(
            ctx.author,
            monto,
            session_id,
            author_wager["id"],
        )
        embed = view.build_embed(countdown=JOIN_TIMEOUT)
        try:
            message = await ctx.send(embed=embed, view=view)
        except Exception:
            _active_races.discard(ctx.channel.id)
            await refund_wager_session(session_id)
            raise
        view.message = message

        # ── Countdown de inscripciones ──────────────────────────────
        for i in range(JOIN_TIMEOUT - 1, 0, -1):
            await asyncio.sleep(1)
            if len(view.players) >= MAX_PLAYERS:
                break
            try:
                await message.edit(embed=view.build_embed(countdown=i), view=view)
            except Exception:
                pass

        async with view.join_lock:
            view.started = True
            for item in view.children:
                item.disabled = True
            real_players = list(view.players)
            race_wagers = dict(view.wagers)

        # Un solo jugador debe confirmar antes de competir contra Jack.
        if len(real_players) == 1:
            solo_view = SoloVsJackView(
                ctx.author,
                monto,
                ctx.channel.id,
                session_id,
                race_wagers,
            )
            solo_view.message = message
            try:
                await message.edit(
                    embed=build_jack_confirmation_embed(ctx.author, monto),
                    view=solo_view,
                )
            except discord.HTTPException:
                _active_races.discard(ctx.channel.id)
                await refund_wager_session(session_id)
            return

        # ── Mostrar inscritos finales antes de arrancar ─────────────
        try:
            await message.edit(embed=view.build_embed(countdown=0), view=view)
        except Exception:
            pass

        await asyncio.sleep(1)
        await run_race(
            message,
            real_players,
            monto,
            ctx.channel.id,
            session_id,
            race_wagers,
        )

    @app_commands.command(name="carrera_alternar", description="Activa o desactiva el sistema de carreras")
    @is_staff()
    async def carrera_alternar(self, interaction: discord.Interaction):
        global _carrera_activa
        await interaction.response.defer(ephemeral=False)
        nueva_activa = not _carrera_activa
        await set_system_toggle("carrera", nueva_activa)
        _carrera_activa = nueva_activa
        estado = "✅ Activado" if _carrera_activa else "🔴 Desactivado"
        await interaction.followup.send(
            f"🏇 Sistema de Carreras: **{estado}**", ephemeral=False
        )

    @carrera.error
    async def carrera_error(self, ctx, error):
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f"❌ {ctx.author.mention} Formato correcto: `!carrera {{monto}}`")
        elif isinstance(error, commands.CommandOnCooldown):
            retry = int(error.retry_after)
            tiempo = f"{retry // 60}m {retry % 60}s" if retry >= 60 else f"{retry}s"
            await ctx.send(
                f"⏳ {ctx.author.mention} Podrás convocar otra carrera en **{tiempo}**.",
                delete_after=10
            )
        else:
            raise error


async def setup(bot):
    global _carrera_activa
    _carrera_activa = await get_system_toggle("carrera", True)
    await bot.add_cog(Carrera(bot))
