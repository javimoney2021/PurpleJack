"""Uso de artículos desde el inventario efímero de /consultar."""

import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")
os.environ.setdefault("GUILD_ID", "980073134411644939")

from modules.shop import (
    ConfirmBuyView, InventarioLayout, UseButton, _get_item_use_lock,
)
from modules.player_panels import SimplePanel


def item(quantity=1):
    return {
        "id": 7,
        "nombre": "1K Goldstars",
        "icono": "⭐",
        "cantidad": quantity,
        "utilizable": True,
        "rol_id": None,
        "limite_uso": 0,
        "duracion": 0,
        "mensaje_uso": "Recibiste Goldstars.",
    }


class InventoryRefreshTests(unittest.IsolatedAsyncioTestCase):
    def test_balance_and_staff_no_longer_expose_craft_or_candy(self):
        from modules.economy import FinanceView
        from modules.staff import Staff

        self.assertEqual(
            [button.label for button in FinanceView(42).children],
            ["Depositar", "Retirar", "Salir"],
        )
        staff_commands = {command.name for command in Staff.__cog_app_commands__}
        self.assertFalse(staff_commands & {
            "add_craft", "delete_craft", "dulces_balance",
            "inventarios_craft", "purga_craft", "add_candy",
        })

    def setUp(self):
        access_patch = patch("bot.check_player_access", new=AsyncMock(return_value=True))
        access_patch.start()
        self.addCleanup(access_patch.stop)

    async def _use_item(self, remaining, *, edit_error=None):
        initial = item(2)
        message = SimpleNamespace(
            edit=AsyncMock(side_effect=edit_error),
            delete=AsyncMock(),
        )
        view = InventarioLayout([initial], 42, SimpleNamespace(id=10), None)
        view.message = message
        button = next(child for child in view.walk_children()
                      if isinstance(child, UseButton))
        component_message = SimpleNamespace(edit=AsyncMock(), delete=AsyncMock())
        interaction = SimpleNamespace(
            user=SimpleNamespace(
                id=42, nick=None, display_name="Usuario", mention="<@42>",
            ),
            message=component_message,
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        with patch("modules.shop.get_inventory", new=AsyncMock(side_effect=[
            [initial], remaining,
        ])), patch("modules.shop.consume_inventory_item", new=AsyncMock(return_value={
            "ok": True, "log_event_ids": [],
        })) as consume, patch("modules.shop._dispatch_item_use_logs", new=AsyncMock()):
            await button.callback(interaction)
        return view, message, component_message, interaction, consume

    async def test_use_refreshes_saved_message_when_one_unit_remains(self):
        view, message, component, interaction, consume = await self._use_item([item(1)])
        consume.assert_awaited_once()
        self.assertEqual(view.items[0]["cantidad"], 1)
        message.edit.assert_awaited_once_with(view=view)
        message.delete.assert_not_awaited()
        component.edit.assert_not_awaited()
        self.assertEqual(interaction.followup.send.await_count, 1)

    async def test_use_deletes_saved_message_after_last_unit(self):
        view, message, component, interaction, consume = await self._use_item([])
        consume.assert_awaited_once()
        self.assertTrue(view.is_finished())
        message.delete.assert_awaited_once()
        component.delete.assert_not_awaited()
        self.assertEqual(interaction.followup.send.await_count, 1)

    async def test_missing_panel_does_not_repeat_consumption_or_report_failure(self):
        missing = discord.NotFound(
            SimpleNamespace(status=404, reason="Not Found"), "Unknown Message",
        )
        view, message, component, interaction, consume = await self._use_item(
            [item(1)], edit_error=missing,
        )
        consume.assert_awaited_once()
        self.assertEqual(interaction.followup.send.await_count, 1)
        confirmation = interaction.followup.send.await_args.kwargs["content"]
        self.assertIn("El artículo se aplicó", confirmation)
        self.assertIn("/consultar", confirmation)
        self.assertNotIn("no fue consumido", confirmation)
        component.edit.assert_not_awaited()

    async def test_consult_inventory_saves_ephemeral_webhook_message(self):
        panel = SimplePanel(42, "consultar")
        button = next(child for child in panel.walk_children()
                      if getattr(child, "mode", None) == "inventario")
        message = SimpleNamespace(edit=AsyncMock(), delete=AsyncMock())
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=42),
            guild=SimpleNamespace(id=10),
            client=object(),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock(return_value=message)),
        )
        with patch("modules.player_panels._owned", new=AsyncMock(return_value=True)), \
             patch("core.database.get_inventory", new=AsyncMock(return_value=[item()])):
            await button.callback(interaction)
        kwargs = interaction.followup.send.await_args.kwargs
        self.assertTrue(kwargs["ephemeral"])
        self.assertTrue(kwargs["wait"])
        self.assertIs(kwargs["view"].message, message)

    async def test_second_click_cannot_consume_same_item_while_first_is_running(self):
        view = InventarioLayout([item()], 42, SimpleNamespace(id=10), None)
        button = next(child for child in view.walk_children()
                      if isinstance(child, UseButton))
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=42),
            response=SimpleNamespace(send_message=AsyncMock()),
        )
        lock = _get_item_use_lock(42, 7)
        await lock.acquire()
        try:
            with patch("modules.shop.consume_inventory_item", new=AsyncMock()) as consume:
                await button.callback(interaction)
            consume.assert_not_awaited()
            self.assertIn("Ya estoy procesando", interaction.response.send_message.await_args.args[0])
        finally:
            lock.release()


    async def test_processed_purchase_is_not_reported_as_failed_when_ui_is_missing(self):
        purchase_item = {
            "id": 9, "nombre": "Premio", "icono": "🎁", "precio": 100,
        }
        view = ConfirmBuyView(
            42, purchase_item, SimpleNamespace(get_channel=lambda _id: None),
        )
        missing = discord.NotFound(
            SimpleNamespace(status=404, reason="Not Found"), "Unknown Message",
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=42, roles=[], nick=None,
                                 display_name="Usuario"),
            response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock(side_effect=missing),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        result = {
            "ok": True, "item": purchase_item, "precio_unitario": 100,
            "total": 100, "cantidad_compra": 1,
        }
        with patch("modules.shop.purchase_item", new=AsyncMock(return_value=result)) as buy, \
             patch("modules.shop.logger.exception"):
            await view.confirmar.callback(interaction)
        buy.assert_awaited_once()
        confirmation = interaction.followup.send.await_args.args[0]
        self.assertIn("La compra fue procesada", confirmation)
        self.assertNotIn("Error al procesar", confirmation)


if __name__ == "__main__":
    unittest.main()
