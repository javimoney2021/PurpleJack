import discord
import random
import time
import asyncio
from discord.ext import commands
from core.database import (
    set_game_cooldown,
    reserve_wager,
    settle_wager,
    lose_wager,
    refund_wager,
)
from core.config import COIN, dados_config
from core import cache

DICE_GIF = "https://pub-a09b3609b6b34dfab5c7aa7742cd1a8a.r2.dev/Purple%20jack%20Harcode/dice.gif"
_ACTIVE_DADOS: set[int] = set()
_DADOS_START_LOCKS: dict[int, asyncio.Lock] = {}
_SEEN_DADOS_MESSAGES: dict[int, float] = {}
DICE_FACES = {1: "1️⃣", 2: "2️⃣", 3: "3️⃣", 4: "4️⃣", 5: "5️⃣", 6: "6️⃣"}


def format_roll(value):
    return DICE_FACES.get(value, str(value))


def format_cooldown(seconds):
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        return f"{seconds // 60}m"
    else:
        return f"{seconds // 3600}h"


def choose_dice_rolls(success: bool):
    """
    Genera dos pares de dados garantizando un resultado decisivo (sin empate).

    Reintenta hasta 50 veces con dados aleatorios; si se agota el límite
    (probabilidad ~10^-17, prácticamente imposible) usa un resultado fijo.
    La probabilidad de éxito/fallo viene configurada en dados_config y se
    aplica ANTES de llamar a esta función: aquí solo aseguramos la
    representación visual coherente con ese resultado.
    """
    for _ in range(50):
        d1, d2 = random.randint(1, 6), random.randint(1, 6)
        b1, b2 = random.randint(1, 6), random.randint(1, 6)
        if success and d1 + d2 > b1 + b2:
            return d1, d2, b1, b2
        if not success and d1 + d2 < b1 + b2:
            return d1, d2, b1, b2
    # Fallback determinista — inalcanzable en la práctica
    return (6, 5, 3, 2) if success else (2, 1, 5, 6)


class DadosRollView(discord.ui.View):
    def __init__(self, author_id: int, monto: int, wager_id: str):
        super().__init__(timeout=60)
        self.author_id = author_id
        self.monto     = monto
        self.wager_id  = wager_id
        self.message   = None
        self.finished  = False
        self.processing = False
        self.resolution_lock = asyncio.Lock()

    async def on_timeout(self):
        async with self.resolution_lock:
            if self.finished or self.processing:
                return
            self.finished = True
            await refund_wager(self.wager_id)
            _ACTIVE_DADOS.discard(self.author_id)
            for child in self.children:
                child.disabled = True
            if self.message:
                try:
                    await self.message.edit(view=self)
                except Exception:
                    pass

    @discord.ui.button(label="🎲 Lanzar Dados", style=discord.ButtonStyle.primary)
    async def lanzar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message(
                "❌ Solo el autor de la apuesta puede lanzar los dados.", ephemeral=True
            )
        if self.finished or self.processing:
            return await interaction.response.send_message(
                "⏳ Esta partida ya se está procesando.",
                ephemeral=True,
            )
        async with self.resolution_lock:
            if self.finished or self.processing:
                return await interaction.response.send_message(
                    "⏳ Esta partida ya se está procesando.",
                    ephemeral=True,
                )
            self.processing = True
            # Al aceptar el clic, se detiene el timeout: ya no puede competir
            # con la liquidación y reembolsar una apuesta en procesamiento.
            self.stop()

            if not dados_config["activa"]:
                self.finished = True
                await refund_wager(self.wager_id)
                _ACTIVE_DADOS.discard(self.author_id)
                return await interaction.response.send_message(
                    "🔧 El sistema de dados está desactivado. Tu apuesta fue reembolsada.",
                    ephemeral=True,
                )

            await interaction.response.defer()

            for child in self.children:
                child.disabled = True

            suspense_embed = discord.Embed(
                title=f"🎲 Lanzando los dados... — {interaction.user.display_name}",
                description="Un momento... el destino está en el aire.",
                color=discord.Color.dark_purple(),
            )
            suspense_embed.set_thumbnail(url=DICE_GIF)
            try:
                await interaction.message.edit(embed=suspense_embed, view=self)
            except Exception:
                pass

            await asyncio.sleep(4)

            # Determinar resultado según probabilidad configurada
            exito = random.random() <= dados_config["exito_prob"]

            # choose_dice_rolls garantiza que no hay empate: el visual siempre
            # coincide con el resultado decidido por la probabilidad
            d1, d2, b1, b2 = choose_dice_rolls(exito)
            autor_suma = d1 + d2
            bot_suma   = b1 + b2

            if exito:
                pago_total = self.monto * 2
                settlement = await settle_wager(self.wager_id, pago_total)
                resultado_text = (
                    f"🎉 ¡Ganaste! Pago total: **{pago_total}** {COIN}; "
                    f"ganancia neta: **+{self.monto}** {COIN}."
                )
                color = discord.Color.green()
            else:
                settlement = await lose_wager(self.wager_id)
                resultado_text = (
                    f"💀 Perdiste tu apuesta inicial de {self.monto} {COIN}."
                )
                color = discord.Color.red()

            if not settlement["ok"]:
                self.finished = True
                _ACTIVE_DADOS.discard(self.author_id)
                try:
                    await interaction.followup.send(
                        "⚠️ Esta partida ya había sido procesada. "
                        "No se realizó ningún cobro ni pago adicional.",
                        ephemeral=True,
                    )
                except Exception:
                    pass
                return

            expira_en = time.time() + dados_config["cooldown"]
            cache.set_game_cooldown_cache(self.author_id, "dados", expira_en)
            await set_game_cooldown(self.author_id, "dados", expira_en)

            self.finished = True
            _ACTIVE_DADOS.discard(self.author_id)

            embed = discord.Embed(
                title=f"🎲 Resultado de Dados — {interaction.user.display_name}",
                description=(
                    f"**Tus dados:** {format_roll(d1)} + {format_roll(d2)} = **{autor_suma}**\n"
                    f"**Dados del bot:** {format_roll(b1)} + {format_roll(b2)} = **{bot_suma}**\n\n"
                    f"{resultado_text}"
                ),
                color=color,
            )
            embed.set_thumbnail(url=DICE_GIF)
            embed.set_footer(
                text=f"Cooldown: {format_cooldown(dados_config['cooldown'])} | "
                     f"Máx apuesta: {dados_config['max_apuesta']} PurpleCoins"
            )

            for child in self.children:
                child.disabled = True

            try:
                await interaction.message.edit(embed=embed, view=self)
                if self.message:
                    asyncio.create_task(self.message.delete(delay=80))
            except Exception:
                pass

    async def on_error(self, interaction, error, item):
        self.finished = True
        await refund_wager(self.wager_id)
        self.stop()
        _ACTIVE_DADOS.discard(self.author_id)
        try:
            await interaction.followup.send(
                "⚠️ La partida se canceló por un error y tu apuesta fue reembolsada.",
                ephemeral=True,
            )
        except Exception:
            pass


class Dados(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="dados")
    async def dados(self, ctx, monto: int = None):
        # Un mismo MessageCreate no debe ejecutarse dos veces dentro de esta
        # instancia. La idempotencia en DB cubre además las otras instancias.
        message_id = ctx.message.id
        if message_id in _SEEN_DADOS_MESSAGES:
            return
        now_monotonic = time.monotonic()
        _SEEN_DADOS_MESSAGES[message_id] = now_monotonic
        if len(_SEEN_DADOS_MESSAGES) > 2_000:
            cutoff = now_monotonic - 600
            for seen_id, seen_at in list(_SEEN_DADOS_MESSAGES.items()):
                if seen_at < cutoff:
                    _SEEN_DADOS_MESSAGES.pop(seen_id, None)

        if monto is None:
            nick = ctx.author.nick or ctx.author.display_name
            return await ctx.reply(
                f"❌ {nick} Usa: `!dados {{monto}}`.",
                mention_author=False,
            )

        if not dados_config["activa"]:
            return await ctx.send(
                "🔧 El sistema de dados está en mantenimiento. Intenta después."
            )

        if monto <= 0:
            return await ctx.send(
                f"❌ {ctx.author.mention} La apuesta debe ser mayor a 0."
            )

        if monto > dados_config["max_apuesta"]:
            return await ctx.send(
                f"❌ {ctx.author.mention} No puedes apostar más de "
                f"**{dados_config['max_apuesta']}** {COIN}."
            )

        start_lock = _DADOS_START_LOCKS.setdefault(
            ctx.author.id,
            asyncio.Lock(),
        )
        async with start_lock:
            if ctx.author.id in _ACTIVE_DADOS:
                return await ctx.send(
                    f"❌ {ctx.author.mention} Ya tienes un juego de Dados en curso. "
                    f"Termina la apuesta actual antes de iniciar otra."
                )

            request_key = f"dados:message:{message_id}"
            wager = await reserve_wager(
                ctx.author.id,
                "dados",
                monto,
                expires_in=120,
                idempotency_key=request_key,
                exclusive_pending=True,
                enforce_cooldown=True,
            )
            if not wager["ok"]:
                reason = wager.get("reason")
                # El mismo MessageCreate pudo llegar a dos instancias durante
                # un despliegue. La segunda no debe responder ni crear ronda.
                if reason == "duplicate_request":
                    return
                if reason == "already_pending":
                    return await ctx.send(
                        f"❌ {ctx.author.mention} Ya tienes un juego de Dados "
                        f"en curso. Termina la apuesta actual antes de iniciar otra."
                    )
                if reason == "cooldown":
                    remaining = max(
                        0,
                        int(wager["expires_at"] - time.time()),
                    )
                    return await ctx.send(
                        f"⏳ {ctx.author.mention} Espera "
                        f"**{remaining // 60}m {remaining % 60}s** "
                        f"antes de volver a apostar."
                    )
                return await ctx.send(
                    f"❌ {ctx.author.mention} No tienes suficiente balance "
                    f"para apostar {monto} {COIN}."
                )

            embed = discord.Embed(
                title=f"🎲 Apuesta de Dados — {ctx.author.display_name}",
                description=(
                    f"{ctx.author.mention} ha apostado **{monto}** {COIN}.\n\n"
                    f"Haz clic en el botón para lanzar tus dados y enfrentarte al bot.\n"
                    f"Chance de éxito: **{int(dados_config['exito_prob'] * 100)}%**."
                ),
                color=discord.Color.blurple(),
            )
            embed.set_thumbnail(url=DICE_GIF)
            embed.set_footer(
                text=f"Cooldown: {format_cooldown(dados_config['cooldown'])} | "
                     f"Máx apuesta: {dados_config['max_apuesta']} PurpleCoins"
            )

            _ACTIVE_DADOS.add(ctx.author.id)
            try:
                view = DadosRollView(ctx.author.id, monto, wager["id"])
                message = await ctx.reply(
                    embed=embed,
                    view=view,
                    mention_author=False,
                )
                view.message = message
            except Exception:
                _ACTIVE_DADOS.discard(ctx.author.id)
                await refund_wager(wager["id"])
                raise


async def setup(bot):
    await bot.add_cog(Dados(bot))
