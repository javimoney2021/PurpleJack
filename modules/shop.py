from discord.ext import commands
import discord
from core.database import (
    get_user, update_balance, get_all_items, get_item_by_name,
    add_to_inventory, get_inventory, remove_from_inventory,
    reduce_stock, add_cargo_temporal
)
from core import cache
from core.config import COIN
import time

# ── CONFIG ─────────────────────────────────────────────
LOG_CHANNEL_ID = 1503681101422526494
ITEMS_PER_PAGE = 5
PURPLE = 0x9B59B6


# ── CONFIRMACION DE COMPRA ─────────────────────────────

class ConfirmBuyView(discord.ui.View):
    def __init__(self, author_id, item, bot):
        super().__init__(timeout=30)
        self.author_id = author_id
        self.item = item
        self.bot = bot

    @discord.ui.button(label="Comprar", style=discord.ButtonStyle.success)
    async def confirmar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message("❌ No es tu confirmación.", ephemeral=True)

        for item in self.children:
            item.disabled = True

        try:
            items_fresh = await get_all_items()
            item_fresh = next((i for i in items_fresh if i["id"] == self.item["id"]), None)

            if not item_fresh:
                return await interaction.response.edit_message(
                    content="❌ El item ya no existe en la tienda.", view=self
                )
            if item_fresh["stock"] == 0:
                return await interaction.response.edit_message(
                    content=f"❌ **{item_fresh['nombre']}** sin stock.", view=self
                )

            cantidad_compra = item_fresh.get("cantidad", 1)
            total = item_fresh["precio"]
            user = await get_user(interaction.user.id)

            if user["balance"] < total:
                return await interaction.response.edit_message(
                    content=f"❌ No tienes suficiente balance. Necesitas **{total}** {COIN}.",
                    view=self
                )

            await update_balance(interaction.user.id, -total)
            await add_to_inventory(interaction.user.id, item_fresh["id"], cantidad_compra)

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

            await interaction.response.edit_message(
                content=(
                    f"✅ **{nombre_display}** Has comprado **{cantidad_compra}x {icono} {item_fresh['nombre']}** "
                    f"exitosamente. Consulta tu `!inv` para verificarlo."
                ),
                view=self
            )

            # ── Log de compra ──────────────────────────
            log_channel = self.bot.get_channel(LOG_CHANNEL_ID)
            if log_channel:
                await log_channel.send(
                    f"{interaction.user.mention} Compró en la Tienda {icono} **{item_fresh['nombre']}**"
                )

        except Exception as e:
            print(f"ERROR ConfirmBuyView confirmar: {e}")
            try:
                await interaction.response.edit_message(content="❌ Error al procesar la compra.", view=self)
            except Exception:
                pass

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.danger)
    async def cancelar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message("❌ No es tu confirmación.", ephemeral=True)
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content="🚫 Compra cancelada.",
            view=self
        )


# ── BOTON DE COMPRA (accesorio en Section) ─────────────

class BuyButton(discord.ui.Button):
    def __init__(self, item, author_id, bot, emoji=None):
        super().__init__(
            style=discord.ButtonStyle.success if item["stock"] != 0 else discord.ButtonStyle.secondary,
            label=f"{item['precio']}",
            emoji=coin_emoji,
            disabled=item["stock"] == 0,
            custom_id=f"buy_{item['id']}"
        )
        self.item = item
        self.author_id = author_id
        self.bot = bot

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message(
                "❌ Este panel no fue generado por ti.", ephemeral=True
            )
        icono = self.item["icono"] if self.item["icono"] else "🔹"
        await interaction.response.send_message(
            content=(
                f"🛒 ¿Estás seguro que deseas comprar "
                f"**{icono} {self.item['nombre']}** por **{self.item['precio']} {COIN}**?"
            ),
            view=ConfirmBuyView(self.author_id, self.item, self.bot),
            ephemeral=True
        )


# ── BOTONES DE PAGINACION ──────────────────────────────

class PrevButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="◀ Anterior",
            custom_id="tienda_prev"
        )

    async def callback(self, interaction: discord.Interaction):
        view: TiendaLayout = self.view
        if interaction.user.id != view.author_id:
            return await interaction.response.send_message(
                "❌ Este panel no fue generado por ti.", ephemeral=True
            )
        if view.page > 0:
            view.page -= 1
        await view._update(interaction)


class NextButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="Siguiente ▶",
            custom_id="tienda_next"
        )

    async def callback(self, interaction: discord.Interaction):
        view: TiendaLayout = self.view
        if interaction.user.id != view.author_id:
            return await interaction.response.send_message(
                "❌ Este panel no fue generado por ti.", ephemeral=True
            )
        total_pages = (len(view.items) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
        if view.page < total_pages - 1:
            view.page += 1
        await view._update(interaction)


# ── TIENDA V2 LAYOUT ───────────────────────────────────

class TiendaLayout(discord.ui.LayoutView):
    def __init__(self, items, author_id, bot):
        super().__init__(timeout=60)
        self.items = items
        self.author_id = author_id
        self.bot = bot
        self.page = 0
        self._build()

    def _build(self):
        # Limpiar componentes anteriores
        self.clear_items()

        total_pages = (len(self.items) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
        start = self.page * ITEMS_PER_PAGE
        page_items = self.items[start:start + ITEMS_PER_PAGE]

        # Container con color morado
        container = discord.ui.Container(accent_color=PURPLE)

        # Título
        container.add_item(discord.ui.TextDisplay(
            f"## 🛒 TIENDA - NAVE SUS\n"
            f"<@{self.author_id}> Compra el item de tu preferencia o Usa `!info [nombre]` para ver la info completa del item.\n"
        ))
        container.add_item(discord.ui.Separator())

        # Secciones por item
        for item in page_items:
            icono = item["icono"] if item["icono"] else "🔹"
            if item["stock"] == -1:
                stock_txt = "∞"
            elif item["stock"] == 0:
                stock_txt = "❌ Agotado"
            else:
                stock_txt = str(item["stock"])

            import re
            match = re.search(r'<a?:(\w+):(\d+)>', icono)
            if match:
                emoji_obj = discord.PartialEmoji(name=match.group(1), id=int(match.group(2)))
            else:
                emoji_obj = icono if icono else None

            section = discord.ui.Section(
                discord.ui.TextDisplay(
                    f"{icono} **{item['nombre']}**\n"
                    f"{item.get('descripcion', '')}  •  Stock: **{stock_txt}**"
                ),
                accessory=BuyButton(item, self.author_id, self.bot, emoji=emoji_obj)
            )
            container.add_item(section)

        # Separador y paginación
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(
            f"-# Página {self.page + 1}/{total_pages}  •  Las compras se descuentan del balance principal."
        ))

        self.add_item(container)

        # Botones de paginación como ActionRow
        nav_row = discord.ui.ActionRow()
        prev = PrevButton()
        prev.disabled = self.page == 0
        next_ = NextButton()
        next_.disabled = self.page >= total_pages - 1
        nav_row.add_item(prev)
        nav_row.add_item(next_)
        self.add_item(nav_row)

    async def _update(self, interaction: discord.Interaction):
        self._build()
        await interaction.response.edit_message(view=self)

    async def on_timeout(self):
        pass


# ── SHOP COG ───────────────────────────────────────────

class Shop(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    async def tienda(self, ctx):
        items = await get_all_items()
        if not items:
            return await ctx.send("🛒 La tienda está vacía por ahora.")

        import asyncio
        view = TiendaLayout(items, ctx.author.id, self.bot)
        msg = await ctx.send(view=view)

        async def auto_delete():
            await asyncio.sleep(60)
            try:
                await msg.delete()
            except Exception:
                pass

        asyncio.create_task(auto_delete())

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

    @commands.command(name="inv")
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

        # ── Log de uso ─────────────────────────────────
        log_channel = self.bot.get_channel(LOG_CHANNEL_ID)
        if log_channel:
            await log_channel.send(
                f"{ctx.author.mention} Ha usado {icono} **{item['nombre']}**"
            )


async def setup(bot):
    await bot.add_cog(Shop(bot))
