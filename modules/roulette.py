from discord.ext import commands
import discord
import random
import time
import asyncio

from core.database import get_user, update_balance
from core.config import game_config, ruleta_config
from core import cache

COIN = "<:PurpleCoin:1501855737842892941>"

# cooldowns manejados via cache + DB
# ROULETTE_COOLDOWN = 180  # Ahora usa ruleta_config["cooldown"]

SLOTS = {
    '0': 'green', '1': 'red', '2': 'black', '3': 'red', '4': 'black',
    '5': 'red', '6': 'black', '7': 'red', '8': 'black', '9': 'red',
    '10': 'black', '11': 'red', '12': 'black', '13': 'red', '14': 'black',
    '15': 'red', '16': 'black', '17': 'red', '18': 'black', '19': 'red',
    '20': 'black', '21': 'red', '22': 'black', '23': 'red', '24': 'black',
    '25': 'red', '26': 'black', '27': 'red', '28': 'black', '29': 'red',
    '30': 'black', '31': 'red', '32': 'black', '33': 'red', '34': 'black',
    '35': 'red', '36': 'black'
}

OPCIONES_VALIDAS = ["black", "red", "par", "impar"] + [str(i) for i in range(37)]


class Roulette(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    async def ruleta(self, ctx, apuesta: int = None, espacio: str = None):
        if not ruleta_config["activa"]:
            return await ctx.send(
                "🔧 La Ruleta se encuentra en mantenimiento, inténtalo más tarde...!"
            )

        if apuesta is None or espacio is None:
            return await ctx.send(
                f"❌ {ctx.author.mention} Formato correcto: "
                f"`!ruleta {{apuesta}} {{opción}}`\n"
                f"Opciones: `black`, `red`, `par`, `impar`, "
                f"o un número del `0` al `36`"
            )

        espacio = espacio.lower().strip()

        if espacio not in OPCIONES_VALIDAS:
            return await ctx.send(
                f"❌ {ctx.author.mention} Opción inválida. "
                f"Usa: `black`, `red`, `par`, `impar` "
                f"o un número del `0` al `36`."
            )

        if apuesta <= 0:
            return await ctx.send(
                f"❌ {ctx.author.mention} La apuesta debe ser mayor a 0."
            )

        if apuesta > ruleta_config["max_apuesta"]:
            return await ctx.send(
                f"❌ {ctx.author.mention} "
                f"No puedes apostar más de "
                f"**{ruleta_config['max_apuesta']}** {COIN}."
            )

        user_id = ctx.author.id
        now = time.time()

        expira_en = cache.get_game_cooldown_cache(user_id, "ruleta")
        if expira_en == 0:
            from core.database import get_game_cooldown
            expira_en = await get_game_cooldown(user_id, "ruleta")
            if expira_en:
                cache.set_game_cooldown_cache(user_id, "ruleta", expira_en)

        if expira_en > now:
            remaining = int(expira_en - now)
            minutos = remaining // 60
            segundos = remaining % 60
            return await ctx.send(
                f"⏳ {ctx.author.mention} "
                f"Espera **{minutos}m {segundos}s** "
                f"para jugar de nuevo.",
                delete_after=10
            )

        user = await get_user(user_id)

        if apuesta > user["balance"]:
            return await ctx.send(
                f"❌ {ctx.author.mention} "
                f"No tienes suficiente balance para esta apuesta."
            )

        expira_en = now + ruleta_config["cooldown"]
        cache.set_game_cooldown_cache(user_id, "ruleta", expira_en)
        from core.database import set_game_cooldown
        await set_game_cooldown(user_id, "ruleta", expira_en)

        embed = discord.Embed(
            description=(
                f"🎰 {ctx.author.mention} apostó "
                f"**{apuesta}** {COIN} en `{espacio}`."
            ),
            color=discord.Color.purple()
        )

        embed.set_footer(
            text="🌀 Girando la ruleta... Espera 10 segundos"
        )

        embed.set_thumbnail(
            url=ctx.author.display_avatar.url
        )

        await ctx.send(embed=embed)

        await asyncio.sleep(10)

        resultado = random.choice(list(SLOTS.keys()))
        color_resultado = SLOTS[resultado]
        resultado_int = int(resultado)

        if espacio in ["black", "red", "par", "impar"]:
            multiplicador = 2
        else:
            multiplicador = 10

        if espacio == "black":
            gano = color_resultado == "black"

        elif espacio == "red":
            gano = color_resultado == "red"

        elif espacio == "par":
            gano = resultado_int != 0 and resultado_int % 2 == 0

        elif espacio == "impar":
            gano = resultado_int % 2 != 0

        else:
            gano = espacio == resultado

        if gano:
            ganancia = apuesta * (multiplicador - 1)

            await update_balance(user_id, ganancia)

            embed_resultado = discord.Embed(
                title="🎰 Resultado de la Ruleta",
                description=(
                    f"🟢 La bola cayó en: "
                    f"**{color_resultado} {resultado}**!\n\n"
                    f"🎉 **¡Ganaste!** {ctx.author.mention}\n"
                    f"Recibes **{ganancia}** "
                    f"{COIN} (x{multiplicador})"
                ),
                color=discord.Color.green()
            )

        else:
            await update_balance(user_id, -apuesta)

            embed_resultado = discord.Embed(
                title="🎰 Resultado de la Ruleta",
                description=(
                    f"🟢 La bola cayó en: "
                    f"**{color_resultado} {resultado}**!\n\n"
                    f"💸 **Perdiste** {ctx.author.mention}\n"
                    f"Pierdes **{apuesta}** {COIN}."
                ),
                color=discord.Color.red()
            )

        embed_resultado.set_thumbnail(
            url=ctx.author.display_avatar.url
        )

        await ctx.send(embed=embed_resultado)


async def setup(bot):
    await bot.add_cog(Roulette(bot))
