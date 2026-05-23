import discord
import asyncio
import random
from discord.ext import commands
from discord import app_commands
from core.database import update_balance, get_user
from core.config import COIN

# ── CONFIG ─────────────────────────────────────────────
TRACK_LENGTH = 20
RACE_DURATION = 12      # segundos totales de carrera
RACE_STEPS    = 8       # cuántas veces se actualiza el embed
STEP_SLEEP    = RACE_DURATION / RACE_STEPS
JOIN_TIMEOUT  = 10      # segundos de inscripción
MAX_PLAYERS   = 5       # máximo jugadores reales (sin bots)

# Emojis de caballos precargados (uno por slot)
HORSE_EMOJIS = [
    "<:c1:1506999542074183791>",
    "<:c2:1506999577386160170>",
    "<:c3:1506999604661714964>",
    "<:c4:1506999620977561640>",
    "<:c5:1506999643110637598>",
]

STAFF_ROLE = "Equipo de Eventos"

# ── ESTADO GLOBAL ──────────────────────────────────────
_active_races  = set()   # {channel_id}
_carrera_activa = True   # sistema habilitado/deshabilitado


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
    def __init__(self, author, monto):
        super().__init__(timeout=JOIN_TIMEOUT)
        self.author  = author
        self.monto   = monto
        self.players = [author]   # lista de Member (solo reales)
        self.message = None
        self.started = False

    def build_embed(self, countdown=None):
        # Asignar emoji fijo por posición de inscripción
        inscritos = "\n".join(
            f"{HORSE_EMOJIS[i]} {p.display_name}"
            for i, p in enumerate(self.players)
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

        embed = discord.Embed(
            title="🏇 ¡Carrera de Caballos!",
            description=desc,
            color=discord.Color.blurple()
        )
        return embed

    @discord.ui.button(label="🏇 Unirse", style=discord.ButtonStyle.primary)
    async def unirse(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.started:
            return await interaction.response.send_message("❌ La carrera ya comenzó.", ephemeral=True)
        if interaction.user in self.players:
            return await interaction.response.send_message("❌ Ya estás inscrito.", ephemeral=True)
        if len(self.players) >= MAX_PLAYERS:
            return await interaction.response.send_message("❌ La carrera está llena.", ephemeral=True)

        user_data = await get_user(interaction.user.id)
        if user_data["balance"] < self.monto:
            return await interaction.response.send_message(
                f"❌ No tienes suficiente balance. Necesitas **{self.monto}** {COIN}.", ephemeral=True
            )

        self.players.append(interaction.user)
        await interaction.response.edit_message(embed=self.build_embed(countdown=None), view=self)

    async def on_timeout(self):
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


# ── TRACK HELPERS ──────────────────────────────────────
def build_track(position, emoji):
    pos    = min(int((position / TRACK_LENGTH) * TRACK_LENGTH), TRACK_LENGTH - 1)
    before = "▬" * pos
    after  = "▬" * (TRACK_LENGTH - pos - 1)
    return f"{before}{emoji}{after}🏁"


def build_race_embed(horses, step, total_steps, monto, n_players):
    lines = []
    for name, emoji, pos in horses:
        lines.append(f"**{name}**\n{build_track(pos, emoji)}")

    pot = monto * n_players
    embed = discord.Embed(
        title=f"🏇 ¡CARRERA EN CURSO! — Tramo {step}/{total_steps}",
        description="\n\n".join(lines),
        color=discord.Color.gold()
    )
    embed.set_footer(text=f"Apuesta: {monto} {COIN} c/u  •  Pozo total: {pot} {COIN}")
    return embed


def build_result_embed(horses, winner_name, monto, players):
    # Ordenar por posición descendente para mostrar podio
    sorted_horses = sorted(horses, key=lambda h: h[2], reverse=True)
    lines = []
    for i, (name, emoji, pos) in enumerate(sorted_horses):
        crown = "🥇" if name == winner_name else f"#{i+1}"
        lines.append(f"{crown} **{name}** — {build_track(pos, emoji)}")

    # Calcular ganancias: ganador recibe apuesta + 150% del pozo de los demás
    pot_others   = monto * (len(players) - 1)
    ganancia_net = int(pot_others * 1.5)   # lo extra que recibe encima de recuperar su apuesta

    result_lines = []
    for player in players:
        if player.display_name == winner_name:
            result_lines.append(
                f"🥇 {player.mention} ganó **+{ganancia_net}** {COIN} "
                f"*(apuesta devuelta + 150% del pozo)*"
            )
        else:
            result_lines.append(f"💀 {player.mention} perdió **-{monto}** {COIN}")

    embed = discord.Embed(
        title="🏁 ¡CARRERA FINALIZADA!",
        description="\n\n".join(lines) + "\n\n" + "\n".join(result_lines),
        color=discord.Color.green()
    )
    return embed


# ── RACE LOGIC ─────────────────────────────────────────
async def run_race(message, players, monto, channel_id):
    n = len(players)
    emojis = random.sample(HORSE_EMOJIS, n)

    # Construir caballos: (nombre, emoji, posicion)
    horses = [
        [player.display_name, emojis[i], 0]
        for i, player in enumerate(players)
    ]

    # ── Ganador completamente aleatorio entre los participantes reales ──
    winner_name = random.choice(players).display_name

    # Simular carrera paso a paso
    for step in range(1, RACE_STEPS + 1):
        for horse in horses:
            if horse[0] == winner_name:
                horse[2] = min(TRACK_LENGTH, horse[2] + random.randint(2, 4))
            else:
                horse[2] = min(TRACK_LENGTH, horse[2] + random.randint(1, 3))

        # Último paso: ganador llega exactamente al final
        if step == RACE_STEPS:
            for horse in horses:
                if horse[0] == winner_name:
                    horse[2] = TRACK_LENGTH
                else:
                    horse[2] = min(TRACK_LENGTH - 1, horse[2])

        embed = build_race_embed(horses, step, RACE_STEPS, monto, n)
        try:
            await message.edit(embed=embed, view=None)
        except Exception:
            pass

        if step < RACE_STEPS:
            await asyncio.sleep(STEP_SLEEP)

    # ── Aplicar resultados económicos ──────────────────────────────────
    # Ganador: recupera su apuesta + 150% del pozo ajeno
    pot_others   = monto * (n - 1)
    ganancia_net = int(pot_others * 1.5)

    for player in players:
        if player.display_name == winner_name:
            # No se le descontó nada aún → recibe solo la ganancia neta
            await update_balance(player.id, ganancia_net)
        else:
            # Pierde su apuesta
            await update_balance(player.id, -monto)

    result_embed = build_result_embed(horses, winner_name, monto, players)
    try:
        await message.edit(embed=result_embed)
    except Exception:
        pass

    await asyncio.sleep(20)
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

        if ctx.channel.id in _active_races:
            return await ctx.send(f"❌ {ctx.author.mention} Ya hay una carrera activa en este canal.")

        user_data = await get_user(ctx.author.id)
        if user_data["balance"] < monto:
            return await ctx.send(
                f"❌ {ctx.author.mention} No tienes suficiente balance. Necesitas **{monto}** {COIN}."
            )

        _active_races.add(ctx.channel.id)

        view    = JoinRaceView(ctx.author, monto)
        embed   = view.build_embed(countdown=JOIN_TIMEOUT)
        message = await ctx.send(embed=embed, view=view)
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

        view.started = True
        for item in view.children:
            item.disabled = True

        # ── Verificar mínimo de jugadores ───────────────────────────
        if len(view.players) < 2:
            _active_races.discard(ctx.channel.id)
            try:
                await message.delete()
            except Exception:
                pass
            return await ctx.send(
                f"🏇 {ctx.author.mention} Se necesita mínimo **2 participantes** para iniciar la carrera.\n"
                f"El cooldown ha sido restablecido, puedes volver a convocar con `!carrera`.",
                delete_after=20
            )

        # ── Cerrar inscripciones e iniciar ──────────────────────────
        try:
            await message.edit(embed=view.build_embed(countdown=0), view=view)
        except Exception:
            pass

        await asyncio.sleep(1)
        await run_race(message, view.players, monto, ctx.channel.id)

    @app_commands.command(name="carrera_alternar", description="Activa o desactiva el sistema de carreras")
    @is_staff()
    async def carrera_alternar(self, interaction: discord.Interaction):
        global _carrera_activa
        _carrera_activa = not _carrera_activa
        estado = "✅ Activado" if _carrera_activa else "🔴 Desactivado"
        await interaction.response.send_message(
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
    await bot.add_cog(Carrera(bot))
