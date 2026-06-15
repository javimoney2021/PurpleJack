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

        # 50/50 configurable — monto fijo de robo: 5000
        ROB_AMOUNT = 5000
        success = random.random() <= rob_config["exito_prob"]

        if success:
            amount = min(ROB_AMOUNT, target_user["balance"])
            await update_balance(author_id, amount)
            await update_balance(target_id, -amount)
            await ctx.reply(
                f"💰 Has robado exitosamente **{amount}** {COIN} a {target.mention}."
            )
        else:
            await ctx.reply("🚔 Tu robo ha fallado.")

        set_rob_cooldown(author_id)


async def setup(bot):
    await bot.add_cog(Rob(bot))
