import datetime
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")
os.environ.setdefault("GUILD_ID", "980073134411644939")

from core import cache, database
from modules.bichos import (
    Bichos, BichosInventoryView, BichosItemShopView, BichosShopView,
    BuyBichoButton, BuyItemButton, EnrollmentView, HomeView,
    SectionView, SECTIONS, SHOP_PAGE_SIZE, UseItemButton,
    build_bichos_inventory, build_bichos_item_shop, build_bichos_shop,
)


class AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_args):
        return False


class BichosTests(unittest.IsolatedAsyncioTestCase):
    def _pool(self, conn):
        pool = MagicMock()
        pool.acquire.return_value = AsyncContext(conn)
        conn.transaction.return_value = AsyncContext(None)
        return pool

    @staticmethod
    def _buttons(view):
        return [child for row in view.children[0].children
                if row.__class__.__name__ == "ActionRow"
                for child in row.children]

    async def test_tables_and_registration_are_persistent_and_idempotent(self):
        conn = MagicMock()
        conn.execute = AsyncMock()
        conn.fetchval = AsyncMock(side_effect=[42, None])
        with patch.object(database, "pool", self._pool(conn)):
            await database.init_bichos_tables()
            self.assertTrue(await database.register_bichos_player(42, "  Piloto   Galáctico  "))
            self.assertFalse(await database.register_bichos_player(42, "Piloto Galáctico"))
        self.assertEqual(conn.execute.await_count, 21)
        self.assertEqual(conn.fetchval.await_args_list[0].args[1:], (42, "Piloto Galáctico"))
        foreign_key_tables = [call.args[0] for call in conn.execute.await_args_list
                              if "REFERENCES bichos_players" in call.args[0]]
        self.assertEqual(len(foreign_key_tables), 2)
        self.assertTrue(all("ON DELETE CASCADE" in query for query in foreign_key_tables))

    async def test_registered_player_gets_public_v2_panel(self):
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=42, display_name="Capitán"),
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        with patch("modules.bichos.get_bichos_registration", new=AsyncMock(return_value=True)), \
             patch("modules.bichos.get_bichos_balance", new=AsyncMock(return_value=0)) as balance:
            await Bichos.bichos.callback(Bichos(None), interaction)
        balance.assert_awaited_once_with(42)
        kwargs = interaction.response.send_message.await_args.kwargs
        self.assertFalse(kwargs["ephemeral"])
        panel = kwargs["view"]
        self.assertIsInstance(panel, HomeView)
        self.assertEqual(panel.to_components()[0]["type"], 17)
        self.assertIn("Saldo Bcoin: 0", panel.to_components()[0]["components"][0]["content"])
        self.assertIn("Capitán", panel.to_components()[0]["components"][0]["content"])
        labels = [child.label for row in panel.children[0].children
                  if row.__class__.__name__ == "ActionRow" for child in row.children]
        self.assertEqual(labels, [title for title, _ in SECTIONS])

    async def test_bcoin_balance_is_queried_per_user(self):
        conn = MagicMock()
        conn.fetchval = AsyncMock(side_effect=[7, 0])
        with patch.object(database, "pool", self._pool(conn)):
            self.assertEqual(await database.get_bichos_balance(42), 7)
            self.assertEqual(await database.get_bichos_balance(43), 0)
        self.assertEqual([call.args[1] for call in conn.fetchval.await_args_list], [42, 43])

    async def test_unregistered_player_gets_private_enrollment(self):
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=42),
            response=SimpleNamespace(send_message=AsyncMock()),
            original_response=AsyncMock(return_value=SimpleNamespace(delete=AsyncMock())),
        )
        with patch("modules.bichos.get_bichos_registration", new=AsyncMock(return_value=False)), \
             patch("modules.bichos.asyncio.create_task") as schedule:
            await Bichos.bichos.callback(Bichos(None), interaction)
        kwargs = interaction.response.send_message.await_args.kwargs
        self.assertTrue(kwargs["ephemeral"])
        self.assertEqual(kwargs["view"].children[0].label, "Inscribirme")
        schedule.call_args.args[0].close()

    async def test_enrollment_saves_nickname_and_confirmation_expires(self):
        view = EnrollmentView(42)
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=42, display_name="Capitán"),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock(
                return_value=SimpleNamespace(delete=AsyncMock()),
            )),
        )
        with patch("bot.check_player_access", new=AsyncMock(return_value=True)), \
             patch("modules.bichos.register_bichos_player", new=AsyncMock(return_value=True)) as register, \
             patch("modules.bichos.asyncio.create_task") as schedule:
            await view.enroll.callback(interaction)
        register.assert_awaited_once_with(42, "Capitán")
        self.assertTrue(interaction.followup.send.await_args.kwargs["ephemeral"])
        self.assertIn("/bichos", interaction.followup.send.await_args.args[0])
        schedule.call_args.args[0].close()

    async def test_section_button_opens_private_placeholder(self):
        public = HomeView()
        button = next(child for row in public.children[0].children
                      if row.__class__.__name__ == "ActionRow"
                      for child in row.children if child.label == "Mis Bichos")
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=42),
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        with patch("bot.check_player_access", new=AsyncMock(return_value=True)), \
             patch("modules.bichos.get_bichos_registration", new=AsyncMock(return_value=True)), \
             patch("modules.bichos.get_owned_bichos", new=AsyncMock(return_value=[])):
            await button.callback(interaction)
        kwargs = interaction.followup.send.await_args.kwargs
        self.assertTrue(kwargs["ephemeral"])
        section = kwargs["view"]
        self.assertIsInstance(section, SectionView)
        self.assertEqual([child.label for child in section.children[0].children[-1].children],
                         ["Volver", "Inicio"])

    async def test_owned_bicho_page_shows_stats_lives_and_actions(self):
        from modules.bichos import build_section
        owned = [{"id": 1, "species_key": "Nebulín", "nickname": "Chispa",
                  "level": 2, "lives": 2, "equipped": True,
                  "image_url": "https://example.com/bicho.png", "attack": 8, "defense": 5}]
        with patch("modules.bichos.get_owned_bichos", new=AsyncMock(return_value=owned)):
            view = await build_section(42, "Mis Bichos")
        self.assertEqual(view.title, "Mis Bichos")
        self.assertEqual([child.label for row in view.children[0].children
                          if row.__class__.__name__ == "ActionRow"
                          for child in row.children],
                         ["Equipar", "Curar", "Cazar", "Abandonar", "Volver", "Inicio"])
        self.assertEqual(view.to_components()[0]["type"], 17)

    async def test_pvp_select_lists_ten_players_and_navigation(self):
        from modules.bichos import build_section, PvpSelect
        players = [{"user_id": i, "nickname": f"Piloto {i}", "bichos_count": 0}
                   for i in range(1, 11)]
        with patch("modules.bichos.get_bichos_players_page",
                   new=AsyncMock(return_value=(players, 15))) as fetch:
            view = await build_section(99, "PVP")
        fetch.assert_awaited_once_with(99, 0, 10)
        selector = next(child for row in view.children[0].children
                        if row.__class__.__name__ == "ActionRow"
                        for child in row.children if isinstance(child, PvpSelect))
        self.assertEqual(len(selector.options), 11)
        self.assertEqual(selector.options[-1].value, "next")

    async def test_bichos_catalog_uses_ram_cache_and_invalidates_on_insert(self):
        cache.clear_bichos_catalog_cache()
        rows = [{"id": 1, "name": "Sodax", "description": "Bicho de fuego",
                 "image_url": "https://example.com/sodax.png",
                 "attack": 9, "defense": 4, "price": 120}]
        conn = MagicMock()
        conn.fetch = AsyncMock(return_value=rows)
        conn.fetchval = AsyncMock(return_value=1)
        try:
            with patch.object(database, "pool", self._pool(conn)):
                self.assertEqual(await database.get_bichos_catalog(), rows)
                self.assertEqual(await database.get_bichos_catalog(), rows)
                self.assertEqual(conn.fetch.await_count, 1)

                await database.add_bichos_catalog(
                    "Sodax", "Bicho de fuego", rows[0]["image_url"], 9, 4, 120,
                )
                self.assertEqual(
                    conn.fetchval.await_args.args[1:],
                    ("Sodax", "Bicho de fuego", rows[0]["image_url"], 9, 4, 120),
                )
                self.assertIsNone(cache.get_bichos_catalog_cache())

                await database.get_bichos_catalog()
                self.assertEqual(conn.fetch.await_count, 2)
        finally:
            cache.clear_bichos_catalog_cache()

    async def test_purchase_bicho_deducts_balance_and_registers_owned(self):
        conn = MagicMock()
        conn.fetchrow = AsyncMock(side_effect=[
            {"bcoin_balance": 100},
            {"id": 3, "name": "Sodax", "price": 60},
        ])
        conn.fetchval = AsyncMock(side_effect=[0, "token-1", 40, 77])
        with patch.object(database, "pool", self._pool(conn)):
            result = await database.purchase_bicho(42, 3, "token-1")
        self.assertEqual(result, {
            "ok": True, "owned_id": 77, "name": "Sodax",
            "price": 60, "balance": 40,
        })
        self.assertEqual(conn.fetchval.await_args_list[1].args[1:],
                         ("token-1", 42, 3, 60))
        self.assertEqual(conn.fetchval.await_args_list[2].args[1:], (42, 60))
        self.assertEqual(conn.fetchval.await_args_list[3].args[1:], (42, "Sodax"))

    async def test_purchase_bicho_rejects_insufficient_balance(self):
        conn = MagicMock()
        conn.fetchrow = AsyncMock(side_effect=[
            {"bcoin_balance": 10},
            {"id": 3, "name": "Sodax", "price": 60},
        ])
        conn.fetchval = AsyncMock(return_value=0)
        with patch.object(database, "pool", self._pool(conn)):
            result = await database.purchase_bicho(42, 3, "token-2")
        self.assertEqual(result, {
            "ok": False, "reason": "insufficient_balance",
            "balance": 10, "price": 60,
        })
        self.assertEqual(conn.fetchval.await_count, 1)

    async def test_purchase_bicho_rejects_ownership_limit(self):
        conn = MagicMock()
        conn.fetchrow = AsyncMock(side_effect=[
            {"bcoin_balance": 1000},
            {"id": 3, "name": "Sodax", "price": 60},
        ])
        conn.fetchval = AsyncMock(return_value=database.MAX_BICHOS_PER_USER)
        with patch.object(database, "pool", self._pool(conn)):
            result = await database.purchase_bicho(42, 3, "token-3")
        self.assertEqual(result, {
            "ok": False, "reason": "max_owned",
            "limit": database.MAX_BICHOS_PER_USER,
            "owned": database.MAX_BICHOS_PER_USER,
        })
        self.assertEqual(conn.fetchval.await_count, 1)

    async def test_purchase_bicho_duplicate_token_does_not_charge(self):
        conn = MagicMock()
        conn.fetchrow = AsyncMock(side_effect=[
            {"bcoin_balance": 100},
            {"id": 3, "name": "Sodax", "price": 60},
        ])
        conn.fetchval = AsyncMock(side_effect=[0, None])
        with patch.object(database, "pool", self._pool(conn)):
            result = await database.purchase_bicho(42, 3, "token-4")
        self.assertEqual(result, {"ok": False, "reason": "duplicate"})
        self.assertEqual(conn.fetchval.await_count, 2)
        queries = [call.args[0] for call in conn.fetchval.await_args_list]
        self.assertFalse(any("UPDATE bichos_players" in query for query in queries))

    async def test_shop_shows_three_bichos_per_page_with_buy_buttons(self):
        catalog = [
            {"id": i, "name": f"Bicho {i}", "description": f"Descripción {i}",
             "image_url": f"https://example.com/bicho-{i}.png",
             "attack": i, "defense": i + 1, "price": i * 100}
            for i in range(1, 8)
        ]
        with patch("modules.bichos.get_bichos_catalog",
                   new=AsyncMock(return_value=catalog)), \
             patch("modules.bichos.get_bichos_balance",
                   new=AsyncMock(return_value=250)):
            first = await build_bichos_shop(42, 0)
            middle = await build_bichos_shop(42, 1)
            last = await build_bichos_shop(42, 99)
        self.assertEqual(SHOP_PAGE_SIZE, 3)
        self.assertEqual((first.page, first.pages, first.total), (0, 3, 7))
        self.assertEqual(len(self._buttons(first)), 7)
        for view, expected in ((first, 3), (middle, 3), (last, 1)):
            buy_buttons = [button for button in self._buttons(view)
                           if isinstance(button, BuyBichoButton)]
            self.assertEqual(len(buy_buttons), expected)
            nav = self._buttons(view)[-4:]
            self.assertEqual([button.label for button in nav],
                             ["Anterior", "Siguiente", "Volver", "Inicio"])
        self.assertTrue(self._buttons(first)[-4].disabled)
        self.assertFalse(self._buttons(first)[-3].disabled)
        self.assertFalse(self._buttons(last)[-4].disabled)
        self.assertTrue(self._buttons(last)[-3].disabled)
        self.assertEqual(last.balance, 250)

    async def test_repeated_buy_interaction_is_not_charged_twice(self):
        bicho = {"id": 3, "name": "Sodax", "description": "Bicho de fuego",
                 "image_url": "https://example.com/sodax.png",
                 "attack": 9, "defense": 4, "price": 60}
        view = BichosShopView(42, [bicho], page=0, pages=1, total=1, balance=100)
        button = next(button for button in self._buttons(view)
                      if isinstance(button, BuyBichoButton))
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=42),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
            edit_original_response=AsyncMock(),
        )
        results = [
            {"ok": True, "owned_id": 1, "name": "Sodax", "price": 60, "balance": 40},
            {"ok": False, "reason": "duplicate"},
        ]
        with patch("modules.bichos.purchase_bicho",
                   new=AsyncMock(side_effect=results)) as purchase, \
             patch("modules.bichos.build_bichos_shop",
                   new=AsyncMock(return_value=view)):
            await button.callback(interaction)
            await button.callback(interaction)
        self.assertEqual([call.args[2] for call in purchase.await_args_list],
                         [button.purchase_token, button.purchase_token])
        self.assertIn("ya fue procesada", interaction.followup.send.await_args.args[0])

    async def test_buy_button_reports_insufficient_balance(self):
        bicho = {"id": 3, "name": "Sodax", "price": 60, "attack": 9,
                 "defense": 4, "description": "Bicho de fuego",
                 "image_url": "https://example.com/sodax.png"}
        view = BichosShopView(42, [bicho], page=0, pages=1, total=1, balance=10)
        button = next(button for button in self._buttons(view)
                      if isinstance(button, BuyBichoButton))
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=42),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
            edit_original_response=AsyncMock(),
        )
        with patch("modules.bichos.purchase_bicho", new=AsyncMock(return_value={
            "ok": False, "reason": "insufficient_balance",
            "balance": 10, "price": 60,
        })):
            await button.callback(interaction)
        message = interaction.followup.send.await_args.args[0]
        self.assertIn("No tienes Bcoin suficientes", message)
        self.assertIn("10", message)
        self.assertIn("60", message)
        interaction.edit_original_response.assert_not_awaited()

    async def test_add_bicho_persists_every_field(self):
        interaction = SimpleNamespace(
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        with patch("modules.bichos.add_bichos_catalog",
                   new=AsyncMock(return_value=1)) as add:
            await Bichos.add_bicho.callback(
                Bichos(None), interaction,
                "Sodax", "Bicho de fuego", 120, 9, 4,
                "https://example.com/sodax.png",
            )
        add.assert_awaited_once_with(
            "Sodax", "Bicho de fuego", "https://example.com/sodax.png", 9, 4, 120,
        )
        self.assertIn("Sodax", interaction.followup.send.await_args.args[0])

    async def test_add_bicho_rejects_invalid_url_without_saving(self):
        interaction = SimpleNamespace(
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        with patch("modules.bichos.add_bichos_catalog",
                   new=AsyncMock()) as add:
            await Bichos.add_bicho.callback(
                Bichos(None), interaction,
                "Sodax", "Bicho de fuego", 120, 9, 4, "http://example.com/sodax.png",
            )
        add.assert_not_awaited()
        interaction.response.send_message.assert_awaited_once()
        self.assertTrue(interaction.response.send_message.await_args.kwargs["ephemeral"])

    async def test_bichos_item_catalog_uses_ram_cache_and_invalidates_on_insert(self):
        cache.clear_bichos_item_catalog_cache()
        rows = [{"id": 1, "name": "Poción Power", "description": "Sube stats",
                 "image_url": "https://example.com/power.png", "boost": "power",
                 "icon": "⚡", "price": 50}]
        conn = MagicMock()
        conn.fetch = AsyncMock(return_value=rows)
        conn.fetchval = AsyncMock(return_value=1)
        try:
            with patch.object(database, "pool", self._pool(conn)):
                self.assertEqual(await database.get_bichos_item_catalog(), rows)
                self.assertEqual(await database.get_bichos_item_catalog(), rows)
                self.assertEqual(conn.fetch.await_count, 1)

                await database.add_bichos_item_catalog(
                    "Poción Power", "Sube stats", rows[0]["image_url"], "power", 50,
                )
                self.assertEqual(
                    conn.fetchval.await_args.args[1:],
                    ("Poción Power", "Sube stats", "⚡",
                     rows[0]["image_url"], "power", 50),
                )
                self.assertIsNone(cache.get_bichos_item_catalog_cache())

                await database.get_bichos_item_catalog()
                self.assertEqual(conn.fetch.await_count, 2)
        finally:
            cache.clear_bichos_item_catalog_cache()

    async def test_purchase_bicho_item_deducts_balance_and_adds_to_inventory(self):
        conn = MagicMock()
        conn.fetchrow = AsyncMock(side_effect=[
            {"bcoin_balance": 200},
            {"id": 5, "name": "Poción Power", "price": 50},
        ])
        conn.fetchval = AsyncMock(side_effect=["token-item", 150, 2])
        with patch.object(database, "pool", self._pool(conn)):
            result = await database.purchase_bicho_item(42, 5, "token-item")
        self.assertEqual(result, {
            "ok": True, "name": "Poción Power", "price": 50,
            "balance": 150, "quantity": 2,
        })
        self.assertEqual(conn.fetchval.await_args_list[0].args[1:],
                         ("token-item", 42, 5, 50))
        self.assertEqual(conn.fetchval.await_args_list[1].args[1:], (42, 50))
        self.assertEqual(conn.fetchval.await_args_list[2].args[1:],
                         (42, "Poción Power"))

    async def test_purchase_bicho_item_rejects_insufficient_balance(self):
        conn = MagicMock()
        conn.fetchrow = AsyncMock(side_effect=[
            {"bcoin_balance": 10},
            {"id": 5, "name": "Poción Power", "price": 50},
        ])
        conn.fetchval = AsyncMock(return_value=0)
        with patch.object(database, "pool", self._pool(conn)):
            result = await database.purchase_bicho_item(42, 5, "token-item-2")
        self.assertEqual(result, {
            "ok": False, "reason": "insufficient_balance",
            "balance": 10, "price": 50,
        })
        conn.fetchval.assert_not_awaited()

    async def test_purchase_bicho_item_duplicate_token_does_not_charge(self):
        conn = MagicMock()
        conn.fetchrow = AsyncMock(side_effect=[
            {"bcoin_balance": 200},
            {"id": 5, "name": "Poción Power", "price": 50},
        ])
        conn.fetchval = AsyncMock(return_value=None)
        with patch.object(database, "pool", self._pool(conn)):
            result = await database.purchase_bicho_item(42, 5, "token-item-3")
        self.assertEqual(result, {"ok": False, "reason": "duplicate"})
        self.assertEqual(conn.fetchval.await_count, 1)

    async def test_use_regeneration_restores_three_lives(self):
        conn = MagicMock()
        conn.fetchrow = AsyncMock(side_effect=[
            {"id": 9, "species_key": "Sodax", "nickname": "Chispa", "lives": 1},
            {"name": "Poción Vital", "boost": "regeneracion"},
            {"quantity": 2},
        ])
        conn.fetchval = AsyncMock(side_effect=["use-token", 1, 3])
        conn.execute = AsyncMock()
        with patch.object(database, "pool", self._pool(conn)):
            result = await database.use_bicho_item(42, "Poción Vital", "use-token")
        self.assertEqual(result, {
            "ok": True, "name": "Poción Vital", "effect": "regeneracion",
            "bicho_name": "Chispa", "quantity": 1, "lives": 3,
        })
        conn.execute.assert_not_awaited()

    async def test_use_regeneration_rejects_bicho_with_full_lives(self):
        conn = MagicMock()
        conn.fetchrow = AsyncMock(side_effect=[
            {"id": 9, "species_key": "Sodax", "nickname": None, "lives": 3},
            {"name": "Poción Vital", "boost": "regeneracion"},
            {"quantity": 1},
        ])
        conn.fetchval = AsyncMock()
        with patch.object(database, "pool", self._pool(conn)):
            result = await database.use_bicho_item(42, "Poción Vital", "use-token")
        self.assertEqual(result, {"ok": False, "reason": "full_lives"})
        conn.fetchval.assert_not_awaited()

    async def test_use_item_requires_equipped_bicho(self):
        conn = MagicMock()
        conn.fetchrow = AsyncMock(return_value=None)
        with patch.object(database, "pool", self._pool(conn)):
            result = await database.use_bicho_item(42, "Poción Vital", "use-token")
        self.assertEqual(result, {"ok": False, "reason": "no_equipped"})

    async def test_use_power_applies_three_hour_boost(self):
        expires = datetime.datetime(2026, 11, 14, 21, 0, tzinfo=datetime.timezone.utc)
        conn = MagicMock()
        conn.fetchrow = AsyncMock(side_effect=[
            {"id": 9, "species_key": "Sodax", "nickname": "Chispa", "lives": 3},
            {"name": "Poción Power", "boost": "power"},
            {"quantity": 1},
            {"boost_attack": 5, "boost_defense": 5, "boost_expires_at": expires},
        ])
        conn.fetchval = AsyncMock(return_value="use-token")
        conn.execute = AsyncMock()
        with patch.object(database, "pool", self._pool(conn)):
            result = await database.use_bicho_item(42, "Poción Power", "use-token")
        self.assertEqual(result["effect"], "power")
        self.assertEqual(result["boost_attack"], 5)
        self.assertEqual(result["boost_defense"], 5)
        self.assertEqual(result["boost_expires_at"], expires)
        self.assertEqual(result["quantity"], 0)
        self.assertIn("INTERVAL '3 hours'", conn.fetchrow.await_args_list[-1].args[0])
        conn.execute.assert_awaited_once()

    async def test_use_vitamin_adds_thirty_percent_training_speed_for_one_hour(self):
        expires = datetime.datetime(2026, 11, 14, 22, 0, tzinfo=datetime.timezone.utc)
        conn = MagicMock()
        conn.fetchrow = AsyncMock(side_effect=[
            {"id": 9, "species_key": "Sodax", "nickname": "Chispa", "lives": 3},
            {"name": "Vitamina", "boost": "vitamina"},
            {"quantity": 3},
            {"training_boost_pct": 30, "training_boost_expires_at": expires},
        ])
        conn.fetchval = AsyncMock(side_effect=["use-token", 2])
        with patch.object(database, "pool", self._pool(conn)):
            result = await database.use_bicho_item(42, "Vitamina", "use-token")
        self.assertEqual(result["effect"], "vitamina")
        self.assertEqual(result["training_boost_pct"], 30)
        self.assertEqual(result["training_boost_expires_at"], expires)
        self.assertEqual(result["quantity"], 2)
        self.assertIn("INTERVAL '1 hour'", conn.fetchrow.await_args_list[-1].args[0])

    async def test_use_item_duplicate_token_does_not_consume(self):
        conn = MagicMock()
        conn.fetchrow = AsyncMock(side_effect=[
            {"id": 9, "species_key": "Sodax", "nickname": "Chispa", "lives": 3},
            {"name": "Poción Power", "boost": "power"},
            {"quantity": 2},
        ])
        conn.fetchval = AsyncMock(return_value=None)
        conn.execute = AsyncMock()
        with patch.object(database, "pool", self._pool(conn)):
            result = await database.use_bicho_item(42, "Poción Power", "use-token")
        self.assertEqual(result, {"ok": False, "reason": "duplicate"})
        conn.execute.assert_not_awaited()

    async def test_item_shop_shows_three_items_per_page(self):
        catalog = [
            {"id": i, "name": f"Ítem {i}", "description": f"Descripción {i}",
             "image_url": f"https://example.com/item-{i}.png",
             "boost": "power", "icon": "⚡", "price": i * 10}
            for i in range(1, 8)
        ]
        with patch("modules.bichos.get_bichos_item_catalog",
                   new=AsyncMock(return_value=catalog)), \
             patch("modules.bichos.get_bichos_balance",
                   new=AsyncMock(return_value=300)):
            first = await build_bichos_item_shop(42, 0)
            last = await build_bichos_item_shop(42, 99)
        self.assertIsInstance(first, BichosItemShopView)
        self.assertEqual((first.page, first.pages, first.total), (0, 3, 7))
        for view, expected in ((first, 3), (last, 1)):
            buttons = [button for button in self._buttons(view)
                       if isinstance(button, BuyItemButton)]
            self.assertEqual(len(buttons), expected)
            self.assertEqual([button.label for button in self._buttons(view)[-4:]],
                             ["Anterior", "Siguiente", "Volver", "Inicio"])
        self.assertTrue(self._buttons(first)[-4].disabled)
        self.assertTrue(self._buttons(last)[-3].disabled)

    async def test_inventory_shows_three_items_per_page_with_use_buttons(self):
        items = [
            {"item_key": f"Ítem {i}", "quantity": i, "icon": "💚",
             "description": f"Descripción {i}",
             "image_url": f"https://example.com/item-{i}.png",
             "boost": "regeneracion", "price": i * 10}
            for i in range(1, 8)
        ]
        with patch("modules.bichos.get_bichos_objects",
                   new=AsyncMock(return_value=items)):
            first = await build_bichos_inventory(42, 0)
            middle = await build_bichos_inventory(42, 1)
            last = await build_bichos_inventory(42, 99)
        self.assertIsInstance(first, BichosInventoryView)
        self.assertEqual((first.page, first.pages, first.total), (0, 3, 7))
        for view, expected in ((first, 3), (middle, 3), (last, 1)):
            buttons = [button for button in self._buttons(view)
                       if isinstance(button, UseItemButton)]
            self.assertEqual(len(buttons), expected)
            self.assertEqual([button.label for button in self._buttons(view)[-4:]],
                             ["Anterior", "Siguiente", "Volver", "Inicio"])

    async def test_owned_bicho_shows_active_boost_and_training_speed(self):
        from modules.bichos import build_section
        owned = [{
            "id": 1, "species_key": "Sodax", "nickname": "Chispa", "level": 2,
            "lives": 3, "equipped": True, "image_url": "https://example.com/bicho.png",
            "attack": 14, "defense": 9, "base_attack": 9, "base_defense": 4,
            "active_attack_boost": 5, "active_defense_boost": 5,
            "training_boost_pct": 30,
            "training_boost_expires_at": datetime.datetime(
                2026, 11, 14, 22, 0, tzinfo=datetime.timezone.utc,
            ),
            "boost_expires_at": datetime.datetime(
                2026, 11, 14, 21, 0, tzinfo=datetime.timezone.utc,
            ),
        }]
        with patch("modules.bichos.get_owned_bichos", new=AsyncMock(return_value=owned)):
            view = await build_section(42, "Mis Bichos")
        content = view.to_components()[0]["components"][0]["content"]
        self.assertIn("**14** (+5)", content)
        self.assertIn("**9** (+5)", content)
        self.assertIn("+30%", content)
        self.assertIn("hasta <t:", content)
        self.assertIn("Power activo", content)

    async def test_use_button_prevents_double_consumption(self):
        item = {"item_key": "Poción Vital", "quantity": 1, "icon": "💚",
                "description": "Restaura vidas", "image_url": "",
                "boost": "regeneracion", "price": 50}
        view = BichosInventoryView(42, [item], page=0, pages=1, total=1)
        button = next(button for button in self._buttons(view)
                      if isinstance(button, UseItemButton))
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=42),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
            edit_original_response=AsyncMock(),
        )
        results = [
            {"ok": True, "name": "Poción Vital", "effect": "regeneracion",
             "bicho_name": "Chispa", "quantity": 0, "lives": 3},
            {"ok": False, "reason": "duplicate"},
        ]
        with patch("modules.bichos.use_bicho_item",
                   new=AsyncMock(side_effect=results)) as use, \
             patch("modules.bichos.build_bichos_inventory",
                   new=AsyncMock(return_value=view)):
            await button.callback(interaction)
            await button.callback(interaction)
        self.assertEqual([call.args[2] for call in use.await_args_list],
                         [button.use_token, button.use_token])
        self.assertIn("ya fue procesado", interaction.followup.send.await_args.args[0])

    async def test_add_item_bicho_persists_all_fields(self):
        interaction = SimpleNamespace(
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        boost = SimpleNamespace(name="Power", value="power")
        with patch("modules.bichos.add_bichos_item_catalog",
                   new=AsyncMock(return_value=1)) as add:
            await Bichos.add_item_bicho.callback(
                Bichos(None), interaction,
                "Poción Power", "Sube stats", 50,
                "https://example.com/power.png", boost,
            )
        add.assert_awaited_once_with(
            "Poción Power", "Sube stats", "https://example.com/power.png",
            "power", 50,
        )
        self.assertIn("Poción Power", interaction.followup.send.await_args.args[0])

    async def test_add_item_bicho_accepts_none_effect(self):
        interaction = SimpleNamespace(
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        boost = SimpleNamespace(name="Sin efecto", value="none")
        with patch("modules.bichos.add_bichos_item_catalog",
                   new=AsyncMock(return_value=1)) as add:
            await Bichos.add_item_bicho.callback(
                Bichos(None), interaction,
                "Piedra decorativa", "Ítem sin efecto", 10,
                "https://example.com/piedra.png", boost,
            )
        add.assert_awaited_once_with(
            "Piedra decorativa", "Ítem sin efecto",
            "https://example.com/piedra.png", "none", 10,
        )

    async def test_inventory_hides_use_button_for_none_effect_items(self):
        items = [
            {"item_key": "Piedra decorativa", "quantity": 1, "icon": "🎁",
             "description": "Ítem sin efecto", "image_url": "",
             "boost": "none", "price": 10},
            {"item_key": "Poción Vital", "quantity": 2, "icon": "💚",
             "description": "Restaura vidas", "image_url": "",
             "boost": "regeneracion", "price": 50},
        ]
        with patch("modules.bichos.get_bichos_objects",
                   new=AsyncMock(return_value=items)):
            view = await build_bichos_inventory(42, 0)
        buttons = [button for button in self._buttons(view)
                   if isinstance(button, UseItemButton)]
        self.assertEqual([button.item["item_key"] for button in buttons],
                         ["Poción Vital"])
        texts = [component.get("content") for component
                 in view.to_components()[0]["components"] if component.get("content")]
        self.assertTrue(any("Este ítem no es consumible." in text for text in texts))

    def test_coordinator_commands_registered(self):
        names = {command.name for command in Bichos.__cog_app_commands__}
        self.assertTrue({"bichos", "add_bicho", "add_bichos", "add_item_bicho",
                         "add_items_bichos", "usuarios_bichos"} <= names)


if __name__ == "__main__":
    unittest.main()
