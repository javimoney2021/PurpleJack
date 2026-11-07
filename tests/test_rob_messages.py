import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import discord

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")

from modules.rob import Rob


class RobMessageTests(unittest.IsolatedAsyncioTestCase):
    async def _run_rob(self, *, success: bool, saboteador: bool):
        author = SimpleNamespace(
            id=1,
            mention="<@1>",
            roles=[SimpleNamespace(id=77)] if saboteador else [],
        )
        target = MagicMock(spec=discord.Member)
        target.id = 2
        target.mention = "<@2>"
        target.roles = [SimpleNamespace(id=99)] if saboteador else []
        target.display_name = "Objetivo"
        ctx = SimpleNamespace(
            author=author,
            message=SimpleNamespace(reply=AsyncMock(return_value=object())),
            send=AsyncMock(return_value=object()),
        )
        veterano_config = {99: {"monto": 100, "msj": "Protegido"}} if saboteador else {}
        with patch("modules.rob.get_rob_cooldown", return_value=0), \
             patch("modules.rob.get_command_cooldown", new=AsyncMock(return_value=0)), \
             patch("modules.rob.get_rob_victim_protection", new=AsyncMock(return_value=0)), \
             patch("modules.rob.get_user", new=AsyncMock(side_effect=[
                 {"balance": 5000}, {"balance": 10000},
             ])), \
             patch("modules.rob.cache.get_veterano_config", return_value=veterano_config), \
             patch("modules.rob.cache.get_saboteador_role_ids", return_value={77}), \
             patch("modules.rob.random.random", return_value=0.0 if success else 0.99), \
             patch("modules.rob.transfer_balance", new=AsyncMock(return_value={"ok": True})) as transfer, \
             patch("modules.rob.update_balance", new=AsyncMock()) as penalize, \
             patch.object(Rob, "_set_rob_cooldown", new=AsyncMock(return_value=None)), \
             patch("modules.rob.send_cd_boost_notice", new=AsyncMock()):
            await Rob(None).rob(ctx, target)
        return ctx.message.reply.await_args.args[0], transfer, penalize

    async def test_normal_success_names_robber_target_and_amount(self):
        message, transfer, penalize = await self._run_rob(success=True, saboteador=False)
        self.assertIn("<@1>", message)
        self.assertIn("<@2>", message)
        self.assertIn("1,500", message)
        self.assertIn("Huye antes de que llegue la policía", message)
        transfer.assert_awaited_once()
        penalize.assert_not_awaited()

    async def test_normal_failure_names_robber_target_and_amount(self):
        message, transfer, penalize = await self._run_rob(success=False, saboteador=False)
        self.assertIn("<@1>", message)
        self.assertIn("<@2>", message)
        self.assertIn("800", message)
        self.assertIn("Robo fallido 🤡", message)
        self.assertIn("sheriff", message)
        transfer.assert_not_awaited()
        penalize.assert_awaited_once_with(1, -800)

    async def test_saboteador_success_identifies_both_players(self):
        message, transfer, penalize = await self._run_rob(success=True, saboteador=True)
        self.assertIn("<@1>", message)
        self.assertIn("<@2>", message)
        self.assertIn("2,000", message)
        self.assertIn("Veterano", message)
        transfer.assert_awaited_once()
        penalize.assert_not_awaited()

    async def test_saboteador_failure_identifies_both_players(self):
        message, transfer, penalize = await self._run_rob(success=False, saboteador=True)
        self.assertIn("<@1>", message)
        self.assertIn("<@2>", message)
        self.assertIn("1,500", message)
        self.assertIn("Robo fallido 🤡", message)
        self.assertIn("Veterano", message)
        transfer.assert_not_awaited()
        penalize.assert_awaited_once_with(1, -1500)


if __name__ == "__main__":
    unittest.main()
