from discord.ext import commands
import random
import time

from core.database import get_user, update_bank, update_cooldown
from core.config import game_config, COIN

WORK_MESSAGES = [
    "🔧 Trabajaste duro y ganaste **{monto}** " + COIN,
    "🔧 Encontraste fallas graves en el Motor Superior de la nave, obtienes **{monto}** " + COIN,
    "⛽ Llenaste el combustible exitosamente, ganas **{monto}** " + COIN,
    "🗑️ La basura espacial ya estaba causando problemas, gracias por encargarte, obtienes **{monto}** " + COIN,
    "☄️ Meteoritos a la vista! Gracias por encargarte de ellos, ganas **{monto}** " + COIN,
    "🪣 Alcantarillas limpias, impostores a la vista, obtienes por la limpieza de ductos, **{monto}** " + COIN,
    "⚡ Has reparado el cableado exitosamente y la nave ha vuelto a funcionar, ganas **{monto}** " + COIN,
    "🛡️ Los Escudos de la nave han sido reparados, por tu trabajo obtienes **{monto}** " + COIN,
    "👻 Buen trabajo has atrapado al fantasma merodeando en la nave, ganas **{monto}** " + COIN,
    "🚀 Saboteadora a la vista, por descubrirla y echarla de la nave obtienes **{monto}** " + COIN,
    "📡 Las Comunicaciones vuelven a estar estables gracias a tu labor, ganas **{monto}** " + COIN,
    "💧 Hey! Buen trabajo en la sala de calderas necesitabamos agua, obtienes **{monto}** " + COIN,
]

CRIME_SUCCESS = [
    "🔥 El crimen cometido en **Castillo Magico** salió bien 🔫 Ganaste **{monto}** " + COIN,
    "🔥 Un Bandido experto en robar Roles 🥸 Ganaste **{monto}** " + COIN,
    "🔥 No todos los Payasos son buenos, este se robo tu sonrisa 🤡 Ganaste **{monto}** " + COIN,
    "🔥 Has conseguido con exito acceder a la boveda del tesoro 💰 Ganaste **{monto}** " + COIN,
]

CRIME_FAIL = [
    "🚔 El Sheriff se lleva el MVP 🤠 Pierdes **-{monto}** " + COIN,
    "🚔 Fallas al intentar secuestar al Alcalde 😭 Pieres **-{monto}** " + COIN,
    "🚔 Te encuentran Irrumpiendo el sistema electrico 💡 Pierdes **-{monto}** " + COIN,
    "🚔 Te pillaron eliminando evidencias en Sala de Seguridad 🎥 Pierdes **-{monto}** " + COIN,
]


class BasicGames(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    async def work(self, ctx):
        user = await get_user(ctx.author.id)
        now = int(time.time())

        cooldown = game_config["work"]["cooldown"]
        last = user.get("cooldown_work", 0)

        if now - last < cooldown:
            remaining = cooldown - (now - last)
            return await ctx.send(f"⏳ Puedes volver a trabajar <t:{now + remaining}:R>.")

        amount = random.randint(
            game_config["work"]["min"],
            game_config["work"]["max"]
        )

        await update_bank(ctx.author.id, amount)
        await update_cooldown(ctx.author.id, "work", now)
        await ctx.send(random.choice(WORK_MESSAGES).format(monto=amount))

    @commands.command()
    async def crime(self, ctx):
        user = await get_user(ctx.author.id)
        now = int(time.time())

        cooldown = game_config["crime"]["cooldown"]
        last = user.get("cooldown_crime", 0)

        if now - last < cooldown:
            remaining = cooldown - (now - last)
            return await ctx.send(f"⏳ Puedes volver a cometer un crimen <t:{now + remaining}:R>.")

        amount = random.randint(
            game_config["crime"]["min"],
            game_config["crime"]["max"]
        )

        success = random.random() <= 0.7

        if success:
            await update_bank(ctx.author.id, amount)
            msg = random.choice(CRIME_SUCCESS)
        else:
            await update_bank(ctx.author.id, -amount)
            msg = random.choice(CRIME_FAIL)

        await update_cooldown(ctx.author.id, "crime", now)
        await ctx.send(msg.format(monto=amount))


async def setup(bot):
    await bot.add_cog(BasicGames(bot))
