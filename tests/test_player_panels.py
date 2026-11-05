"""Pruebas locales sin conexión a Discord ni a PostgreSQL."""

import ast
import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")
os.environ.setdefault("GUILD_ID", "980073134411644939")

from modules.player_panels import (
    GamePanel, PanelContext, SimplePanel, RouletteNumber, _delete_panel_after,
    JUGAR_THUMBNAIL, CONSULTAR_THUMBNAIL, EMPLEOS_THUMBNAIL,
    RouletteOptions,
)


class PanelTests(unittest.IsolatedAsyncioTestCase):
    def test_layouts_are_components_v2(self):
        for kind in ("trabajar", "consultar"):
            panel = SimplePanel(1, kind)
            self.assertEqual(len(panel.to_components()), 1)
            self.assertEqual(panel.to_components()[0]["type"], 17)
        game = GamePanel(1, 100, None)
        game.build()
        self.assertEqual(game.to_components()[0]["type"], 17)

    def test_jugar_and_consultar_use_their_v2_thumbnails(self):
        game = GamePanel(1, 100, None)
        game.build()
        for panel, expected in (
            (game, JUGAR_THUMBNAIL),
            (SimplePanel(1, "consultar"), CONSULTAR_THUMBNAIL),
        ):
            section = panel.to_components()[0]["components"][0]
            self.assertEqual(section["type"], 9)
            self.assertEqual(section["accessory"]["type"], 11)
            self.assertEqual(section["accessory"]["media"]["url"], expected)

    async def test_panel_autodelete_closes_menu_without_ending_game(self):
        panel = GamePanel(1, 100, None)
        panel.active.add("memo")
        message = SimpleNamespace(delete=AsyncMock())
        await _delete_panel_after(panel, message, 0)
        self.assertTrue(panel.expired)
        self.assertTrue(panel.is_finished())
        self.assertEqual(panel.active, {"memo"})
        message.delete.assert_awaited_once()

        consult = SimplePanel(1, "consultar")
        consult_message = SimpleNamespace(delete=AsyncMock())
        await _delete_panel_after(consult, consult_message, 0)
        self.assertTrue(consult.is_finished())
        consult_message.delete.assert_awaited_once()

        work = SimplePanel(1, "trabajar")
        work_message = SimpleNamespace(delete=AsyncMock())
        await _delete_panel_after(work, work_message, 0)
        self.assertTrue(work.is_finished())
        work_message.delete.assert_awaited_once()

    async def test_work_panel_schedules_private_deletion_at_sixty_seconds(self):
        from modules.player_panels import PlayerPanels

        message = SimpleNamespace(delete=AsyncMock())
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=42),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock(return_value=message)),
        )
        with patch("modules.player_panels.asyncio.create_task") as schedule:
            await PlayerPanels.trabajar.callback(PlayerPanels(None), interaction)
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        kwargs = interaction.followup.send.await_args.kwargs
        self.assertTrue(kwargs["ephemeral"])
        self.assertTrue(kwargs["wait"])
        self.assertEqual(kwargs["view"].kind, "trabajar")
        scheduled = schedule.call_args.args[0]
        self.assertEqual(scheduled.cr_frame.f_locals["delay"], 60)
        scheduled.close()

    async def test_cooldown_disables_game_without_panel_bells(self):
        game = GamePanel(1, 100, None)
        with patch("modules.player_panels._cooldown", new=AsyncMock(return_value=9999999999)):
            await game.refresh()
        container = game.children[0]
        rows = [child for child in container.children if child.__class__.__name__ == "ActionRow"]
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(len(row.children) == 1 for row in rows))
        self.assertTrue(all(row.children[0].disabled for row in rows))
        self.assertFalse(any("🔔" in row.children[0].label for row in rows))

    async def test_finished_game_updates_open_panel_without_polling(self):
        from modules.player_panels import _open_game_panels, notify_game_finished

        panel = GamePanel(42, 100, None)
        panel.active.add("dados")
        panel.message = SimpleNamespace(edit=AsyncMock())
        _open_game_panels.setdefault(42, set()).add(panel)
        try:
            with patch("modules.player_panels._cooldown", new=AsyncMock()) as cooldown:
                await notify_game_finished(42, "dados", 9999999999)
            self.assertNotIn("dados", panel.active)
            self.assertEqual(panel.cooldowns["dados"], 9999999999)
            panel.message.edit.assert_awaited_once_with(view=panel)
            cooldown.assert_not_awaited()
            self.assertFalse(hasattr(panel, "_poll_task"))
        finally:
            _open_game_panels[42].discard(panel)

    async def test_tienda_opens_v2_directly(self):
        from modules.player_commands import PlayerCommands
        from modules.shop import TiendaLayout

        player = PlayerCommands(None)
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=42),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        items = [{"id": 1, "precio": 200, "nombre": "A", "icono": "🔹", "stock": 1},
                 {"id": 2, "precio": 100, "nombre": "B", "icono": "🔹", "stock": 1}]
        with patch("core.database.get_all_items", new=AsyncMock(return_value=items)):
            await PlayerCommands.tienda.callback(player, interaction)
        kwargs = interaction.followup.send.await_args.kwargs
        self.assertIsInstance(kwargs["view"], TiendaLayout)
        self.assertEqual([item["precio"] for item in kwargs["view"].items], [100, 200])
        self.assertTrue(kwargs["ephemeral"])

    def test_number_modal_range(self):
        modal = RouletteNumber(GamePanel(1, 100, None))
        self.assertEqual(modal.title, "Número de ruleta")
        self.assertEqual(modal.number.max_length, 2)

    def test_employment_panel_includes_all_employment_views(self):
        panel = SimplePanel(1, "trabajar")
        header = panel.to_components()[0]["components"][0]
        self.assertEqual(header["type"], 9)
        self.assertEqual(header["accessory"]["media"]["url"], EMPLEOS_THUMBNAIL)
        rows = [child for child in panel.children[0].children
                if child.__class__.__name__ == "ActionRow"]
        self.assertEqual(
            [button.mode for row in rows for button in row.children],
            ["trabajar", "aplicar", "renunciar", "oficina", "empleos", "exp"],
        )
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(len(row.children) == 2 for row in rows))

    def test_consult_panel_uses_two_columns_of_three_buttons(self):
        panel = SimplePanel(1, "consultar")
        rows = [child for child in panel.children[0].children
                if child.__class__.__name__ == "ActionRow"]
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(len(row.children) == 2 for row in rows))
        self.assertEqual(
            [[button.mode for button in row.children] for row in rows],
            [["balance", "inventario"], ["cd", "top"], ["time", "info"]],
        )
        self.assertIn("Panel de consultas Purplejack", str(panel.to_components()))

    async def test_cd_consultation_stays_ephemeral_without_autodelete(self):
        from modules.economy import Economy

        panel = SimplePanel(42, "consultar")
        button = next(child for child in panel.walk_children()
                      if getattr(child, "mode", None) == "cd")
        economy = Economy(None)
        interaction = SimpleNamespace(
            id=123,
            user=SimpleNamespace(id=42),
            guild=SimpleNamespace(id=9),
            client=SimpleNamespace(get_cog=lambda name: economy),
            channel=SimpleNamespace(send=AsyncMock()),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock(return_value=object())),
        )
        with patch("bot.check_player_access", new=AsyncMock(return_value=True)):
            await button.callback(interaction)
        kwargs = interaction.followup.send.await_args.kwargs
        self.assertTrue(kwargs["ephemeral"])
        self.assertNotIn("delete_after", kwargs)
        interaction.channel.send.assert_not_awaited()

    def test_game_panel_has_no_redundant_start_notice(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "modules" / "player_panels.py").read_text(encoding="utf-8")
        self.assertNotIn("Iniciando **", source)

    def test_reminder_prompt_is_v2_with_one_explicit_game(self):
        from core.game_reminders import ReminderPrompt
        prompt = ReminderPrompt(42, "dados", 9999999999)
        self.assertEqual(prompt.to_components()[0]["type"], 17)
        self.assertIn("Dados", str(prompt.to_components()))
        self.assertEqual(prompt.timeout, 15)

    async def test_reminder_confirmation_persists_and_deletes_after_three_seconds(self):
        from core.game_reminders import ReminderPrompt
        prompt = ReminderPrompt(42, "dados", 9999999999)
        button = prompt.children[0].children[1].children[0]
        message = SimpleNamespace(edit=AsyncMock(), delete=AsyncMock())
        prompt.message = message
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=42),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
            message=message,
        )
        with patch("core.game_reminders.database.get_game_cooldown",
                   new=AsyncMock(return_value=9999999999)), \
             patch("core.game_reminders.set_reminder", new=AsyncMock()) as save, \
             patch("core.game_reminders.asyncio.sleep", new=AsyncMock()) as sleep:
            await button.callback(interaction)
        save.assert_awaited_once_with(42, "dados", 9999999999)
        sleep.assert_awaited_once_with(3)
        message.edit.assert_awaited_once()
        message.delete.assert_awaited_once()

    async def test_reminder_prompt_expires_after_fifteen_seconds(self):
        from core.game_reminders import ReminderPrompt, _delete_prompt_after
        prompt = ReminderPrompt(42, "memo", 9999999999)
        prompt.message = SimpleNamespace(delete=AsyncMock())
        with patch("core.game_reminders.asyncio.sleep", new=AsyncMock()) as sleep:
            await _delete_prompt_after(prompt, 15)
        sleep.assert_awaited_once_with(15)
        prompt.message.delete.assert_awaited_once()

    async def test_reminder_offer_is_ephemeral(self):
        from core.game_reminders import send_game_reminder_prompt
        message = SimpleNamespace(edit=AsyncMock(), delete=AsyncMock())
        interaction = SimpleNamespace(
            response=SimpleNamespace(is_done=Mock(return_value=True)),
            followup=SimpleNamespace(send=AsyncMock(return_value=message)),
        )
        with patch("core.game_reminders.reminder_enabled",
                   new=AsyncMock(return_value=False)), \
             patch("core.game_reminders.asyncio.sleep", new=AsyncMock()) as sleep:
            await send_game_reminder_prompt(interaction, 42, "dados", 9999999999)
        sleep.assert_awaited_once_with(2)
        kwargs = interaction.followup.send.await_args.kwargs
        self.assertTrue(kwargs["ephemeral"])
        self.assertTrue(kwargs["wait"])
        prompt = kwargs["view"]
        self.assertIs(prompt.message, message)
        prompt._deletion_task.cancel()
        await asyncio.gather(prompt._deletion_task, return_exceptions=True)

    async def test_every_game_and_work_offer_waits_two_seconds(self):
        from core.game_reminders import send_game_reminder_prompt

        for game in ("ruleta", "dados", "bj", "memo", "trabajar"):
            with self.subTest(game=game):
                events = []
                message = SimpleNamespace(delete=AsyncMock())

                async def pause(seconds):
                    events.append(("wait", seconds))

                async def send(*args, **kwargs):
                    events.append(("send", game))
                    return message

                interaction = SimpleNamespace(
                    response=SimpleNamespace(is_done=Mock(return_value=True)),
                    followup=SimpleNamespace(send=AsyncMock(side_effect=send)),
                )
                with patch("core.game_reminders.reminder_enabled",
                           new=AsyncMock(return_value=False)), \
                     patch("core.game_reminders.asyncio.sleep", new=pause):
                    await send_game_reminder_prompt(
                        interaction, 42, game, 9999999999,
                    )
                self.assertEqual(events, [("wait", 2), ("send", game)])
                prompt = interaction.followup.send.await_args.kwargs["view"]
                prompt._deletion_task.cancel()
                await asyncio.gather(prompt._deletion_task, return_exceptions=True)

    async def test_roulette_options_delete_two_seconds_after_choice(self):
        options = RouletteOptions(GamePanel(42, 100, None))
        options.message = SimpleNamespace(delete=AsyncMock())
        options.close_after_selection()
        self.assertTrue(options.closing)
        self.assertTrue(options.is_finished())
        with patch("modules.player_panels.asyncio.sleep", new=AsyncMock()) as sleep:
            await options._delete_task
        sleep.assert_awaited_once_with(2)
        options.message.delete.assert_awaited_once()

    async def test_roulette_selection_closes_menu_for_color_and_number(self):
        for choice in ("black", "numero"):
            with self.subTest(choice=choice):
                panel = GamePanel(42, 100, None)
                panel.launch = AsyncMock()
                options = RouletteOptions(panel)
                options.message = SimpleNamespace(delete=AsyncMock())
                button = next(button for button in options.children
                              if button.option == choice)
                interaction = SimpleNamespace(
                    response=SimpleNamespace(
                        defer=AsyncMock(), send_modal=AsyncMock(),
                    ),
                )
                with patch("modules.player_panels._owned", new=AsyncMock(return_value=True)), \
                     patch("modules.player_panels.asyncio.sleep", new=AsyncMock()):
                    await button.callback(interaction)
                    await options._delete_task
                options.message.delete.assert_awaited_once()
                if choice == "numero":
                    interaction.response.send_modal.assert_awaited_once()
                    panel.launch.assert_not_awaited()
                else:
                    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
                    panel.launch.assert_awaited_once_with(interaction, "ruleta", choice)

    async def test_finance_exit_deletes_its_own_message(self):
        from modules.economy import FinanceView
        view = FinanceView(42)
        view.message = SimpleNamespace(delete=AsyncMock())
        button = next(child for child in view.children if child.label == "Salir")
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=42),
            response=SimpleNamespace(defer=AsyncMock()),
            delete_original_response=AsyncMock(),
            message=SimpleNamespace(delete=AsyncMock()),
        )
        await button.callback(interaction)
        interaction.response.defer.assert_awaited_once()
        view.message.delete.assert_awaited_once()
        interaction.message.delete.assert_not_awaited()
        interaction.delete_original_response.assert_not_awaited()

    async def test_finance_exit_ignores_already_deleted_message(self):
        import discord
        from modules.economy import FinanceView
        view = FinanceView(42)
        missing = discord.NotFound(
            SimpleNamespace(status=404, reason="Not Found"), "Unknown Message",
        )
        view.message = SimpleNamespace(delete=AsyncMock(side_effect=missing))
        button = next(child for child in view.children if child.label == "Salir")
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=42),
            response=SimpleNamespace(defer=AsyncMock()),
        )
        await button.callback(interaction)
        interaction.response.defer.assert_awaited_once()
        view.message.delete.assert_awaited_once()

    async def test_withdraw_all_edits_saved_ephemeral_panel(self):
        import discord
        from modules.economy import FinanceView

        embed = discord.Embed(title="Finanzas")
        embed.add_field(name="Balance", value="10", inline=True)
        embed.add_field(name="Banco", value="500", inline=True)
        panel_message = SimpleNamespace(embeds=[embed], edit=AsyncMock())
        view = FinanceView(42)
        view.message = panel_message
        button = next(child for child in view.children if child.label == "Retirar")
        click = SimpleNamespace(
            user=SimpleNamespace(id=42),
            message=SimpleNamespace(edit=AsyncMock()),
            response=SimpleNamespace(send_modal=AsyncMock()),
        )
        await button.callback(click)
        modal = click.response.send_modal.await_args.args[0]
        self.assertIs(modal.message, panel_message)
        modal.amount._value = "all"
        submit = SimpleNamespace(
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        with patch("modules.economy.get_user", new=AsyncMock(side_effect=[
            {"balance": 10, "bank": 500}, {"balance": 510, "bank": 0},
        ])), patch("modules.economy.update_bank", new=AsyncMock()) as bank, \
             patch("modules.economy.update_balance", new=AsyncMock()) as balance:
            await modal.on_submit(submit)
        bank.assert_awaited_once_with(42, -500)
        balance.assert_awaited_once_with(42, 500, track_event=False)
        submit.response.defer.assert_awaited_once_with(ephemeral=True)
        panel_message.edit.assert_awaited_once()
        click.message.edit.assert_not_awaited()
        submit.followup.send.assert_not_awaited()

    async def test_deposit_all_edits_saved_ephemeral_panel(self):
        import discord
        from modules.economy import FinanceView

        embed = discord.Embed(title="Finanzas")
        embed.add_field(name="Balance", value="500", inline=True)
        embed.add_field(name="Banco", value="10", inline=True)
        panel_message = SimpleNamespace(embeds=[embed], edit=AsyncMock())
        view = FinanceView(42)
        view.message = panel_message
        button = next(child for child in view.children if child.label == "Depositar")
        click = SimpleNamespace(
            user=SimpleNamespace(id=42),
            message=SimpleNamespace(edit=AsyncMock()),
            response=SimpleNamespace(send_modal=AsyncMock()),
        )
        await button.callback(click)
        modal = click.response.send_modal.await_args.args[0]
        self.assertIs(modal.message, panel_message)
        modal.amount._value = "all"
        submit = SimpleNamespace(
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        with patch("modules.economy.get_user", new=AsyncMock(side_effect=[
            {"balance": 500, "bank": 10}, {"balance": 0, "bank": 510},
        ])), patch("modules.economy.update_bank", new=AsyncMock(return_value=500)) as bank, \
             patch("modules.economy.update_balance", new=AsyncMock()) as balance, \
             patch("modules.economy.cache.mark_dirty"):
            await modal.on_submit(submit)
        balance.assert_awaited_once_with(42, -500, track_event=False)
        bank.assert_awaited_once_with(42, 500, track_event=False)
        submit.response.defer.assert_awaited_once_with(ephemeral=True)
        panel_message.edit.assert_awaited_once()
        click.message.edit.assert_not_awaited()
        submit.followup.send.assert_not_awaited()

    async def test_withdraw_all_confirms_when_panel_was_removed(self):
        import discord
        from modules.economy import WithdrawModal

        embed = discord.Embed(title="Finanzas")
        embed.add_field(name="Balance", value="10", inline=True)
        embed.add_field(name="Banco", value="500", inline=True)
        missing = discord.NotFound(
            SimpleNamespace(status=404, reason="Not Found"), "Unknown Message",
        )
        panel_message = SimpleNamespace(
            embeds=[embed], edit=AsyncMock(side_effect=missing),
        )
        modal = WithdrawModal(42, panel_message)
        modal.amount._value = "all"
        submit = SimpleNamespace(
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        with patch("modules.economy.get_user", new=AsyncMock(side_effect=[
            {"balance": 10, "bank": 500}, {"balance": 510, "bank": 0},
        ])), patch("modules.economy.update_bank", new=AsyncMock()) as bank, \
             patch("modules.economy.update_balance", new=AsyncMock()) as balance:
            await modal.on_submit(submit)
        bank.assert_awaited_once_with(42, -500)
        balance.assert_awaited_once_with(42, 500, track_event=False)
        self.assertIn("Retiro realizado", submit.followup.send.await_args.args[0])

    def test_bot_has_no_callable_text_prefix(self):
        from bot import bot
        self.assertEqual(bot.command_prefix, [])

    async def test_slash_context_uses_first_public_followup(self):
        interaction = SimpleNamespace(
            id=456, user=SimpleNamespace(id=1), guild=object(), client=object(),
            channel=SimpleNamespace(send=AsyncMock(return_value="second")),
            followup=SimpleNamespace(send=AsyncMock(return_value="first")),
        )
        context = PanelContext(interaction, private=False, first_followup=True)
        self.assertEqual(await context.message.reply("one"), "first")
        self.assertEqual(await context.send("two"), "second")
        interaction.followup.send.assert_awaited_once()
        interaction.channel.send.assert_awaited_once()

    async def test_top_context_is_public_and_deleted_after_fifteen_seconds(self):
        interaction = SimpleNamespace(
            id=789, user=SimpleNamespace(id=1), guild=object(), client=object(),
            channel=SimpleNamespace(send=AsyncMock(return_value="top")),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        context = PanelContext(interaction, private=False, auto_delete_after=15)
        self.assertEqual(await context.send(embed=object(), delete_after=60), "top")
        self.assertEqual(interaction.channel.send.await_args.kwargs["delete_after"], 15)
        interaction.followup.send.assert_not_awaited()

    async def test_work_reminder_uses_settled_cooldown(self):
        from modules.Empleos import _ofrecer_recordatorio_trabajo
        from core.game_reminders import _current_cooldown_expiry, ReminderPrompt

        author = SimpleNamespace(id=42, roles=[])
        interaction = object()
        with patch("modules.Empleos.send_game_reminder_prompt", new=AsyncMock()) as send:
            _ofrecer_recordatorio_trabajo(
                interaction, author, {"cooldown_expires_at": 9999999999},
            )
            await asyncio.sleep(0)
        send.assert_awaited_once_with(interaction, 42, "trabajar", 9999999999)
        self.assertIn("volver a trabajar", str(ReminderPrompt(42, "trabajar", 9999999999).to_components()))
        with patch("modules.Empleos.get_empleo_user", new=AsyncMock(return_value={
            "empleo_actual": "piromano", "ultimo_trabajo": 100,
        })):
            self.assertEqual(await _current_cooldown_expiry(42, "trabajar"), 10900)

    async def test_work_reminder_is_offered_to_coordinator_too(self):
        from modules.Empleos import _ofrecer_recordatorio_trabajo
        from core.config import COORDINADOR_ROLE_ID

        author = SimpleNamespace(id=42, roles=[SimpleNamespace(
            id=COORDINADOR_ROLE_ID, name="Coordinador-ES",
        )])
        interaction = object()
        with patch("modules.Empleos.send_game_reminder_prompt", new=AsyncMock()) as send:
            _ofrecer_recordatorio_trabajo(
                interaction, author, {"cooldown_expires_at": 9999999999},
            )
            await asyncio.sleep(0)
        send.assert_awaited_once_with(interaction, 42, "trabajar", 9999999999)

    def test_standalone_commands_and_message_context_registered(self):
        from modules.player_commands import PlayerCommands, robar_mensaje
        from discord import app_commands
        self.assertEqual(
            {command.name for command in PlayerCommands.__cog_app_commands__},
            {"work", "crime", "collect", "tienda", "rob", "carrera", "retar"},
        )
        self.assertIsInstance(robar_mensaje, app_commands.ContextMenu)

    def test_rob_no_longer_uses_top_positions(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "modules" / "rob.py").read_text(encoding="utf-8")
        self.assertNotIn("get_balance_ranking", source)
        self.assertNotIn("_get_top_target", source)

    async def test_message_context_targets_message_author(self):
        from modules.player_commands import robar_mensaje
        target = SimpleNamespace(id=42)
        guild = SimpleNamespace(fetch_member=AsyncMock(return_value=target))
        executor = SimpleNamespace(_execute=AsyncMock())
        interaction = SimpleNamespace(
            guild=guild,
            response=SimpleNamespace(defer=AsyncMock()),
            client=SimpleNamespace(get_cog=lambda name: executor),
        )
        message = SimpleNamespace(author=SimpleNamespace(id=42))
        await robar_mensaje.callback(interaction, message)
        guild.fetch_member.assert_awaited_once_with(42)
        executor._execute.assert_awaited_once_with(
            interaction, "Rob", "rob", target,
        )

    def test_legacy_message_dispatch_disabled(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "bot.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        self.assertFalse(any(isinstance(node, ast.AsyncFunctionDef) and node.name == "on_message"
                             for node in tree.body))
        self.assertNotIn("modules.adivinar", source)
        self.assertIn("intents.message_content = False", source)

    def test_no_prefix_commands_or_event_tables_are_registered(self):
        root = Path(__file__).resolve().parents[1]
        python_sources = [root / "bot.py", *(root / "modules").glob("*.py")]
        for path in python_sources:
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("@commands.command", source, str(path))
            self.assertNotIn("@commands.cooldown", source, str(path))
        database_source = (root / "core" / "database.py").read_text(encoding="utf-8")
        self.assertNotIn("CREATE TABLE IF NOT EXISTS evento_estado", database_source)
        self.assertNotIn("CREATE TABLE IF NOT EXISTS evento_puntos", database_source)

    def test_cache_flush_worker_is_guarded_on_reconnect(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "bot.py").read_text(encoding="utf-8")
        self.assertIn("if cache_flush_task is None or cache_flush_task.done():", source)

    async def test_slash_migration_verifies_guild_before_global_cleanup(self):
        import bot as bot_module

        events = []
        local = [SimpleNamespace(name=name)
                 for name in bot_module._REQUIRED_PLAYER_COMMANDS]
        old_global = [SimpleNamespace(name="comando_viejo")]

        async def sync(*, guild=None):
            events.append("sync_guild" if guild else "sync_global")
            return []

        async def fetch_commands(*, guild=None):
            events.append("fetch_guild" if guild else "fetch_global")
            if guild:
                return local
            return old_global if events.count("fetch_global") == 1 else []

        def clear_commands(*, guild=None):
            events.append("clear_global")

        tree = SimpleNamespace(
            get_commands=lambda: local,
            copy_global_to=lambda **kwargs: events.append("copy_to_guild"),
            sync=sync,
            fetch_commands=fetch_commands,
            clear_commands=clear_commands,
        )
        client = SimpleNamespace(
            tree=tree, user="TestBot", guilds=[SimpleNamespace(id=int(bot_module.GUILD_ID))],
            get_guild=lambda guild_id: object(),
        )
        await bot_module.sync_slash_commands(client)
        self.assertLess(events.index("sync_guild"), events.index("clear_global"))
        self.assertLess(events.index("fetch_guild"), events.index("clear_global"))
        self.assertLess(events.index("clear_global"), events.index("sync_global"))

    async def test_slash_migration_rejects_wrong_guild_without_deleting(self):
        import bot as bot_module
        tree = SimpleNamespace(clear_commands=unittest.mock.Mock(), sync=AsyncMock())
        client = SimpleNamespace(
            tree=tree, user="TestBot", guilds=[], get_guild=lambda guild_id: None,
        )
        with self.assertRaisesRegex(RuntimeError, "no es un servidor conectado"):
            await bot_module.sync_slash_commands(client)
        tree.clear_commands.assert_not_called()
        tree.sync.assert_not_awaited()

    async def test_slash_migration_keeps_globals_if_guild_is_incomplete(self):
        import bot as bot_module
        local = [SimpleNamespace(name=name)
                 for name in bot_module._REQUIRED_PLAYER_COMMANDS]
        tree = SimpleNamespace(
            get_commands=lambda: local,
            copy_global_to=unittest.mock.Mock(),
            sync=AsyncMock(),
            fetch_commands=AsyncMock(return_value=local[:-1]),
            clear_commands=unittest.mock.Mock(),
        )
        client = SimpleNamespace(
            tree=tree, user="TestBot",
            guilds=[SimpleNamespace(id=int(bot_module.GUILD_ID))],
            get_guild=lambda guild_id: object(),
        )
        with self.assertRaisesRegex(RuntimeError, "no registró todos"):
            await bot_module.sync_slash_commands(client)
        tree.clear_commands.assert_not_called()
        self.assertEqual(tree.sync.await_count, 1)

    async def test_startup_diagnostics_report_stage_without_secrets(self):
        import bot as bot_module

        async def healthy():
            return 42

        async def broken():
            raise RuntimeError(f"falló con {bot_module.TOKEN}")

        self.assertEqual(await bot_module._startup_step("prueba correcta", healthy()), 42)
        with self.assertLogs("purplejack", level="ERROR") as captured:
            with self.assertRaises(RuntimeError) as raised:
                await bot_module._startup_step("prueba fallida", broken())
        self.assertIn("[STARTUP:ERROR] prueba fallida", captured.output[0])
        self.assertIn("[REDACTADO]", captured.output[0])
        self.assertNotIn(bot_module.TOKEN, captured.output[0])
        self.assertNotIn(bot_module.TOKEN, str(raised.exception))

    def test_shutdown_signal_does_not_block_startup_when_unavailable(self):
        import bot as bot_module

        supported = SimpleNamespace(add_signal_handler=Mock())
        self.assertTrue(bot_module._register_shutdown_signal(supported))
        supported.add_signal_handler.assert_called_once()

        unsupported = SimpleNamespace(
            add_signal_handler=Mock(side_effect=NotImplementedError),
        )
        with self.assertLogs("purplejack", level="WARNING") as captured:
            self.assertFalse(bot_module._register_shutdown_signal(unsupported))
        self.assertIn("[STARTUP:SIGNAL]", captured.output[0])

    def test_squarecloud_runs_production_entrypoint(self):
        root = Path(__file__).resolve().parents[1]
        config = (root / "squarecloud.app").read_text(encoding="utf-8")
        self.assertIn("MAIN=main.py", config.splitlines())
        source = (root / "main.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        self.assertTrue(any(
            isinstance(node, ast.ImportFrom)
            and node.module == "bot"
            and any(alias.name == "run_bot" for alias in node.names)
            for node in tree.body
        ))
        self.assertTrue(any(
            isinstance(node, ast.If)
            and any(isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Name)
                    and child.func.id == "run_bot"
                    for child in ast.walk(node))
            for node in tree.body
        ))

    def test_help_lists_only_available_slash_entry_points(self):
        from modules.economy import (
            NAVE_COMMAND_PAGES, NAVE_INFO_DESCRIPTION,
            NaveHelpView, _build_nave_commands_embed,
        )
        self.assertEqual(len(NAVE_COMMAND_PAGES), 3)
        self.assertIn("/consultar", NAVE_INFO_DESCRIPTION)
        for page in range(len(NAVE_COMMAND_PAGES)):
            embed = _build_nave_commands_embed(page)
            self.assertLessEqual(len(embed.fields[0].value), 1024)
            self.assertNotIn("!", embed.fields[0].value)
            self.assertNotIn("Prefijo", embed.footer.text)
            self.assertLessEqual(len(embed), 6000)
        self.assertEqual(len(NaveHelpView(1).children), 4)

    async def test_roulette_cooldown_starts_after_settlement(self):
        from modules.roulette import Roulette

        events = []
        async def settle(*args):
            events.append("settle")
            return {"ok": True}
        async def cooldown(*args):
            events.append("cooldown")
        async def send(*args, **kwargs):
            events.append("message")

        context = SimpleNamespace(
            author=SimpleNamespace(id=1, nick="Tester", display_name="Tester",
                                   mention="<@1>"),
            message=SimpleNamespace(id=123),
            channel=SimpleNamespace(),
            interaction=SimpleNamespace(),
            send=send,
        )
        with patch("modules.roulette.cache.get_game_cooldown_cache", return_value=0), \
             patch("modules.roulette.get_game_cooldown", new=AsyncMock(return_value=0)), \
             patch("modules.roulette.get_user", new=AsyncMock(return_value={"balance": 1000})), \
             patch("modules.roulette.reserve_wager", new=AsyncMock(return_value={"ok": True, "id": "w"})), \
             patch("modules.roulette.settle_wager", new=settle), \
             patch("modules.roulette.set_game_cooldown", new=cooldown), \
             patch("modules.roulette.cache.set_game_cooldown_cache"), \
             patch("modules.roulette.asyncio.sleep", new=AsyncMock()), \
             patch("modules.roulette.random.choice", return_value="1"), \
             patch("modules.roulette.send_game_reminder_prompt",
                   new=AsyncMock(side_effect=lambda *args: events.append("prompt"))):
            await Roulette.ruleta(Roulette(None), context, 100, "red")
        self.assertEqual(events, ["message", "settle", "cooldown", "message", "prompt"])


if __name__ == "__main__":
    unittest.main()
