from discord.ext import commands
import discord
import logging
import time
from core.database import (
    get_user,
    flush_user_to_db,
    load_collect_cooldowns_for_user,
    save_collect_cooldowns,
)
from core import cache
from core.config import COIN
from core.cache import MAX_BANK

logger = logging.getLogger(__name__)


class Collect(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    async def collect(self, ctx):
        try:
            config = cache.get_collect_config()
            if not config:
                return await ctx.send("💷 No hay collects configurados aún.")

            user_id = ctx.author.id
            user_roles_ids = {role.id for role in ctx.author.roles}

            roles_aplicables = {
                rol_id: cfg
                for rol_id, cfg in config.items()
                if rol_id in user_roles_ids
            }

            if not roles_aplicables:
                return await ctx.send(
                    f"❌ {ctx.author.mention} No tienes ningún Rol con collect disponible. "
                    f"Adquiérelos en la **!tienda**"
                )

            # Leer cooldowns desde cache (sin await si ya están cargados)
            cooldowns = cache.get_collect_cooldowns(user_id)
            if cooldowns is None:
                cooldowns = await load_collect_cooldowns_for_user(user_id)

            now = time.time()
            cobros = {}
            total_ganado = 0
            lineas = []

            for rol_id, cfg in roles_aplicables.items():
                ultima_vez = cooldowns.get(rol_id, 0)
                cooldown_secs = cfg["cooldown_horas"] * 3600
                disponible_en = ultima_vez + cooldown_secs
                nombre_rol = f"<@&{rol_id}>"

                if now >= disponible_en:
                    cobros[rol_id] = now
                    cache.update_collect_cooldown(user_id, rol_id, now)
                    total_ganado += cfg["cantidad"]
                    lineas.append(f"{nombre_rol}  →  {COIN} **{cfg['cantidad']}**")
                else:
                    ts = int(disponible_en)
                    lineas.append(f"{nombre_rol}  →  <t:{ts}:R>")

            # ── Garantizar usuario en cache ANTES de actualizar el banco ──
            # Esto evita la pérdida silenciosa de datos si el usuario no
            # estaba en cache (inactividad >2h o primer uso del día).
            aplicado_banco    = 0
            excedente_balance = 0
            if cobros and total_ganado > 0:
                await get_user(user_id)
                # Calcular distribución ANTES de aplicar para el mensaje
                banco_actual      = cache.get_cached(user_id)["bank"]
                espacio_disponible = max(0, MAX_BANK - banco_actual)
                aplicado_banco    = min(total_ganado, espacio_disponible)
                excedente_balance = total_ganado - aplicado_banco
                cache.update_cached_bank(user_id, total_ganado, track_event=False)

            # Construir y enviar embed de inmediato
            nick = ctx.author.nick or ctx.author.display_name
            embed = discord.Embed(
                title=f"💷 Mis Collects - {nick} 💷",
                description="\n".join(lineas),
                color=discord.Color.purple(),
            )
            if total_ganado > 0:
                if excedente_balance > 0:
                    embed.add_field(
                        name="Total cobrado",
                        value=(
                            f"🏦 **{aplicado_banco:,}** {COIN} → Banco\n"
                            f"💰 **{excedente_balance:,}** {COIN} → Balance *(banco lleno)*"
                        ),
                        inline=False,
                    )
                    embed.set_footer(text=f"🏦 Banco al límite ({MAX_BANK:,}). El excedente fue al balance.")
                else:
                    embed.add_field(
                        name="Total cobrado",
                        value=f"{COIN} **{total_ganado:,}** Enviados a tu banco.",
                        inline=False,
                    )
                    embed.set_footer(text="💷 Tus collects se enviarán al banco.")
            else:
                embed.set_footer(text="💷 Tus collects se enviarán al banco.")

            await ctx.message.reply(embed=embed)

            # ── Persistir a DB de forma garantizada ──────────────────────
            # Se awaita directamente para asegurar que el banco y cooldowns
            # queden escritos en DB antes de terminar el handler.
            # Blindado contra reinicios inmediatos post-collect.
            if cobros and total_ganado > 0:
                try:
                    await flush_user_to_db(user_id)
                    await save_collect_cooldowns(user_id, cobros)
                except Exception as e:
                    logger.warning(f"collect persist error [{user_id}]: {e}")

        except Exception as e:
            logger.error(f"ERROR !collect: {e}")
            await ctx.send("❌ Ocurrió un error al procesar tu collect.")


async def setup(bot):
    await bot.add_cog(Collect(bot))
