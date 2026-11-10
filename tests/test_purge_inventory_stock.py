import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")

from core import database


class _AsyncContext:
    def __init__(self, value):
        self.value = value
        self.error = None

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, kind, error, traceback):
        self.error = error
        return False


class PurgeInventoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_inventory_reads_only_normal_items_after_craft_retirement(self):
        conn = MagicMock()
        conn.fetch = AsyncMock(return_value=[{
            "id": 7, "nombre": "Veterano", "cantidad": 2,
        }])
        pool = MagicMock()
        pool.acquire.return_value = _AsyncContext(conn)
        with patch.object(database, "pool", pool):
            items = await database.get_inventory_from_db(42)
        self.assertEqual([item["nombre"] for item in items], ["Veterano"])
        conn.fetch.assert_awaited_once()
        self.assertNotIn("halloween", conn.fetch.await_args.args[0].lower())

    async def test_halloween_cleanup_drops_only_retired_tables_and_clears_inventory_cache(self):
        conn = MagicMock()
        conn.execute = AsyncMock()
        conn.transaction.return_value = _AsyncContext(None)
        pool = MagicMock()
        pool.acquire.return_value = _AsyncContext(conn)
        with patch.object(database, "pool", pool), \
             patch.object(database.cache, "_inventory_cache", {42: ["old-craft"]}) as inventory_cache:
            await database.drop_halloween_tables()
            self.assertEqual(inventory_cache, {})
        self.assertEqual([call.args[0] for call in conn.execute.await_args_list], [
            "DROP TABLE IF EXISTS halloween_craft_inventory",
            "DROP TABLE IF EXISTS halloween_craft_items",
            "DROP TABLE IF EXISTS halloween_candy_balances",
            "DROP TABLE IF EXISTS halloween_candy_awards",
        ])

    async def _purge(self, inventory, lots, fail_stock=False):
        conn = MagicMock()
        statements = []

        async def fetch(query, *_args):
            if "FROM inventario_vencimientos" in query:
                return lots
            if "FROM inventario" in query:
                return inventory
            return []

        async def fetchval(query, *_args):
            return False

        async def execute(query, *_args):
            statements.append(query.strip())
            if fail_stock and "UPDATE items SET stock" in query:
                raise RuntimeError("stock unavailable")

        conn.fetch = AsyncMock(side_effect=fetch)
        conn.fetchval = AsyncMock(side_effect=fetchval)
        conn.execute = AsyncMock(side_effect=execute)
        transaction = _AsyncContext(None)
        conn.transaction.return_value = transaction
        pool = MagicMock()
        pool.acquire.return_value = _AsyncContext(conn)
        with patch.object(database, "pool", pool), \
             patch.object(database.cache, "purge_user_cache") as clear_cache:
            if fail_stock:
                with self.assertRaisesRegex(RuntimeError, "stock unavailable"):
                    await database.purge_user_data(42)
            else:
                self.assertTrue(await database.purge_user_data(42))
        return conn, statements, transaction, clear_cache

    async def test_restock_and_both_inventory_deletes_share_transaction(self):
        conn, statements, transaction, clear_cache = await self._purge(
            [{"item_id": 7, "cantidad": 2}],
            [{"item_id": 7, "cantidad": 2, "stock_unidades": 2}],
        )
        self.assertIsNone(transaction.error)
        restock_index = next(i for i, sql in enumerate(statements)
                             if "UPDATE items SET stock" in sql)
        lots_index = next(i for i, sql in enumerate(statements)
                          if "DELETE FROM inventario_vencimientos" in sql)
        inventory_index = next(i for i, sql in enumerate(statements)
                               if "DELETE FROM inventario WHERE" in sql)
        self.assertLess(restock_index, lots_index)
        self.assertLess(lots_index, inventory_index)
        self.assertEqual(conn.execute.await_args_list[restock_index].args[1:], (2, 7))
        clear_cache.assert_called_once_with(42)

    async def test_inconsistent_lot_does_not_create_stock(self):
        _conn, statements, _transaction, _clear_cache = await self._purge(
            [{"item_id": 7, "cantidad": 1}],
            [{"item_id": 7, "cantidad": 2, "stock_unidades": 2}],
        )
        self.assertFalse(any("UPDATE items SET stock" in sql for sql in statements))

    async def test_stock_failure_rolls_back_and_preserves_cache(self):
        _conn, statements, transaction, clear_cache = await self._purge(
            [{"item_id": 7, "cantidad": 2}],
            [{"item_id": 7, "cantidad": 2, "stock_unidades": 2}],
            fail_stock=True,
        )
        self.assertIsInstance(transaction.error, RuntimeError)
        self.assertFalse(any("DELETE FROM inventario_vencimientos" in sql
                             for sql in statements))
        clear_cache.assert_not_called()
