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
CRAFT_ITEMS_PER_PAGE = 5
CRAFT_PANEL_SECONDS = 60


class CraftBuyButton(discord.ui.Button):
    def __init__(self, item: dict):
        super().__init__(
            label="CRAFT",
            style=discord.ButtonStyle.secondary,
            custom_id=f"halloween_craft_{item['id']}",
        )
        self.item_id = item["id"]

    async def callback(self, interaction: discord.Interaction):
        view: CraftLayout = self.view
        if not await view.check_access(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            result = await purchase_craft_item(interaction.user.id, self.item_id)
        except Exception:
            logger.exception("Falló el canje de Halloween de %s", interaction.user.id)
            return await interaction.followup.send(
                "❌ No pude completar el canje. Tus dulces no fueron descontados.",
                ephemeral=True,
            )

        if not result["ok"]:
            reasons = {
                "not_found": "❌ Este artículo ya no está disponible.",
                "insufficient": "❌ No tienes suficientes dulces para este canje.",
                "inventory_cap": "❌ Ya tienes x3 de este artículo en tu inventario.",
            }
            return await interaction.followup.send(
                reasons.get(result["reason"], "❌ No se pudo completar el canje."),
                ephemeral=True,
            )

        view.balance = result["balance"]
        view.build()
        try:
            await interaction.edit_original_response(view=view)
        except discord.HTTPException:
            logger.warning("Canje procesado, pero no se pudo refrescar el panel de %s", interaction.user.id)
        if view.balance_message is not None:
            try:
                embed = view.balance_message.embeds[0]
                embed.set_field_at(
                    2, name=embed.fields[2].name,
                    value=f"{view.balance} {CANDY}", inline=True,
                )
                await view.balance_message.edit(embed=embed)
            except (IndexError, discord.HTTPException):
                logger.warning("Canje procesado, pero no se pudo actualizar !bal de %s", interaction.user.id)
        await interaction.followup.send(
            f"✅ Canjeaste **{result['item_name']}**. Ya aparece en **!inv**.",
            ephemeral=True,
        )


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
