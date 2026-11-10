import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")

from core import cache
from core.rankings import get_balance_ranking
from modules.staff import EconomiaView, ExperienciaLaboralPaginationView


class RankingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        cache.restore_balance_index([])
        cache.restore_member_names([])

    async def test_balance_ranking_uses_live_ram_without_discord(self):
        cache.restore_balance_index([
            {"id": 11, "balance": 100},
            {"id": 12, "balance": 200},
        ])
        self.assertEqual(get_balance_ranking(2), [(12, 200), (11, 100)])
        cache._set_balance_index_value(11, 300)
        self.assertEqual(get_balance_ranking(2), [(11, 300), (12, 200)])


    async def test_labor_ranking_uses_saved_names_or_ids_without_http(self):
        cache.set_member_name(11, "Piloto")
        guild = MagicMock()
        guild.fetch_member = AsyncMock()
        view = ExperienciaLaboralPaginationView(7, guild, [
            {"user_id": 11, "empleo_actual": "limpiador", "exp_laboral": 10},
            {"user_id": 12, "empleo_actual": "ingeniero", "exp_laboral": 20},
        ])
        embed = await view.get_embed()
        self.assertIn("Piloto", embed.description)
        self.assertIn("ID 12", embed.description)
        guild.fetch_member.assert_not_awaited()

    async def test_economy_inventory_export_uses_no_member_http(self):
        cache.set_member_name(11, "Piloto")
        interaction = MagicMock()
        interaction.user.id = 7
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()
        interaction.guild.fetch_member = AsyncMock()
        view = EconomiaView(7)
        rows = [
            {"user_id": 11, "nombre": "Casco", "cantidad": 1},
            {"user_id": 12, "nombre": "Escudo", "cantidad": 2},
        ]
        with patch("modules.staff.get_all_inventarios", new=AsyncMock(return_value=rows)):
            await view.inventarios.callback(interaction)
        interaction.guild.fetch_member.assert_not_awaited()
        self.assertTrue(interaction.followup.send.await_args.kwargs["ephemeral"])

    async def test_profile_cache_is_purged(self):
        cache.set_member_name(11, "Piloto")
        cache.purge_user_cache(11)
        self.assertIsNone(cache.get_member_name(11))
