import discord
from discord.ext import commands
import random
import time

from core.database import get_user, update_balance, update_bank
from core.config import rob_config, COIN
from core import cache
from core.cache import get_rob_cooldown, set_rob_cooldown


def _format_rob_cooldown(seconds: int) -> str:
    """Muestra solo las unidades significativas (omite '0h' si quedan minutos)."""
    horas   = seconds // 3600
    minutos = (seconds % 3600) // 60
    segs    = seconds % 60
    if horas > 0:
        return f"{horas}h {minutos}m {segs}s"
    if minutos > 0:
        return f"{minutos}m {segs}s"
    return f"{segs}s"


class Rob(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    async def rob(self, ctx, target: discord.Member = None):
        if not rob_config["activa"]:
            return await ctx.send(
                "🔫 Las calles están llenas de Sheriffs y Veteranos, "
                "está siendo imposible atracar a alguien."
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

        # Verificar cooldown del atacante
        cooldown_ts = get_rob_cooldown(author_id)
        now = time.time()
        if cooldown_ts > now:
            remaining = int(cooldown_ts - now)
            return await ctx.send(
                f"⏳ {ctx.author.mention} Espera **{_format_rob_cooldown(remaining)}** "
                f"para robar de nuevo."
            )

        author_user = await get_user(author_id)
        target_user = await get_user(target_id)

        # Verificar protección Veterano
        veterano_cfg = cache.get_veterano_config()
        if veterano_cfg:
            target_roles_ids = {r.id for r in target.roles}
            for rol_id, cfg in veterano_cfg.items():
                if rol_id in target_roles_ids:
                    await update_bank(author_id, -cfg["monto"])
                    set_rob_cooldown(author_id)
                    await ctx.send(
                        f"🖐️ Lo siento tanto {ctx.author.mention} {cfg['msj']}"
                    )
                    return

        # Verificar balance mínimo del objetivo
        if target_user["balance"] < 100:
            target_nick = target.nick or target.display_name
            return await ctx.send(
                f"🦋 Solo hay mariposas en la cartera de **{target_nick}**. "
                f"¿Qué le vas a robar? ¡Ve a trabajar!"
            )

        if target_user["balance"] < 1000:
            await update_bank(author_id, -500)
            await update_bank(target_id, 500)
            set_rob_cooldown(author_id)
            author_actualizado = await get_user(author_id)
            bank_final  = author_actualizado["bank"]
            deuda_txt   = (
                f" Tu banco quedó en **{bank_final}** {COIN}, paga tus deudas."
                if bank_final < 0 else ""
            )
            return await ctx.send(
                f"😔 {ctx.author.mention} ¿No te da vergüenza robar a los pobres? "
                f"Se te descontaron **-500** {COIN} del banco y se acreditaron a "
                f"{target.mention}.{deuda_txt}"
            )

        success = random.random() <= rob_config["exito_prob"]

        if success:
            percentage = random.uniform(0.10, 0.50)
            amount     = max(1, int(target_user["balance"] * percentage))
            # Rob es transferencia entre usuarios → escritura inmediata a DB
            await update_balance(author_id, amount)
            await update_balance(target_id, -amount)
            await ctx.send(
                f"💰 {ctx.author.mention} ¡Robo exitoso! "
                f"Le robaste **{amount}** {COIN} a {target.mention}."
            )
        else:
            percentage_penalty = random.uniform(0.10, 0.20)
            penalty     = max(1, int(target_user["balance"] * percentage_penalty))
            compensation = 1500
            # Transferencia entre usuarios → escritura inmediata a DB
            await update_balance(author_id, -penalty)
            await update_bank(author_id, -compensation)
            await update_bank(target_id, compensation)
            await ctx.send(
                f"🚔 {ctx.author.mention} ¡Robo fallido! Perdiste **{penalty}** {COIN} "
                f"de tu balance + **1500** {COIN} de indemnización descontados de tu banco."
            )
            await ctx.send(
                f"🛡️ {target.mention} Alguien intentó robarte, pero falló. "
                f"Recibiste **{compensation}** {COIN} en tu banco como indemnización."
            )

        set_rob_cooldown(author_id)


async def setup(bot):
    await bot.add_cog(Rob(bot))
