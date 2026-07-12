from discord.ext import commands
from discord import app_commands, ui, ButtonStyle, Interaction
import discord
import time

from core.database import get_user, update_balance, update_bank
from core import cache
from core.config import COIN, game_config, ruleta_config, rob_config, rr_config, dados_config
from core.cache import MAX_BANK

TOP_COOLDOWN = 300


class FinanceView(ui.View):
    def __init__(self, user_id):
        super().__init__(timeout=60)
        self.user_id = user_id

    @ui.button(label="Depositar", style=ButtonStyle.green)
    async def depositar(self, interaction: Interaction, button: ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message(
                "❌ No es tu menú.", ephemeral=True
            )
        await interaction.response.send_modal(
            DepositModal(self.user_id, interaction.message)
        )

    @ui.button(label="Retirar", style=ButtonStyle.primary)
    async def retirar(self, interaction: Interaction, button: ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message(
                "❌ No es tu menú.", ephemeral=True
            )
        await interaction.response.send_modal(
            WithdrawModal(self.user_id, interaction.message)
        )

    @ui.button(label="Salir", style=ButtonStyle.danger)
    async def salir(self, interaction: Interaction, button: ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message(
                "❌ No es tu menú.", ephemeral=True
            )
        await interaction.message.delete()
        await interaction.response.defer()


class DepositModal(ui.Modal, title="Depositar al Banco"):
    amount = ui.TextInput(label="¿Cuánto deseas depositar?", placeholder="Ej: 500 o All")

    def __init__(self, user_id, message):
        super().__init__()
        self.user_id = user_id
        self.message = message

    async def on_submit(self, interaction: Interaction):
        try:
            # Una sola llamada — get_user es cache-first, sin hit extra a DB
            user = await get_user(self.user_id)
            raw  = self.amount.value.strip().lower()
            amount = user["balance"] if raw == "all" else int(raw)

            if amount <= 0:
                return await interaction.response.send_message(
                    "❌ Cantidad inválida.", ephemeral=True
                )
            if amount > user["balance"]:
                return await interaction.response.send_message(
                    "❌ No tienes suficiente balance.", ephemeral=True
                )

            # Validar límite de banco antes de proceder
            banco_actual = user["bank"]
            if banco_actual >= MAX_BANK:
                return await interaction.response.send_message(
                    f"🏦 Tu banco ya está al límite máximo ({MAX_BANK:,} {COIN}).\n"
                    f"Retira fondos antes de depositar.",
                    ephemeral=True
                )

            # Calcular cuánto cabe realmente en el banco
            espacio_disponible = MAX_BANK - banco_actual
            aplicado_banco    = min(amount, espacio_disponible)
            excedente_balance = amount - aplicado_banco

            # update_balance / update_bank hacen flush inmediato a DB
            # update_bank internamente aplica el mismo cálculo vía cache,
            # por lo que el resultado es siempre consistente.
            await update_balance(self.user_id, -amount)
            await update_bank(self.user_id, amount)

            # Refrescar datos para actualizar el embed
            user = await get_user(self.user_id)
            embed = self.message.embeds[0]
            embed.set_field_at(0, name=embed.fields[0].name,
                               value=f"{user['balance']} {COIN}", inline=True)
            embed.set_field_at(1, name=embed.fields[1].name,
                               value=f"{user['bank']} {COIN}",    inline=True)
            await self.message.edit(embed=embed)

            # Informar distribución si el banco se llenó durante el depósito
            if excedente_balance > 0:
                await interaction.response.send_message(
                    f"🏦 Banco lleno: **{aplicado_banco:,}** {COIN} depositados al banco.\n"
                    f"💰 **{excedente_balance:,}** {COIN} quedaron en tu balance.",
                    ephemeral=True
                )
            else:
                await interaction.response.defer()
        except ValueError:
            await interaction.response.send_message(
                "❌ Ingresa un número válido.", ephemeral=True
            )


class WithdrawModal(ui.Modal, title="Retirar del Banco"):
    amount = ui.TextInput(label="¿Cuánto deseas retirar?", placeholder="Ej: 500 o All")

    def __init__(self, user_id, message):
        super().__init__()
        self.user_id = user_id
        self.message = message

    async def on_submit(self, interaction: Interaction):
        try:
            # Una sola llamada — get_user es cache-first, sin hit extra a DB
            user = await get_user(self.user_id)
            raw  = self.amount.value.strip().lower()
            amount = user["bank"] if raw == "all" else int(raw)

            if amount <= 0:
                return await interaction.response.send_message(
                    "❌ Cantidad inválida.", ephemeral=True
                )
            if amount > user["bank"]:
                return await interaction.response.send_message(
                    "❌ No tienes suficiente en el banco.", ephemeral=True
                )

            await update_bank(self.user_id, -amount)
            await update_balance(self.user_id, amount)

            user = await get_user(self.user_id)
            embed = self.message.embeds[0]
            embed.set_field_at(0, name=embed.fields[0].name,
                               value=f"{user['balance']} {COIN}", inline=True)
            embed.set_field_at(1, name=embed.fields[1].name,
                               value=f"{user['bank']} {COIN}",    inline=True)
            await self.message.edit(embed=embed)
            await interaction.response.defer()
        except ValueError:
            await interaction.response.send_message(
                "❌ Ingresa un número válido.", ephemeral=True
            )


class Economy(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="bal")
    async def balance(self, ctx):
        user = await get_user(ctx.author.id)
        embed = discord.Embed(
            title=f"💰 Finanzas de {ctx.author.display_name}",
            color=discord.Color.purple(),
        )
        embed.add_field(name=f"{COIN} Balance", value=f"{user['balance']} {COIN}", inline=True)
        embed.add_field(name="🏦 Banco",        value=f"{user['bank']} {COIN}",    inline=True)
        embed.set_thumbnail(url=ctx.author.display_avatar.url)
        await ctx.message.reply(embed=embed, view=FinanceView(ctx.author.id), delete_after=120)

    def format_cooldown(self, seconds: int) -> str:
        if seconds >= 3600:
            return f"{seconds // 3600}h"
        return f"{seconds // 60}m"

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.CommandOnCooldown):
            retry = int(error.retry_after)
            if retry >= 60:
                tiempo = f"{retry // 60}m {retry % 60}s" if retry % 60 else f"{retry // 60}m"
            else:
                tiempo = f"{retry}s"
            await ctx.send(
                f"⏳ {ctx.author.mention} Podrás usar este comando de nuevo en **{tiempo}**.",
                delete_after=10,
            )
        else:
            raise error

    @commands.command(name="cd")
    @commands.cooldown(1, 30, commands.BucketType.user)
    async def cooldowns(self, ctx):
        embed = discord.Embed(
            title="⏱️ Cooldowns Actuales",
            color=discord.Color.purple(),
        )
        embed.set_thumbnail(
            url="https://raw.githubusercontent.com/javimoney2021/PurpleJack/main/Thumbs/CD.png"
        )

        work_cd   = self.format_cooldown(game_config["work"]["cooldown"])
        crime_cd  = self.format_cooldown(game_config["crime"]["cooldown"])
        ruleta_cd = self.format_cooldown(ruleta_config["cooldown"])
        rob_cd    = self.format_cooldown(rob_config["cooldown"])
        rr_cd     = self.format_cooldown(rr_config["cooldown"])
        dados_cd  = self.format_cooldown(dados_config["cooldown"])

        descripcion = (
            f"**!work**     — Cada {work_cd}\n"
            f"**!crime**    — Cada {crime_cd}\n"
            f"**!ruleta**   — Cada {ruleta_cd}\n"
            f"**!rr**       — Cada {rr_cd}\n"
            f"**!rob**      — Cada {rob_cd}\n"
            f"**!dados**    — Cada {dados_cd}\n"
            f"**!collect**  — Configurado por Rol **(ver !collect)**"
        )
        embed.description = descripcion

        collect_config = cache.get_collect_config()
        if collect_config:
            lineas_collect = []
            for rol_id, cfg in collect_config.items():
                horas = cfg["cooldown_horas"]
                if horas >= 1 and horas == int(horas):
                    tiempo = f"{int(horas)} hora" if horas == 1 else f"{int(horas)} horas"
                else:
                    minutos = int(round(horas * 60))
                    tiempo  = f"{minutos} minuto" if minutos == 1 else f"{minutos} minutos"
                lineas_collect.append(f"<@&{rol_id}>: **{cfg['cantidad']}** {COIN}")
            embed.add_field(
                name="**Cargos con Collect Activo**",
                value="\n".join(lineas_collect),
                inline=False,
            )

        await ctx.send(embed=embed, delete_after=15)

    @commands.command()
    async def top(self, ctx):
        user_id = ctx.author.id

        if cache.check_top_cooldown(user_id):
            return await ctx.send(
                f"⏳ {ctx.author.mention} Espera antes de consultar el top de nuevo.",
                delete_after=10,
            )

        cache.set_top_cooldown(user_id)
        await cache.flush_to_db()

        from core.database import pool

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, balance FROM users ORDER BY balance DESC LIMIT 15"
            )

        # Usar la función pública get_all_cache() en lugar de importar _cache
        user_cache = cache.get_all_cache()

        resultados = []
        for row in rows:
            uid = row["id"]
            if uid in user_cache:
                total = user_cache[uid]["balance"]
            else:
                total = row["balance"]
            resultados.append((uid, total))

        resultados.sort(key=lambda x: x[1], reverse=True)

        medallas    = ["🥇", "🥈", "🥉"]
        descripcion = ""

        for i, (uid, balance) in enumerate(resultados):
            member = ctx.guild.get_member(uid)
            nombre = member.display_name if member else f"<@{uid}>"
            posicion     = medallas[i] if i < 3 else f"**{i+1}.**"
            descripcion += f"{posicion} {nombre} —— {COIN} **{balance}**\n"

        embed = discord.Embed(
            title=f"{COIN} TOP BALANCES MAS RICOS {COIN}",
            description=descripcion,
            color=discord.Color.blue(),
        )
        embed.set_footer(text="Solo se muestra el Top 15 de los más ricos.")
        await ctx.send(embed=embed, delete_after=60)

    @app_commands.command(name="ayuda_nave", description="Muestra la guía de la Nave-Sus")
    async def ayuda_nave(self, interaction: discord.Interaction):
        from core.database import get_nave_contenido
        contenido = await get_nave_contenido()
        if not contenido:
            return await interaction.response.send_message(
                "❌ La guía aún no ha sido configurada.", ephemeral=True
            )
        embed = discord.Embed(
            title="🚀 Guía de la Nave-Sus",
            description=contenido,
            color=discord.Color.teal(),
        )
        embed.set_footer(text="Usa los comandos de economía para crecer en la nave.")
        await interaction.response.send_message(embed=embed, ephemeral=True, delete_after=25)

    @commands.command(name="prob")
    @commands.cooldown(1, 150, commands.BucketType.user)
    async def probabilidades(self, ctx):
        crime_exito  = int(game_config["crime"]["ganar_prob"] * 100)
        crime_fallo  = int(game_config["crime"]["perder_prob"] * 100)
        rob_exito    = int(rob_config["exito_prob"] * 100)
        rob_fallo    = int(rob_config["fallo_prob"] * 100)
        rr_exito     = int(rr_config["ganar_prob"] * 100)
        rr_fallo     = int(rr_config["perder_prob"] * 100)
        dados_exito  = int(dados_config["exito_prob"] * 100)
        dados_fallo  = int(dados_config["fallo_prob"] * 100)

        embed = discord.Embed(
            title="🍀 Probabilidades Actuales",
            color=discord.Color.purple(),
        )
        embed.add_field(
            name="",
            value=(
                f"**!crime** — Éxito: `{crime_exito}%` · Fallo: `{crime_fallo}%`\n"
                f"**!rr** — Éxito: `{rr_exito}%` · Fallo: `{rr_fallo}%` *(por disparo)*\n"
                f"**!rob** — Éxito: `{rob_exito}%` · Fallo: `{rob_fallo}%`\n"
                f"**!dados** — Éxito: `{dados_exito}%` · Fallo: `{dados_fallo}%`"
            ),
            inline=False,
        )
        await ctx.send(embed=embed, delete_after=25)


async def setup(bot):
    await bot.add_cog(Economy(bot))
