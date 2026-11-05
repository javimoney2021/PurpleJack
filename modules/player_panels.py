"""Puertas de entrada privadas V2; los tableros de juego siguen siendo públicos."""

import asyncio
from datetime import datetime, timezone
import logging
import time
from types import SimpleNamespace

import discord
from discord import app_commands
from discord.ext import commands

from core.database import get_command_cooldown, get_game_cooldown

logger = logging.getLogger(__name__)

GAMES = {
    "ruleta": ("🎰 Ruleta", "Roulette", "ruleta"),
    "dados": ("🎲 Dados", "Dados", "dados"),
    "bj": ("🃏 Blackjack", "Blackjack", "blackjack"),
    "memo": ("🧠 Memo", "Memo", "memo"),
}
_open_game_panels: dict[int, set["GamePanel"]] = {}


def _forget_game_panel(panel: "GamePanel") -> None:
    panels = _open_game_panels.get(panel.owner_id)
    if panels is not None:
        panels.discard(panel)
        if not panels:
            _open_game_panels.pop(panel.owner_id, None)
JUGAR_THUMBNAIL = (
    "https://pub-a09b3609b6b34dfab5c7aa7742cd1a8a.r2.dev/"
    "Purple%20jack%20Harcode/jugar.png"
)
CONSULTAR_THUMBNAIL = (
    "https://pub-a09b3609b6b34dfab5c7aa7742cd1a8a.r2.dev/"
    "Purple%20jack%20Harcode/Consultar.png"
)
EMPLEOS_THUMBNAIL = (
    "https://pub-a09b3609b6b34dfab5c7aa7742cd1a8a.r2.dev/"
    "Purple%20jack%20Harcode/EmpleosPJ.png"
)
PANEL_LIFETIME_SECONDS = 60
ROULETTE_OPTIONS_DELETE_DELAY = 2


async def _delete_panel_after(view: discord.ui.LayoutView, message, delay: int) -> None:
    """Cierra solo el menú; las partidas lanzadas continúan independientes."""
    await asyncio.sleep(delay)
    view.stop()
    if isinstance(view, GamePanel):
        view.expired = True
        _forget_game_panel(view)
    try:
        await message.delete()
    except (discord.HTTPException, discord.NotFound):
        pass


async def _delete_roulette_options_after(view: "RouletteOptions") -> None:
    await asyncio.sleep(ROULETTE_OPTIONS_DELETE_DELAY)
    if view.message is not None:
        try:
            await view.message.delete()
        except (discord.HTTPException, discord.NotFound):
            pass


async def _cooldown(user_id: int, game: str) -> float:
    if game == "memo":
        return await get_command_cooldown("user", user_id, "memo")
    return await get_game_cooldown(user_id, game)


class _SourceMessage:
    def __init__(self, context, message_id: int):
        self.context = context
        self.id = message_id
        self.reference = None
        self.created_at = datetime.now(timezone.utc)

    async def reply(self, *args, **kwargs):
        return await self.context.reply(*args, **kwargs)

    async def delete(self):
        pass  # Una invocación slash no crea mensaje de usuario que borrar.


class PanelContext:
    """Compatibilidad transitoria con callbacks existentes, sin procesar mensajes !."""

    def __init__(self, interaction: discord.Interaction, *, private: bool,
                 first_followup: bool = False, auto_delete_after: int | None = None):
        self.interaction = interaction
        self.author = interaction.user
        self.guild = interaction.guild
        self.channel = interaction.channel
        self.bot = interaction.client
        self.private = private
        self.first_followup = first_followup
        self.auto_delete_after = auto_delete_after
        self.replied = False
        self.board_created = False
        self.message = _SourceMessage(self, interaction.id)
        self.command = SimpleNamespace(reset_cooldown=lambda _ctx: None)

    async def send(self, *args, **kwargs):
        kwargs.pop("mention_author", None)
        kwargs.pop("delete_after", None)
        if self.private:
            self.replied = True
            return await self.interaction.followup.send(
                *args, ephemeral=True, wait=True, **kwargs,
            )
        if self.auto_delete_after is not None:
            kwargs["delete_after"] = self.auto_delete_after
        if self.first_followup and not self.replied:
            self.replied = True
            result = await self.interaction.followup.send(
                *args, ephemeral=False, wait=True, **kwargs,
            )
            if kwargs.get("view") is not None:
                self.board_created = True
            return result
        result = await self.channel.send(*args, **kwargs)
        self.replied = True
        if kwargs.get("view") is not None:
            self.board_created = True
        return result

    async def reply(self, *args, **kwargs):
        return await self.send(*args, **kwargs)


async def _owned(view, interaction: discord.Interaction) -> bool:
    if interaction.user.id != view.owner_id:
        await interaction.response.send_message("Este panel no es tuyo.", ephemeral=True)
        return False
    from bot import check_player_access
    return await check_player_access(interaction)


class GameButton(discord.ui.Button):
    def __init__(self, game: str, disabled: bool):
        super().__init__(label=GAMES[game][0], style=discord.ButtonStyle.primary,
                         disabled=disabled)
        self.game = game

    async def callback(self, interaction: discord.Interaction):
        view: GamePanel = self.view
        if not await _owned(view, interaction):
            return
        if self.game == "ruleta":
            await interaction.response.defer(ephemeral=True)
            options = RouletteOptions(view)
            options.message = await interaction.followup.send(
                "🎰 Escoge una opción para la ruleta:",
                view=options, ephemeral=True, wait=True,
            )
        else:
            await interaction.response.defer(ephemeral=True)
            await view.launch(interaction, self.game)


class GamePanel(discord.ui.LayoutView):
    def __init__(self, owner_id: int, bet: int, source: discord.Interaction):
        super().__init__(timeout=900)
        self.owner_id = owner_id
        self.bet = bet
        self.source = source
        self.message: discord.WebhookMessage | None = None
        self.active: set[str] = set()
        self.active_since: dict[str, float] = {}
        self.cooldowns: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self.expired = False

    async def refresh(self):
        for game in GAMES:
            expiry = await _cooldown(self.owner_id, game)
            self.cooldowns[game] = expiry
        self.build()

    def build(self):
        self.clear_items()
        container = discord.ui.Container(accent_color=0x7B42D6)
        container.add_item(discord.ui.Section(
            discord.ui.TextDisplay(
                f"## 🎮 Juegos PurpleJack\nApuesta seleccionada: **{self.bet}** PurpleCoin. "
                "Escoge un juego; su tablero se publicará en el canal."
            ),
            accessory=discord.ui.Thumbnail(
                JUGAR_THUMBNAIL, description="Mando de juegos PurpleJack",
            ),
        ))
        container.add_item(discord.ui.Separator())
        for game in GAMES:
            expiry = self.cooldowns.get(game, 0)
            row = discord.ui.ActionRow()
            row.add_item(GameButton(game, game in self.active or expiry > time.time()))
            container.add_item(row)
        self.add_item(container)

    async def launch(self, interaction: discord.Interaction, game: str, option: str | None = None):
        if self.expired:
            await interaction.followup.send("Este panel ya venció. Abre /jugar nuevamente.", ephemeral=True)
            return
        async with self._lock:
            expiry = await _cooldown(self.owner_id, game)
            if game in self.active or expiry > time.time():
                await interaction.followup.send(
                    f"⏳ {GAMES[game][0]} estará disponible <t:{int(expiry)}:R>."
                    if expiry > time.time() else "Ya tienes ese juego en curso.", ephemeral=True,
                )
                return
            self.active.add(game)
            self.active_since[game] = time.monotonic()
            self.build()
            try:
                if self.message is not None:
                    await self.message.edit(view=self)
            except discord.HTTPException:
                logger.warning("No se pudo actualizar el panel de juego de %s", self.owner_id)
            cog = interaction.client.get_cog(GAMES[game][1])
            if cog is None:
                self.active.discard(game)
                await interaction.followup.send("Juego no disponible.", ephemeral=True)
                return
            ctx = PanelContext(interaction, private=False)
            failed = False
            try:
                command = getattr(cog, GAMES[game][2])
                if game == "ruleta":
                    await command(ctx, self.bet, option)
                else:
                    await command(ctx, self.bet)
            except Exception:
                failed = True
                logger.exception("Falló el inicio de %s desde /jugar", game)
                await interaction.followup.send(
                    "No pude iniciar el juego. Revisa el registro antes de intentarlo de nuevo.",
                    ephemeral=True,
                )
            finally:
                if failed or (not ctx.board_created and game != "ruleta") or (
                    game == "ruleta" and await _cooldown(self.owner_id, game) <= time.time()
                ):
                    self.active.discard(game)
                    self.active_since.pop(game, None)
                    await self.refresh()
                    if self.message is not None:
                        try:
                            await self.message.edit(view=self)
                        except discord.HTTPException:
                            pass
    async def on_timeout(self):
        _forget_game_panel(self)


async def notify_game_finished(user_id: int, game: str, expires_at: float) -> None:
    """Actualiza los paneles abiertos al liquidar un juego, sin sondeo a Aiven."""
    for panel in tuple(_open_game_panels.get(user_id, ())):
        if panel.expired or game not in GAMES:
            continue
        panel.active.discard(game)
        panel.active_since.pop(game, None)
        panel.cooldowns[game] = expires_at
        panel.build()
        if panel.message is not None:
            try:
                await panel.message.edit(view=panel)
            except discord.HTTPException:
                logger.warning("No se pudo actualizar /jugar de %s tras %s", user_id, game)


class RouletteNumber(discord.ui.Modal, title="Número de ruleta"):
    number = discord.ui.TextInput(label="Número del 0 al 36", max_length=2)

    def __init__(self, panel: GamePanel):
        super().__init__()
        self.panel = panel

    async def on_submit(self, interaction: discord.Interaction):
        if not await _owned(self.panel, interaction):
            return
        if not self.number.value.isdigit() or not 0 <= int(self.number.value) <= 36:
            await interaction.response.send_message("Elige un número del 0 al 36.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await self.panel.launch(interaction, "ruleta", str(int(self.number.value)))
        finally:
            # El modal difiere una respuesta privada: elimina el indicador de
            # espera sin publicar otro mensaje de "iniciando".
            try:
                await interaction.delete_original_response()
            except (discord.HTTPException, discord.NotFound):
                pass


class RouletteOptionButton(discord.ui.Button):
    def __init__(self, option: str):
        super().__init__(label=option.title() if option != "numero" else "Número",
                         style=discord.ButtonStyle.secondary)
        self.option = option

    async def callback(self, interaction: discord.Interaction):
        options: RouletteOptions = self.view
        panel = options.panel
        if not await _owned(panel, interaction):
            return
        if options.closing:
            await interaction.response.send_message(
                "Esta selección ya fue procesada.", ephemeral=True,
            )
            return
        if self.option == "numero":
            await interaction.response.send_modal(RouletteNumber(panel))
            options.close_after_selection()
            return
        await interaction.response.defer(ephemeral=True)
        options.close_after_selection()
        await panel.launch(interaction, "ruleta", self.option)


class RouletteOptions(discord.ui.View):
    def __init__(self, panel: GamePanel):
        super().__init__(timeout=120)
        self.panel = panel
        self.message: discord.WebhookMessage | None = None
        self.closing = False
        self._delete_task: asyncio.Task | None = None
        for option in ("black", "red", "par", "impar", "numero"):
            self.add_item(RouletteOptionButton(option))

    def close_after_selection(self) -> None:
        if self.closing:
            return
        self.closing = True
        self.stop()
        self._delete_task = asyncio.create_task(_delete_roulette_options_after(self))


class SimplePanelButton(discord.ui.Button):
    def __init__(self, label: str, emoji: str, mode: str):
        super().__init__(label=label, emoji=emoji, style=discord.ButtonStyle.secondary)
        self.mode = mode

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if not await _owned(view, interaction):
            return
        if self.mode in {"aplicar", "info"}:
            await interaction.response.send_modal(NameModal(self.mode))
            return
        await interaction.response.defer(ephemeral=True)
        if self.mode == "inventario":
            from core.database import get_inventory
            from modules.shop import InventarioLayout
            items = await get_inventory(interaction.user.id)
            if not items:
                await interaction.followup.send("🎒 Tu inventario está vacío.", ephemeral=True)
            else:
                inventory = InventarioLayout(
                    items, interaction.user.id, interaction.guild, interaction.client,
                )
                inventory.message = await interaction.followup.send(
                    view=inventory, ephemeral=True, wait=True,
                )
            return
        cog_name, command_name = {
            "trabajar": ("Empleos", "trabajar"),
            "renunciar": ("Empleos", "renunciar"),
            "oficina": ("Empleos", "oficina"),
            "empleos": ("Empleos", "empleos"),
            "exp": ("Empleos", "exp"),
            "balance": ("Economy", "balance"),
            "cd": ("Economy", "cooldowns"),
            "top": ("Economy", "top"),
            "time": ("Shop", "tiempo_restante"),
        }[self.mode]
        cog = interaction.client.get_cog(cog_name)
        if cog is None:
            await interaction.followup.send("Función no disponible.", ephemeral=True)
            return
        ctx = PanelContext(
            interaction,
            private=self.mode not in {"trabajar", "top"},
            auto_delete_after=15 if self.mode == "top" else None,
        )
        try:
            command = getattr(cog, command_name)
            await command(ctx)
        except Exception:
            logger.exception("Falló /%s: %s", view.kind, self.mode)
            await interaction.followup.send("No pude completar esta acción.", ephemeral=True)


class NameModal(discord.ui.Modal):
    name = discord.ui.TextInput(label="Nombre", max_length=100)

    def __init__(self, mode: str):
        super().__init__(title="Aplicar a empleo" if mode == "aplicar" else "Consultar artículo")
        self.mode = mode

    async def on_submit(self, interaction: discord.Interaction):
        from bot import check_player_access
        if not await check_player_access(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        cog_name = "Empleos" if self.mode == "aplicar" else "Shop"
        method = "aplicar" if self.mode == "aplicar" else "info"
        cog = interaction.client.get_cog(cog_name)
        ctx = PanelContext(interaction, private=True)
        try:
            command = getattr(cog, method)
            if self.mode == "info":
                await command(ctx, nombre=self.name.value)
            else:
                await command(ctx, self.name.value)
        except Exception:
            logger.exception("Falló el modal %s", self.mode)
            await interaction.followup.send("No pude completar la consulta.", ephemeral=True)


class SimplePanel(discord.ui.LayoutView):
    def __init__(self, owner_id: int, kind: str):
        super().__init__(timeout=900)
        self.owner_id = owner_id
        self.kind = kind
        container = discord.ui.Container(accent_color=0x7B42D6)
        if kind == "trabajar":
            container.add_item(discord.ui.Section(
                discord.ui.TextDisplay(
                    "## 💼 Centro laboral\nEl tablero de la jornada se abrirá en el canal. "
                    "Para aplicar: Limpiador, Ingeniero o Plomero. "
                    "Los empleos de maestría se gestionan desde Oficina."
                ),
                accessory=discord.ui.Thumbnail(
                    EMPLEOS_THUMBNAIL, description="Empleos PurpleJack",
                ),
            ))
            buttons = (("Trabajar", "🛠️", "trabajar"),
                       ("Aplicar", "📋", "aplicar"),
                       ("Renunciar", "🚪", "renunciar"),
                       ("Oficina", "🏢", "oficina"),
                       ("Empleos", "💼", "empleos"),
                       ("XP laboral", "📈", "exp"))
        else:
            container.add_item(discord.ui.Section(
                discord.ui.TextDisplay(
                    "## 🛰️ Consultar\nPanel de consultas Purplejack"
                ),
                accessory=discord.ui.Thumbnail(
                    CONSULTAR_THUMBNAIL, description="Consultas Purplejack",
                ),
            ))
            buttons = (("Balance", "💰", "balance"),
                       ("Inventario", "🎒", "inventario"),
                       ("CD", "⏳", "cd"),
                       ("Top", "🏆", "top"),
                       ("Time", "🕒", "time"),
                       ("Info", "🔎", "info"))
        row = None
        for index, (label, emoji, mode) in enumerate(buttons):
            if index % 2 == 0:
                row = discord.ui.ActionRow()
                container.add_item(row)
            row.add_item(SimplePanelButton(label, emoji, mode))
        self.add_item(container)


class PlayerPanels(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="jugar", description="Abre el panel de juegos con una apuesta común")
    @app_commands.describe(apuesta="Apuesta en PurpleCoin (mínimo 100)")
    async def jugar(self, interaction: discord.Interaction, apuesta: int):
        if apuesta < 100:
            await interaction.response.send_message(
                "Apuesta minima es 100 PurpleCoin no querras vivir de migajas?",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        panel = GamePanel(interaction.user.id, apuesta, interaction)
        await panel.refresh()
        panel.message = await interaction.followup.send(
            view=panel, ephemeral=True, wait=True,
        )
        _open_game_panels.setdefault(interaction.user.id, set()).add(panel)
        asyncio.create_task(_delete_panel_after(
            panel, panel.message, PANEL_LIFETIME_SECONDS,
        ))

    @app_commands.command(name="trabajar", description="Abre el centro de empleos")
    async def trabajar(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        panel = SimplePanel(interaction.user.id, "trabajar")
        message = await interaction.followup.send(
            view=panel, ephemeral=True, wait=True,
        )
        asyncio.create_task(_delete_panel_after(
            panel, message, PANEL_LIFETIME_SECONDS,
        ))

    @app_commands.command(name="consultar", description="Consulta tus datos y recursos")
    async def consultar(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        panel = SimplePanel(interaction.user.id, "consultar")
        message = await interaction.followup.send(
            view=panel, ephemeral=True, wait=True,
        )
        asyncio.create_task(_delete_panel_after(
            panel, message, PANEL_LIFETIME_SECONDS,
        ))


async def setup(bot):
    await bot.add_cog(PlayerPanels(bot))
