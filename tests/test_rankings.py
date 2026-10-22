import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from core import cache
from core.rankings import get_balance_ranking
from modules.rob import Rob
from modules.staff import CandyRankingView


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

    async def test_rob_validates_only_selected_rank_position(self):
        cache.restore_balance_index([
            {"id": 11, "balance": 100},
            {"id": 12, "balance": 200},
        ])
        ctx = MagicMock()
        ctx.guild.fetch_member = AsyncMock(return_value=MagicMock(id=11))
        target = await Rob(MagicMock())._get_top_target(ctx, 2)
        self.assertEqual(target.id, 11)
        ctx.guild.fetch_member.assert_awaited_once_with(11)

    async def test_candy_name_uses_snapshot_without_http(self):
        cache.set_member_name(11, "Piloto")
        view = CandyRankingView(7)
        with patch("modules.staff.get_candy_ranking_page", new=AsyncMock(
            return_value=([{"user_id": 11, "balance": 5}], 1)
        )):
            embed = await view.get_embed()
        self.assertIn("Piloto", embed.description)
        self.assertEqual(view._display_name(12), "<@12>")

    async def test_profile_cache_is_purged(self):
        cache.set_member_name(11, "Piloto")
        cache.purge_user_cache(11)
        self.assertIsNone(cache.get_member_name(11))
