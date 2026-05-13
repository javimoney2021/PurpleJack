import discord
from discord.ext import commands
import random
import time
import asyncio

from core.database import get_user, update_balance, update_bank
from core.config import rob_config, COIN
from core import cache
from core.cache import (
    get_rob_cooldown, set_rob_cooldown,
    get_rob_protection, set_rob_protection
)

class Rob(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    async def rob(self, ctx, target: discord.Member = None):
        if not rob_config["activa"]:
            return await ctx.send(
                "🔫 Las calles están llenas de Sheriffs y Veteranos, está siendo imposible atracar a alguien."
            )

        if target is None:
            return await ctx.send(
                f"❌ {ctx.author.mention} Formato correcto: `!rob @usuario`"
            )

        if target == ctx.author:
            return await ctx.send(
                f"❌ {ctx.author.mention} No puedes robarte a ti mismo."
            )

        author_id = ctx.author.id
        target_id = target.id

        # Verificar cooldown del author
        cooldown_ts = get_rob_cooldown(author_id)
        now = time.time()
        if cooldown_ts > now:
            remaining = int(cooldown_ts - now)
            return await ctx.send(
                f"⏳ {ctx.author.mention} Espera **{remaining // 3600}h {(remaining % 3600) // 60}m {remaining % 60}s** para robar de nuevo."
            )

        # Verificar protección del objetivo
        protection_ts = get_rob_protection(target_id)
        if protection_ts > now:
            remaining = int(protection_ts - now)
            author_nick = ctx.author.nick or ctx.author.display_name
            return await ctx.message.reply(
                f"🛡️ **{author_nick}** Esta persona fue recientemente atacada por la inseguridad, ¿un poquito de caridad humana no? Protección restante: **{remaining // 60}m {remaining % 60}s**."
            )

        author_user = await get_user(author_id)
        target_user = await get_user(target_id)

        # Verificar balance mínimo del objetivo
        if target_user["balance"] < 100:
            target_nick = target.nick or target.display_name
            return await ctx.send(
                f"🦋 Solo hay mariposas en la cartera de **{target_nick}**. ¿Qué le vas a robar? ¡Ve a trabajar!"
            )

        if target_user["balance"] < 1000:
            await update_bank(author_id, -500)
            await update_bank(target_id, 500)
            set_rob_cooldown(author_id)
            author_actualizado = await get_user(author_id)
            bank_final = author_actualizado["bank"]
            deuda_txt = f" Tu banco quedó en **{bank_final}** {COIN}, paga tus deudas." if bank_final < 0 else ""
            return await ctx.send(
                f"😔 {ctx.author.mention} ¿No te da vergüenza robar a los pobres? Se te descontaron **-500** {COIN} del banco y se acreditaron a {target.mention}.{deuda_txt}"
            )

        # Calcular éxito/fallo
        success = random.random() <= rob_config["exito_prob"]

        if success:
            # Éxito: robar 10%-50% del balance del objetivo
            percentage = random.uniform(0.10, 0.50)
            amount = int(target_user["balance"] * percentage)
            if amount == 0:
                amount = 1  # Mínimo 1

            await update_balance(author_id, amount)
            await update_balance(target_id, -amount)

            await ctx.send(
                f"💰 {ctx.author.mention} ¡Robo exitoso! Le robaste **{amount}** {COIN} a {target.mention}."
            )
        else:
            # Fallo: perder 10%-20% del balance del objetivo
            percentage_penalty = random.uniform(0.10, 0.20)
            penalty = int(target_user["balance"] * percentage_penalty)
            if penalty == 0:
                penalty = 1

            compensation = 1500

            await update_balance(author_id, -penalty)
            await update_bank(author_id, -compensation)
            await update_bank(target_id, compensation)

            await ctx.send(
                f"🚔 {ctx.author.mention} ¡Robo fallido! Perdiste **{penalty}** {COIN} de tu balance + **1500** {COIN} de indemnización descontados de tu banco."
            )
            await ctx.send(
                f"🛡️ {target.mention} Alguien intentó robarte, pero falló. Recibiste **{compensation}** {COIN} en tu banco como indemnización."
            )

        # Aplicar cooldown al author (1 hora)
        set_rob_cooldown(author_id)

        # Aplicar protección al objetivo (1 hora)
        set_rob_protection(target_id)


async def setup(bot):
    await bot.add_cog(Rob(bot))
