import discord
import asyncio
import random
from discord.ext import commands
from core.database import get_user, update_balance
from core.config import COIN, memo_config

# ── CONFIG ─────────────────────────────────────────────
MAX_INTENTOS  = 7
AUTO_DELETE   = 80
HIDDEN_EMOJI  = "🟦"
EMOJIS_PARES  = ["🎲", "🍪", "🍇", "🔪", "💎", "🍼", "👑", "🚀"]
MEMO_WIN_THUMBNAIL = "https://pub-a09b3609b6b34dfab5c7aa7742cd1a8a.r2.dev/Purple%20jack%20Harcode/mvp.png"
MEMO_LOSS_THUMBNAIL = "https://pub-a09b3609b6b34dfab5c7aa7742cd1a8a.r2.dev/Purple%20jack%20Harcode/perdi.png"

# ── ESTADO GLOBAL ──────────────────────────────────────
_active_memo: set[int] = set()   # {user_id}
_memo_cooldowns: dict[int, float] = {}  # {user_id: expira_en}


# ── VIEW ───────────────────────────────────────────────
class MemoView(discord.ui.View):
    def __init__(self, author: discord.Member, monto: int, tablero: list[str]):
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
        self._terminado    = False
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

    def _make_callback(self, idx: int):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.author.id:
                return await interaction.response.send_message(
                    "❌ Este tablero no es tuyo.", ephemeral=True
                )
            if self.bloqueado:
                return await interaction.response.send_message(
                    "⏳ Espera un momento...", ephemeral=True
                )
            if self.revelado[idx]:
                return await interaction.response.send_message(
                    "✅ Esta casilla ya está descubierta.", ephemeral=True
                )
            if idx in self.seleccion:
                return await interaction.response.send_message(
                    "❌ Ya seleccionaste esta casilla.", ephemeral=True
                )

            self.seleccion.append(idx)

            # ── Mostrar casilla seleccionada temporalmente ────────
            self._build_buttons()
            # Forzar visible la selección actual
            for item in self.children:
                cid = int(item.custom_id.split("_")[1])
                if cid in self.seleccion:
                    item.label = self.tablero[cid]
                    item.style = discord.ButtonStyle.primary

            await interaction.response.edit_message(
                embed=self._build_embed(), view=self
            )

            # ── Evaluar par cuando hay 2 seleccionadas ────────────
            if len(self.seleccion) == 2:
                self.bloqueado = True
                i1, i2 = self.seleccion

                if self.tablero[i1] == self.tablero[i2]:
                    # ✅ Par correcto
                    self.revelado[i1] = True
                    self.revelado[i2] = True
                    self.pares_ok += 1
                    self.racha += 1
                    if self.racha >= 2 and self.racha % 2 == 0 and self.intentos_fail > 0:
                        self.intentos_fail -= 1
                    self.seleccion = []
                    self._build_buttons()

                    if self.pares_ok == 8:
                        if self._terminado:
                            return
                        self._terminado = True
                        self.bloqueado  = True
                        # 🏆 Ganó — devuelve la apuesta + premio total
                        recompensa_total = self.monto * 3
                        ganancia_neta = self.monto * 2
                        await update_balance(self.author.id, recompensa_total)
                        embed = self._build_embed(
                            estado=(
                                f"🏆 ¡Ganaste! Recibes **+{recompensa_total}** {COIN} en total "
                                f"({ganancia_neta} de ganancia)."
                            ),
                            thumbnail_url=MEMO_WIN_THUMBNAIL,
                        )
                        self.stop()
                        self._deshabilitar_todo()
                        try:
                            await interaction.edit_original_response(embed=embed, view=self)
                        except Exception:
                            pass
                        await asyncio.sleep(AUTO_DELETE)
                        try:
                            await self.message.delete()
                        except Exception:
                            pass
                        _active_memo.discard(self.author.id)
                        return

                    self.bloqueado = False
                    try:
                        await interaction.edit_original_response(
                            embed=self._build_embed(), view=self
                        )
                    except Exception:
                        pass

                else:
                    # ❌ Par incorrecto
                    self.intentos_fail += 1
                    self.racha = 0
                    intentos_restantes = MAX_INTENTOS - self.intentos_fail

                    if intentos_restantes <= 0:
                        if self._terminado:
                            self.bloqueado = False
                            return
                        self._terminado = True
                        # 💀 Perdió — apuesta ya descontada al iniciar
                        self.revelado = [True] * 16   # revelar todo
                        self._build_buttons()
                        self._deshabilitar_todo()
                        embed = self._build_embed(
                            estado=f"💀 ¡Perdiste! Se descuentan **-{self.monto}** {COIN}",
                            thumbnail_url=MEMO_LOSS_THUMBNAIL,
                        )
                        self.stop()
                        try:
                            await interaction.edit_original_response(embed=embed, view=self)
                        except Exception:
                            pass
                        await asyncio.sleep(AUTO_DELETE)
                        try:
                            await self.message.delete()
                        except Exception:
                            pass
                        _active_memo.discard(self.author.id)
                        return

                    # Mostrar las dos incorrectas 1.5s y luego ocultarlas
                    await asyncio.sleep(1.5)
                    self.seleccion = []
                    self.bloqueado = False
                    self._build_buttons()
                    try:
                        await interaction.edit_original_response(
                            embed=self._build_embed(), view=self
                        )
                    except Exception:
                        pass

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
        _active_memo.discard(self.author.id)
        self._deshabilitar_todo()
        if self.message:
            try:
                await self.message.edit(
                    embed=self._build_embed(estado="⏰ Tiempo agotado. Partida cancelada."),
                    view=self
                )
                await asyncio.sleep(AUTO_DELETE)
                await self.message.delete()
            except Exception:
                pass


# ── COG ────────────────────────────────────────────────
class Memo(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="memo")
    async def memo(self, ctx, monto: int = None):
        import time
        user_id = ctx.author.id
        now     = time.time()

        # ── Cooldown individual por usuario ────────────────────────
        expira_en = _memo_cooldowns.get(user_id, 0)
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

        if monto <= 0:
            return await ctx.send(
                f"❌ {ctx.author.mention} El monto debe ser mayor a 0."
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

        # ── Aplicar cooldown individual ANTES de iniciar partida ──
        _memo_cooldowns[user_id] = now + memo_config["cooldown"]

        # ── Generar tablero aleatorio ──────────────────────────────
        tablero = EMOJIS_PARES * 2
        random.shuffle(tablero)

        await update_balance(user_id, -monto)  # descuenta al iniciar
        _active_memo.add(user_id)

        view = MemoView(ctx.author, monto, tablero)
        msg  = await ctx.send(embed=view._build_embed(), view=view)
        view.message = msg

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
