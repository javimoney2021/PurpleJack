"""Paneles y catálogos base de Bichos World; juego aún en plantilla."""

import asyncio
import logging
import uuid
from math import ceil

import discord
from discord import app_commands
from discord.ext import commands

from core.config import COORDINADOR_ROLE_ID
from core.database import (
    MAX_BICHOS_PER_USER,
    get_bichos_registration, get_bichos_balance, register_bichos_player, get_owned_bichos,
    get_bichos_objects, get_bichos_players_page, get_bichos_users_page,
    get_bichos_catalog, add_bichos_catalog,
    get_bichos_item_catalog, add_bichos_item_catalog,
    purchase_bicho, purchase_bicho_item, use_bicho_item,
)

logger = logging.getLogger(__name__)
SHOP_PAGE_SIZE = 3
ITEM_SHOP_PAGE_SIZE = 3
INVENTORY_PAGE_SIZE = 3
BOOST_LABELS = {
    "regeneracion": "Regeneración",
    "power": "Power",
    "vitamina": "Vitamina",
    "none": "Sin efecto",
}
BOOST_EMOJIS = {
    "regeneracion": "💚", "power": "⚡", "vitamina": "💊", "none": "🎁",
}
BOOST_DESCRIPTIONS = {
    "regeneracion": "Restaura las 3 vidas del Bicho equipado.",
    "power": "+5 ataque y +5 defensa al Bicho equipado durante 3 horas.",
    "vitamina": "+30% de velocidad de entrenamiento durante 1 hora.",
    "none": "Ítem sin efecto; se guarda en el inventario pero no es consumible.",
}
USABLE_BOOSTS = {"regeneracion", "power", "vitamina"}
SECTIONS = (
    ("Mis Bichos", "🐾"),
    ("Entrenamiento", "🏋️"),
    ("Inventario", "🎒"),
    ("Tienda", "🛒"),
    ("PVP", "⚔️"),
)


async def _delete_after(message, seconds: int = 30) -> None:
    await asyncio.sleep(seconds)
    try:
        await message.delete()
    except (discord.HTTPException, discord.NotFound):
        pass


async def _schedule_original_deletion(interaction: discord.Interaction) -> None:
    try:
        message = await interaction.original_response()
    except discord.HTTPException:
        logger.warning("No se pudo obtener el mensaje efímero de inscripción de %s", interaction.user.id)
        return
    asyncio.create_task(_delete_after(message))


class EnrollmentView(discord.ui.View):
    def __init__(self, owner_id: int):
        super().__init__(timeout=30)
        self.owner_id = owner_id

    @discord.ui.button(label="Inscribirme", style=discord.ButtonStyle.success, emoji="📝")
    async def enroll(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Esta inscripción no te pertenece.", ephemeral=True)
            return
        from bot import check_player_access
        if not await check_player_access(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            created = await register_bichos_player(
                interaction.user.id, interaction.user.display_name,
            )
        except Exception:
            logger.exception("No se pudo inscribir a %s en Bichos World", interaction.user.id)
            await interaction.followup.send("No pude completar tu inscripción. Inténtalo de nuevo.", ephemeral=True)
            return
        self.stop()
        confirmation = (
            "✅ Ya estás inscrito en Bichos World. Usa **/bichos** para entrar."
            if created else "✅ Ya estabas inscrito. Usa **/bichos** para entrar."
        )
        message = await interaction.followup.send(confirmation, ephemeral=True, wait=True)
        asyncio.create_task(_delete_after(message))


async def send_enrollment(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(
        "Para acceder al Mundo de Bichos debes inscribirte primero.",
        view=EnrollmentView(interaction.user.id),
        ephemeral=True,
    )
    await _schedule_original_deletion(interaction)


class SectionButton(discord.ui.Button):
    def __init__(self, title: str, emoji: str):
        super().__init__(label=title, emoji=emoji, style=discord.ButtonStyle.secondary)
        self.title = title

    async def callback(self, interaction: discord.Interaction):
        if getattr(self.view, "private", False) and interaction.user.id != self.view.owner_id:
            await interaction.response.send_message("Este panel privado no te pertenece.", ephemeral=True)
            return
        from bot import check_player_access
        if not await check_player_access(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            registered = await get_bichos_registration(interaction.user.id)
        except Exception:
            logger.exception("No se pudo consultar la inscripción de Bichos World")
            await interaction.followup.send("No pude abrir Bichos World ahora.", ephemeral=True)
            return
        if not registered:
            message = await interaction.followup.send(
                "Para acceder al Mundo de Bichos debes inscribirte primero.",
                view=EnrollmentView(interaction.user.id), ephemeral=True, wait=True,
            )
            asyncio.create_task(_delete_after(message))
            return
        try:
            section = await build_section(interaction.user.id, self.title)
        except Exception:
            logger.exception("No se pudo abrir la sección %s de Bichos World", self.title)
            await interaction.followup.send("No pude abrir esta sección ahora.", ephemeral=True)
            return
        if getattr(self.view, "private", False):
            await interaction.edit_original_response(view=section)
        else:
            await interaction.followup.send(
                view=section, ephemeral=True,
            )


class HomeView(discord.ui.LayoutView):
    def __init__(self, *, private: bool = False, owner_id: int | None = None,
                 balance: int = 0, owner_name: str = "Usuario"):
        super().__init__(timeout=900)
        self.private = private
        self.owner_id = owner_id
        container = discord.ui.Container(accent_color=0x7942C7)
        container.add_item(discord.ui.TextDisplay(
            "## Bichos World by Purplejack\n"
            "Bienvenidos al Mundo de Bichos... Desplázate por los botones "
            "de abajo para gestionar tu mundo de Bichos!\n"
            f"**Saldo Bcoin: {balance:,}** · {discord.utils.escape_markdown(owner_name)}"
        ))
        container.add_item(discord.ui.Separator())
        for start in (0, 2, 4):
            row = discord.ui.ActionRow()
            for title, emoji in SECTIONS[start:start + 2]:
                row.add_item(SectionButton(title, emoji))
            container.add_item(row)
        self.add_item(container)


class NavigationButton(discord.ui.Button):
    def __init__(self, label: str, emoji: str):
        super().__init__(label=label, emoji=emoji, style=discord.ButtonStyle.primary)

    async def callback(self, interaction: discord.Interaction):
        view: SectionView = self.view
        if interaction.user.id != view.owner_id:
            await interaction.response.send_message("Este panel privado no te pertenece.", ephemeral=True)
            return
        from bot import check_player_access
        if not await check_player_access(interaction):
            return
        balance = await get_bichos_balance(view.owner_id)
        await interaction.response.edit_message(
            view=HomeView(private=True, owner_id=view.owner_id, balance=balance,
                          owner_name=interaction.user.display_name),
        )


class PlaceholderButton(discord.ui.Button):
    def __init__(self, label: str, emoji: str):
        super().__init__(label=label, emoji=emoji, style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.view.owner_id:
            await interaction.response.send_message("Este panel privado no te pertenece.", ephemeral=True)
            return
        from bot import check_player_access
        if not await check_player_access(interaction):
            return
        await interaction.response.send_message(
            f"{self.label} estará disponible en una próxima actualización.", ephemeral=True,
        )


class ShopEntryButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Tienda de Bichos", emoji="🐾",
            style=discord.ButtonStyle.secondary,
        )

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if interaction.user.id != view.owner_id:
            await interaction.response.send_message("Este panel privado no te pertenece.", ephemeral=True)
            return
        from bot import check_player_access
        if not await check_player_access(interaction):
            return
        await interaction.response.defer()
        try:
            shop = await build_bichos_shop(view.owner_id, 0)
        except Exception:
            logger.exception("No se pudo abrir la tienda de Bichos World")
            await interaction.followup.send("No pude abrir la tienda de Bichos.", ephemeral=True)
            return
        await interaction.edit_original_response(view=shop)


class ItemShopEntryButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="Tienda de Ítems", emoji="🎁",
            style=discord.ButtonStyle.secondary,
        )

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if interaction.user.id != view.owner_id:
            await interaction.response.send_message("Este panel privado no te pertenece.", ephemeral=True)
            return
        from bot import check_player_access
        if not await check_player_access(interaction):
            return
        await interaction.response.defer()
        try:
            shop = await build_bichos_item_shop(view.owner_id, 0)
        except Exception:
            logger.exception("No se pudo abrir la tienda de ítems de Bichos World")
            await interaction.followup.send("No pude abrir la tienda de ítems.", ephemeral=True)
            return
        await interaction.edit_original_response(view=shop)


class ShopPageButton(discord.ui.Button):
    def __init__(self, direction: int, *, disabled: bool = False):
        super().__init__(
            label="Anterior" if direction < 0 else "Siguiente",
            emoji="◀️" if direction < 0 else "▶️",
            style=discord.ButtonStyle.primary,
            disabled=disabled,
        )
        self.direction = direction

    async def callback(self, interaction: discord.Interaction):
        view: BichosShopView = self.view
        await interaction.response.defer()
        try:
            shop = await build_bichos_shop(view.owner_id, view.page + self.direction)
        except Exception:
            logger.exception("No se pudo paginar la tienda de Bichos World")
            await interaction.followup.send("No pude cambiar de página.", ephemeral=True)
            return
        await interaction.edit_original_response(view=shop)


class ShopBackButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Volver", emoji="◀️", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        view: BichosShopView = self.view
        await interaction.response.defer()
        try:
            section = await build_section(view.owner_id, "Tienda")
        except Exception:
            logger.exception("No se pudo volver a la sección Tienda")
            await interaction.followup.send("No pude volver a la tienda.", ephemeral=True)
            return
        await interaction.edit_original_response(view=section)


class BuyBichoButton(discord.ui.Button):
    def __init__(self, bicho: dict):
        super().__init__(label="Comprar", emoji="🛒", style=discord.ButtonStyle.success)
        self.bicho = bicho
        # Un token por botón evita cobrar dos veces la misma interacción repetida.
        self.purchase_token = uuid.uuid4().hex

    async def callback(self, interaction: discord.Interaction):
        view: BichosShopView = self.view
        await interaction.response.defer(ephemeral=True)
        try:
            result = await purchase_bicho(
                view.owner_id, int(self.bicho["id"]), self.purchase_token,
            )
        except Exception:
            logger.exception("No se pudo comprar el Bicho %s", self.bicho.get("name"))
            await interaction.followup.send("No pude completar la compra.", ephemeral=True)
            return

        if not result.get("ok"):
            reason = result.get("reason")
            if reason == "insufficient_balance":
                text = (
                    "❌ No tienes Bcoin suficientes.\n"
                    f"Saldo: **{result['balance']:,}** · Precio: **{result['price']:,}**."
                )
            elif reason == "max_owned":
                text = f"❌ Ya tienes el máximo de **{result['limit']}** Bichos."
            elif reason == "duplicate":
                text = "ℹ️ Esta compra ya fue procesada."
            elif reason == "not_found":
                text = "❌ Ese Bicho ya no está disponible en la tienda."
            elif reason == "not_registered":
                text = "❌ Debes inscribirte en Bichos World primero."
            else:
                text = "❌ No pude completar la compra."
            await interaction.followup.send(text, ephemeral=True)
            return

        try:
            shop = await build_bichos_shop(view.owner_id, view.page)
            await interaction.edit_original_response(view=shop)
        except Exception:
            logger.exception("No se pudo refrescar la tienda tras la compra")

        await interaction.followup.send(
            f"✅ Compraste **{discord.utils.escape_markdown(result['name'])}** por "
            f"**{result['price']:,}** Bcoin.\n"
            f"Saldo restante: **{result['balance']:,}**.",
            ephemeral=True,
        )


class BuyItemButton(discord.ui.Button):
    def __init__(self, item: dict):
        super().__init__(label="Comprar", emoji="🛒", style=discord.ButtonStyle.success)
        self.item = item
        self.purchase_token = uuid.uuid4().hex

    async def callback(self, interaction: discord.Interaction):
        view: "BichosItemShopView" = self.view
        await interaction.response.defer(ephemeral=True)
        try:
            result = await purchase_bicho_item(
                view.owner_id, int(self.item["id"]), self.purchase_token,
            )
        except Exception:
            logger.exception("No se pudo comprar el ítem %s", self.item.get("name"))
            await interaction.followup.send("No pude completar la compra.", ephemeral=True)
            return

        if not result.get("ok"):
            reason = result.get("reason")
            if reason == "insufficient_balance":
                text = (
                    "❌ No tienes Bcoin suficientes.\n"
                    f"Saldo: **{result['balance']:,}** · Precio: **{result['price']:,}**."
                )
            elif reason == "duplicate":
                text = "ℹ️ Esta compra ya fue procesada."
            elif reason == "not_found":
                text = "❌ Ese ítem ya no está disponible en la tienda."
            elif reason == "not_registered":
                text = "❌ Debes inscribirte en Bichos World primero."
            else:
                text = "❌ No pude completar la compra."
            await interaction.followup.send(text, ephemeral=True)
            return

        try:
            shop = await build_bichos_item_shop(view.owner_id, view.page)
            await interaction.edit_original_response(view=shop)
        except Exception:
            logger.exception("No se pudo refrescar la tienda de ítems tras la compra")

        await interaction.followup.send(
            f"✅ Compraste **{discord.utils.escape_markdown(result['name'])}** por "
            f"**{result['price']:,}** Bcoin.\n"
            f"Tienes **{result['quantity']}** unidad(es).\n"
            f"Saldo restante: **{result['balance']:,}**.",
            ephemeral=True,
        )


class ItemShopPageButton(discord.ui.Button):
    def __init__(self, direction: int, *, disabled: bool = False):
        super().__init__(
            label="Anterior" if direction < 0 else "Siguiente",
            emoji="◀️" if direction < 0 else "▶️",
            style=discord.ButtonStyle.primary,
            disabled=disabled,
        )
        self.direction = direction

    async def callback(self, interaction: discord.Interaction):
        view: "BichosItemShopView" = self.view
        await interaction.response.defer()
        try:
            shop = await build_bichos_item_shop(view.owner_id, view.page + self.direction)
        except Exception:
            logger.exception("No se pudo paginar la tienda de ítems de Bichos World")
            await interaction.followup.send("No pude cambiar de página.", ephemeral=True)
            return
        await interaction.edit_original_response(view=shop)


class BichosItemShopView(discord.ui.LayoutView):
    def __init__(self, owner_id: int, items: list[dict], *, page: int,
                 pages: int, total: int, balance: int):
        super().__init__(timeout=900)
        self.owner_id = owner_id
        self.page = page
        self.pages = pages
        self.total = total
        self.balance = balance
        container = discord.ui.Container(accent_color=0x7942C7)
        container.add_item(discord.ui.TextDisplay(
            f"## 🎁 Tienda de Ítems\n"
            f"Página {page + 1}/{pages} · {total} ítem(s) · "
            f"Saldo: **{balance:,} Bcoin**"
        ))
        if not items:
            container.add_item(discord.ui.TextDisplay(
                "No hay ítems disponibles en la tienda todavía."
            ))
        for index, item in enumerate(items):
            if index:
                container.add_item(discord.ui.Separator())
            if item.get("image_url"):
                container.add_item(discord.ui.MediaGallery(
                    discord.MediaGalleryItem(media=item["image_url"]),
                ))
            name = discord.utils.escape_markdown(item["name"])
            description = discord.utils.escape_markdown(
                item.get("description") or "Sin descripción."
            )
            boost = item.get("boost") or ""
            boost_label = BOOST_LABELS.get(boost, "Sin efecto")
            boost_emoji = BOOST_EMOJIS.get(boost, "🎁")
            container.add_item(discord.ui.TextDisplay(
                f"### {name}\n{description}\n"
                f"{boost_emoji} **Efecto:** {boost_label}\n"
                f"💰 **Precio:** {item['price']:,} Bcoin"
            ))
            row = discord.ui.ActionRow()
            row.add_item(BuyItemButton(item))
            container.add_item(row)
        navigation = discord.ui.ActionRow()
        navigation.add_item(ItemShopPageButton(-1, disabled=page <= 0))
        navigation.add_item(ItemShopPageButton(1, disabled=page >= pages - 1))
        navigation.add_item(ShopBackButton())
        navigation.add_item(NavigationButton("Inicio", "🏠"))
        container.add_item(navigation)
        self.add_item(container)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Esta tienda no te pertenece.", ephemeral=True,
            )
            return False
        from bot import check_player_access
        return await check_player_access(interaction)


async def build_bichos_item_shop(owner_id: int, page: int = 0) -> BichosItemShopView:
    catalog = await get_bichos_item_catalog()
    pages = max(1, ceil(len(catalog) / ITEM_SHOP_PAGE_SIZE))
    page = min(max(0, page), pages - 1)
    start = page * ITEM_SHOP_PAGE_SIZE
    items = catalog[start:start + ITEM_SHOP_PAGE_SIZE]
    balance = await get_bichos_balance(owner_id)
    return BichosItemShopView(
        owner_id, items, page=page, pages=pages,
        total=len(catalog), balance=balance,
    )


class UseItemButton(discord.ui.Button):
    def __init__(self, item: dict):
        super().__init__(label="Usar", emoji="✨", style=discord.ButtonStyle.success)
        self.item = item
        self.use_token = uuid.uuid4().hex

    async def callback(self, interaction: discord.Interaction):
        view: "BichosInventoryView" = self.view
        await interaction.response.defer(ephemeral=True)
        try:
            result = await use_bicho_item(
                view.owner_id, self.item["item_key"], self.use_token,
            )
        except Exception:
            logger.exception("No se pudo usar el ítem %s", self.item.get("item_key"))
            await interaction.followup.send("No pude usar ese ítem.", ephemeral=True)
            return

        if not result.get("ok"):
            reason = result.get("reason")
            if reason == "no_equipped":
                text = "❌ Equipa un Bicho antes de usar este ítem."
            elif reason == "full_lives":
                text = "❌ Ese Bicho ya tiene las 3 vidas."
            elif reason == "out_of_owned":
                text = "❌ Ya no tienes ese ítem."
            elif reason == "duplicate":
                text = "ℹ️ Este uso ya fue procesado."
            elif reason == "not_found":
                text = "❌ Ese ítem ya no está disponible."
            elif reason == "invalid_boost":
                text = "❌ Ese ítem no tiene un efecto válido."
            else:
                text = "❌ No pude usar ese ítem."
            await interaction.followup.send(text, ephemeral=True)
            return

        try:
            inventory = await build_bichos_inventory(view.owner_id, view.page)
            await interaction.edit_original_response(view=inventory)
        except Exception:
            logger.exception("No se pudo refrescar el inventario tras usar el ítem")

        name = discord.utils.escape_markdown(result["name"])
        bicho = discord.utils.escape_markdown(result["bicho_name"])
        if result["effect"] == "regeneracion":
            text = f"💚 **{name}** regeneró las 3 vidas de **{bicho}**."
        elif result["effect"] == "power":
            text = (
                f"⚡ **{name}**: **{bicho}** recibió +5 ataque y +5 defensa "
                "durante 3 horas."
            )
        else:
            text = (
                f"💊 **{name}**: **{bicho}** ahora tiene "
                f"**+{result['training_boost_pct']}%** de velocidad de entrenamiento "
                "durante 1 hora."
            )
        text += (
            f"\nQuedan **{result['quantity']}** unidad(es)."
            if result["quantity"]
            else "\nYa no te quedan unidades de ese ítem."
        )
        await interaction.followup.send(text, ephemeral=True)


class InventoryPageButton(discord.ui.Button):
    def __init__(self, direction: int, *, disabled: bool = False):
        super().__init__(
            label="Anterior" if direction < 0 else "Siguiente",
            emoji="◀️" if direction < 0 else "▶️",
            style=discord.ButtonStyle.primary,
            disabled=disabled,
        )
        self.direction = direction

    async def callback(self, interaction: discord.Interaction):
        view: "BichosInventoryView" = self.view
        await interaction.response.defer()
        try:
            inventory = await build_bichos_inventory(
                view.owner_id, view.page + self.direction,
            )
        except Exception:
            logger.exception("No se pudo paginar el inventario de Bichos World")
            await interaction.followup.send("No pude cambiar de página.", ephemeral=True)
            return
        await interaction.edit_original_response(view=inventory)


class BichosInventoryView(discord.ui.LayoutView):
    def __init__(self, owner_id: int, items: list[dict], *, page: int,
                 pages: int, total: int):
        super().__init__(timeout=900)
        self.owner_id = owner_id
        self.page = page
        self.pages = pages
        self.total = total
        container = discord.ui.Container(accent_color=0x7942C7)
        container.add_item(discord.ui.TextDisplay(
            f"## 🎒 Inventario\n"
            f"Página {page + 1}/{pages} · {total} ítem(s)\n"
            "Los efectos se aplican al Bicho equipado."
        ))
        if not items:
            container.add_item(discord.ui.TextDisplay(
                "Aún no tienes ítems de Bichos World."
            ))
        for index, item in enumerate(items):
            if index:
                container.add_item(discord.ui.Separator())
            if item.get("image_url"):
                container.add_item(discord.ui.MediaGallery(
                    discord.MediaGalleryItem(media=item["image_url"]),
                ))
            name = discord.utils.escape_markdown(item["item_key"])
            description = discord.utils.escape_markdown(
                item.get("description") or "Sin descripción."
            )
            boost = item.get("boost") or ""
            boost_label = BOOST_LABELS.get(boost, "Sin efecto")
            boost_emoji = BOOST_EMOJIS.get(boost, "🎁")
            usable = boost in USABLE_BOOSTS
            effect_text = f"{boost_emoji} **Efecto:** {boost_label}"
            if not usable:
                effect_text += "\nEste ítem no es consumible."
            container.add_item(discord.ui.TextDisplay(
                f"### {name}\n{description}\n"
                f"{effect_text}\n"
                f"**Cantidad:** {item['quantity']}"
            ))
            if usable:
                row = discord.ui.ActionRow()
                row.add_item(UseItemButton(item))
                container.add_item(row)
        navigation = discord.ui.ActionRow()
        navigation.add_item(InventoryPageButton(-1, disabled=page <= 0))
        navigation.add_item(InventoryPageButton(1, disabled=page >= pages - 1))
        navigation.add_item(NavigationButton("Volver", "◀️"))
        navigation.add_item(NavigationButton("Inicio", "🏠"))
        container.add_item(navigation)
        self.add_item(container)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Este inventario no te pertenece.", ephemeral=True,
            )
            return False
        from bot import check_player_access
        return await check_player_access(interaction)


async def build_bichos_inventory(owner_id: int, page: int = 0) -> BichosInventoryView:
    items = await get_bichos_objects(owner_id)
    pages = max(1, ceil(len(items) / INVENTORY_PAGE_SIZE))
    page = min(max(0, page), pages - 1)
    start = page * INVENTORY_PAGE_SIZE
    return BichosInventoryView(
        owner_id, items[start:start + INVENTORY_PAGE_SIZE],
        page=page, pages=pages, total=len(items),
    )


class PageButton(discord.ui.Button):
    def __init__(self, direction: int):
        super().__init__(
            label="Anterior" if direction < 0 else "Siguiente",
            style=discord.ButtonStyle.primary,
        )
        self.direction = direction

    async def callback(self, interaction: discord.Interaction):
        view: SectionView = self.view
        if interaction.user.id != view.owner_id:
            await interaction.response.send_message("Este panel privado no te pertenece.", ephemeral=True)
            return
        from bot import check_player_access
        if not await check_player_access(interaction):
            return
        await interaction.response.defer()
        updated = await build_section(view.owner_id, view.title, view.page + self.direction)
        await interaction.edit_original_response(view=updated)


class PvpSelect(discord.ui.Select):
    def __init__(self, users: list[dict], *, page: int, pages: int):
        options = [discord.SelectOption(
            label=(" ".join(row["nickname"].split()) or f"Usuario {row['user_id']}")[:100],
            value=str(row["user_id"]),
            description=f"{row['bichos_count']} Bicho(s) · ID {row['user_id']}"[:100],
        ) for row in users]
        if page < pages - 1:
            options.append(discord.SelectOption(label="Siguiente", value="next", emoji="▶️"))
        elif page > 0:
            options.append(discord.SelectOption(label="Anterior", value="previous", emoji="◀️"))
        super().__init__(placeholder="Selecciona un jugador", options=options)

    async def callback(self, interaction: discord.Interaction):
        view: SectionView = self.view
        if interaction.user.id != view.owner_id:
            await interaction.response.send_message("Este panel privado no te pertenece.", ephemeral=True)
            return
        from bot import check_player_access
        if not await check_player_access(interaction):
            return
        choice = self.values[0]
        if choice in {"next", "previous"}:
            await interaction.response.defer()
            updated = await build_section(
                view.owner_id, "PVP", view.page + (1 if choice == "next" else -1),
            )
            await interaction.edit_original_response(view=updated)
        else:
            await interaction.response.send_message(
                "Los desafíos PVP estarán disponibles en una próxima actualización.",
                ephemeral=True,
            )


class SectionView(discord.ui.LayoutView):
    def __init__(self, owner_id: int, title: str, *, page: int = 0,
                 rows: list[dict] | None = None, total: int = 0):
        super().__init__(timeout=900)
        self.owner_id = owner_id
        self.private = True
        self.title = title
        self.page = page
        rows = rows or []
        container = discord.ui.Container(accent_color=0x7942C7)
        if title == "Mis Bichos":
            if rows:
                bicho = rows[page]
                name = discord.utils.escape_markdown(bicho.get("nickname") or bicho["species_key"])
                hearts = "❤️" * max(0, min(3, bicho["lives"])) + "🖤" * (3 - max(0, min(3, bicho["lives"])))
                attack_boost = int(bicho.get("active_attack_boost") or 0)
                defense_boost = int(bicho.get("active_defense_boost") or 0)
                attack = int(bicho.get("attack") or 0)
                defense = int(bicho.get("defense") or 0)
                attack_text = f"**{attack}**" + (f" (+{attack_boost})" if attack_boost else "")
                defense_text = f"**{defense}**" + (f" (+{defense_boost})" if defense_boost else "")
                boost_text = ""
                expires = bicho.get("boost_expires_at")
                if attack_boost and expires:
                    boost_text = f"\n⚡ Power activo hasta <t:{int(expires.timestamp())}:R>"
                training_pct = int(bicho.get("training_boost_pct") or 0)
                if training_pct:
                    training_text = f"\n💊 Entrenamiento: **+{training_pct}%**"
                    training_expires = bicho.get("training_boost_expires_at")
                    if training_expires:
                        training_text += (
                            f" hasta <t:{int(training_expires.timestamp())}:R>"
                        )
                    boost_text += training_text
                container.add_item(discord.ui.TextDisplay(
                    f"## 🐾 {name}\nEspecie: **{bicho['species_key']}** · "
                    f"Nivel: **{bicho['level']}** · "
                    f"Ataque: {attack_text} · "
                    f"Defensa: {defense_text}"
                    f"{boost_text}\n"
                    f"Estado: {hearts} · {'Equipado' if bicho['equipped'] else 'Sin equipar'}\n"
                    f"Bicho {page + 1}/{len(rows)} (máximo {MAX_BICHOS_PER_USER})"
                ))
                if bicho.get("image_url"):
                    container.add_item(discord.ui.MediaGallery(
                        discord.MediaGalleryItem(media=bicho["image_url"]),
                    ))
                row = discord.ui.ActionRow()
                for label, emoji in (("Equipar", "🛡️"), ("Curar", "💚"),
                                     ("Cazar", "🔍"), ("Abandonar", "🚪")):
                    row.add_item(PlaceholderButton(label, emoji))
                container.add_item(row)
                if len(rows) > 1:
                    row = discord.ui.ActionRow()
                    if page > 0:
                        row.add_item(PageButton(-1))
                    if page < len(rows) - 1:
                        row.add_item(PageButton(1))
                    container.add_item(row)
            else:
                container.add_item(discord.ui.TextDisplay(
                    f"## 🐾 Mis Bichos\nAún no tienes Bichos "
                    f"(máximo {MAX_BICHOS_PER_USER})."
                ))
        elif title == "Entrenamiento":
            equipped = next((b for b in rows if b["equipped"]), None)
            name = (equipped.get("nickname") or equipped["species_key"]) if equipped else "Ninguno"
            training_pct = int(equipped.get("training_boost_pct") or 0) if equipped else 0
            training_text = ""
            if training_pct:
                training_text = f"\n💊 Velocidad de entrenamiento: **+{training_pct}%**"
                training_expires = equipped.get("training_boost_expires_at")
                if training_expires:
                    training_text += (
                        f" hasta <t:{int(training_expires.timestamp())}:R>"
                    )
            container.add_item(discord.ui.TextDisplay(
                f"## 🏋️ Entrenamiento\nBicho equipado: **{discord.utils.escape_markdown(name)}**"
                f"{training_text}"
            ))
            row = discord.ui.ActionRow()
            row.add_item(PlaceholderButton("Entrenar", "💪"))
            row.add_item(PlaceholderButton("Vitaminas", "💊"))
            container.add_item(row)
        elif title == "Inventario":
            items = "\n".join(
                f"• {row.get('icon') or '🎁'} {discord.utils.escape_markdown(row['item_key'])} ×{row['quantity']}"
                for row in rows
            ) or "Aún no tienes ítems de Bichos World."
            container.add_item(discord.ui.TextDisplay(f"## 🎒 Inventario\n{items}"))
        elif title == "Tienda":
            container.add_item(discord.ui.TextDisplay("## 🛒 Tienda\nEscoge una sección de la tienda."))
            row = discord.ui.ActionRow()
            row.add_item(ItemShopEntryButton())
            row.add_item(ShopEntryButton())
            container.add_item(row)
        else:
            pages = max(1, ceil(total / 10))
            container.add_item(discord.ui.TextDisplay(
                f"## ⚔️ PVP\nDesafía a tus compañeros a una pelea de bichos...\n"
                f"Página {page + 1}/{pages} · {total} jugadores disponibles."
            ))
            if rows:
                row = discord.ui.ActionRow()
                row.add_item(PvpSelect(rows, page=page, pages=pages))
                container.add_item(row)
            if page > 0 and page < pages - 1:
                row = discord.ui.ActionRow()
                row.add_item(PageButton(-1))
                container.add_item(row)
        row = discord.ui.ActionRow()
        row.add_item(NavigationButton("Volver", "◀️"))
        row.add_item(NavigationButton("Inicio", "🏠"))
        container.add_item(row)
        self.add_item(container)


class BichosShopView(discord.ui.LayoutView):
    def __init__(self, owner_id: int, bichos: list[dict], *, page: int,
                 pages: int, total: int, balance: int):
        super().__init__(timeout=900)
        self.owner_id = owner_id
        self.page = page
        self.pages = pages
        self.total = total
        self.balance = balance
        container = discord.ui.Container(accent_color=0x7942C7)
        container.add_item(discord.ui.TextDisplay(
            f"## 🛒 Tienda de Bichos\n"
            f"Página {page + 1}/{pages} · {total} Bicho(s) · "
            f"Saldo: **{balance:,} Bcoin**"
        ))
        if not bichos:
            container.add_item(discord.ui.TextDisplay(
                "No hay Bichos disponibles en la tienda todavía."
            ))
        for index, bicho in enumerate(bichos):
            if index:
                container.add_item(discord.ui.Separator())
            if bicho.get("image_url"):
                container.add_item(discord.ui.MediaGallery(
                    discord.MediaGalleryItem(media=bicho["image_url"]),
                ))
            name = discord.utils.escape_markdown(bicho["name"])
            description = discord.utils.escape_markdown(
                bicho.get("description") or "Sin descripción."
            )
            container.add_item(discord.ui.TextDisplay(
                f"### {name}\n{description}\n"
                f"⚔️ **Ataque:** {bicho['attack']} · "
                f"🛡️ **Defensa:** {bicho['defense']}\n"
                f"💰 **Precio:** {bicho['price']:,} Bcoin"
            ))
            row = discord.ui.ActionRow()
            row.add_item(BuyBichoButton(bicho))
            container.add_item(row)
        navigation = discord.ui.ActionRow()
        navigation.add_item(ShopPageButton(-1, disabled=page <= 0))
        navigation.add_item(ShopPageButton(1, disabled=page >= pages - 1))
        navigation.add_item(ShopBackButton())
        navigation.add_item(NavigationButton("Inicio", "🏠"))
        container.add_item(navigation)
        self.add_item(container)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Esta tienda no te pertenece.", ephemeral=True,
            )
            return False
        from bot import check_player_access
        return await check_player_access(interaction)


async def build_bichos_shop(owner_id: int, page: int = 0) -> BichosShopView:
    catalog = await get_bichos_catalog()
    pages = max(1, ceil(len(catalog) / SHOP_PAGE_SIZE))
    page = min(max(0, page), pages - 1)
    start = page * SHOP_PAGE_SIZE
    bichos = catalog[start:start + SHOP_PAGE_SIZE]
    balance = await get_bichos_balance(owner_id)
    return BichosShopView(
        owner_id, bichos, page=page, pages=pages,
        total=len(catalog), balance=balance,
    )


async def build_section(owner_id: int, title: str, page: int = 0) -> discord.ui.LayoutView:
    page = max(0, page)
    if title in {"Mis Bichos", "Entrenamiento"}:
        rows = await get_owned_bichos(owner_id)
        return SectionView(owner_id, title, page=min(page, max(0, len(rows) - 1)), rows=rows)
    if title == "Inventario":
        return await build_bichos_inventory(owner_id, page)
    if title == "PVP":
        rows, total = await get_bichos_players_page(owner_id, page * 10, 10)
        pages = max(1, ceil(total / 10))
        page = min(page, pages - 1)
        if page * 10 >= total and total:
            rows, total = await get_bichos_players_page(owner_id, page * 10, 10)
        return SectionView(owner_id, title, page=page, rows=rows, total=total)
    return SectionView(owner_id, title)


def coordinator_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        if COORDINADOR_ROLE_ID in {role.id for role in getattr(interaction.user, "roles", ())}:
            return True
        await interaction.response.send_message(
            "Solo el Coordinador puede usar este comando.", ephemeral=True,
        )
        return False
    return app_commands.check(predicate)


class StaffUsersView(discord.ui.View):
    def __init__(self, owner_id: int):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.page = 0

    async def get_embed(self) -> discord.Embed:
        rows, total = await get_bichos_users_page(self.page * 10, 10)
        pages = max(1, ceil(total / 10))
        self.page = min(self.page, pages - 1)
        if self.page * 10 >= total and total:
            rows, total = await get_bichos_users_page(self.page * 10, 10)
        self.anterior.disabled = self.page == 0
        self.siguiente.disabled = self.page >= pages - 1
        embed = discord.Embed(
            title="🐾 Usuarios de Bichos World",
            color=discord.Color.purple(),
        )
        embed.description = "\n".join(
            f"**{self.page * 10 + i}.** "
            f"{discord.utils.escape_markdown(row['nickname'])} "
            f"(`<@{row['user_id']}>`) — **{row['bichos_count']}** Bicho(s)"
            for i, row in enumerate(rows, start=1)
        ) or "Aún no hay jugadores inscritos."
        embed.set_footer(text=f"Página {self.page + 1}/{pages} · {total} inscritos")
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("Este listado no te pertenece.", ephemeral=True)
        return False

    @discord.ui.button(label="Anterior", style=discord.ButtonStyle.secondary)
    async def anterior(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        self.page = max(0, self.page - 1)
        await interaction.edit_original_response(embed=await self.get_embed(), view=self)

    @discord.ui.button(label="Siguiente", style=discord.ButtonStyle.secondary)
    async def siguiente(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        self.page += 1
        await interaction.edit_original_response(embed=await self.get_embed(), view=self)


class Bichos(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="bichos", description="Entra al panel de Bichos World")
    async def bichos(self, interaction: discord.Interaction):
        try:
            registered = await get_bichos_registration(interaction.user.id)
        except Exception:
            logger.exception("No se pudo consultar Bichos World para %s", interaction.user.id)
            await interaction.response.send_message("No pude abrir Bichos World ahora.", ephemeral=True)
            return
        if not registered:
            await send_enrollment(interaction)
            return
        try:
            balance = await get_bichos_balance(interaction.user.id)
        except Exception:
            logger.exception("No se pudo consultar el saldo Bcoin de %s", interaction.user.id)
            await interaction.response.send_message("No pude abrir Bichos World ahora.", ephemeral=True)
            return
        await interaction.response.send_message(
            view=HomeView(owner_id=interaction.user.id, balance=balance,
                          owner_name=interaction.user.display_name),
            ephemeral=False,
        )

    async def _add_bicho_impl(self, interaction: discord.Interaction, nombre: str,
                              descripcion: str, imagen_url: str, ataque: int,
                              defensa: int, precio: int):
        nombre = nombre.strip()
        descripcion = descripcion.strip()
        imagen_url = imagen_url.strip()
        if not nombre or len(nombre) > 80:
            await interaction.response.send_message(
                "El nombre debe tener entre 1 y 80 caracteres.", ephemeral=True,
            )
            return
        if not descripcion or len(descripcion) > 1000:
            await interaction.response.send_message(
                "La descripción debe tener entre 1 y 1000 caracteres.", ephemeral=True,
            )
            return
        if not imagen_url.startswith("https://") or len(imagen_url) > 500:
            await interaction.response.send_message("La imagen debe ser una URL HTTPS válida.", ephemeral=True)
            return
        if min(ataque, defensa, precio) < 0:
            await interaction.response.send_message(
                "Ataque, defensa y precio no pueden ser negativos.", ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            created = await add_bichos_catalog(
                nombre, descripcion, imagen_url, ataque, defensa, precio,
            )
        except Exception:
            logger.exception("No se pudo añadir el Bicho %s", nombre)
            await interaction.followup.send("No pude guardar ese Bicho.", ephemeral=True)
            return
        await interaction.followup.send(
            f"{'✅ Bicho añadido al catálogo: ' + nombre if created else 'Ya existe un Bicho con ese nombre.'}",
            ephemeral=True,
        )

    @app_commands.command(
        name="add_bicho", description="Añade un Bicho al catálogo de la tienda",
    )
    @app_commands.describe(
        nombre="Nombre único del Bicho",
        descripcion="Descripción visible en la tienda",
        precio="Precio de tienda en Bcoin",
        ataque="Ataque base",
        defensa="Defensa base",
        imagen_url="URL HTTPS de la imagen del Bicho",
    )
    @coordinator_only()
    async def add_bicho(self, interaction: discord.Interaction, nombre: str,
                        descripcion: str, precio: int, ataque: int,
                        defensa: int, imagen_url: str):
        await self._add_bicho_impl(
            interaction, nombre, descripcion, imagen_url, ataque, defensa, precio,
        )

    async def _add_item_bicho_impl(self, interaction: discord.Interaction, nombre: str,
                                   descripcion: str, imagen_url: str, boost: str,
                                   precio: int):
        nombre = nombre.strip()
        descripcion = descripcion.strip()
        imagen_url = imagen_url.strip()
        if not nombre or len(nombre) > 80:
            await interaction.response.send_message(
                "El nombre debe tener entre 1 y 80 caracteres.", ephemeral=True,
            )
            return
        if not descripcion or len(descripcion) > 1000:
            await interaction.response.send_message(
                "La descripción debe tener entre 1 y 1000 caracteres.", ephemeral=True,
            )
            return
        if not imagen_url.startswith("https://") or len(imagen_url) > 500:
            await interaction.response.send_message("La imagen debe ser una URL HTTPS válida.", ephemeral=True)
            return
        if boost not in BOOST_LABELS:
            await interaction.response.send_message("Elige un efecto válido para el ítem.", ephemeral=True)
            return
        if precio < 0:
            await interaction.response.send_message("El precio no puede ser negativo.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            created = await add_bichos_item_catalog(
                nombre, descripcion, imagen_url, boost, precio,
            )
        except Exception:
            logger.exception("No se pudo añadir el ítem de Bichos %s", nombre)
            await interaction.followup.send("No pude guardar ese ítem.", ephemeral=True)
            return
        await interaction.followup.send(
            f"{'✅ Ítem añadido al catálogo: ' + nombre if created else 'Ya existe un ítem con ese nombre.'}",
            ephemeral=True,
        )

    @app_commands.command(
        name="add_item_bicho", description="Añade un ítem al catálogo de la tienda de Bichos",
    )
    @app_commands.describe(
        nombre="Nombre único del ítem",
        descripcion="Descripción visible en la tienda",
        precio="Precio de tienda en Bcoin",
        imagen_url="URL HTTPS de la imagen del ítem",
        boost="Efecto que aplica el ítem al usarlo",
    )
    @app_commands.choices(boost=[
        app_commands.Choice(name="Regeneración", value="regeneracion"),
        app_commands.Choice(name="Power", value="power"),
        app_commands.Choice(name="Vitamina", value="vitamina"),
        app_commands.Choice(name="Sin efecto", value="none"),
    ])
    @coordinator_only()
    async def add_item_bicho(self, interaction: discord.Interaction, nombre: str,
                             descripcion: str, precio: int, imagen_url: str,
                             boost: app_commands.Choice[str]):
        await self._add_item_bicho_impl(
            interaction, nombre, descripcion, imagen_url, boost.value, precio,
        )

    @app_commands.command(name="usuarios_bichos", description="Lista inscritos y sus Bichos")
    @coordinator_only()
    async def usuarios_bichos(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        view = StaffUsersView(interaction.user.id)
        try:
            await interaction.followup.send(
                embed=await view.get_embed(), view=view, ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            logger.exception("No se pudo listar usuarios de Bichos World")
            await interaction.followup.send("No pude consultar los inscritos.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Bichos(bot))
