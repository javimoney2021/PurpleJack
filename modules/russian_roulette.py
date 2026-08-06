import asyncio
import logging
import random
import time

import asyncpg
import discord
from discord.ext import commands

from core.config import COIN, rr_config
from core.database import (
    set_game_cooldown,
    reserve_wager,
    increase_wager,
    extend_wager_expiry,
    settle_wager,
    lose_wager,
    refund_wager,
)
from core import cache

logger = logging.getLogger(__name__)

WAIT_IMAGE    = "https://raw.githubusercontent.com/javimoney2021/PurpleJack/main/Thumbs/Shot.png"
SUCCESS_IMAGE = "https://raw.githubusercontent.com/javimoney2021/PurpleJack/main/Thumbs/Salvado.png"
FAILURE_IMAGE = "https://raw.githubusercontent.com/javimoney2021/PurpleJack/main/Thumbs/Derrota.png"
END_IMAGE     = "https://raw.githubusercontent.com/javimoney2021/PurpleJack/main/Thumbs/Victoria.png"


ROUND_REWARDS = [0.6, 0.8, 1.0, 1.5, 2.0]
ROUND_LABELS  = ["1º ronda", "2º ronda", "3º ronda", "4º ronda", "5º ronda"]

rr_games = {}
_RR_START_LOCKS: dict[int, asyncio.Lock] = {}
_SEEN_RR_MESSAGES: dict[int, float] = {}


def format_percent(value):
    return int(value * 100)


def build_rr_embed(user: discord.Member, game, state: str, description: str, thumbnail: str):
    embed = discord.Embed(
        title=f"**RULETA RUSA - {user.display_name}**",
        description=description,
        color=discord.Color.purple() if state != "lost" else discord.Color.red(),
    )
    embed.add_field(name="Apuesta inicial", value=f"{game.apuesta} {COIN}", inline=True)
    if game.risk_amount != game.apuesta:
        embed.add_field(
            name="Riesgo actual",
            value=f"{game.risk_amount} {COIN}",
            inline=True,
        )
    if state == "lost":
        embed.add_field(name="Rondas completadas", value=f"{game.round}/5",      inline=True)
        embed.add_field(name="Ganancia final",       value=f"0 {COIN}",           inline=False)
    elif game.active:
        embed.add_field(name="Ronda",                value=f"{game.round + 1}/5", inline=True)
        embed.add_field(name="Ganancia provisional", value=f"{game.ganancia} {COIN}", inline=False)
    else:
        embed.add_field(name="Rondas completadas",   value=f"{game.round}/5",      inline=True)
        embed.add_field(name="Ganancia final",       value=f"{game.ganancia} {COIN}", inline=False)
    embed.set_thumbnail(url=thumbnail)
    return embed


class RRGameState:
    def __init__(self, user_id: int, apuesta: int, author_name: str, wager_id: str):
        self.user_id     = user_id
        self.apuesta     = apuesta
        self.round       = 0
        self.ganancia    = 0
        self.active      = True
        self.finished    = False
        self.message     = None
        self.author_name = author_name
        self.wager_id    = wager_id
        self.risk_amount = apuesta
        self.processing  = False
        self.action_lock = asyncio.Lock()
        self.current_view = None


class RRView(discord.ui.View):
    def __init__(self, game: RRGameState, author_id: int):
        super().__init__(timeout=150)
        self.game      = game
        self.author_id = author_id
        self.game.current_view = self

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "❌ No es tu partida de Ruleta Rusa.", ephemeral=True
            )
            return False
        if not self.game.active:
            await interaction.response.send_message(
                "La Ruleta Rusa expiró por inactividad. Crea otra.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self) -> None:
        async with self.game.action_lock:
            if self.game.current_view is not self:
                return
            if self.game.finished:
                return
            self.game.active = False
            self.game.finished = True
            refund = await refund_wager(self.game.wager_id)
            for item in self.children:
                item.disabled = True
            if refund.get("ok"):
                estado = (
                    f"Tu riesgo de **{self.game.risk_amount} {COIN}** fue reembolsado."
                )
            else:
                estado = (
                    "La apuesta ya había sido procesada; no se realizó "
                    "ningún movimiento adicional."
                )
            if self.game.message:
                timeout_embed = discord.Embed(
                    title=f"**RULETA RUSA - {self.game.author_name}**",
                    description=f"⏳ La Ruleta Rusa expiró por inactividad. {estado}",
                    color=discord.Color.dark_grey(),
                )
                timeout_embed.set_thumbnail(url=FAILURE_IMAGE)
                try:
                    await self.game.message.edit(embed=timeout_embed, view=self)
                except Exception:
                    pass
            rr_games.pop(self.game.user_id, None)

    @discord.ui.button(label="Disparar", style=discord.ButtonStyle.danger, row=0)
    async def disparar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.game.processing:
            return await interaction.response.send_message(
                "⏳ Ya se está procesando una acción.",
                ephemeral=True,
            )
        if self.game.action_lock.locked():
            return await interaction.response.send_message(
                "⏳ Ya se está procesando una acción.",
                ephemeral=True,
            )
        await self.game.action_lock.acquire()
        self.game.processing = True
        try:
            if not self.game.active or self.game.finished:
                return await interaction.response.send_message(
                    "⌛ Esta partida ya finalizó.",
                    ephemeral=True,
                )
            if not await extend_wager_expiry(self.game.wager_id, 300):
                raise RuntimeError("La apuesta ya no está pendiente")
            target_risk = None
            if self.game.round == 3:
                target_risk = int(self.game.apuesta * 1.8)
            elif self.game.round == 4:
                target_risk = int(self.game.apuesta * 2.8)

            if target_risk is not None and target_risk > self.game.risk_amount:
                extra = target_risk - self.game.risk_amount
                increased = await increase_wager(self.game.wager_id, extra)
                if not increased["ok"]:
                    return await interaction.response.send_message(
                        f"❌ Necesitas **{extra} {COIN}** adicionales para asumir "
                        f"el riesgo de esta ronda. Puedes reclamar tu ganancia actual.",
                        ephemeral=True,
                    )
                self.game.risk_amount = target_risk

            preparing_embed = build_rr_embed(
                interaction.user,
                self.game,
                state="waiting",
                description=(
                    f"Preparando disparo... \n\n"
                    f"Ronda actual: **{self.game.round + 1}/5**\n"
                    f"Esperando resultado..."
                ),
                thumbnail=WAIT_IMAGE,
            )
            for item in self.children:
                item.disabled = True
            await interaction.response.edit_message(embed=preparing_embed, view=self)

            await asyncio.sleep(5)

            if not self.game.active:
                return

            success = random.random() <= rr_config["ganar_prob"]
            if success:
                self.game.round += 1
                reward = int(round(self.game.apuesta * ROUND_REWARDS[self.game.round - 1]))
                self.game.ganancia += reward

                if self.game.round >= 5:
                    self.game.active   = False
                    self.game.finished = True
                    total_embed = build_rr_embed(
                        interaction.user,
                        self.game,
                        state="victory",
                        description=(
                            f"💥 **Victoria Total**! Completaste las 5 fases de la Ruleta Rusa, "
                            f"**Suerte** es tu segundo nombre!.\n\n"
                            f"Recuperas tu riesgo de **{self.game.risk_amount} {COIN}** "
                            f"y recibes **{self.game.ganancia} {COIN}** de ganancia."
                        ),
                        thumbnail=END_IMAGE,
                    )
                    for item in self.children:
                        item.disabled = True
                    settlement = await settle_wager(
                        self.game.wager_id,
                        self.game.risk_amount + self.game.ganancia,
                    )
                    if not settlement.get("ok"):
                        raise RuntimeError(
                            f"Liquidación rechazada: {settlement.get('reason')}"
                        )
                    self.stop()
                    await interaction.edit_original_response(embed=total_embed, view=self)
                    rr_games.pop(self.game.user_id, None)
                    return

                reward_percent = format_percent(
                    sum(ROUND_REWARDS[: self.game.round])
                )

                if self.game.round == 3:
                    aviso = (
                        f"\n\n⚠️ **¡Atención!** Las rondas 4 y 5 comprometen más que tu Apuesta Inicial.\n\n"
                        f"Derrota en ronda 4 → pierdes **{int(self.game.apuesta * 1.8)} {COIN}**\n"
                        f"Derrota en ronda 5 → pierdes **{int(self.game.apuesta * 2.8)} {COIN}**\n\n"
                        f"¿Grandes ganancias implican grandes riesgos, continuas?"
                    )
                elif self.game.round == 4:
                    aviso = (
                        f"\n\n🌟 **Maravillosas ganancias, la suerte te persigue...**\n"
                        f"¿Te atreves a dar el último paso a por el **Gran Botín**?"
                    )
                else:
                    aviso = ""

                result_embed = build_rr_embed(
                    interaction.user,
                    self.game,
                    state="success",
                    description=(
                        f"✅ Te salvaste en la **{ROUND_LABELS[self.game.round - 1]}**!\n\n"
                        f"Ganancia acumulada: **{self.game.ganancia} {COIN}**\n"
                        f"Equivale al **{reward_percent}%** de la apuesta inicial.\n"
                        f"Puedes reclamar ya o arriesgarte al siguiente disparo.\n\n"
                        f"{aviso}"
                    ),
                    thumbnail=SUCCESS_IMAGE,
                )
                active_view = RRView(self.game, self.author_id)
                await interaction.edit_original_response(embed=result_embed, view=active_view)
                self.stop()
                return

            self.game.active   = False
            self.game.finished = True

            ronda_actual = self.game.round + 1
            perdida = self.game.risk_amount
            if ronda_actual == 4:
                extra_txt = f"⚠️ Ronda 4 — Perdiste **{perdida} {COIN}** (riesgo x1.8)"
            elif ronda_actual == 5:
                extra_txt = f"⚠️ Ronda 5 — Perdiste **{perdida} {COIN}** (riesgo x2.8)"
            else:
                extra_txt = f"Perdiste tu apuesta inicial de **{perdida} {COIN}**"

            loss_embed = build_rr_embed(
                interaction.user,
                self.game,
                state="lost",
                description=(
                    f"💥 ¡Bala inoportuna! {extra_txt}.\n\n"
                    f"Mejor suerte la próxima vez."
                ),
                thumbnail=FAILURE_IMAGE,
            )
            for item in self.children:
                item.disabled = True
            settlement = await lose_wager(self.game.wager_id)
            if not settlement.get("ok"):
                raise RuntimeError(
                    f"Liquidación rechazada: {settlement.get('reason')}"
                )
            self.stop()
            await interaction.edit_original_response(embed=loss_embed, view=self)
            rr_games.pop(self.game.user_id, None)

        except Exception as e:
            logger.error(f"Error en disparar: {e}")
            self.game.active = False
            self.game.finished = True
            self.stop()
            for item in self.children:
                item.disabled = True
            refund = await refund_wager(self.game.wager_id)
            rr_games.pop(self.game.user_id, None)
            if refund.get("ok"):
                error_message = (
                    "❌ La partida se canceló por un error y tu apuesta fue reembolsada."
                )
            else:
                error_message = (
                    "⚠️ La partida se cerró. La apuesta ya estaba procesada y "
                    "no se realizó ningún movimiento adicional."
                )
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(
                        error_message,
                        ephemeral=True,
                    )
                else:
                    await interaction.response.send_message(
                        error_message,
                        ephemeral=True,
                    )
            except Exception:
                pass
            try:
                await interaction.edit_original_response(view=self)
            except Exception:
                pass
        finally:
            self.game.processing = False
            if self.game.action_lock.locked():
                self.game.action_lock.release()

    @discord.ui.button(label="Reclamar", style=discord.ButtonStyle.success, row=0)
    async def reclamar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.game.processing:
            return await interaction.response.send_message(
                "⏳ Ya se está procesando una acción.",
                ephemeral=True,
            )
        if self.game.action_lock.locked():
            return await interaction.response.send_message(
                "⏳ Ya se está procesando una acción.",
                ephemeral=True,
            )
        await self.game.action_lock.acquire()
        self.game.processing = True
        try:
            if not self.game.active or self.game.finished:
                return await interaction.response.send_message(
                    "⌛ Esta partida ya finalizó.",
                    ephemeral=True,
                )
            if not await extend_wager_expiry(self.game.wager_id, 300):
                raise RuntimeError("La apuesta ya no está pendiente")
            if self.game.round == 0:
                return await interaction.response.send_message(
                    "❌ Debes sobrevivir al menos a un disparo para reclamar.",
                    ephemeral=True,
                )

            self.game.active   = False
            self.game.finished = True

            claim_embed = build_rr_embed(
                interaction.user,
                self.game,
                state="claimed",
                description=(
                    f"🟢 Recuperas tu riesgo de **{self.game.risk_amount} {COIN}** "
                    f"y reclamas **{self.game.ganancia} {COIN}** de ganancia.\n\n"
                    f"Gracias por jugar Ruleta Rusa."
                ),
                thumbnail=END_IMAGE,
            )
            for item in self.children:
                item.disabled = True
            settlement = await settle_wager(
                self.game.wager_id,
                self.game.risk_amount + self.game.ganancia,
            )
            if not settlement.get("ok"):
                raise RuntimeError(
                    f"Liquidación rechazada: {settlement.get('reason')}"
                )
            self.stop()
            await interaction.response.edit_message(embed=claim_embed, view=self)
            rr_games.pop(self.game.user_id, None)

        except Exception as e:
            logger.error(f"Error en reclamar: {e}")
            self.game.active = False
            self.game.finished = True
            self.stop()
            for item in self.children:
                item.disabled = True
            refund = await refund_wager(self.game.wager_id)
            rr_games.pop(self.game.user_id, None)
            if refund.get("ok"):
                error_message = (
                    "❌ La partida se canceló por un error y tu apuesta fue reembolsada."
                )
            else:
                error_message = (
                    "⚠️ La partida se cerró. La apuesta ya estaba procesada y "
                    "no se realizó ningún movimiento adicional."
                )
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(
                        error_message,
                        ephemeral=True,
                    )
                else:
                    await interaction.response.send_message(
                        error_message,
                        ephemeral=True,
                    )
            except Exception:
                pass
            try:
                await interaction.edit_original_response(view=self)
            except Exception:
                pass
        finally:
            self.game.processing = False
            if self.game.action_lock.locked():
                self.game.action_lock.release()


class RussianRoulette(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="rr")
    async def rr(self, ctx, monto: int = None):
        message_id = ctx.message.id
        if message_id in _SEEN_RR_MESSAGES:
            return
        now_monotonic = time.monotonic()
        _SEEN_RR_MESSAGES[message_id] = now_monotonic
        if len(_SEEN_RR_MESSAGES) > 2_000:
            cutoff = now_monotonic - 600
            for seen_id, seen_at in list(_SEEN_RR_MESSAGES.items()):
                if seen_at < cutoff:
                    _SEEN_RR_MESSAGES.pop(seen_id, None)

        if not rr_config["activa"]:
            return await ctx.send("🔧 La Ruleta Rusa se encuentra desactivada.")

        if monto is None or monto <= 0:
            return await ctx.send(
                f"❌ {ctx.author.mention} Formato correcto: `!rr {{monto}}`"
            )

        if monto > rr_config["max_apuesta"]:
            return await ctx.reply(
                f"❌ No puedes apostar más de **{rr_config['max_apuesta']} {COIN}**.",
                mention_author=False,
            )

        start_lock = _RR_START_LOCKS.setdefault(
            ctx.author.id,
            asyncio.Lock(),
        )
        async with start_lock:
            partida = rr_games.get(ctx.author.id)
            if partida and partida.active:
                return await ctx.send(
                    f"❌ {ctx.author.mention} Ya tienes una partida activa de Ruleta Rusa."
                )
            if partida:
                rr_games.pop(ctx.author.id, None)

            try:
                wager = await reserve_wager(
                    ctx.author.id,
                    "rr",
                    monto,
                    expires_in=300,
                    idempotency_key=f"rr:message:{message_id}",
                    exclusive_pending=True,
                    enforce_cooldown=True,
                )
            except asyncpg.UniqueViolationError:
                return await ctx.send(
                    f"❌ {ctx.author.mention} Ya tienes una partida activa de Ruleta Rusa."
                )

            if not wager["ok"]:
                reason = wager.get("reason")
                if reason == "duplicate_request":
                    return
                if reason == "already_pending":
                    return await ctx.send(
                        f"❌ {ctx.author.mention} Ya tienes una partida activa de Ruleta Rusa."
                    )
                if reason == "cooldown":
                    return await ctx.send(
                        f"⏳ {ctx.author.mention} Espera "
                        f"<t:{int(wager['expires_at'])}:R> para volver a jugar Ruleta Rusa."
                    )
                return await ctx.send(
                    f"❌ {ctx.author.mention} No tienes suficiente balance para esta apuesta."
                )

            game = RRGameState(
                ctx.author.id,
                monto,
                ctx.author.display_name,
                wager["id"],
            )
            rr_games[ctx.author.id] = game
            initial_embed = build_rr_embed(
                ctx.author,
                game,
                state="waiting",
                description=(
                    f"Has iniciado una partida de Ruleta Rusa con **{monto} {COIN}**.\n\n"
                    f"Tienes **5 disparos**. Cada salvada aumenta tu ganancia acumulada.\n"
                    f"Pulsa **Disparar** para comenzar, o reclama cuando quieras si sobrevives."
                ),
                thumbnail=WAIT_IMAGE,
            )
            view = RRView(game, ctx.author.id)
            message = None
            try:
                message = await ctx.send(embed=initial_embed, view=view)
                game.message = message
                expira_en = time.time() + rr_config["cooldown"]
                await set_game_cooldown(ctx.author.id, "rr", expira_en)
                cache.set_game_cooldown_cache(ctx.author.id, "rr", expira_en)
            except Exception as error:
                rr_games.pop(ctx.author.id, None)
                refund = await refund_wager(wager["id"])
                if message is not None:
                    try:
                        await message.edit(
                            content=(
                                "❌ No se pudo registrar la partida. "
                                "La apuesta fue reembolsada."
                                if refund.get("ok")
                                else "⚠️ La partida fue cerrada sin movimientos adicionales."
                            ),
                            embed=None,
                            view=None,
                        )
                    except Exception:
                        pass
                logger.error("No se pudo iniciar !rr: %s", error)
                if message is None:
                    await ctx.send(
                        "❌ No se pudo iniciar la partida. La apuesta fue reembolsada."
                    )


async def setup(bot):
    await bot.add_cog(RussianRoulette(bot))
