from discord.ext import commands
import discord
from core.database import (
    get_user, update_balance, get_all_items, get_item_by_name,
    add_to_inventory, get_inventory, remove_from_inventory,
    reduce_stock, add_cargo_temporal
)
from core import cache
import time

COIN = "<:PurpleCoin:1501855737842892941>"


# ── VISTA DE TIENDA ────────────────────────────────────

class TiendaView(discord.ui.View):
    def __init__(self, items, author_id):
        super().__init__(timeout=120)
        self.author_id = author_id
        for item in items:
            sin_stock = item["stock"] == 0
            self.add_item(ItemBoton(
                item=item,
                label=f"{item['nombre']} • {item['precio']}",
                emoji=None,
                disabled=sin_stock,
                author_id=author_id
            ))


class ItemBoton(discord.ui.Button):
    def __init__(self, item, label, emoji, disabled, author_id):
        super().__init__(
            style=discord.ButtonStyle.success if not disabled else discord.ButtonStyle.secondary,
            label=label,
            emoji=emoji,
            disabled=disabled,
            custom_id=f"buy_{item['id']}"
        )
        self.item = item
        self.author_id = author_id

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message(
                "❌ Este panel no fue generado por ti.", ephemeral=True
            )

        item = self.item

        # Refresco stock desde caché
        items_fresh = await get_all_items()
        item_fresh = next((i for i in items_fresh if i["id"] == item["id"]), None)
        if not item_fresh:
            return await interaction.response.send_message(
                "❌ El item ya no existe en la tienda.", ephemeral=True
            )
        if item_fresh["stock"] == 0:
            return await interaction.response.send_message(
                f"**{item_fresh['nombre']}** Sin Stock por el momento, vuelve después...",
                ephemeral=False
            )

        cantidad_compra = item_fresh.get("cantidad", 1)
        total = item_fresh["precio"]
        user = await get_user(interaction.user.id)

        if user["balance"] < total:
            return await interaction.response.send_message(
                f"❌ No tienes suficiente balance. Necesitas **{total}** {COIN}.",
                ephemeral=False
            )

        await update_balance(interaction.user.id, -total)
        await add_to_inventory(interaction.user.id, item_fresh["id"], cantidad_compra)

        # Actualizar caché de inventario
        cache.add_to_inventory_cache(interaction.user.id, {
            "id": item_fresh["id"],
            "nombre": item_fresh["nombre"],
            "icono": item_fresh["icono"],
            "utilizable": item_fresh["utilizable"],
            "mensaje_uso": item_fresh["mensaje_uso"],
            "rol_id": item_fresh["rol_id"],
            "duracion": item_fresh.get("duracion", 0),
            "cantidad": cantidad_compra
        })

        if item_fresh["stock"] != -1:
            await reduce_stock(item_fresh["id"])

        icono = item_fresh["icono"] if item_fresh["icono"] else "🔹"
        nombre_display = interaction.user.nick or interaction.user.display_name
        await interaction.response.send_message(
            f"**{nombre_display}** Has comprado **{cantidad_compra}x {icono} {item_fresh['nombre']}** "
            f"exitosamente... Consulta tu `!inventario` para verificarlo.",
            ephemeral=False
        )


# ── SHOP COG ───────────────────────────────────────────

class Shop(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    async def tienda(self, ctx):
        items = await get_all_items()

        if not items:
            return await ctx.send("🛒 La tienda está vacía por ahora.")

        embed = discord.Embed(
            title="🛒 Tienda PurpleJack",
            description=(
                "Haz click en los botones abajo para comprar un item instantáneamente.\n"
                f"Usa `!info [nombre]` para ver los detalles completos de un item."
            ),
            color=discord.Color.purple()
        )

        for item in items:
            icono = item["icono"] if item["icono"] else "🔹"
            if item["stock"] == -1:
                stock_txt = "∞"
            elif item["stock"] == 0:
                stock_txt = "❌ Agotado"
            else:
                stock_txt = str(item["stock"])
            embed.add_field(
                name=f"{icono} {item['nombre']}",
                value=f"{item.get('descripcion', '')}  •  Stock: **{stock_txt}**",
                inline=False
            )

        embed.set_footer(text="Las compras se descuentan del balance principal.")
        view = TiendaView(items, ctx.author.id)
        await ctx.send(embed=embed, view=view)

    @commands.command()
    async def info(self, ctx, *, nombre: str = None):
        if nombre is None:
            return await ctx.send(f"❌ Formato: `!info {{nombre del item}}`")

        item = await get_item_by_name(nombre)
        if not item:
            return await ctx.send(f"❌ Item `{nombre}` no encontrado en la tienda.")

        icono = item["icono"] if item["icono"] else "🔹"

        if item["stock"] == -1:
            stock_txt = "∞ Ilimitado"
        elif item["stock"] == 0:
            stock_txt = "❌ Agotado"
        else:
            stock_txt = str(item["stock"])

        usable_txt = "✅ Sí" if item["utilizable"] else "❌ No"

        embed = discord.Embed(
            title=f"{icono} {item['nombre']}",
            description=item.get("descripcion_larga") or "*Sin descripción disponible.*",
            color=discord.Color.purple()
        )
        embed.add_field(name="💰 Precio", value=f"{item['precio']} {COIN}", inline=True)
        embed.add_field(name="📦 Stock", value=stock_txt, inline=True)
        embed.add_field(name="🎯 Usable", value=usable_txt, inline=True)

        duracion = item.get("duracion", 0)
        if item["rol_id"] and duracion is not None:
            dur_txt = "Permanente" if duracion == 0 else f"{duracion} día(s)"
            embed.add_field(name="⏳ Duración del Cargo", value=dur_txt, inline=True)

        await ctx.send(embed=embed)

    @commands.command()
    async def inventario(self, ctx):
        items = await get_inventory(ctx.author.id)
        if not items:
            return await ctx.send(f"🎒 {ctx.author.mention} Tu inventario está vacío.")

        embed = discord.Embed(
            title=f"🎒 Inventario de {ctx.author.display_name}",
            color=discord.Color.purple()
        )
        descripcion = ""
        for item in items:
            icono = item["icono"] if item["icono"] else "🔹"
            usable = "✅ Usable" if item["utilizable"] else "—"
            descripcion += f"— {icono} **{item['nombre']}** x{item['cantidad']} | {usable}\n"

        embed.description = descripcion
        embed.set_footer(text="Usa !usar {nombre} para usar un item.")
        await ctx.send(embed=embed)

    @commands.command()
    async def usar(self, ctx, *, nombre: str = None):
        if nombre is None:
            return await ctx.send(f"❌ {ctx.author.mention} Formato: `!usar {{nombre del item}}`")

        items = await get_inventory(ctx.author.id)
        item = next((i for i in items if i["nombre"].lower() == nombre.lower().strip()), None)

        if not item:
            return await ctx.send(f"❌ {ctx.author.mention} No tienes `{nombre}` en tu inventario.")

        if not item["utilizable"]:
            return await ctx.send(f"❌ {ctx.author.mention} Este item no es usable.")

        await remove_from_inventory(ctx.author.id, nombre)

        # Asignar cargo si el item tiene rol configurado
        if item.get("rol_id"):
            role = ctx.guild.get_role(int(item["rol_id"]))
            if role:
                await ctx.author.add_roles(role)
                duracion = item.get("duracion", 0)
                if duracion and duracion > 0:
                    expira_en = time.time() + (duracion * 86400)
                    await add_cargo_temporal(
                        ctx.author.id,
                        ctx.guild.id,
                        int(item["rol_id"]),
                        expira_en
                    )            
                
        icono = item["icono"] if item["icono"] else "🔹"
        mensaje = item["mensaje_uso"] if item["mensaje_uso"] else f"Usaste {icono} **{item['nombre']}**."

        embed = discord.Embed(
            description=f"{ctx.author.mention} {mensaje}",
            color=discord.Color.gold()
        )
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Shop(bot))
