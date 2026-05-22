import discord
import asyncio
import random
from discord.ext import commands
from core.database import update_balance, get_user
from core.config import COIN

# ── CONFIG ─────────────────────────────────────────────
TRACK_LENGTH = 20
RACE_DURATION = 12      # segundos totales de carrera
RACE_STEPS = 8          # cuántas veces se actualiza el embed
STEP_SLEEP = RACE_DURATION / RACE_STEPS
JOIN_TIMEOUT = 10       # segundos de inscripción
MAX_PLAYERS = 5
WIN_PROB = 0.70
MULTIPLIER_WIN = 3.5
MULTIPLIER_LOSS = 1.5   # se descuenta apuesta x1.5

HORSE_EMOJIS = [
    "<:c1:1506999542074183791>",
    "<:c2:1506999577386160170>",
    "<:c3:1506999604661714964>",
    "<:c4:1506999620977561640>",
    "<:c5:1506999643110637598>",
]

BOT_NAMES = ["Trueno", "Relámpago", "Fantasma", "Tormenta", "Sombra"]

# ── ESTADO GLOBAL ──────────────────────────────────────
_active_races = set()  # {channel_id}


# ── JOIN VIEW ──────────────────────────────────────────
class JoinRaceView(discord.ui.View):
    def __init__(self, author, monto):
        super().__init__(timeout=JOIN_TIMEOUT)
        self.author = author
        self.monto = monto
        self.players = [author]   # lista de Member (reales)
        self.message = None
        self.started = False

    def build_embed(self, countdown=None):
        inscritos = "\n".join(
            f"🏇 {p.display_name}" for p in self.players
        )
        desc = (
            f"{self.author.mention} ha llamado a una carrera por **{self.monto}** {COIN}\n\n"
            f"Presiona el botón para participar por el mismo monto.\n\n"
            f"**Inscritos ({len(self.players)}/{MAX_PLAYERS}):**\n{inscritos}\n\n"
        )
        if countdown is not None:
            desc += f"⏳ La carrera inicia en **{countdown}s**"
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

        # Verificar balance
        user_data = await get_user(interaction.user.id)
        if user_data["balance"] < self.monto:
            return await interaction.response.send_message(
                f"❌ No tienes suficiente balance. Necesitas **{self.monto}** {COIN}.", ephemeral=True
            )

        self.players.append(interaction.user)
        await interaction.response.edit_message(embed=self.build_embed(countdown=None), view=self)

    async def on_timeout(self):
        self.started = True
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(embed=self.build_embed(countdown=0), view=self)
            except Exception:
                pass


# ── RACE LOGIC ─────────────────────────────────────────
def build_track(position, emoji):
    filled = int((position / TRACK_LENGTH) * TRACK_LENGTH)
    track = "░" * TRACK_LENGTH
    pos = min(filled, TRACK_LENGTH - 1)
    track = "░" * pos + emoji + "░" * (TRACK_LENGTH - pos - 1)
    return f"`{track}` 🏁"


def build_race_embed(horses, step, total_steps, monto):
    lines = []
    for name, emoji, pos, is_bot in horses:
        tag = "🤖" if is_bot else "👤"
        lines.append(f"{tag} **{name}**\n{build_track(pos, emoji)}")

    embed = discord.Embed(
        title=f"🏇 ¡CARRERA EN CURSO! — Tramo {step}/{total_steps}",
        description="\n\n".join(lines),
        color=discord.Color.gold()
    )
    embed.set_footer(text=f"Apuesta: {monto} {COIN} por participante")
    return embed


def build_result_embed(horses, winner_name, winner_is_bot, monto, real_players):
    lines = []
    for name, emoji, pos, is_bot in sorted(horses, key=lambda h: h[2], reverse=True):
        crown = "🥇" if name == winner_name else ""
        tag = "🤖" if is_bot else "👤"
        lines.append(f"{crown}{tag} **{name}** — {build_track(pos, emoji)}")

    ganancia = int(monto * MULTIPLIER_WIN)
    perdida = int(monto * MULTIPLIER_LOSS)

    result_lines = []
    for player in real_players:
        if player.display_name == winner_name:
            result_lines.append(f"🥇 {player.mention} ganó **+{ganancia}** {COIN}")
        else:
            result_lines.append(f"💀 {player.mention} perdió **-{perdida}** {COIN}")

    embed = discord.Embed(
        title="🏁 ¡CARRERA FINALIZADA!",
        description="\n\n".join(lines) + "\n\n" + "\n".join(result_lines),
        color=discord.Color.green()
    )
    return embed


async def run_race(message, players, monto, channel_id):
    emojis = random.sample(HORSE_EMOJIS, MAX_PLAYERS)
    bot_pool = [n for n in BOT_NAMES if n not in [p.display_name for p in players]]
    random.shuffle(bot_pool)

    # Construir lista de caballos: (nombre, emoji, posicion, es_bot)
    horses = []
    for i, player in enumerate(players):
        horses.append([player.display_name, emojis[i], 0, False])
    for i in range(len(players), MAX_PLAYERS):
        horses.append([bot_pool[i - len(players)], emojis[i], 0, True])

    # Determinar ganador real entre jugadores humanos
    # 70% chance de que un humano gane, 30% un bot
    if random.random() <= WIN_PROB and players:
        winner_name = random.choice(players).display_name
    else:
        bot_horses = [h for h in horses if h[3]]
        winner_name = random.choice(bot_horses)[0] if bot_horses else random.choice(horses)[0]

    # Simular carrera
    for step in range(1, RACE_STEPS + 1):
        for horse in horses:
            name = horse[0]
            if name == winner_name:
                # El ganador avanza más
                horse[2] = min(TRACK_LENGTH, horse[2] + random.randint(2, 4))
            else:
                horse[2] = min(TRACK_LENGTH, horse[2] + random.randint(1, 3))

        # Asegurar que el ganador llegue primero al final
        if step == RACE_STEPS:
            for horse in horses:
                if horse[0] == winner_name:
                    horse[2] = TRACK_LENGTH
                else:
                    horse[2] = min(TRACK_LENGTH - 1, horse[2])

        embed = build_race_embed(horses, step, RACE_STEPS, monto)
        try:
            await message.edit(embed=embed, view=None)
        except Exception:
            pass

        if step < RACE_STEPS:
            await asyncio.sleep(STEP_SLEEP)

    # Aplicar resultados
    winner_is_bot = next(h[3] for h in horses if h[0] == winner_name)
    for player in players:
        if player.display_name == winner_name:
            await update_balance(player.id, int(monto * MULTIPLIER_WIN))
        else:
            await update_balance(player.id, -int(monto * MULTIPLIER_LOSS))

    result_embed = build_result_embed(horses, winner_name, winner_is_bot, monto, players)
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

        view = JoinRaceView(ctx.author, monto)
        embed = view.build_embed(countdown=JOIN_TIMEOUT)
        message = await ctx.send(embed=embed, view=view)
        view.message = message

        # Countdown
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

        try:
            await message.edit(embed=view.build_embed(countdown=0), view=view)
        except Exception:
            pass

        await asyncio.sleep(1)
        await run_race(message, view.players, monto, ctx.channel.id)

    @carrera.error
    async def carrera_error(self, ctx, error):
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f"❌ {ctx.author.mention} Formato correcto: `!carrera {{monto}}`")
        else:
            raise error


async def setup(bot):
    await bot.add_cog(Carrera(bot))
