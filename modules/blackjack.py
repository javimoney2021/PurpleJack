import asyncio
import logging
import random
import time

import discord
from discord.ext import commands

from core import cache
from core.config import COIN, blackjack_config
from core.database import (
    refund_wager,
    reserve_wager,
    set_game_cooldown,
    settle_wager,
)

logger = logging.getLogger(__name__)

SUITS = ("♠️", "♥️", "♦️", "♣️")
RANKS = ("A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K")
CARD_BACK = "🂠"
GAME_TIMEOUT = 120
WAGER_EXPIRY = 300

_ACTIVE_BLACKJACK: set[int] = set()
_BLACKJACK_START_LOCKS: dict[int, asyncio.Lock] = {}
_SEEN_BLACKJACK_MESSAGES: dict[int, float] = {}


def build_deck() -> list[tuple[str, str]]:
    deck = [(rank, suit) for suit in SUITS for rank in RANKS]
    random.shuffle(deck)
    return deck


def hand_value(hand: list[tuple[str, str]]) -> int:
    value = 0
    aces = 0
    for rank, _ in hand:
        if rank == "A":
            value += 11
            aces += 1
        elif rank in {"J", "Q", "K"}:
            value += 10
        else:
            value += int(rank)
    while value > 21 and aces:
        value -= 10
        aces -= 1
    return value


def format_hand(hand: list[tuple[str, str]], *, hide_second: bool = False) -> str:
    if not hand:
        return "—"
    cards = []
    for index, (rank, suit) in enumerate(hand):
        cards.append(CARD_BACK if hide_second and index == 1 else f"`{rank}{suit}`")
    return "  ".join(cards)


def format_cooldown(seconds: int) -> str:
    if seconds >= 3600:
        hours, rest = divmod(seconds, 3600)
        minutes = rest // 60
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    if seconds >= 60:
        minutes, rest = divmod(seconds, 60)
        return f"{minutes}m {rest}s" if rest else f"{minutes}m"
    return f"{seconds}s"


class BlackjackGame:
    def __init__(
        self,
        user_id: int,
        display_name: str,
        amount: int,
        wager_id: str,
    ):
        self.user_id = user_id
        self.display_name = display_name
        self.amount = amount
        self.wager_id = wager_id
        self.deck = build_deck()
        self.player: list[tuple[str, str]] = []
        self.dealer: list[tuple[str, str]] = []
        self.cooldown = int(blackjack_config["cooldown"])
        self.message: discord.Message | None = None
        self.finished = False
        self.processing = False
        self.action_lock = asyncio.Lock()

        self.player.append(self.draw())
        self.dealer.append(self.draw())
        self.player.append(self.draw())
        self.dealer.append(self.draw())

    def draw(self) -> tuple[str, str]:
        return self.deck.pop()

    def is_natural(self, hand: list[tuple[str, str]]) -> bool:
        return len(hand) == 2 and hand_value(hand) == 21

    def dealer_play(self) -> None:
        while hand_value(self.dealer) < 17:
            self.dealer.append(self.draw())

    def outcome(self) -> str:
        player_value = hand_value(self.player)
        dealer_value = hand_value(self.dealer)
        player_natural = self.is_natural(self.player)
        dealer_natural = self.is_natural(self.dealer)
        if player_natural or dealer_natural:
            if player_natural and dealer_natural:
                return "push"
            return "blackjack" if player_natural else "loss"
        if player_value > 21:
            return "loss"
        if dealer_value > 21:
            return "win"
        if player_value > dealer_value:
            return "win"
        if player_value < dealer_value:
            return "loss"
        return "push"

    def payout_for(self, outcome: str) -> tuple[int, int]:
        if outcome == "blackjack":
            profit = self.amount * 3 // 2
            return self.amount + profit, profit
        if outcome == "win":
            return self.amount * 2, self.amount
        if outcome == "loss":
            return 0, -self.amount
        return self.amount, 0


class BlackjackView(discord.ui.View):
    def __init__(self, game: BlackjackGame):
        super().__init__(timeout=GAME_TIMEOUT)
        self.game = game

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.game.user_id:
            return True
        await interaction.response.send_message(
            "❌ Esta mesa de Blackjack no te pertenece.",
            ephemeral=True,
        )
        return False

    def disable_all(self) -> None:
        for child in self.children:
            child.disabled = True

    def build_embed(
        self,
        *,
        reveal_dealer: bool = False,
        description: str | None = None,
        color: discord.Color | None = None,
    ) -> discord.Embed:
        player_value = hand_value(self.game.player)
        dealer_value = hand_value(self.game.dealer)
        dealer_display = format_hand(
            self.game.dealer,
            hide_second=not reveal_dealer,
        )
        dealer_score = str(dealer_value) if reveal_dealer else "?"

        embed = discord.Embed(
            title=f"🃏 Blackjack — {self.game.display_name}",
            description=description or "Elige **Pedir** otra carta o **Plantarte**.",
            color=color or discord.Color.dark_purple(),
        )
        embed.add_field(
            name=f"Tus cartas — {player_value}",
            value=format_hand(self.game.player),
            inline=False,
        )
        embed.add_field(
            name=f"Cartas del Dealer — {dealer_score}",
            value=dealer_display,
            inline=False,
        )
        embed.add_field(
            name="Apuesta",
            value=f"**{self.game.amount}** {COIN}",
            inline=False,
        )
        embed.set_footer(
            text=(
                "Dealer automático: pide con 16 o menos | finaliza con 17 o más | "
                f"CD: {format_cooldown(self.game.cooldown)} | "
                f"Máx: {blackjack_config['max_apuesta']} | "
                "Pago: 1:1 | Blackjack: 3:2"
            )
        )
        return embed

    async def _edit(
        self,
        embed: discord.Embed,
        *,
        interaction: discord.Interaction | None = None,
    ) -> None:
        last_error = None
        if interaction is not None:
            try:
                await interaction.edit_original_response(embed=embed, view=self)
                return
            except (discord.HTTPException, discord.NotFound) as error:
                last_error = error
        if self.game.message is not None:
            try:
                await self.game.message.edit(embed=embed, view=self)
                return
            except (discord.HTTPException, discord.NotFound) as error:
                last_error = error
        if last_error:
            raise last_error

    async def _apply_cooldown(self) -> None:
        expires_at = time.time() + self.game.cooldown
        try:
            await set_game_cooldown(self.game.user_id, "bj", expires_at)
            cache.set_game_cooldown_cache(self.game.user_id, "bj", expires_at)
        except Exception:
            logger.exception(
                "La partida BJ %s se liquidó, pero no se pudo guardar el cooldown.",
                self.game.wager_id,
            )

    def _result_description(self, outcome: str, net: int, detail: str) -> tuple[str, discord.Color]:
        if outcome in {"win", "blackjack"}:
            result_text = "Blackjack natural" if outcome == "blackjack" else "Ganaste"
            return (
                f"{detail}\n\n🎉 **{result_text}.** Ganancia neta: **+{net}** {COIN}.",
                discord.Color.green(),
            )
        if outcome == "loss":
            returned = self.game.amount + net
            refund_text = (
                f" Se devolvieron **{returned}** {COIN}."
                if returned > 0
                else ""
            )
            return (
                f"{detail}\n\n💀 **Perdiste {abs(net)}** {COIN}.{refund_text}",
                discord.Color.red(),
            )
        return (
            f"{detail}\n\n🤝 **Empate.** Recuperas tus **{self.game.amount}** {COIN}.",
            discord.Color.gold(),
        )

    async def _settle(
        self,
        outcome: str,
        detail: str,
        *,
        interaction: discord.Interaction | None = None,
    ) -> None:
        payout, net = self.game.payout_for(outcome)
        settlement = None
        try:
            settlement = await settle_wager(self.game.wager_id, payout)
            if not settlement.get("ok"):
                raise RuntimeError(settlement.get("reason", "settlement_failed"))

            self.game.finished = True
            self.game.processing = False
            self.disable_all()
            self.stop()
            _ACTIVE_BLACKJACK.discard(self.game.user_id)
            await self._apply_cooldown()

            description, color = self._result_description(outcome, net, detail)
            embed = self.build_embed(
                reveal_dealer=True,
                description=description,
                color=color,
            )
            try:
                await self._edit(embed, interaction=interaction)
            except (discord.HTTPException, discord.NotFound):
                logger.exception(
                    "BJ %s fue liquidado pero no se pudo mostrar el resultado.",
                    self.game.wager_id,
                )
            if self.game.message is not None:
                asyncio.create_task(self.game.message.delete(delay=120))
        except Exception as error:
            # Si la liquidación alcanzó a confirmarse, refund_wager no podrá
            # tocarla. Si quedó pendiente, intenta devolverla inmediatamente.
            refund = None
            try:
                refund = await refund_wager(self.game.wager_id)
            except Exception:
                logger.exception("No se pudo reembolsar BJ %s.", self.game.wager_id)

            self.game.finished = True
            self.game.processing = False
            self.disable_all()
            self.stop()
            _ACTIVE_BLACKJACK.discard(self.game.user_id)
            refunded = bool(refund and refund.get("ok"))
            description = (
                "❌ La partida no pudo liquidarse. Tu apuesta fue reembolsada."
                if refunded
                else (
                    "⚠️ La partida fue cerrada sin movimientos adicionales. "
                    "Si la apuesta continúa pendiente, el recuperador automático la devolverá."
                )
            )
            embed = self.build_embed(
                reveal_dealer=True,
                description=description,
                color=discord.Color.red(),
            )
            try:
                await self._edit(embed, interaction=interaction)
            except Exception:
                pass
            logger.error(
                "Error liquidando BJ %s: %s | settlement=%r",
                self.game.wager_id,
                error,
                settlement,
            )

    async def _resolve(
        self,
        *,
        interaction: discord.Interaction | None = None,
        timeout: bool = False,
    ) -> None:
        player_value = hand_value(self.game.player)
        initial_natural = (
            self.game.is_natural(self.game.player)
            or self.game.is_natural(self.game.dealer)
        )
        if player_value <= 21 and not initial_natural:
            self.game.dealer_play()
        outcome = self.game.outcome()

        if timeout:
            detail = "⌛ Tiempo agotado: te plantaste automáticamente."
        elif player_value > 21:
            detail = f"Te pasaste de 21 con **{player_value}**."
        elif self.game.is_natural(self.game.player) and self.game.is_natural(self.game.dealer):
            detail = "Ambos consiguieron Blackjack natural."
        elif self.game.is_natural(self.game.player):
            detail = "¡Blackjack natural!"
        elif self.game.is_natural(self.game.dealer):
            detail = "El Dealer consiguió Blackjack natural."
        elif hand_value(self.game.dealer) > 21:
            detail = f"El Dealer se pasó con **{hand_value(self.game.dealer)}**."
        else:
            detail = (
                f"Tu mano: **{player_value}** | "
                f"Dealer: **{hand_value(self.game.dealer)}**."
            )

        await self._settle(outcome, detail, interaction=interaction)

    async def resolve_initial(self) -> None:
        async with self.game.action_lock:
            if self.game.finished:
                return
            self.game.processing = True
            await self._resolve()

    @discord.ui.button(label="Pedir", emoji="➕", style=discord.ButtonStyle.success)
    async def hit(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.game.finished:
            return await interaction.response.send_message(
                "⌛ Esta partida ya terminó.",
                ephemeral=True,
            )
        if self.game.processing or self.game.action_lock.locked():
            return await interaction.response.send_message(
                "⏳ La mesa está procesando la jugada anterior.",
                ephemeral=True,
            )

        self.game.processing = True
        await interaction.response.defer()
        async with self.game.action_lock:
            if self.game.finished:
                return
            self.game.player.append(self.game.draw())
            player_value = hand_value(self.game.player)
            if player_value >= 21:
                await self._resolve(interaction=interaction)
                return

            self.game.processing = False
            await self._edit(self.build_embed(), interaction=interaction)

    @discord.ui.button(label="Plantarse", emoji="✋", style=discord.ButtonStyle.primary)
    async def stand(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.game.finished:
            return await interaction.response.send_message(
                "⌛ Esta partida ya terminó.",
                ephemeral=True,
            )
        if self.game.processing or self.game.action_lock.locked():
            return await interaction.response.send_message(
                "⏳ La mesa está procesando la jugada anterior.",
                ephemeral=True,
            )

        self.game.processing = True
        await interaction.response.defer()
        async with self.game.action_lock:
            if self.game.finished:
                return
            await self._resolve(interaction=interaction)

    async def on_timeout(self):
        async with self.game.action_lock:
            if self.game.finished:
                return
            self.game.processing = True
            await self._resolve(timeout=True)

    async def on_error(self, interaction, error, item):
        logger.error(
            "Error en Blackjack %s (item=%s): %s",
            self.game.wager_id,
            item,
            error,
            exc_info=(type(error), error, error.__traceback__),
        )
        refunded = False
        if not self.game.finished:
            try:
                refund = await refund_wager(self.game.wager_id)
                refunded = bool(refund.get("ok"))
            except Exception:
                logger.exception("No se pudo reembolsar BJ tras on_error.")
        self.game.finished = True
        self.game.processing = False
        self.disable_all()
        self.stop()
        _ACTIVE_BLACKJACK.discard(self.game.user_id)
        recovery_text = (
            "La apuesta fue reembolsada."
            if refunded
            else "Si continúa pendiente, el recuperador automático la devolverá."
        )
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    f"⚠️ La partida se cerró por un error. {recovery_text}",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    f"⚠️ La partida se cerró por un error. {recovery_text}",
                    ephemeral=True,
                )
        except Exception:
            pass
        if self.game.message is not None:
            try:
                await self.game.message.edit(view=None)
            except Exception:
                pass


class Blackjack(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="bj")
    async def blackjack(self, ctx, apuesta: int = None):
        message_id = ctx.message.id
        if message_id in _SEEN_BLACKJACK_MESSAGES:
            return
        now_monotonic = time.monotonic()
        _SEEN_BLACKJACK_MESSAGES[message_id] = now_monotonic
        if len(_SEEN_BLACKJACK_MESSAGES) > 2_000:
            cutoff = now_monotonic - 600
            for seen_id, seen_at in list(_SEEN_BLACKJACK_MESSAGES.items()):
                if seen_at < cutoff:
                    _SEEN_BLACKJACK_MESSAGES.pop(seen_id, None)

        if not blackjack_config["activa"]:
            return await ctx.send(
                "🃏 La mesa de Blackjack no se encuentra disponible por el momento."
            )
        if apuesta is None:
            return await ctx.send(
                f"❌ {ctx.author.mention} Formato correcto: `!bj {{apuesta}}`."
            )
        if apuesta < 100:
            return await ctx.message.reply(
                f"Apuesta minima es 100 {COIN} no querras vivir de migajas?"
            )
        if apuesta > blackjack_config["max_apuesta"]:
            return await ctx.send(
                f"❌ {ctx.author.mention} La apuesta máxima es "
                f"**{blackjack_config['max_apuesta']}** {COIN}."
            )

        start_lock = _BLACKJACK_START_LOCKS.setdefault(
            ctx.author.id,
            asyncio.Lock(),
        )
        async with start_lock:
            if ctx.author.id in _ACTIVE_BLACKJACK:
                return await ctx.send(
                    f"❌ {ctx.author.mention} Ya tienes una partida de Blackjack activa."
                )

            wager = await reserve_wager(
                ctx.author.id,
                "bj",
                apuesta,
                expires_in=WAGER_EXPIRY,
                idempotency_key=f"bj:message:{message_id}",
                exclusive_pending=True,
                enforce_cooldown=True,
            )
            if not wager["ok"]:
                reason = wager.get("reason")
                if reason == "duplicate_request":
                    return
                if reason == "already_pending":
                    return await ctx.send(
                        f"❌ {ctx.author.mention} Ya tienes una partida de Blackjack activa."
                    )
                if reason == "cooldown":
                    return await ctx.send(
                        f"⏳ {ctx.author.mention} Podrás volver a jugar "
                        f"<t:{int(wager['expires_at'])}:R>."
                    )
                return await ctx.send(
                    f"❌ {ctx.author.mention} No tienes suficiente balance para apostar "
                    f"**{apuesta}** {COIN}."
                )

            game = BlackjackGame(
                ctx.author.id,
                ctx.author.display_name,
                apuesta,
                wager["id"],
            )
            view = BlackjackView(game)
            _ACTIVE_BLACKJACK.add(ctx.author.id)
            try:
                message = await ctx.reply(
                    embed=view.build_embed(),
                    view=view,
                    mention_author=False,
                )
                game.message = message

                if game.is_natural(game.player) or game.is_natural(game.dealer):
                    await view.resolve_initial()
            except Exception:
                _ACTIVE_BLACKJACK.discard(ctx.author.id)
                try:
                    await refund_wager(wager["id"])
                except Exception:
                    logger.exception("No se pudo reembolsar el inicio fallido de BJ.")
                raise

    @blackjack.error
    async def blackjack_error(self, ctx, error):
        if isinstance(error, (commands.BadArgument, commands.MissingRequiredArgument)):
            await ctx.send(
                f"❌ {ctx.author.mention} Formato correcto: `!bj {{apuesta}}`."
            )
            return
        raise error


async def setup(bot):
    await bot.add_cog(Blackjack(bot))
