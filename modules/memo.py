import discord
import asyncio
import random
from discord.ext import commands
from discord import app_commands
from core.database import get_user, update_balance
from core.config import COIN

# ── CONFIG ─────────────────────────────────────────────
MAX_INTENTOS  = 7
AUTO_DELETE   = 20
MAX_APUESTA   = 300
STAFF_ROLE    = "Equipo de Eventos"
HIDDEN_EMOJI  = "🟦"
EMOJIS_PARES  = ["🎲", "🍪", "🍇", "🔪", "💎", "🍼", "👑", "🚀"]

# ── ESTADO GLOBAL ──────────────────────────────────────
_active_memo: set[int] = set()   # {user_id}
_memo_config = {"activa": True}


def is_staff():
    async def predicate(interaction: discord.Interaction):
        role = discord.utils.get(interaction.user.roles, name=STAFF_ROLE)
        if not role:
            await interaction.response.send_message("❌ No tienes permisos para usar este comando.", ephemeral=True)
            return False
        return True
    return app_commands.check(predicate)


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
                    self.bloqueado = False
                    self._build_buttons()

                    if self.pares_ok == 8:
                        # 🏆 Ganó — devuelve la apuesta + premio total
                        recompensa_total = self.monto * 3
                        ganancia_neta = self.monto * 2
                        await update_balance(self.author.id, recompensa_total)
                        embed = self._build_embed(
                            estado=(
                                f"🏆 ¡Ganaste! Recibes **+{recompensa_total}** {COIN} en total "
                                f"({ganancia_neta} de ganancia)."
                            )
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
                        # 💀 Perdió — apuesta ya descontada al iniciar
                        self.revelado = [True] * 16   # revelar todo
                        self._build_buttons()
                        self._deshabilitar_todo()
                        embed = self._build_embed(
                            estado=f"💀 ¡Perdiste! Se descuentan **-{self.monto}** {COIN}"
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

    def _build_embed(self, estado: str = None) -> discord.Embed:
        intentos_restantes = MAX_INTENTOS - self.intentos_fail
        corazones = "❤️" * intentos_restantes + "🖤" * self.intentos_fail

        desc = (
            f"**Pares encontrados:** {self.pares_ok}/8\n"
            f"**Intentos fallidos:** {corazones}\n\n"
        )
        if estado:
            desc += f"\n{estado}"

        embed = discord.Embed(
            title="🧠 Juego de Memoria",
            description=desc,
            color=discord.Color.blurple()
        )
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

    @app_commands.command(name="memo_alternar", description="Activa o desactiva el sistema de memoria")
    @is_staff()
    async def memo_alternar(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)
        _memo_config["activa"] = not _memo_config["activa"]
        estado = "✅ Activado" if _memo_config["activa"] else "🔴 Desactivado"
        await interaction.followup.send(
            f"🧠 Sistema de Memoria: **{estado}**\n"
            f"📌 Apuesta máxima: **{MAX_APUESTA}** {COIN}",
            ephemeral=False,
        )

    @commands.command(name="memo")
    @commands.cooldown(1, 300, commands.BucketType.user)
    async def memo(self, ctx, monto: int = None):
        if monto is None:
            return await ctx.send(
                f"❌ {ctx.author.mention} Formato correcto: `!memo {{monto}}`"
            )
        if not _memo_config["activa"]:
            return await ctx.send("🔧 El sistema de Memo está desactivado. Intenta después.")

        if monto <= 0:
            ctx.command.reset_cooldown(ctx)
            return await ctx.send(
                f"❌ {ctx.author.mention} El monto debe ser mayor a 0."
            )
        if ctx.author.id in _active_memo:
            return await ctx.send(
                f"❌ {ctx.author.mention} Ya tienes una partida activa."
            )
        if monto > MAX_APUESTA:
            ctx.command.reset_cooldown(ctx)
            return await ctx.send(
                f"❌ {ctx.author.mention} La apuesta máxima es **{MAX_APUESTA}** {COIN}."
            )    

        user_data = await get_user(ctx.author.id)
        if user_data["balance"] < monto:
            return await ctx.send(
                f"❌ {ctx.author.mention} No tienes suficiente balance. "
                f"Necesitas **{monto}** {COIN}."
            )

        # ── Generar tablero aleatorio ──────────────────────────────
        tablero = EMOJIS_PARES * 2
        random.shuffle(tablero)

        await update_balance(ctx.author.id, -monto)  # descuenta al iniciar
        _active_memo.add(ctx.author.id)

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
        elif isinstance(error, commands.CommandOnCooldown):
            retry = int(error.retry_after)
            tiempo = f"{retry // 60}m {retry % 60}s" if retry >= 60 else f"{retry}s"
            await ctx.send(
                f"⏳ {ctx.author.mention} Podrás jugar nuevamente en **{tiempo}**.",
                delete_after=10
            )
        else:    
            raise error


async def setup(bot):
    await bot.add_cog(Memo(bot))
