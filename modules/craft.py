"""Panel privado de canje de dulces de Halloween."""

import asyncio
import logging
import time

import discord

from core.halloween import (
    CANDY,
    get_candy_balance,
    get_craft_items,
    purchase_craft_item,
)


logger = logging.getLogger(__name__)
CRAFT_ITEMS_PER_PAGE = 3
CRAFT_PANEL_SECONDS = 60


class CraftBuyButton(discord.ui.Button):
    def __init__(self, item: dict):
        super().__init__(
            label="Craft",
            style=discord.ButtonStyle.success,
            custom_id=f"halloween_craft_{item['id']}",
        )
        self.item_id = item["id"]

    async def callback(self, interaction: discord.Interaction):
        view: CraftLayout = self.view
        if not await view.check_access(interaction):
            return
        if view.processing:
            return await interaction.response.send_message(
                "⏳ Se está procesando un canje.", ephemeral=True,
            )
        item = next((item for item in view.items if item["id"] == self.item_id), None)
        if item is None:
            return await interaction.response.send_message(
                "❌ Este artículo ya no está disponible.", ephemeral=True,
            )
        view.pending_item = item
        view.notice = None
        view.build()
        await interaction.response.edit_message(view=view)


class CraftConfirmButton(discord.ui.Button):
    def __init__(self, accept: bool):
        super().__init__(
            label="Aceptar" if accept else "Cancelar",
            style=discord.ButtonStyle.success if accept else discord.ButtonStyle.danger,
        )
        self.accept = accept

    async def callback(self, interaction: discord.Interaction):
        view: CraftLayout = self.view
        if not await view.check_access(interaction):
            return
        if view.processing:
            return await interaction.response.send_message(
                "⏳ Se está procesando un canje.", ephemeral=True,
            )
        if view.pending_item is None:
            return await interaction.response.send_message(
                "❌ Este canje ya fue atendido.", ephemeral=True,
            )
        if not self.accept:
            view.pending_item = None
            view.notice = "Canje cancelado. No se descontaron dulces."
            view.build()
            return await interaction.response.edit_message(view=view)

        view.processing = True
        item_id = view.pending_item["id"]
        try:
            await interaction.response.defer(ephemeral=True)
            try:
                result = await purchase_craft_item(interaction.user.id, item_id)
            except Exception:
                logger.exception("Falló el canje de Halloween de %s", interaction.user.id)
                result = {"ok": False, "reason": "error"}

            view.pending_item = None
            if result["ok"]:
                view.balance = result["balance"]
                view.notice = f"✅ Canjeaste **{result['item_name']}**. Ya aparece en **!inv**."
            else:
                reasons = {
                    "not_found": "❌ Este artículo ya no está disponible.",
                    "insufficient": "❌ No tienes suficientes dulces para este canje.",
                    "inventory_cap": "❌ Ya tienes x3 de este artículo en tu inventario.",
                    "error": "❌ No pude completar el canje. Tus dulces no fueron descontados.",
                }
                view.notice = reasons.get(result["reason"], "❌ No se pudo completar el canje.")
            view.build()
            try:
                await interaction.edit_original_response(view=view)
            except discord.HTTPException:
                logger.warning("No se pudo refrescar el panel de crafteo de %s", interaction.user.id)
                await interaction.followup.send(view.notice, ephemeral=True)

            if result["ok"] and view.balance_message is not None:
                try:
                    embed = view.balance_message.embeds[0]
                    embed.set_field_at(
                        2, name=embed.fields[2].name,
                        value=f"{view.balance} {CANDY}", inline=True,
                    )
                    await view.balance_message.edit(embed=embed)
                except (IndexError, discord.HTTPException):
                    logger.warning("Canje procesado, pero no se pudo actualizar !bal de %s", interaction.user.id)
        finally:
            view.processing = False


class CraftNavButton(discord.ui.Button):
    def __init__(self, direction: int, disabled: bool):
        super().__init__(
            label="◀ Anterior" if direction < 0 else "Siguiente ▶",
            style=discord.ButtonStyle.secondary,
            disabled=disabled,
        )
        self.direction = direction

    async def callback(self, interaction: discord.Interaction):
        view: CraftLayout = self.view
        if not await view.check_access(interaction):
            return
        view.page += self.direction
        view.build()
        await interaction.response.edit_message(view=view)


class CraftLayout(discord.ui.LayoutView):
    def __init__(self, items: list[dict], owner_id: int, balance: int,
                 balance_message: discord.Message | None = None):
        super().__init__(timeout=CRAFT_PANEL_SECONDS)
        self.items = items
        self.owner_id = owner_id
        self.balance = balance
        self.balance_message = balance_message
        self.page = 0
        self.pending_item: dict | None = None
        self.notice: str | None = None
        self.processing = False
        self.expires_at = time.monotonic() + CRAFT_PANEL_SECONDS
        self.build()

    async def check_access(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "❌ Este panel no te pertenece.", ephemeral=True,
            )
            return False
        if time.monotonic() >= self.expires_at:
            await interaction.response.send_message(
                "⌛ El panel de crafteo caducó. Abre **!bal** nuevamente.",
                ephemeral=True,
            )
            return False
        return True

    def build(self):
        self.clear_items()
        total_pages = max(1, (len(self.items) + CRAFT_ITEMS_PER_PAGE - 1) // CRAFT_ITEMS_PER_PAGE)
        self.page = max(0, min(self.page, total_pages - 1))
        start = self.page * CRAFT_ITEMS_PER_PAGE
        page_items = self.items[start:start + CRAFT_ITEMS_PER_PAGE]

        container = discord.ui.Container(accent_color=0xF28C28)
        container.add_item(discord.ui.TextDisplay(
            "## 🎃 CRAFTEO HALLOWEEN 🎃\n"
            "Canjea tus dulces por alguno de los siguientes items..."
        ))
        container.add_item(discord.ui.Separator())
        if self.pending_item is not None:
            item = self.pending_item
            container.add_item(discord.ui.TextDisplay(
                f"¿Confirmas el canje de {item['icono'] or '🎁'} **{item['nombre']}** "
                f"por **{item['precio']} {CANDY}**?\n"
                f"Tus dulces actuales: **{self.balance} {CANDY}**. "
                "Solo se descontarán al aceptar."
            ))
            self.add_item(container)
            row = discord.ui.ActionRow()
            row.add_item(CraftConfirmButton(True))
            row.add_item(CraftConfirmButton(False))
            self.add_item(row)
            return
        if self.notice:
            container.add_item(discord.ui.TextDisplay(self.notice))
        if not page_items:
            container.add_item(discord.ui.TextDisplay("Aún no hay artículos para canjear."))
        for item in page_items:
            icon = item["icono"] or "🎁"
            container.add_item(discord.ui.Section(
                discord.ui.TextDisplay(
                    f"{icon} **{item['nombre']}**\n"
                    f"{item['descripcion']}  •  **{item['precio']} {CANDY}**"
                ),
                accessory=CraftBuyButton(item),
            ))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(
            f"-# Página {self.page + 1}/{total_pages}  •  Tus dulces: {self.balance} {CANDY}"
        ))
        self.add_item(container)

        if total_pages > 1:
            row = discord.ui.ActionRow()
            row.add_item(CraftNavButton(-1, self.page == 0))
            row.add_item(CraftNavButton(1, self.page >= total_pages - 1))
            self.add_item(row)

    async def delete_after_open(self, message: discord.WebhookMessage):
        await asyncio.sleep(max(0, self.expires_at - time.monotonic()))
        self.stop()
        try:
            await message.delete()
        except (discord.HTTPException, discord.NotFound):
            pass


async def open_craft_panel(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        items = await get_craft_items()
        balance = await get_candy_balance(interaction.user.id)
        view = CraftLayout(items, interaction.user.id, balance, interaction.message)
        message = await interaction.followup.send(view=view, ephemeral=True, wait=True)
        asyncio.create_task(view.delete_after_open(message))
    except Exception:
        logger.exception("No se pudo abrir el crafteo de %s", interaction.user.id)
        await interaction.followup.send(
            "❌ No se pudo abrir el crafteo. Inténtalo de nuevo.", ephemeral=True,
        )
