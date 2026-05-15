import discord
import random
import time
from discord.ext import commands
from core.database import get_user, update_balance, get_game_cooldown, set_game_cooldown
from core.config import COIN, dados_config
from core import cache

DICE_GIF = "https://pub-a09b3609b6b34dfab5c7aa7742cd1a8a.r2.dev/Purple%20jack%20Harcode/dice.gif"
DICE_FACES = {
    1: "1️⃣",
    2: "2️⃣",
    3: "3️⃣",
    4: "4️⃣",
    5: "5️⃣",
    6: "6️⃣"
}


def format_roll(value):
    return DICE_FACES.get(value, str(value))


def choose_dice_rolls(success: bool):
    while True:
        autor_dado_1 = random.randint(1, 6)
        autor_dado_2 = random.randint(1, 6)
        bot_dado_1 = random.randint(1, 6)
        bot_dado_2 = random.randint(1, 6)
        autor_suma = autor_dado_1 + autor_dado_2
        bot_suma = bot_dado_1 + bot_dado_2
        if success and autor_suma > bot_suma:
            return autor_dado_1, autor_dado_2, bot_dado_1, bot_dado_2
        if not success and autor_suma <= bot_suma:
            return autor_dado_1, autor_dado_2, bot_dado_1, bot_dado_2


class DadosRollView(discord.ui.View):
    def __init__(self, author_id: int, monto: int):
        super().__init__(timeout=60)
        self.author_id = author_id
        self.monto = monto
        self.message = None

    async def on_timeout(self):
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

        exito = random.random() <= dados_config["exito_prob"]
        autor_dado_1, autor_dado_2, bot_dado_1, bot_dado_2 = choose_dice_rolls(exito)
        autor_suma = autor_dado_1 + autor_dado_2
        bot_suma = bot_dado_1 + bot_dado_2

        if exito:
            ganancia = self.monto * 2
            await update_balance(self.author_id, ganancia)
            resultado_text = (
                f"🎉 ¡Ganaste! Tu apuesta se duplica: +{ganancia} {COIN}."
            )
            color = discord.Color.green()
        else:
            await update_balance(self.author_id, -self.monto)
            resultado_text = (
                f"💀 Perdiste tu apuesta inicial de {self.monto} {COIN}."
            )
            color = discord.Color.red()

        expira_en = time.time() + dados_config["cooldown"]
        cache.set_game_cooldown_cache(self.author_id, "dados", expira_en)
        await set_game_cooldown(self.author_id, "dados", expira_en)

        embed = discord.Embed(
            title="🎲 Resultado de Dados",
            description=(
                f"**Tus dados:** {format_roll(autor_dado_1)} + {format_roll(autor_dado_2)} = **{autor_suma}**\n"
                f"**Dados del bot:** {format_roll(bot_dado_1)} + {format_roll(bot_dado_2)} = **{bot_suma}**\n\n"
                f"{resultado_text}"
            ),
            color=color
        )
        embed.set_thumbnail(url=DICE_GIF)
        embed.set_footer(text=f"Cooldown: {dados_config['cooldown']}s | Máx apuesta: {dados_config['max_apuesta']} {COIN}")

        for child in self.children:
            child.disabled = True

        try:
            await interaction.message.edit(embed=embed, view=self)
        except Exception:
            pass


class Dados(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="dados")
    async def dados(self, ctx, monto: int = None):
        if monto is None:
            return await ctx.send(
                f"❌ {ctx.author.mention} Usa: `!dados {{monto}}`."
            )

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
                f"❌ {ctx.author.mention} No puedes apostar más de **{dados_config['max_apuesta']}** {COIN}."
            )

        user = await get_user(ctx.author.id)
        if monto > user["balance"]:
            return await ctx.send(
                f"❌ {ctx.author.mention} No tienes suficiente balance para apostar {monto} {COIN}."
            )

        now = time.time()
        expira_en = cache.get_game_cooldown_cache(ctx.author.id, "dados")
        if expira_en == 0:
            expira_en = await get_game_cooldown(ctx.author.id, "dados")
            if expira_en:
                cache.set_game_cooldown_cache(ctx.author.id, "dados", expira_en)

        if expira_en > now:
            remaining = int(expira_en - now)
            minutos = remaining // 60
            segundos = remaining % 60
            return await ctx.send(
                f"⏳ {ctx.author.mention} Espera **{minutos}m {segundos}s** antes de volver a apostar."
            )

        embed = discord.Embed(
            title="🎲 Apuesta de Dados",
            description=(
                f"{ctx.author.mention} ha apostado **{monto}** {COIN}.\n\n"
                f"Haz clic en el botón para lanzar tus dados y enfrentarte al bot.\n"
                f"Chance de éxito: **{int(dados_config['exito_prob']*100)}%**."
            ),
            color=discord.Color.blurple()
        )
        embed.set_thumbnail(url=DICE_GIF)
        embed.set_footer(
            text=f"Cooldown: {dados_config['cooldown']}s | Máx apuesta: {dados_config['max_apuesta']} {COIN}"
        )

        view = DadosRollView(ctx.author.id, monto)
        message = await ctx.send(embed=embed, view=view)
        view.message = message


async def setup(bot):
    await bot.add_cog(Dados(bot))
