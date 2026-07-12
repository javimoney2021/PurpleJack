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

    async def _get_top_target(self, ctx, position: int):
        """Resuelve una posición del Top 15 al miembro correspondiente."""
        await cache.flush_to_db()

        from core.database import pool

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, balance FROM users ORDER BY balance DESC LIMIT 15"
            )

        user_cache = cache.get_all_cache()
        rankings = [
            (row["id"], user_cache.get(row["id"], {}).get("balance", row["balance"]))
            for row in rows
        ]
        rankings.sort(key=lambda entry: entry[1], reverse=True)

        if position > len(rankings):
            return None

        target_id = rankings[position - 1][0]
        member = ctx.guild.get_member(target_id)
        if member is not None:
            return member
        try:
            return await ctx.guild.fetch_member(target_id)
        except (discord.NotFound, discord.HTTPException):
            return None

    @commands.command()
    async def rob(self, ctx, target_input: str = None):
        if not rob_config["activa"]:
            return await ctx.send(
                "🔫 Las calles están llenas de Sheriffs y Veteranos, "
                "está siendo imposible atracar a alguien."
            )

        if target_input is None:
            return await ctx.send(
                f"❌ {ctx.author.mention} Formato correcto: `!rob @usuario` o `!rob <posición del top>`"
            )

        if target_input.isdigit() and len(target_input) <= 2:
            position = int(target_input)
            if not 1 <= position <= 15:
                return await ctx.send("❌ Indica una posición válida del **1** al **15**.")
            target = await self._get_top_target(ctx, position)
            if target is None:
                return await ctx.send(
                    f"❌ No se encontró un jugador disponible en la posición **{position}.**"
                )
        else:
            try:
                target = await commands.MemberConverter().convert(ctx, target_input)
            except commands.BadArgument:
                return await ctx.send(
                    f"❌ No se pudo encontrar a ese usuario. Usa `!rob @usuario` o `!rob <posición del top>`."
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
        if target_user["balance"] < 5000:
            set_rob_cooldown(author_id)
            return await ctx.message.reply(
                f"😳 No te avergüenza robar a alguien que no tiene ni para una Tarjeta de Rol? "
                f"Atrévete a por los más grandes."
            )

        # Porcentajes dinámicos sobre el balance del target
        # Éxito: 15% — Fallo: 8% (hardcoded)
        success = random.random() <= rob_config["exito_prob"]

        if success:
            monto_robo = int(target_user["balance"] * 0.15)
            await update_balance(author_id, monto_robo)
            await update_balance(target_id, -monto_robo)
            await ctx.message.reply(
                f"✅ Robo exitoso. Le sacaste **{monto_robo:,}** {COIN} a {target.mention} "
                f"sin que se diera cuenta."
            )
        else:
            penalizacion = int(target_user["balance"] * 0.08)
            await update_balance(author_id, -penalizacion)
            await ctx.message.reply(
                f"🚔 Tu robo falló. Perdiste **{penalizacion:,}** {COIN} intentando "
                f"robar a {target.mention}."
            )

        set_rob_cooldown(author_id)


async def setup(bot):
    await bot.add_cog(Rob(bot))
