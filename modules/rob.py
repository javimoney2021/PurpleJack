import discord
from discord.ext import commands
import random
import time

from core.database import (
    get_user,
    update_balance,
    update_bank,
    transfer_balance,
    get_command_cooldown,
    get_rob_victim_protection,
    set_command_cooldown,
)
from core.config import rob_config, COIN, ROB_VICTIM_PROTECTION_SECONDS
from core import cache
from core.cache import get_rob_cooldown, set_rob_cooldown
from core.cd_boost import resolve_cd_boost, send_cd_boost_notice

SABOTEADOR_EXITO_PROB = 0.70
SABOTEADOR_ROBO_PORCENTAJE = 0.20
SABOTEADOR_FALLO_PORCENTAJE = 0.15
EVENT_ROB_VICTIM_PENALTY_PERCENT = 30


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


def _apply_event_victim_penalty(user_id: int, stolen_amount: int) -> None:
    penalty = stolen_amount * EVENT_ROB_VICTIM_PENALTY_PERCENT // 100
    cache.record_evento_balance_delta(user_id, -penalty)


async def _reply_recent_victim(ctx, target: discord.Member) -> None:
    await ctx.message.reply(
        f"🥺 **{target.display_name}** fue víctima de la delincuencia recientemente, "
        "un poquito de porfavor...",
        mention_author=False,
    )


class Rob(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _set_rob_cooldown(self, user_id: int):
        cooldown_seconds, cd_boost = await resolve_cd_boost(
            user_id,
            rob_config["cooldown"],
        )
        expira_en = time.time() + cooldown_seconds
        set_rob_cooldown(user_id, expira_en)
        await set_command_cooldown("user", user_id, "rob", expira_en)
        return cd_boost

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

    async def _get_reply_target(self, ctx):
        """Resuelve al autor del mensaje al que se respondió, incluso sin caché."""
        reference = ctx.message.reference
        if reference is None or reference.message_id is None:
            return None

        message = reference.resolved
        if not isinstance(message, discord.Message):
            channel = ctx.guild.get_channel(reference.channel_id) or ctx.channel
            try:
                message = await channel.fetch_message(reference.message_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return None

        member = message.author
        if isinstance(member, discord.Member):
            return member
        try:
            return await ctx.guild.fetch_member(member.id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None

    @commands.command()
    async def rob(self, ctx, target_input: str = None):
        if not rob_config["activa"]:
            return await ctx.send(
                "🔫 Las calles están llenas de Sheriffs y Veteranos, "
                "está siendo imposible atracar a alguien."
            )

        target = None
        if target_input is None:
            target = await self._get_reply_target(ctx)
            if target is None:
                return await ctx.send(
                    f"❌ {ctx.author.mention} Responde al mensaje de un usuario o usa "
                    "`!rob @usuario` / `!rob <posición del top>`."
                )
        elif target_input.isdigit() and len(target_input) <= 2:
            position = int(target_input)
            if not 1 <= position <= 15:
                return await ctx.send("❌ Indica una posición válida del **1** al **15**.")
            target = await self._get_top_target(ctx, position)
            if target is None:
                return await ctx.send(
                    f"❌ No se encontró un jugador disponible en la posición **{position}.**"
                )
        elif target is None:
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
        if cooldown_ts <= now:
            cooldown_ts = await get_command_cooldown("user", author_id, "rob")
        if cooldown_ts > now:
            remaining = int(cooldown_ts - now)
            return await ctx.send(
                f"⏳ {ctx.author.mention} Espera **{_format_rob_cooldown(remaining)}** "
                f"para robar de nuevo."
            )

        protected_until = await get_rob_victim_protection(target_id)
        if protected_until > now:
            return await _reply_recent_victim(ctx, target)

        author_user = await get_user(author_id)
        target_user = await get_user(target_id)

        # Un Saboteador solo activa sus reglas especiales al atacar a un Veterano.
        robo_saboteador = False
        veterano_cfg = cache.get_veterano_config()
        if veterano_cfg:
            target_roles_ids = {r.id for r in target.roles}
            proteccion_veterano = next(
                (cfg for rol_id, cfg in veterano_cfg.items() if rol_id in target_roles_ids),
                None,
            )
            if proteccion_veterano:
                attacker_role_ids = {r.id for r in ctx.author.roles}
                es_saboteador = bool(
                    attacker_role_ids & cache.get_saboteador_role_ids()
                )
                if es_saboteador:
                    robo_saboteador = True
                else:
                    await update_bank(author_id, -proteccion_veterano["monto"])
                    cd_boost = await self._set_rob_cooldown(author_id)
                    response_message = await ctx.send(
                        f"🖐️ Lo siento tanto {ctx.author.mention} {proteccion_veterano['msj']}"
                    )
                    await send_cd_boost_notice(
                        response_message,
                        ctx.author,
                        cd_boost,
                    )
                    return

        # Verificar balance mínimo del objetivo
        if target_user["balance"] < 5000:
            cd_boost = await self._set_rob_cooldown(author_id)
            response_message = await ctx.message.reply(
                f"😳 No te avergüenza robar a alguien que no tiene ni para una Tarjeta de Rol? "
                f"Atrévete a por los más grandes."
            )
            await send_cd_boost_notice(response_message, ctx.author, cd_boost)
            return

        if robo_saboteador:
            target_nick = target.nick or target.display_name
            success = random.random() <= SABOTEADOR_EXITO_PROB
            if success:
                monto_robo = int(target_user["balance"] * SABOTEADOR_ROBO_PORCENTAJE)
                transfer = await transfer_balance(
                    target_id,
                    author_id,
                    monto_robo,
                    track_sender_event=False,
                    victim_protection_seconds=ROB_VICTIM_PROTECTION_SECONDS,
                )
                if not transfer["ok"]:
                    if transfer.get("reason") == "victim_protected":
                        return await _reply_recent_victim(ctx, target)
                    return await ctx.send(
                        "⚠️ El saldo del objetivo cambió durante el robo. Inténtalo de nuevo."
                    )
                _apply_event_victim_penalty(target_id, monto_robo)
                response_message = await ctx.message.reply(
                    f"😈 Logras romper la Protección del **Veterano** de {target_nick} "
                    f"y le sacas **{monto_robo:,}** {COIN}.",
                    mention_author=False,
                )
            else:
                penalizacion = int(target_user["balance"] * SABOTEADOR_FALLO_PORCENTAJE)
                await update_balance(author_id, -penalizacion)
                response_message = await ctx.message.reply(
                    f"☠️ Ese **Veterano** de {target_nick} al parecer está en Ultra... "
                    f"Fallas el robo y pierdes **{penalizacion:,}** {COIN}.",
                    mention_author=False,
                )
        else:
            # Robo normal: probabilidad configurable, porcentajes económicos fijos.
            success = random.random() <= rob_config["exito_prob"]
            if success:
                monto_robo = int(target_user["balance"] * 0.15)
                transfer = await transfer_balance(
                    target_id,
                    author_id,
                    monto_robo,
                    track_sender_event=False,
                    victim_protection_seconds=ROB_VICTIM_PROTECTION_SECONDS,
                )
                if not transfer["ok"]:
                    if transfer.get("reason") == "victim_protected":
                        return await _reply_recent_victim(ctx, target)
                    return await ctx.send(
                        "⚠️ El saldo del objetivo cambió durante el robo. Inténtalo de nuevo."
                    )
                _apply_event_victim_penalty(target_id, monto_robo)
                response_message = await ctx.message.reply(
                    f"✅ Robo exitoso. Le sacaste **{monto_robo:,}** {COIN} a {target.mention} "
                    f"sin que se diera cuenta."
                )
            else:
                penalizacion = int(target_user["balance"] * 0.08)
                await update_balance(author_id, -penalizacion)
                target_nick = target.nick or target.display_name
                response_message = await ctx.message.reply(
                    f"🚔 Tu robo falló. Perdiste **{penalizacion:,}** {COIN} intentando "
                    f"robar a {target_nick}."
                )

        cd_boost = await self._set_rob_cooldown(author_id)
        await send_cd_boost_notice(response_message, ctx.author, cd_boost)


async def setup(bot):
    await bot.add_cog(Rob(bot))
