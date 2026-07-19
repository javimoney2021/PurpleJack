from discord.ext import commands
import discord
import logging
import asyncio
from core.database import (
    update_bank, get_all_items, get_item_by_name, purchase_item,
    get_inventory, remove_from_inventory, add_cargo_temporal
)
from core import cache
from core.config import COIN, LOG_CHANNEL_ID, TARJETA_CREDITO_ROL_ID, STAFF_ROLE_ID
import time
import re

logger = logging.getLogger(__name__)

# ── CONFIG ─────────────────────────────────────────────
ITEMS_PER_PAGE = 5
PURPLE = 0x9B59B6
SHOP_EXPIRE_SECONDS = 150
SHOP_EXPIRED_MESSAGE = "Tienda Caducó, consulte la tienda nuevamente"


# ── CONFIRMACION DE COMPRA ─────────────────────────────

class ConfirmBuyView(discord.ui.View):
    def __init__(self, author_id, item, bot, unidades: int = 1):
        super().__init__(timeout=30)
        self.author_id = author_id
        self.item = item
        self.bot = bot
        self.unidades = unidades  # cantidad elegida en el modal

    @discord.ui.button(label="Comprar", style=discord.ButtonStyle.success)
    async def confirmar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message("❌ No es tu confirmación.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)

        for item in self.children:
            item.disabled = True

        try:
            tiene_tarjeta = any(r.id == TARJETA_CREDITO_ROL_ID for r in interaction.user.roles)
            purchase = await purchase_item(
                interaction.user.id,
                self.item["id"],
                self.unidades,
                use_bank=tiene_tarjeta,
            )

            if not purchase["ok"]:
                reason = purchase["reason"]
                item_fresh = purchase.get("item") or self.item
                precio_unitario = purchase.get("precio_unitario", item_fresh.get("precio", 0))
                total = purchase.get("total", precio_unitario * self.unidades)

                if reason == "not_found":
                    return await interaction.edit_original_response(
                        content="❌ El item ya no existe en la tienda.", view=self
                    )
                if reason == "out_of_stock":
                    return await interaction.edit_original_response(
                        content=f"❌ **{item_fresh['nombre']}** sin stock.", view=self
                    )
                if reason == "insufficient_stock":
                    return await interaction.edit_original_response(
                        content=f"❌ Solo hay **{purchase['available']}** unidad/es disponibles de **{item_fresh['nombre']}**.",
                        view=self
                    )
                if reason == "limit":
                    disponibles = purchase["available"]
                    limite = purchase["limit"]
                    if disponibles <= 0:
                        return await interaction.edit_original_response(
                            content=f"🫤 Has alcanzado el limite de compra de **{limite}** unidad/es de **{item_fresh['nombre']}** Por usuario.",
                            view=self
                        )
                    return await interaction.edit_original_response(
                        content=f"🫤 Solo puedes comprar **{disponibles}** unidad/es más de **{item_fresh['nombre']}** (límite: {limite} por usuario).",
                        view=self
                    )
                if reason == "insufficient_bank":
                    return await interaction.edit_original_response(
                        content=f"❌ No tienes suficiente banco. Necesitas **{total}** {COIN} ({self.unidades}x {precio_unitario}).",
                        view=self
                    )
                if reason == "insufficient_balance":
                    return await interaction.edit_original_response(
                        content=f"❌ {interaction.user.mention} No tienes suficiente balance. Necesitas **{total}** {COIN} ({self.unidades}x {precio_unitario}),\nO una 💳 **Tarjeta de Crédito para usar el dinero de tu Banco directamente**",
                        view=self
                    )
                return await interaction.edit_original_response(
                    content="❌ No se pudo procesar la compra.", view=self
                )

            item_fresh = purchase["item"]
            precio_unitario = purchase["precio_unitario"]
            total = purchase["total"]
            cantidad_compra = purchase["cantidad_compra"]

            icono = item_fresh["icono"] if item_fresh["icono"] else "🔹"
            nombre_display = interaction.user.nick or interaction.user.display_name

            if tiene_tarjeta:
                cashback = int(total * 0.08)
                await update_bank(interaction.user.id, cashback)
                await interaction.edit_original_response(
                    content=(
                        f"✅ **{nombre_display}** Has comprado **{cantidad_compra}x {icono} {item_fresh['nombre']}** "
                        f"por **{total}** {COIN} exitosamente. Consulta tu `!inv` para verificarlo.\n"
                        f"💳 Por poseer **Tarjeta de Credito** Recibes: **{cashback}** {COIN} de Cashback depositado en tu banco."
                    ),
                    view=self
                )
            else:
                await interaction.edit_original_response(
                    content=(
                        f"✅ **{nombre_display}** Has comprado **{cantidad_compra}x {icono} {item_fresh['nombre']}** "
                        f"por **{total}** {COIN} exitosamente. Consulta tu `!inv` para verificarlo."
                    ),
                    view=self
                )

            # ── Log de compra ──────────────────────────
            log_channel = self.bot.get_channel(LOG_CHANNEL_ID)
            if log_channel:
                nombre_log = interaction.user.nick or interaction.user.display_name
                await log_channel.send(
                    f"🛒 **{nombre_log}** compró {cantidad_compra}x {icono} **{item_fresh['nombre']}** por **{total}** {COIN}"
                )

        except Exception as e:
            logger.error(f"ERROR ConfirmBuyView confirmar: {e}")
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


# ── MODAL DE CANTIDAD ──────────────────────────────────

class QuantityModal(discord.ui.Modal):
    def __init__(self, item, author_id, bot):
        super().__init__(title=f"Comprar {item['nombre'][:40]}")
        self.item = item
        self.author_id = author_id
        self.bot = bot

        # Calcular máximo según stock (∞ → sin límite superior en el campo)
        stock = item["stock"]
        placeholder = "Ej: 1" if stock == -1 else f"Máx disponible: {stock}"

        self.cantidad_input = discord.ui.TextInput(
            label=f"¿Cuántas unidades deseas adquirir?",
            placeholder=placeholder,
            min_length=1,
            max_length=4,
            required=True
        )
        self.add_item(self.cantidad_input)

    async def on_submit(self, interaction: discord.Interaction):
        # ── Validar que sea un número entero positivo ──────────
        try:
            unidades = int(self.cantidad_input.value.strip())
        except ValueError:
            return await interaction.response.send_message(
                "❌ Ingresa un número entero válido.", ephemeral=True
            )

        if unidades <= 0:
            return await interaction.response.send_message(
                "❌ La cantidad debe ser mayor a 0.", ephemeral=True
            )

        # ── Validar contra el stock disponible ─────────────────
        stock = self.item["stock"]
        if stock != -1 and unidades > stock:
            return await interaction.response.send_message(
                f"❌ Solo hay **{stock}** unidad/es disponibles de **{self.item['nombre']}**.",
                ephemeral=True
            )

        icono = self.item["icono"] if self.item["icono"] else "🔹"
        precio_unitario = self.item["precio"]
        total = precio_unitario * unidades

        await interaction.response.send_message(
            content=(
                f"{interaction.user.mention} 🛒 ¿Confirmas la compra de "
                f"**{unidades}x {icono} {self.item['nombre']}**?\n"
                f"💰 Precio unitario: **{precio_unitario}** {COIN}  •  "
                f"**Total: {total} {COIN}**"
            ),
            view=ConfirmBuyView(self.author_id, self.item, self.bot, unidades=unidades),
            ephemeral=True
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
        view: TiendaLayout = self.view
        if view and view.is_shop_expired():
            return await view.send_expired_message(interaction)
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message(
                "❌ Este panel no fue generado por ti.", ephemeral=True
            )
        # ── Abrir modal de cantidad antes de confirmar ─────────
        await interaction.response.send_modal(
            QuantityModal(self.item, self.author_id, self.bot)
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
        if view.is_shop_expired():
            return await view.send_expired_message(interaction)
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
        if view.is_shop_expired():
            return await view.send_expired_message(interaction)
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
        super().__init__(timeout=3600)
        self.items = items
        self.author_id = author_id
        self.bot = bot
        self.page = 0
        self.expires_at = time.time() + SHOP_EXPIRE_SECONDS
        self._build()

    def is_shop_expired(self) -> bool:
        return time.time() >= self.expires_at

    async def send_expired_message(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            SHOP_EXPIRED_MESSAGE,
            ephemeral=True
        )

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


class OpenShopView(discord.ui.View):
    def __init__(self, author_id: int, bot, command_message: discord.Message):
        super().__init__(timeout=60)
        self.author_id = author_id
        self.bot = bot
        self.command_message = command_message

    @discord.ui.button(label="Abrir Tienda", style=discord.ButtonStyle.success)
    async def abrir_tienda(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message(
                "❌ Este panel no fue generado por ti.", ephemeral=True
            )

        items = await get_all_items()
        if not items:
            return await interaction.response.send_message(
                "🛒 La tienda está vacía por ahora.", ephemeral=True
            )

        items = sorted(items, key=lambda i: i["precio"])
        await interaction.response.send_message(
            view=TiendaLayout(items, interaction.user.id, self.bot),
            ephemeral=True
        )

        asyncio.create_task(self._delete_public_messages(interaction.message))

    async def _delete_public_messages(self, prompt_message: discord.Message):
        await asyncio.sleep(3)
        for message in (prompt_message, self.command_message):
            try:
                await message.delete()
            except Exception:
                pass


# ── BOTON DE USAR ITEM (inventario) ───────────────────

class UseButton(discord.ui.Button):
    def __init__(self, item, author_id, guild, bot):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label="Usar",
            emoji="💠",
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

            # ── Verificar límite de uso diario ─────────
            limite_uso = item.get("limite_uso", 0)
            if limite_uso and limite_uso > 0:
                from core.database import get_usos_diarios
                usos_hoy = await get_usos_diarios(interaction.user.id, item["id"])
                if usos_hoy >= limite_uso:
                    icono = item["icono"] if item["icono"] else "🔹"
                    return await interaction.followup.send(
                        f"⏳ Solo puedes utilizar **{limite_uso}** de {icono} **{item['nombre']}** cada día.",
                        ephemeral=True
                    )

            role = None
            if item.get("rol_id"):
                role = self.guild.get_role(int(item["rol_id"]))
                if not role:
                    return await interaction.followup.send(
                        "❌ El rol configurado para este item no existe en el servidor.",
                        ephemeral=True
                    )

                member = self.guild.get_member(interaction.user.id)
                if member is None:
                    try:
                        member = await self.guild.fetch_member(interaction.user.id)
                    except Exception as e:
                        logger.warning(
                            f"No se pudo obtener member para usar item {item['nombre']} "
                            f"({interaction.user.id}): {e}"
                        )
                        return await interaction.followup.send(
                            "❌ No pude verificar tu miembro en el servidor. El item no fue consumido.",
                            ephemeral=True
                        )

                restricted_role_ids = cache.get_restricted_item_role_ids()
                if role.id in restricted_role_ids:
                    conflicting_role = next(
                        (
                            member_role
                            for member_role in member.roles
                            if member_role.id != role.id and member_role.id in restricted_role_ids
                        ),
                        None,
                    )
                    if conflicting_role is not None:
                        now = time.time()
                        expirations = [
                            cargo["expira_en"]
                            for cargo in cache.get_cargos_cache().get(interaction.user.id, [])
                            if cargo.get("guild_id") == self.guild.id
                            and cargo.get("rol_id") == conflicting_role.id
                            and cargo.get("expira_en", 0) > now
                        ]
                        if expirations:
                            expires_at = int(max(expirations))
                            message = (
                                f"❌ Actualmente ya posees {conflicting_role.mention} "
                                f"<t:{expires_at}:R> Podrás activar uno nuevo."
                            )
                        else:
                            message = (
                                f"❌ Actualmente ya posees {conflicting_role.mention}. "
                                "Debes retirar ese rol antes de activar uno nuevo."
                            )
                        return await interaction.followup.send(message, ephemeral=True)

                try:
                    await member.add_roles(role, reason=f"Uso de item {item['nombre']} en Purple Jack")
                except discord.Forbidden:
                    return await interaction.followup.send(
                        "❌ No tengo permisos o jerarquía suficiente para otorgar ese rol. El item no fue consumido.",
                        ephemeral=True
                    )
                except discord.HTTPException as e:
                    logger.warning(
                        f"Discord no pudo asignar rol {role.id} por item {item['nombre']} "
                        f"a {interaction.user.id}: {e}"
                    )
                    return await interaction.followup.send(
                        "❌ Discord no confirmó la entrega del rol. El item no fue consumido.",
                        ephemeral=True
                    )

                try:
                    member_check = await self.guild.fetch_member(interaction.user.id)
                except Exception:
                    member_check = member

                if role.id not in {r.id for r in member_check.roles}:
                    return await interaction.followup.send(
                        "❌ No se pudo confirmar que el rol fue entregado. El item no fue consumido.",
                        ephemeral=True
                    )

            removed = await remove_from_inventory(interaction.user.id, item["nombre"])
            if not removed:
                if role:
                    try:
                        await interaction.user.remove_roles(role)
                    except Exception:
                        pass
                return await interaction.followup.send(
                    f"❌ Ya no tienes **{item['nombre']}** en tu inventario.", ephemeral=True
                )

            if role:
                duracion = item.get("duracion", 0)
                if duracion and duracion > 0:
                    expira_en = time.time() + duracion
                    await add_cargo_temporal(
                        interaction.user.id,
                        self.guild.id,
                        int(item["rol_id"]),
                        expira_en
                    )

            # ── Registrar uso diario si tiene límite ───
            if limite_uso and limite_uso > 0:
                from core.database import registrar_uso_diario
                await registrar_uso_diario(interaction.user.id, item["id"])

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
            nombre_log = interaction.user.nick or interaction.user.display_name
            if log_channel:
                await log_channel.send(
                    f"✨ **{nombre_log}** usó {icono} **{item['nombre']}**"
                )

            # El canal especial es adicional y solo se usa al consumir el item.
            item_config = await get_item_by_name(item["nombre"])
            log_uso_channel_id = (item_config or item).get("log_uso_channel_id")
            if log_uso_channel_id and log_uso_channel_id != LOG_CHANNEL_ID:
                special_log_channel = self.bot.get_channel(log_uso_channel_id)
                if special_log_channel:
                    try:
                        await special_log_channel.send(
                            f"<@&{STAFF_ROLE_ID}> ✨ {interaction.user.mention} usó {icono} **{item['nombre']}**",
                            allowed_mentions=discord.AllowedMentions(roles=True, users=True),
                        )
                    except discord.HTTPException as error:
                        logger.warning(
                            "No se pudo enviar el log especial de uso para item %s: %s",
                            item["id"],
                            error,
                        )
                else:
                    logger.warning(
                        "Canal de log especial no encontrado para item %s: %s",
                        item["id"],
                        log_uso_channel_id,
                    )

        except Exception as e:
            logger.error(f"ERROR UseButton callback: {e}")
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
            logger.error(f"ERROR InventarioLayout._build: {e}")
            raise

    async def on_timeout(self):
        pass


# ── SHOP COG ───────────────────────────────────────────


def format_tiempo_restante(segundos: int) -> str:
    if segundos <= 0:
        return "Expirado"

    dias = segundos // 86400
    horas = (segundos % 86400) // 3600
    minutos = (segundos % 3600) // 60

    if dias >= 1:
        return f"{dias} día{'s' if dias != 1 else ''}"
    if horas >= 1:
        return f"{horas} hora{'s' if horas != 1 else ''}"
    return f"{minutos} minuto{'s' if minutos != 1 else ''}"


class Shop(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.CommandOnCooldown):
            retry = int(error.retry_after)
            if retry >= 60:
                tiempo = f"{retry // 60}m {retry % 60}s" if retry % 60 else f"{retry // 60}m"
            else:
                tiempo = f"{retry}s"
            await ctx.send(
                f"⏳ {ctx.author.mention} Podrás usar este comando de nuevo en **{tiempo}**.",
                delete_after=10
            )
        else:
            raise error

    @commands.command()
    @commands.cooldown(1, 30, commands.BucketType.user)
    async def tienda(self, ctx):
        items = await get_all_items()
        if not items:
            return await ctx.send("🛒 La tienda está vacía por ahora.")

        embed = discord.Embed(
            title="🛒 Tienda de Articulos - Nave SUS",
            description=(
                "Ingresa ahora a la tienda de articulos exclusivos de la **Nave**\n"
                "Exp del Servidor, Roles, Oroestrellas y mas !!!"
            ),
            color=discord.Color.blue()
        )
        embed.set_thumbnail(url=ctx.author.display_avatar.url)
        embed.set_footer(text="Presiona Abrir Tienda para revisar el stock disponible.")
        await ctx.message.reply(embed=embed, view=OpenShopView(ctx.author.id, self.bot, ctx.message))

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
            if duracion == 0:
                dur_txt = "Permanente"
            elif duracion >= 86400:
                dur_txt = f"{int(duracion // 86400)} día(s)"
            elif duracion >= 3600:
                dur_txt = f"{int(duracion // 3600)} hora(s)"
            else:
                dur_txt = f"{int(duracion // 60)} minuto(s)"
            embed.add_field(name="⏳ Duración del Cargo", value=dur_txt, inline=True)

        await ctx.send(embed=embed)

    @commands.command(name="time")
    async def tiempo_restante(self, ctx):
        cargos = cache.get_cargos_cache().get(ctx.author.id, [])
        ahora = time.time()

        roles_activos = []
        for cargo in cargos:
            expira_en = cargo.get("expira_en", 0)
            if expira_en <= ahora:
                continue

            guild = self.bot.get_guild(cargo.get("guild_id", ctx.guild.id)) if self.bot else ctx.guild
            role = guild.get_role(int(cargo["rol_id"])) if guild else None
            if role or cargo.get("rol_id"):
                roles_activos.append((role, int(expira_en - ahora), cargo.get("rol_id")))

        if not roles_activos:
            embed = discord.Embed(
                title="🟢 ROLES - TIEMPO RESTANTE",
                description="No tienes roles temporales activos en este momento.",
                color=discord.Color.green()
            )
            return await ctx.reply(embed=embed)

        lines = [
            "🟢 **ROLES - TIEMPO RESTANTE**",
            ""
        ]
        for role, segundos, rol_id in roles_activos:
            role_mention = role.mention if role else f"<@&{rol_id}>"
            lines.append(f"➔ {role_mention} - {format_tiempo_restante(segundos)}")

        embed = discord.Embed(
            description="\n".join(lines),
            color=discord.Color.green()
        )

        await ctx.reply(embed=embed)

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
            logger.error(f"ERROR inventario command: {e}")
            await ctx.send(f"❌ Error al abrir inventario: `{e}`")


async def setup(bot):
    await bot.add_cog(Shop(bot))
