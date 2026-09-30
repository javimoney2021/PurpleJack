import discord
import asyncio
import logging
import random
import time
from discord.ext import commands
from core.database import (
    get_user,
    get_command_cooldown,
    set_command_cooldown,
    reserve_wager,
    settle_wager,
    lose_wager,
    refund_wager,
)
from core.config import COIN, memo_config

logger = logging.getLogger("purplejack.memo")

# ── CONFIG ─────────────────────────────────────────────
MAX_INTENTOS  = 6
AUTO_DELETE   = 80
HIDDEN_EMOJI  = "🟦"
EMOJIS_PARES  = ["🎲", "🍪", "🍇", "🔪", "💎", "🍼", "👑", "🚀"]
MEMO_WIN_THUMBNAIL = "https://pub-a09b3609b6b34dfab5c7aa7742cd1a8a.r2.dev/Purple%20jack%20Harcode/MvpPJ.png"
MEMO_LOSS_THUMBNAIL = "https://pub-a09b3609b6b34dfab5c7aa7742cd1a8a.r2.dev/Purple%20jack%20Harcode/perdi.png"

# ── ESTADO GLOBAL ──────────────────────────────────────
_active_memo: set[int] = set()   # {user_id}
_memo_cooldowns: dict[int, float] = {}  # {user_id: expira_en}


# ── VIEW ───────────────────────────────────────────────
class MemoView(discord.ui.View):
    def __init__(
        self,
        author: discord.Member,
        monto: int,
        tablero: list[str],
        wager_id: str,
    ):
        super().__init__(timeout=120)
        self.author        = author
        self.monto         = monto
        self.tablero       = tablero          # 16 emojis en orden
        self.revelado      = [False] * 16     # casillas permanentemente visibles
        self.seleccion     = []               # índices del turno actual (máx 2)
        self.intentos_fail = 0
        self.pares_ok      = 0
        self.bloqueado     = False
        self.racha         = 0
        self.message       = None
        self.wager_id      = wager_id
        self._terminado    = False
        self._wager_finalizada = False
        # Serializa clics simultáneos del mismo tablero (doble pulsación,
        # varias pestañas o latencia de Discord).
        self._interaction_lock = asyncio.Lock()
        self._build_buttons()

    def _build_buttons(self):
        self.clear_items()
        for i in range(16):
            fila  = i // 4
            label = self.tablero[i] if self.revelado[i] else HIDDEN_EMOJI
            btn   = discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.secondary if not self.revelado[i] else discord.ButtonStyle.success,
                row=fila,
                custom_id=f"memo_{i}"
            )
            btn.callback = self._make_callback(i)
            self.add_item(btn)

    async def _responder(self, interaction: discord.Interaction, mensaje: str):
        """Responde aunque la interacción ya haya sido diferida."""
        if interaction.response.is_done():
            return await interaction.followup.send(mensaje, ephemeral=True)
        return await interaction.response.send_message(mensaje, ephemeral=True)

    async def _editar_tablero(
        self,
        interaction: discord.Interaction,
        *,
        embed: discord.Embed,
    ):
        """Edita el tablero con reintento y respaldo sobre el mensaje guardado."""
        ultimo_error = None
        for intento in range(2):
            try:
                await interaction.edit_original_response(embed=embed, view=self)
                return
            except (discord.HTTPException, discord.NotFound) as error:
                ultimo_error = error
                if intento == 0:
                    await asyncio.sleep(0.25)

        if self.message is not None:
            try:
                await self.message.edit(embed=embed, view=self)
                return
            except (discord.HTTPException, discord.NotFound) as error:
                ultimo_error = error
        if ultimo_error is not None:
            raise ultimo_error

    async def _programar_eliminacion(self):
        await asyncio.sleep(AUTO_DELETE)
        if self.message is None:
            return
        try:
            await self.message.delete()
        except (discord.HTTPException, discord.NotFound):
            pass

    def _make_callback(self, idx: int):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.author.id:
                return await self._responder(interaction, "❌ Este tablero no es tuyo.")
            if self._interaction_lock.locked():
                return await self._responder(interaction, "⏳ Espera un momento...")

            # Reconoce el clic antes de cualquier cambio visual, espera o I/O.
            # Discord ya no marcará la interacción como vencida durante una
            # latencia puntual de su API o de la base de datos.
            try:
                await interaction.response.defer()
            except (discord.HTTPException, discord.NotFound) as error:
                logger.warning("No se pudo diferir clic de Memo %s: %s", self.wager_id, error)
                return

            async with self._interaction_lock:
                if self._terminado:
                    return await self._responder(interaction, "⌛ Esta partida ya finalizó.")
                if self.bloqueado:
                    return await self._responder(interaction, "⏳ Espera un momento...")
                if self.revelado[idx]:
                    return await self._responder(interaction, "✅ Esta casilla ya está descubierta.")
                if idx in self.seleccion:
                    return await self._responder(interaction, "❌ Ya seleccionaste esta casilla.")

                self.bloqueado = True
                self.seleccion.append(idx)
                self._build_buttons()
                for item in self.children:
                    cid = int(item.custom_id.split("_")[1])
                    if cid in self.seleccion:
                        item.label = self.tablero[cid]
                        item.style = discord.ButtonStyle.primary
                await self._editar_tablero(interaction, embed=self._build_embed())

                if len(self.seleccion) != 2:
                    self.bloqueado = False
                    return

                i1, i2 = self.seleccion
                if self.tablero[i1] == self.tablero[i2]:
                    self.revelado[i1] = True
                    self.revelado[i2] = True
                    self.pares_ok += 1
                    self.racha += 1
                    if self.racha >= 3 and self.racha % 3 == 0 and self.intentos_fail > 0:
                        self.intentos_fail -= 1
                    self.seleccion = []
                    self._build_buttons()

                    if self.pares_ok == 8:
                        self._terminado = True
                        self.bloqueado = True
                        recompensa_total = self.monto * 3
                        ganancia_neta = self.monto * 2
                        settlement = await settle_wager(self.wager_id, recompensa_total)
                        if not settlement.get("ok"):
                            raise RuntimeError(f"No se pudo liquidar Memo: {settlement.get('reason')}")
                        self._wager_finalizada = True
                        embed = self._build_embed(
                            estado=(
                                f"🏆 ¡Ganaste! Recibes **+{recompensa_total}** {COIN} en total "
                                f"({ganancia_neta} de ganancia)."
                            ),
                            thumbnail_url=MEMO_WIN_THUMBNAIL,
                        )
                        self.stop()
                        self._deshabilitar_todo()
                        _active_memo.discard(self.author.id)
                        await self._editar_tablero(interaction, embed=embed)
                        asyncio.create_task(self._programar_eliminacion())
                        return

                    self.bloqueado = False
                    await self._editar_tablero(interaction, embed=self._build_embed())
                    return

                self.intentos_fail += 1
                self.racha = 0
                intentos_restantes = MAX_INTENTOS - self.intentos_fail
                if intentos_restantes <= 0:
                    self._terminado = True
                    settlement = await lose_wager(self.wager_id)
                    if not settlement.get("ok"):
                        raise RuntimeError(f"No se pudo liquidar Memo: {settlement.get('reason')}")
                    self._wager_finalizada = True
                    self.revelado = [True] * 16
                    self._build_buttons()
                    self._deshabilitar_todo()
                    embed = self._build_embed(
                        estado=f"💀 ¡Perdiste! Se descuentan **-{self.monto}** {COIN}",
                        thumbnail_url=MEMO_LOSS_THUMBNAIL,
                    )
                    self.stop()
                    _active_memo.discard(self.author.id)
                    await self._editar_tablero(interaction, embed=embed)
                    asyncio.create_task(self._programar_eliminacion())
                    return

                # Las dos incorrectas quedan visibles brevemente y el lock
                # bloquea cualquier clic concurrente durante esa transición.
                await asyncio.sleep(1.5)
                self.seleccion = []
                self.bloqueado = False
                self._build_buttons()
                await self._editar_tablero(interaction, embed=self._build_embed())

        return callback

    def _build_embed(self, estado: str = None, thumbnail_url: str = None) -> discord.Embed:
        intentos_restantes = MAX_INTENTOS - self.intentos_fail
        corazones = "❤️" * intentos_restantes + "🖤" * self.intentos_fail

        desc = (
            f"**Pares encontrados:** {self.pares_ok}/8\n"
            f"**Intentos fallidos:** {corazones}\n\n"
        )
        if estado:
            desc += f"\n{estado}"

        nick = self.author.nick or self.author.display_name
        embed = discord.Embed(
            title=f"🧠 Juego de Memoria - {nick}",
            description=desc,
            color=discord.Color.blurple()
        )
        if thumbnail_url:
            embed.set_thumbnail(url=thumbnail_url)
        embed.set_footer(text=f"Apuesta: {self.monto} PurpleCoins  •  Solo tú puedes jugar")
        return embed

    def _deshabilitar_todo(self):
        for item in self.children:
            item.disabled = True

    async def on_timeout(self):
        async with self._interaction_lock:
            if self._terminado:
                return
            self._terminado = True
            _active_memo.discard(self.author.id)
            try:
                reembolso = await refund_wager(self.wager_id)
                self._wager_finalizada = bool(reembolso.get("ok"))
            except Exception:
                logger.exception("No se pudo reembolsar Memo %s al vencer.", self.wager_id)
            self._deshabilitar_todo()
            if self.message:
                estado = (
                    f"⏰ Tiempo agotado. Partida cancelada y apuesta de "
                    f"**{self.monto}** {COIN} reembolsada."
                    if self._wager_finalizada
                    else "⚠️ Tiempo agotado. El reembolso se reintentará automáticamente."
                )
                try:
                    await self.message.edit(
                        embed=self._build_embed(estado=estado),
                        view=self,
                    )
                    asyncio.create_task(self._programar_eliminacion())
                except (discord.HTTPException, discord.NotFound) as error:
                    logger.warning("No se pudo cerrar Memo %s por tiempo: %s", self.wager_id, error)

    async def on_error(self, interaction, error, item):
        logger.error(
            "Error en Memo %s (item=%s): %s",
            self.wager_id,
            item,
            error,
            exc_info=(type(error), error, error.__traceback__),
        )
        async with self._interaction_lock:
            if not self._terminado:
                self._terminado = True
            _active_memo.discard(self.author.id)
            self._deshabilitar_todo()
            self.stop()
            reembolso = None
            if not self._wager_finalizada:
                try:
                    reembolso = await refund_wager(self.wager_id)
                    self._wager_finalizada = bool(reembolso.get("ok"))
                except Exception:
                    logger.exception("No se pudo reembolsar Memo %s tras un error.", self.wager_id)
        mensaje = (
            "⚠️ La partida se canceló por un error y tu apuesta fue reembolsada."
            if reembolso and reembolso.get("ok")
            else "⚠️ La partida ya había sido procesada; no se realizó ningún cobro ni pago adicional."
        )
        try:
            await self._responder(
                interaction,
                mensaje,
            )
        except (discord.HTTPException, discord.NotFound) as response_error:
            logger.warning("No se pudo responder error de Memo %s: %s", self.wager_id, response_error)


# ── COG ────────────────────────────────────────────────
class Memo(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="memo")
    async def memo(self, ctx, monto: int = None):
        user_id = ctx.author.id
        now     = time.time()

        # ── Cooldown individual por usuario ────────────────────────
        expira_en = _memo_cooldowns.get(user_id, 0)
        if expira_en <= now:
            expira_en = await get_command_cooldown("user", user_id, "memo")
        if expira_en > now:
            remaining = int(expira_en - now)
            tiempo = f"{remaining // 60}m {remaining % 60}s" if remaining >= 60 else f"{remaining}s"
            return await ctx.send(
                f"⏳ {ctx.author.mention} Podrás jugar nuevamente en **{tiempo}**.",
                delete_after=10
            )

        if monto is None:
            nick = ctx.author.nick or ctx.author.display_name
            return await ctx.reply(
                f"❌ **{nick}** Formato correcto: `!memo {{monto}}`",
                mention_author=False,
            )
        if not memo_config["activa"]:
            return await ctx.send("🔧 El sistema de Memo está desactivado. Intenta después.")

        if monto < 100:
            return await ctx.message.reply(
                f"Apuesta minima es 100 {COIN} no querras vivir de migajas?"
            )
        if user_id in _active_memo:
            return await ctx.send(
                f"❌ {ctx.author.mention} Ya tienes una partida activa."
            )
        if monto > memo_config["max_apuesta"]:
            return await ctx.send(
                f"❌ {ctx.author.mention} La apuesta máxima es **{memo_config['max_apuesta']}** {COIN}."
            )

        user_data = await get_user(user_id)
        if user_data["balance"] < monto:
            return await ctx.send(
                f"❌ {ctx.author.mention} No tienes suficiente balance. "
                f"Necesitas **{monto}** {COIN}."
            )

        # ── Generar tablero aleatorio ──────────────────────────────
        tablero = EMOJIS_PARES * 2
        random.shuffle(tablero)

        wager = await reserve_wager(
            user_id,
            "memo",
            monto,
            expires_in=180,
        )
        if not wager["ok"]:
            return await ctx.send(
                f"❌ {ctx.author.mention} No tienes suficiente balance. "
                f"Necesitas **{monto}** {COIN}."
            )

        expira_en = now + memo_config["cooldown"]
        try:
            _memo_cooldowns[user_id] = expira_en
            await set_command_cooldown("user", user_id, "memo", expira_en)
            _active_memo.add(user_id)
            view = MemoView(ctx.author, monto, tablero, wager["id"])
            msg = await ctx.send(embed=view._build_embed(), view=view)
            view.message = msg
        except Exception:
            _active_memo.discard(user_id)
            await refund_wager(wager["id"])
            raise

    @memo.error
    async def memo_error(self, ctx, error):
        if isinstance(error, commands.BadArgument):
            await ctx.send(
                f"❌ {ctx.author.mention} El monto debe ser un número entero. "
                f"Formato: `!memo {{monto}}`"
            )
        else:
            raise error


async def setup(bot):
    await bot.add_cog(Memo(bot))
