from discord.ext import commands
from discord import ui, ButtonStyle, Interaction
import discord
import time
from core.database import get_user, update_balance, update_bank

from core import cache

class FinanceView(ui.View):
    def __init__(self, user_id):
        super().__init__(timeout=60)
        self.user_id = user_id

    @ui.button(label="Depositar", style=ButtonStyle.green)
    async def depositar(self, interaction: Interaction, button: ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ No es tu menú.", ephemeral=True)
        await interaction.response.send_modal(DepositModal(self.user_id))

    @ui.button(label="Retirar", style=ButtonStyle.red)
    async def retirar(self, interaction: Interaction, button: ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ No es tu menú.", ephemeral=True)
        await interaction.response.send_modal(WithdrawModal(self.user_id))


class DepositModal(ui.Modal, title="Depositar al Banco"):
    amount = ui.TextInput(label="¿Cuánto deseas depositar?", placeholder="Ej: 500")

    def __init__(self, user_id):
        super().__init__()
        self.user_id = user_id

    async def on_submit(self, interaction: Interaction):
        try:
            amount = int(self.amount.value)
            if amount <= 0:
                return await interaction.response.send_message("❌ Cantidad inválida.", ephemeral=True)
            user = await get_user(self.user_id)
            if amount > user["balance"]:
                return await interaction.response.send_message("❌ No tienes suficiente balance.", ephemeral=True)
            await update_balance(self.user_id, -amount)
            await update_bank(self.user_id, amount)
            await interaction.response.send_message(f"✅ Depositaste **{amount}**<:PurpleCoin:1501855737842892941> al banco.", ephemeral=False)
        except ValueError:
            await interaction.response.send_message("❌ Ingresa un número válido.", ephemeral=True)


class WithdrawModal(ui.Modal, title="Retirar del Banco"):
    amount = ui.TextInput(label="¿Cuánto deseas retirar?", placeholder="Ej: 500")

    def __init__(self, user_id):
        super().__init__()
        self.user_id = user_id

    async def on_submit(self, interaction: Interaction):
        try:
            amount = int(self.amount.value)
            if amount <= 0:
                return await interaction.response.send_message("❌ Cantidad inválida.", ephemeral=True)
            user = await get_user(self.user_id)
            if amount > user["bank"]:
                return await interaction.response.send_message("❌ No tienes suficiente en el banco.", ephemeral=True)
            await update_bank(self.user_id, -amount)
            await update_balance(self.user_id, amount)
            await interaction.response.send_message(f"✅ Retiraste **{amount}**<:PurpleCoin:1501855737842892941> del banco.", ephemeral=True)
        except ValueError:
            await interaction.response.send_message("❌ Ingresa un número válido.", ephemeral=True)


class Economy(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="bal")
    async def balance(self, ctx):
        user = await get_user(ctx.author.id)
        embed = discord.Embed(
            title=f"💰 Finanzas de {ctx.author.display_name}",
            color=discord.Color.purple()
        )
        embed.add_field(name="<:PurpleCoin:1501855737842892941> Balance", value=f"{user['balance']} ", inline=True)
        embed.add_field(name="🏦 Banco", value=f"{user['bank']} ", inline=True)
        embed.set_thumbnail(url=ctx.author.display_avatar.url)
        await ctx.send(embed=embed, view=FinanceView(ctx.author.id))

    @commands.command()
    async def top(self, ctx):
        from core.cache import _cache
        from core.database import pool

        now = time.time()
        user_id = ctx.author.id

        if user_id in top_cooldowns:
            elapsed = now - top_cooldowns[user_id]
            if elapsed < TOP_COOLDOWN:
                remaining = int(TOP_COOLDOWN - elapsed)
                minutos = remaining // 60
                segundos = remaining % 60
                return await ctx.send(
                    f"⏳ {ctx.author.mention} Espera **{minutos}m {segundos}s** para ver el top de nuevo.",
                    delete_after=10
                )

        top_cooldowns[user_id] = now

        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT id, balance FROM users ORDER BY balance DESC LIMIT 10")

        resultados = []
        for row in rows:
            uid = row["id"]
            balance = _cache[uid]["balance"] if uid in _cache else row["balance"]
            resultados.append((uid, balance))

        resultados.sort(key=lambda x: x[1], reverse=True)

        medallas = ["🥇", "🥈", "🥉"]
        descripcion = ""

        for i, (uid, balance) in enumerate(resultados):
            try:
                member = await ctx.guild.fetch_member(uid)
                nombre = member.display_name
            except:
                nombre = "Usuario desconocido"
            posicion = medallas[i] if i < 3 else f"**#{i+1}**"
            descripcion += f"{posicion} {nombre} —— <:PurpleCoin:1501855737842892941> **{balance}** \n"

        embed = discord.Embed(
            title="<:PurpleCoin:1501855737842892941> TOP GLOBAL MÁS RICOS <:PurpleCoin:1501855737842892941>",
            description=descripcion,
            color=discord.Color.blue()
        )
        embed.set_footer(text="Solo se muestra el Top 10 de los más ricos.")
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Economy(bot))
