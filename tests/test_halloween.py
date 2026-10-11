import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from core import halloween
from modules.craft import CraftLayout
from modules.economy import FinanceView


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
        self.assertEqual(len(CraftLayout(items[:5], 42, 10).children), 1)
        self.assertEqual(len(CraftLayout(items, 42, 10).children), 2)


if __name__ == "__main__":
    unittest.main()
