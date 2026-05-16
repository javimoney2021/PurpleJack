from discord.ext import commands
import discord
from core.database import (
    get_user, update_balance, update_bank, get_all_items, get_item_by_name,
    add_to_inventory, get_inventory, remove_from_inventory,
    reduce_stock, add_cargo_temporal
)
from core import cache
from core.config import COIN
import time
import re

# ── CONFIG ─────────────────────────────────────────────
LOG_CHANNEL_ID = 1503681101422526494
ITEMS_PER_PAGE = 5
PURPLE = 0x9B59B6
TARJETA_CREDITO_ROL_ID = 1505205139416551527


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

        await interaction.response.defer(ephemeral=False)

        for item in self.children:
            item.disabled = True

        try:
            items_fresh = await get_all_items()
            item_fresh = next((i for i in items_fresh if i["id"] == self.item["id"]), None)

            if not item_fresh:
                return await interaction.edit_original_response(
                    content="❌ El item ya no existe en la tienda.", view=self
                )
            if item_fresh["stock"] == 0:
                return await interaction.edit_original_response(
                    content=f"❌ **{item_fresh['nombre']}** sin stock.", view=self
                )

            # ── Validar límite por usuario ─────────────
            limite = item_fresh.get("limite_por_usuario", 0)
            if limite and limite > 0:
                inv = cache.get_inventory_cache(interaction.user.id)
                if inv is None:
                    from core.database import get_inventory
                    inv = await get_inventory(interaction.user.id)
                poseidos = next((i["cantidad"] for i in inv if i["id"] == item_fresh["id"]), 0)
                if poseidos >= limite:
                    return await interaction.edit_original_response(
                        content=f"🫤 Has alcanzado el limite de compra de **{limite}** unidad/es de **{item_fresh['nombre']}** Por usuario.",
                        view=self
                    )

            cantidad_compra = item_fresh.get("cantidad", 1)
            total = item_fresh["precio"]
            user = await get_user(interaction.user.id)

            tiene_tarjeta = any(r.id == TARJETA_CREDITO_ROL_ID for r in interaction.user.roles)

            if tiene_tarjeta:
                if user["bank"] < total:
                    return await interaction.edit_original_response(
                        content=f"❌ No tienes suficiente banco. Necesitas **{total}** {COIN}.",
                        view=self
                    )
                await update_bank(interaction.user.id, -total)
            else:
                if user["balance"] < total:
                    return await interaction.edit_original_response(
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

            if tiene_tarjeta:
                cashback = int(total * 0.08)
                await update_bank(interaction.user.id, cashback)
                await interaction.edit_original_response(
                    content=(
                        f"✅ **{nombre_display}** Has comprado **{cantidad_compra}x {icono} {item_fresh['nombre']}** "
                        f"exitosamente. Consulta tu `!inv` para verificarlo.\n"
                        f"💳 Por poseer **Tarjeta de Credito** Recibes: **{cashback}** {COIN} de Cashback depositado en tu banco."
                    ),
                    view=self
                )
            else:
                await interaction.edit_original_response(
                    content=(
                        f"✅ **{nombre_display}** Has comprado **{cantidad_compra}x {icono} {item_fresh['nombre']}** "
                        f"exitosamente. Consulta tu `!inv` para verificarlo."
                    ),
                    view=self
                )

            # ── Log de compra ──────────────────────────
            log_channel = self.bot.get_channel(LOG_CHANNEL_ID)
            if log_channel:
                nombre_log = interaction.user.nick or interaction.user.display_name
                await log_channel.send(
                    f"🛒 **{nombre_log}** compró {icono} **{item_fresh['nombre']}**"
                )

        except Exception as e:
            print(f"ERROR ConfirmBuyView confirmar: {e}")
            try:
                await interaction.edit_original_response(content="❌ Error al procesar la compra.", view=self)
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
        match = re.search(r'<a?:(\w+):(\d+)>', COIN)
        coin_emoji = discord.PartialEmoji(name=match.group(1), id=int(match.group(2))) if match else None

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
        self.clear_items()

        total_pages = (len(self.items) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
        start = self.page * ITEMS_PER_PAGE
        page_items = self.items[start:start + ITEMS_PER_PAGE]

        container = discord.ui.Container(accent_color=PURPLE)

        container.add_item(discord.ui.TextDisplay(
            f"## 🛒 TIENDA - NAVE SUS\n"
            f"<@{self.author_id}> Compra el item de tu preferencia o Usa `!info [nombre]` para ver la info completa del item.\n"
        ))
        container.add_item(discord.ui.Separator())

        for item in page_items:
            icono = item["icono"] if item["icono"] else "🔹"
            if item["stock"] == -1:
                stock_txt = "∞"
            elif item["stock"] == 0:
                stock_txt = "❌ Agotado"
            else:
                stock_txt = str(item["stock"])

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

        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(
            f"-# Página {self.page + 1}/{total_pages}  •  Las compras se descuentan del balance principal."
        ))

        self.add_item(container)

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


# ── BOTON DE USAR ITEM (inventario) ───────────────────

class UseButton(discord.ui.Button):
    def __init__(self, item, author_id, guild, bot):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label="Usar",
            emoji="⚡",
            custom_id=f"use_{item['id']}"
        )
        self.item = item
        self.author_id = author_id
        self.guild = guild
        self.bot = bot

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message(
                "❌ Este panel no fue generado por ti.", ephemeral=True
            )

        await interaction.response.defer(ephemeral=True)

        try:
            items = await get_inventory(interaction.user.id)
            item = next((i for i in items if i["id"] == self.item["id"]), None)

            if not item:
                return await interaction.followup.send(
                    f"❌ Ya no tienes **{self.item['nombre']}** en tu inventario.", ephemeral=True
                )

            await remove_from_inventory(interaction.user.id, item["nombre"])

            if item.get("rol_id"):
                role = self.guild.get_role(int(item["rol_id"]))
                if role:
                    await interaction.user.add_roles(role)
                    duracion = item.get("duracion", 0)
                    if duracion and duracion > 0:
                        expira_en = time.time() + (duracion * 86400)
                        await add_cargo_temporal(
                            interaction.user.id,
                            self.guild.id,
                            int(item["rol_id"]),
                            expira_en
                        )

            icono = item["icono"] if item["icono"] else "🔹"
            mensaje = item["mensaje_uso"] if item["mensaje_uso"] else f"Usaste {icono} **{item['nombre']}**."

            await interaction.followup.send(
                content=f"{interaction.user.mention} {mensaje}",
                ephemeral=True
            )

            # Recargar inventario real y reconstruir el layout
            items_actualizados = await get_inventory(interaction.user.id)
            inv_view: InventarioLayout = self.view
            if items_actualizados:
                inv_view.items = items_actualizados
                inv_view._build()
                await interaction.message.edit(view=inv_view)
            else:
                await interaction.message.delete()

            # ── Log de uso ──────────────────────────
            log_channel = self.bot.get_channel(LOG_CHANNEL_ID)
            if log_channel:
                nombre_log = interaction.user.nick or interaction.user.display_name
                await log_channel.send(
                    f"✨ **{nombre_log}** usó {icono} **{item['nombre']}**"
                )

        except Exception as e:
            print(f"ERROR UseButton callback: {e}")
            try:
                await interaction.followup.send("❌ Error al usar el item.", ephemeral=True)
            except Exception:
                pass


# ── INVENTARIO LAYOUT ──────────────────────────────────

class InventarioLayout(discord.ui.LayoutView):
    def __init__(self, items, author_id, guild, bot):
        super().__init__(timeout=60)
        self.items = items
        self.author_id = author_id
        self.guild = guild
        self.bot = bot
        self._build()

    def _build(self):
        try:
            self.clear_items()

            container = discord.ui.Container(accent_color=PURPLE)

            container.add_item(discord.ui.TextDisplay(
                f"## 🎒 INVENTARIO\n"
                f"<@{self.author_id}> Estos son los items que tienes actualmente.\n"
            ))
            container.add_item(discord.ui.Separator())

            for item in self.items:
                icono = item["icono"] if item["icono"] else "🔹"
                cantidad = item.get("cantidad", 1)

                texto = discord.ui.TextDisplay(
                    f"{icono} **{item['nombre']}** x{cantidad}"
                )

                if item["utilizable"]:
                    container.add_item(discord.ui.Section(
                        texto,
                        accessory=UseButton(item, self.author_id, self.guild, self.bot)
                    ))
                else:
                    container.add_item(texto)

            container.add_item(discord.ui.Separator())
            container.add_item(discord.ui.TextDisplay(
                f"-# Total: **{len(self.items)}** tipo(s) de item  •  Usa ⚡ para consumir un item usable."
            ))

            self.add_item(container)

        except Exception as e:
            print(f"ERROR InventarioLayout._build: {e}")
            raise

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
        items = sorted(items, key=lambda i: i["precio"])

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
        try:
            items = await get_inventory(ctx.author.id)
            if not items:
                return await ctx.send(f"🎒 {ctx.author.mention} Tu inventario está vacío.")

            import asyncio
            view = InventarioLayout(items, ctx.author.id, ctx.guild, self.bot)
            msg = await ctx.send(view=view)

            async def auto_delete():
                await asyncio.sleep(60)
                try:
                    await msg.delete()
                except Exception:
                    pass

            asyncio.create_task(auto_delete())

        except Exception as e:
            print(f"ERROR inventario command: {e}")
            await ctx.send(f"❌ Error al abrir inventario: `{e}`")


async def setup(bot):
    await bot.add_cog(Shop(bot))
