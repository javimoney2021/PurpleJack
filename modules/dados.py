import discord
import random
import time
import asyncio
from discord.ext import commands
from core.database import get_user, get_game_cooldown, set_game_cooldown
from core.config import COIN, dados_config
from core import cache

DICE_GIF = "https://pub-a09b3609b6b34dfab5c7aa7742cd1a8a.r2.dev/Purple%20jack%20Harcode/dice.gif"
_ACTIVE_DADOS: set[int] = set()
DICE_FACES = {1: "1️⃣", 2: "2️⃣", 3: "3️⃣", 4: "4️⃣", 5: "5️⃣", 6: "6️⃣"}


def format_roll(value):
    return DICE_FACES.get(value, str(value))


def format_cooldown(seconds):
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        return f"{seconds // 60}m"
    else:
        return f"{seconds // 3600}h"


def choose_dice_rolls(success: bool):
    """
    Genera dos pares de dados garantizando un resultado decisivo (sin empate).

    Reintenta hasta 50 veces con dados aleatorios; si se agota el límite
    (probabilidad ~10^-17, prácticamente imposible) usa un resultado fijo.
    La probabilidad de éxito/fallo viene configurada en dados_config y se
    aplica ANTES de llamar a esta función: aquí solo aseguramos la
    representación visual coherente con ese resultado.
    """
    for _ in range(50):
        d1, d2 = random.randint(1, 6), random.randint(1, 6)
        b1, b2 = random.randint(1, 6), random.randint(1, 6)
        if success and d1 + d2 > b1 + b2:
            return d1, d2, b1, b2
        if not success and d1 + d2 < b1 + b2:
            return d1, d2, b1, b2
    # Fallback determinista — inalcanzable en la práctica
    return (6, 5, 3, 2) if success else (2, 1, 5, 6)


class DadosRollView(discord.ui.View):
    def __init__(self, author_id: int, monto: int):
        super().__init__(timeout=60)
        self.author_id = author_id
        self.monto     = monto
        self.message   = None

    async def on_timeout(self):
        _ACTIVE_DADOS.discard(self.author_id)
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass

    @discord.ui.button(label="🎲 Lanzar Dados", style=discord.ButtonStyle.primary)
    async def lanzar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message(
                "❌ Solo el autor de la apuesta puede lanzar los dados.", ephemeral=True
            )

        if not dados_config["activa"]:
            return await interaction.response.send_message(
                "🔧 El sistema de dados está desactivado.", ephemeral=True
            )

        await interaction.response.defer()

        for child in self.children:
            child.disabled = True

        suspense_embed = discord.Embed(
            title=f"🎲 Lanzando los dados... — {interaction.user.display_name}",
            description="Un momento... el destino está en el aire.",
            color=discord.Color.dark_purple(),
        )
        suspense_embed.set_thumbnail(url=DICE_GIF)
        try:
            await interaction.message.edit(embed=suspense_embed, view=self)
        except Exception:
            pass

        await asyncio.sleep(4)

        # Determinar resultado según probabilidad configurada
        exito = random.random() <= dados_config["exito_prob"]

        # choose_dice_rolls garantiza que no hay empate: el visual siempre
        # coincide con el resultado decidido por la probabilidad
        d1, d2, b1, b2 = choose_dice_rolls(exito)
        autor_suma = d1 + d2
        bot_suma   = b1 + b2

        # Mini-juego: actualizar solo en RAM — flush_loop persiste a DB cada 10 min
        # get_user asegura que el usuario está en cache (seguridad extra)
        await get_user(self.author_id)

        if exito:
            ganancia = self.monto * 2
            cache.update_cached_balance(self.author_id, ganancia)
            resultado_text = f"🎉 ¡Ganaste! Tu apuesta se duplica: +{ganancia} {COIN}."
            color = discord.Color.green()
        else:
            cache.update_cached_balance(self.author_id, -self.monto)
            resultado_text = f"💀 Perdiste tu apuesta inicial de {self.monto} {COIN}."
            color = discord.Color.red()

        _ACTIVE_DADOS.discard(self.author_id)

        expira_en = time.time() + dados_config["cooldown"]
        cache.set_game_cooldown_cache(self.author_id, "dados", expira_en)
        await set_game_cooldown(self.author_id, "dados", expira_en)

        embed = discord.Embed(
            title=f"🎲 Resultado de Dados — {interaction.user.display_name}",
            description=(
                f"**Tus dados:** {format_roll(d1)} + {format_roll(d2)} = **{autor_suma}**\n"
                f"**Dados del bot:** {format_roll(b1)} + {format_roll(b2)} = **{bot_suma}**\n\n"
                f"{resultado_text}"
            ),
            color=color,
        )
        embed.set_thumbnail(url=DICE_GIF)
        embed.set_footer(
            text=f"Cooldown: {format_cooldown(dados_config['cooldown'])} | "
                 f"Máx apuesta: {dados_config['max_apuesta']} PurpleCoins"
        )

        for child in self.children:
            child.disabled = True

        try:
            await interaction.message.edit(embed=embed, view=self)
            if self.message:
                asyncio.create_task(self.message.delete(delay=80))
        except Exception:
            pass


class Dados(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="dados")
    async def dados(self, ctx, monto: int = None):
        if monto is None:
            return await ctx.send(f"❌ {ctx.author.mention} Usa: `!dados {{monto}}`.")

        if not dados_config["activa"]:
            return await ctx.send(
                "🔧 El sistema de dados está en mantenimiento. Intenta después."
            )

        if monto <= 0:
            return await ctx.send(
                f"❌ {ctx.author.mention} La apuesta debe ser mayor a 0."
            )

        if monto > dados_config["max_apuesta"]:
            return await ctx.send(
                f"❌ {ctx.author.mention} No puedes apostar más de "
                f"**{dados_config['max_apuesta']}** {COIN}."
            )

        if ctx.author.id in _ACTIVE_DADOS:
            return await ctx.send(
                f"❌ {ctx.author.mention} Ya tienes un juego de Dados en curso. "
                f"Termina la apuesta actual antes de iniciar otra."
            )

        # get_user garantiza usuario en cache para la actualización del botón
        user = await get_user(ctx.author.id)
        if monto > user["balance"]:
            return await ctx.send(
                f"❌ {ctx.author.mention} No tienes suficiente balance "
                f"para apostar {monto} {COIN}."
            )

        now = time.time()
        expira_en = cache.get_game_cooldown_cache(ctx.author.id, "dados")
        if expira_en == 0:
            expira_en = await get_game_cooldown(ctx.author.id, "dados")
            if expira_en:
                cache.set_game_cooldown_cache(ctx.author.id, "dados", expira_en)

        if expira_en > now:
            remaining = int(expira_en - now)
            minutos  = remaining // 60
            segundos = remaining % 60
            return await ctx.send(
                f"⏳ {ctx.author.mention} Espera **{minutos}m {segundos}s** "
                f"antes de volver a apostar."
            )

        embed = discord.Embed(
            title=f"🎲 Apuesta de Dados — {ctx.author.display_name}",
            description=(
                f"{ctx.author.mention} ha apostado **{monto}** {COIN}.\n\n"
                f"Haz clic en el botón para lanzar tus dados y enfrentarte al bot.\n"
                f"Chance de éxito: **{int(dados_config['exito_prob'] * 100)}%**."
            ),
            color=discord.Color.blurple(),
        )
        embed.set_thumbnail(url=DICE_GIF)
        embed.set_footer(
            text=f"Cooldown: {format_cooldown(dados_config['cooldown'])} | "
                 f"Máx apuesta: {dados_config['max_apuesta']} PurpleCoins"
        )

        _ACTIVE_DADOS.add(ctx.author.id)

        view    = DadosRollView(ctx.author.id, monto)
        message = await ctx.reply(embed=embed, view=view, mention_author=False)
        view.message = message


async def setup(bot):
    await bot.add_cog(Dados(bot))
