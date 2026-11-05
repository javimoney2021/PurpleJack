import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import discord

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")
os.environ.setdefault("GUILD_ID", "980073134411644939")

import bot
from modules.staff import Staff


class SlashAccessTests(unittest.IsolatedAsyncioTestCase):
    def _interaction(self, name="jugar"):
        return SimpleNamespace(
            type=discord.InteractionType.application_command,
            data={"name": name},
            guild=SimpleNamespace(id=980073134411644939),
            guild_id=980073134411644939,
            user=SimpleNamespace(
                id=42,
                joined_at=datetime.now(timezone.utc),
                roles=[SimpleNamespace(id=bot.PLAYER_COMMAND_ROLE_ID)],
            ),
            response=SimpleNamespace(send_message=AsyncMock()),
        )

    async def test_new_member_with_role_can_use_slash(self):
        interaction = self._interaction()
        with patch.object(bot.cache, "get_active_cargo_expiry", return_value=None):
            self.assertTrue(await bot.check_player_access(interaction))
        interaction.response.send_message.assert_not_awaited()

    async def test_punishment_blocks_staff_and_player_commands(self):
        with patch.object(bot.cache, "get_active_cargo_expiry", return_value=9999999999):
            for command_name in ("jugar", "tienda", "punish"):
                interaction = self._interaction(command_name)
                self.assertFalse(await bot.bot.tree.interaction_check(interaction))
                self.assertIn("Restringido", interaction.response.send_message.await_args.args[0])
                self.assertTrue(interaction.response.send_message.await_args.kwargs["ephemeral"])

    async def test_privacy_request_remains_accessible_during_punishment(self):
        interaction = self._interaction("eliminar_mis_datos")
        with patch.object(bot.cache, "get_active_cargo_expiry", return_value=9999999999):
            self.assertTrue(await bot.bot.tree.interaction_check(interaction))

    async def test_punished_autocomplete_responds_with_empty_choices(self):
        interaction = self._interaction("editar_item")
        interaction.type = discord.InteractionType.autocomplete
        interaction.response.autocomplete = AsyncMock()
        with patch.object(bot.cache, "get_active_cargo_expiry", return_value=9999999999):
            self.assertFalse(await bot.bot.tree.interaction_check(interaction))
        interaction.response.autocomplete.assert_awaited_once_with([])
        interaction.response.send_message.assert_not_awaited()

    def test_punish_accepts_only_minutes_and_hours(self):
        self.assertEqual(Staff.parse_punishment_duration("15m"), 900)
        self.assertEqual(Staff.parse_punishment_duration("2h"), 7200)
        with self.assertRaises(ValueError):
            Staff.parse_punishment_duration("15d")
