import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from core import halloween
from modules.craft import CraftBuyButton, CraftConfirmButton, CraftLayout
from modules.economy import FinanceView
from modules.staff import CandyRankingView, Staff


class _AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_args):
        return False


class HalloweenTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        halloween._candy_balances.clear()
        halloween._user_locks.clear()

    def _pool(self, conn):
        pool = MagicMock()
        pool.acquire.return_value = _AsyncContext(conn)
        conn.transaction.return_value = _AsyncContext(None)
        return pool

    async def test_losing_roll_records_zero_without_credit(self):
        conn = MagicMock()
        conn.fetchval = AsyncMock(return_value=0)
        with patch.object(halloween.database, "pool", self._pool(conn)), \
             patch.object(halloween.random, "random", return_value=0.9):
            self.assertEqual(await halloween.award_candy(42, "work:1"), 0)
        conn.fetchval.assert_awaited_once()
        self.assertNotIn(42, halloween._candy_balances)

    async def test_winning_roll_credits_once_in_db_and_ram(self):
        conn = MagicMock()
        conn.fetchval = AsyncMock(side_effect=[2, 7, None])
        with patch.object(halloween.database, "pool", self._pool(conn)), \
             patch.object(halloween.random, "random", return_value=0.1), \
             patch.object(halloween.random, "randint", return_value=2):
            self.assertEqual(await halloween.award_candy(42, "dados:1"), 2)
            self.assertEqual(await halloween.award_candy(42, "dados:1"), 0)
        self.assertEqual(halloween._candy_balances[42], 7)
        self.assertEqual(conn.fetchval.await_count, 3)

    async def test_staff_candy_credit_is_atomic_and_updates_ram(self):
        conn = MagicMock()
        conn.fetchval = AsyncMock(return_value=17)
        with patch.object(halloween.database, "pool", self._pool(conn)):
            self.assertEqual(await halloween.add_candy_balance(42, 5), 17)
        self.assertEqual(halloween._candy_balances[42], 17)
        self.assertIn("ON CONFLICT (user_id) DO UPDATE", conn.fetchval.await_args.args[0])
        with self.assertRaises(ValueError):
            await halloween.add_candy_balance(42, 0)

    async def test_candy_ranking_page_reads_sorted_persistent_balance(self):
        conn = MagicMock()
        conn.fetchval = AsyncMock(return_value=12)
        conn.fetch = AsyncMock(return_value=[{"user_id": 42, "balance": 9}])
        with patch.object(halloween.database, "pool", self._pool(conn)):
            rows, total = await halloween.get_candy_ranking_page(10, 10)
        self.assertEqual((rows, total), ([{"user_id": 42, "balance": 9}], 12))
        self.assertIn("ORDER BY balance DESC, user_id ASC", conn.fetch.await_args.args[0])
        self.assertEqual(conn.fetch.await_args.args[1:], (10, 10))

    async def test_staff_candy_ranking_paginates_ten_users(self):
        self.assertIn("dulces_balance", [command.name for command in Staff.__cog_app_commands__])
        self.assertIn("add_candy", [command.name for command in Staff.__cog_app_commands__])
        view = CandyRankingView(7)
        rows = [{"user_id": user_id, "balance": 100 - user_id}
                for user_id in range(1, 11)]
        with patch("modules.staff.get_candy_ranking_page", new=AsyncMock(return_value=(rows, 12))), \
             patch("modules.staff.cache.get_member_name", return_value="Piloto"):
            embed = await view.get_embed()
        self.assertEqual(embed.title, "🍬 Top 10 Dulces")
        self.assertIn("Página 1/2", embed.footer.text)
        self.assertFalse(view.siguiente.disabled)
        self.assertTrue(view.anterior.disabled)
        self.assertIn("**99**", embed.description)
        view.page = 1
        with patch("modules.staff.get_candy_ranking_page", new=AsyncMock(return_value=(
            [{"user_id": 11, "balance": 10}, {"user_id": 12, "balance": 8}], 12,
        ))), patch("modules.staff.cache.get_member_name", return_value="Piloto"):
            second = await view.get_embed()
        self.assertIn("`11.`", second.description)
        self.assertIn("Página 2/2", second.footer.text)
        self.assertTrue(view.siguiente.disabled)
        self.assertFalse(view.anterior.disabled)

    async def test_notice_is_separate_and_not_scheduled_for_deletion(self):
        channel = MagicMock()
        channel.send = AsyncMock()
        with patch.object(halloween, "award_candy", new=AsyncMock(return_value=3)):
            await halloween.award_and_notify(channel, 42, "bj:1")
        args, kwargs = channel.send.await_args
        self.assertIn("<@42>", args[0])
        self.assertIn("3 <:Candy:1558963919492481055>", args[0])
        self.assertNotIn("delete_after", kwargs)

    async def test_craft_purchase_is_atomic_and_capped(self):
        conn = MagicMock()
        conn.fetchrow = AsyncMock(return_value={"id": 1, "nombre": "Calabaza", "precio": 3})
        conn.fetchval = AsyncMock(side_effect=[10, 2, 7])
        conn.execute = AsyncMock()
        with patch.object(halloween.database, "pool", self._pool(conn)), \
             patch.object(halloween.database.cache, "invalidate_inventory_cache") as invalidate:
            result = await halloween.purchase_craft_item(42, 1)
        self.assertEqual(result["balance"], 7)
        self.assertEqual(halloween._candy_balances[42], 7)
        conn.execute.assert_awaited_once()
        invalidate.assert_called_once_with(42)

        conn.fetchval = AsyncMock(side_effect=[10, 3])
        conn.execute.reset_mock()
        with patch.object(halloween.database, "pool", self._pool(conn)):
            result = await halloween.purchase_craft_item(42, 1)
        self.assertEqual(result["reason"], "inventory_cap")
        conn.execute.assert_not_awaited()

    def test_finance_button_and_craft_pagination(self):
        self.assertEqual(
            [button.label for button in FinanceView(42).children],
            ["Depositar", "Retirar", "Craft", "Salir"],
        )
        items = [
            {"id": index, "nombre": f"Item {index}", "icono": "🎃",
             "descripcion": "Prueba", "precio": 2}
            for index in range(1, 7)
        ]
        self.assertEqual(len(CraftLayout(items[:3], 42, 10).children), 1)
        self.assertEqual(len(CraftLayout(items[:4], 42, 10).children), 2)
        view = CraftLayout(items, 42, 10)
        self.assertEqual([child.item_id for child in view.walk_children()
                          if isinstance(child, CraftBuyButton)], [1, 2, 3])
        view.page = 1
        view.build()
        self.assertEqual([child.item_id for child in view.walk_children()
                          if isinstance(child, CraftBuyButton)], [4, 5, 6])

    async def test_craft_catalog_orders_by_price_then_id(self):
        conn = MagicMock()
        conn.fetch = AsyncMock(return_value=[])
        with patch.object(halloween.database, "pool", self._pool(conn)):
            await halloween.get_craft_items()
        self.assertIn("WHERE active=TRUE", conn.fetch.await_args.args[0])
        self.assertIn("ORDER BY precio ASC, id ASC", conn.fetch.await_args.args[0])

    async def test_delete_craft_hides_listing_without_deleting_owned_copies(self):
        conn = MagicMock()
        conn.fetchval = AsyncMock(side_effect=["Calabaza", None])
        with patch.object(halloween.database, "pool", self._pool(conn)):
            self.assertEqual(await halloween.delete_craft_item(7), "Calabaza")
            self.assertIsNone(await halloween.delete_craft_item(7))
        query = conn.fetchval.await_args_list[0].args[0]
        self.assertIn("SET active=FALSE", query)
        self.assertNotIn("DELETE FROM", query)

    async def test_delete_craft_autocomplete_lists_current_items(self):
        items = [
            {"id": 7, "nombre": "Calabaza", "icono": "🎃", "precio": 4},
            {"id": 8, "nombre": "Fantasma", "icono": "👻", "precio": 2},
        ]
        with patch("modules.staff.get_craft_items", new=AsyncMock(return_value=items)):
            choices = await Staff(None).delete_craft_autocomplete(MagicMock(), "cala")
        self.assertEqual(len(choices), 1)
        self.assertEqual(choices[0].value, "7")
        self.assertIn("Calabaza", choices[0].name)

    async def test_purchase_rechecks_that_listing_is_active(self):
        conn = MagicMock()
        conn.fetchrow = AsyncMock(return_value=None)
        with patch.object(halloween.database, "pool", self._pool(conn)):
            result = await halloween.purchase_craft_item(42, 7)
        self.assertEqual(result["reason"], "not_found")
        self.assertIn("active=TRUE FOR UPDATE", conn.fetchrow.await_args.args[0])

    async def test_craft_requires_confirmation_and_can_cancel(self):
        item = {"id": 1, "nombre": "Calabaza", "icono": "🎃",
                "descripcion": "Prueba", "precio": 3}
        view = CraftLayout([item], 42, 10)
        interaction = MagicMock()
        interaction.user.id = 42
        interaction.response.edit_message = AsyncMock()
        interaction.response.send_message = AsyncMock()
        buy = next(child for child in view.walk_children()
                   if isinstance(child, CraftBuyButton))
        self.assertEqual(buy.label, "Craft")
        with patch("modules.craft.purchase_craft_item", new=AsyncMock()) as purchase:
            await buy.callback(interaction)
            self.assertEqual(view.pending_item, item)
            purchase.assert_not_awaited()
            cancel = next(child for child in view.walk_children()
                          if isinstance(child, CraftConfirmButton) and not child.accept)
            await cancel.callback(interaction)
            purchase.assert_not_awaited()
        self.assertIsNone(view.pending_item)
        self.assertEqual(view.balance, 10)

    async def test_accept_crafts_once_and_updates_balance(self):
        item = {"id": 1, "nombre": "Calabaza", "icono": "🎃",
                "descripcion": "Prueba", "precio": 3}
        view = CraftLayout([item], 42, 10)
        view.pending_item = item
        view.build()
        interaction = MagicMock()
        interaction.user.id = 42
        interaction.response.defer = AsyncMock()
        interaction.response.send_message = AsyncMock()
        interaction.edit_original_response = AsyncMock()
        accept = next(child for child in view.walk_children()
                      if isinstance(child, CraftConfirmButton) and child.accept)
        result = {"ok": True, "balance": 7, "item_name": "Calabaza"}
        entered = asyncio.Event()
        release = asyncio.Event()

        async def delayed_purchase(*_args):
            entered.set()
            await release.wait()
            return result

        with patch("modules.craft.purchase_craft_item", new=AsyncMock(side_effect=delayed_purchase)) as purchase:
            first = asyncio.create_task(accept.callback(interaction))
            await entered.wait()
            await accept.callback(interaction)
            release.set()
            await first
        purchase.assert_awaited_once_with(42, 1)
        self.assertEqual(view.balance, 7)
        self.assertIsNone(view.pending_item)
        self.assertIn("Canjeaste", view.notice)


if __name__ == "__main__":
    unittest.main()
