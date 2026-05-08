from discord.ext import commands
import discord
import time
from core.database import (
    get_user, update_bank,
    load_collect_cooldowns_for_user, save_collect_cooldowns
)
from core import cache

COIN = "<:PurpleCoin:1501855737842892941>"


class Collect(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    async def collect(self, ctx):
        config = cache.get_collect_config()
        if not config:
            return await ctx.send("💷 No hay collects configurados aún.")

        user_id = ctx.author.id
        user_roles_ids = {role.id for role in ctx.author.roles}

        # Roles del usuario que tienen collect configurado
        roles_aplicables = {
            rol_id: cfg
            for rol_id, cfg in config.items()
            if rol_id in user_roles_ids
        }

        if not roles_aplicables:
            return await ctx.send(
                f"❌ {ctx.author.mention} No tienes ningún Rol con collect disponible. adquierelos en la **!tienda**"
            )

        # Cargar cooldowns desde caché o DB (lazy)
        cooldowns = cache.get_collect_cooldowns(user_id)
        if cooldowns is None:
            cooldowns = await load_collect_cooldowns_for_user(user_id)

        now = time.time()
        cobros = {}        # { rol_id: timestamp } — roles cobrados en esta ejecución
        total_ganado = 0
        lineas = []

        for rol_id, cfg in roles_aplicables.items():
            ultima_vez = cooldowns.get(rol_id, 0)
            cooldown_secs = cfg["cooldown_horas"] * 3600
            disponible_en = ultima_vez + cooldown_secs

            role_obj = ctx.guild.get_role(rol_id)
            nombre_rol = f"<@&{rol_id}>" if not role_obj else f"<@&{rol_id}>"

            if now >= disponible_en:
                # Disponible — cobrar
                cobros[rol_id] = now
                cache.update_collect_cooldown(user_id, rol_id, now)
                total_ganado += cfg["cantidad"]
                lineas.append(f"{nombre_rol}  →  {COIN} **{cfg['cantidad']}**")
            else:
                # En cooldown — mostrar timestamp relativo
                ts = int(disponible_en)
                lineas.append(f"{nombre_rol}  →  <t:{ts}:R>")

        # Guardar banco y cooldowns si hubo cobros
        if cobros:
            await update_bank(user_id, total_ganado)
            await save_collect_cooldowns(user_id, cobros)

        descripcion = "\n".join(lineas)

        embed = discord.Embed(
            title="💷 Mis Collects 💷",
            description=descripcion,
            color=discord.Color.purple()
        )

        if total_ganado > 0:
            embed.add_field(
                name="Total cobrado",
                value=f"{COIN} **{total_ganado}** enviados a tu banco.",
                inline=False
            )

        embed.set_footer(text="💷 Todos los collects se enviarán a tu banco.")
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Collect(bot))
