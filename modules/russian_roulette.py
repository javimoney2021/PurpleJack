import asyncio
import random
import time

import discord
from discord.ext import commands

from core.config import COIN, rr_config
from core.database import get_user, update_balance
from core import cache

WAIT_IMAGE = "https://raw.githubusercontent.com/javimoney2021/PurpleJack/main/Thumbs/Shot.png"
SUCCESS_IMAGE = "https://raw.githubusercontent.com/javimoney2021/PurpleJack/main/Thumbs/Salvado.png"
FAILURE_IMAGE = "https://raw.githubusercontent.com/javimoney2021/PurpleJack/main/Thumbs/Derrota.png"
END_IMAGE = "https://raw.githubusercontent.com/javimoney2021/PurpleJack/main/Thumbs/Victoria.png"


ROUND_REWARDS = [0.6, 0.8, 1.0, 1.5, 2.0]
ROUND_LABELS = ["1º ronda", "2º ronda", "3º ronda", "4º ronda", "5º ronda"]

rr_games = {}
# cooldowns manejados via cache + DB


def format_percent(value):
    return int(value * 100)


def build_rr_embed(user: discord.Member, game, state: str, description: str, thumbnail: str):
    embed = discord.Embed(
        title=f"**RULETA RUSA - {user.display_name}**",
        description=description,
        color=discord.Color.purple() if state != "lost" else discord.Color.red()
    )

    embed.add_field(name="Apuesta", value=f"{game.apuesta} {COIN}", inline=True)
    if state == "lost":
        embed.add_field(name="Rondas completadas", value=f"{game.round}/5", inline=True)
        embed.add_field(name="Ganancia final", value=f"0 {COIN}", inline=False)
    elif game.active:
        embed.add_field(name="Ronda", value=f"{game.round + 1}/5", inline=True)
        embed.add_field(name="Ganancia provisional", value=f"{game.ganancia} {COIN}", inline=False)
    else:
        embed.add_field(name="Rondas completadas", value=f"{game.round}/5", inline=True)
        embed.add_field(name="Ganancia final", value=f"{game.ganancia} {COIN}", inline=False)

    embed.set_thumbnail(url=thumbnail)
    return embed


class RRGameState:
    def __init__(self, user_id: int, apuesta: int, author_name: str):
        self.user_id = user_id
        self.apuesta = apuesta
        self.round = 0
        self.ganancia = 0
        self.active = True
        self.finished = False
        self.message = None
        self.author_name = author_name


class RRView(discord.ui.View):
    def __init__(self, game: RRGameState, author_id: int):
        super().__init__(timeout=150)
        self.game = game
        self.author_id = author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ No es tu partida de Ruleta Rusa.", ephemeral=True)
            return False

        if not self.game.active:
            await interaction.response.send_message(
                "La Ruleta Rusa expiró por inactividad. Crea otra.", ephemeral=True
            )
            return False

        return True

    async def on_timeout(self) -> None:
        if self.game.finished:
            return
        self.game.active = False
        for item in self.children:
            item.disabled = True
        if self.game.message:
            timeout_embed = discord.Embed(
                title=f"**RULETA RUSA - {self.game.author_name}**",
                description=(
                    "⏳ La Ruleta Rusa expiró por inactividad. "
                    "Crea otra para intentarlo de nuevo."
                ),
                color=discord.Color.dark_grey()
            )
            timeout_embed.set_thumbnail(url=FAILURE_IMAGE)
            try:
                await self.game.message.edit(embed=timeout_embed, view=self)
            except Exception:
                pass

    @discord.ui.button(label="Disparar", style=discord.ButtonStyle.danger, row=0)
    async def disparar(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            preparing_embed = build_rr_embed(
                interaction.user,
                self.game,
                state="waiting",
                description=(
                    f"Preparando disparo... \n\n"
                    f"Ronda actual: **{self.game.round + 1}/5**\n"
                    f"Esperando resultado..."
                ),
                thumbnail=WAIT_IMAGE
            )
            
            # Crear vista deshabilitada para la espera
            disabled_view = RRView(self.game, self.author_id)
            for item in disabled_view.children:
                item.disabled = True
            
            await interaction.response.edit_message(embed=preparing_embed, view=disabled_view)

            await asyncio.sleep(5)

            if not self.game.active:
                return

            success = random.random() <= rr_config["ganar_prob"]
            if success:
                self.game.round += 1
                reward = int(round(self.game.apuesta * ROUND_REWARDS[self.game.round - 1]))
                self.game.ganancia += reward

                if self.game.round >= 5:
                    self.game.active = False
                    self.game.finished = True
                    total_embed = build_rr_embed(
                        interaction.user,
                        self.game,
                        state="victory",
                        description=(
                            f"💥 **Victoria Total**! Completaste las 5 fases de la Ruleta Rusa, **Suerte** es tu segundo nombre!.\n\n"
                            f"Has acumulado **{self.game.ganancia} {COIN}** Dinero enviado a tu balance."
                        ),
                        thumbnail=END_IMAGE
                    )
                    final_view = RRView(self.game, self.author_id)
                    for item in final_view.children:
                        item.disabled = True
                    await update_balance(self.game.user_id, self.game.ganancia)
                    await interaction.edit_original_response(embed=total_embed, view=final_view)
                    rr_games.pop(self.game.user_id, None)
                    return

                reward_percent = format_percent(sum(ROUND_REWARDS[: self.game.round]))

                # Mensaje especial según la ronda siguiente
                if self.game.round == 3:
                    aviso = (
                        f"\n\n⚠️ **¡Atención!** Las rondas 4 y 5 comprometen más que tu Apuesta Inicial.\n\n"
                        f"Derrota en ronda 4 → pierdes **{int(self.game.apuesta * 1.8)} {COIN}**\n"
                        f"Derrota en ronda 5 → pierdes **{int(self.game.apuesta * 2.0)} {COIN}**\n\n"
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
                        f"Puedes reclamar ya o arriesgarte al siguiente disparo.\n\n"
                        f"🔸 Total posible si completas la ronda actual: **{reward_percent}%**"
                        f"{aviso}"
                    ),
                    thumbnail=SUCCESS_IMAGE
                )
                active_view = RRView(self.game, self.author_id)
                await interaction.edit_original_response(embed=result_embed, view=active_view)
                return

            self.game.active = False
            self.game.finished = True

            # Pérdida según ronda: ronda 4 = +30%, ronda 5 = +50%
            ronda_actual = self.game.round + 1
            if ronda_actual == 4:
                perdida = int(self.game.apuesta * 1.3)
                extra_txt = f"⚠️ Ronda 4 — Perdiste **{perdida} {COIN}** (apuesta +30%)"
            elif ronda_actual == 5:
                perdida = int(self.game.apuesta * 1.5)
                extra_txt = f"⚠️ Ronda 5 — Perdiste **{perdida} {COIN}** (apuesta +50%)"
            else:
                perdida = self.game.apuesta
                extra_txt = f"Perdiste tu apuesta inicial de **{perdida} {COIN}**"

            loss_embed = build_rr_embed(
                interaction.user,
                self.game,
                state="lost",
                description=(
                    f"💥 ¡Bala inoportuna! {extra_txt}.\n\n"
                    f"Mejor suerte la próxima vez."
                ),
                thumbnail=FAILURE_IMAGE
            )
            loss_view = RRView(self.game, self.author_id)
            for item in loss_view.children:
                item.disabled = True
            await update_balance(self.game.user_id, -perdida)
            await interaction.edit_original_response(embed=loss_embed, view=loss_view)
            rr_games.pop(self.game.user_id, None)
        except Exception as e:
            print(f"Error en disparar: {e}")
            try:
                await interaction.response.send_message(f"❌ Error al procesar: {e}", ephemeral=True)
            except Exception:
                pass


    @discord.ui.button(label="Reclamar", style=discord.ButtonStyle.success, row=0)
    async def reclamar(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            if self.game.round == 0:
                return await interaction.response.send_message(
                    "❌ Debes sobrevivir al menos a un disparo para reclamar.", ephemeral=True
                )

            self.game.active = False
            self.game.finished = True

            claim_embed = build_rr_embed(
                interaction.user,
                self.game,
                state="claimed",
                description=(
                    f"🟢 Has reclamado **{self.game.ganancia} {COIN}** al balance.\n\n"
                    f"Gracias por jugar Ruleta Rusa."
                ),
                thumbnail=END_IMAGE
            )
            claim_view = RRView(self.game, self.author_id)
            for item in claim_view.children:
                item.disabled = True
            await update_balance(self.game.user_id, self.game.ganancia)
            await interaction.response.edit_message(embed=claim_embed, view=claim_view)
            rr_games.pop(self.game.user_id, None)
        except Exception as e:
            print(f"Error en reclamar: {e}")
            try:
                await interaction.response.send_message(f"❌ Error al procesar: {e}", ephemeral=True)
            except Exception:
                pass


class RussianRoulette(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="rr")
    async def rr(self, ctx, monto: int = None):
        if not rr_config["activa"]:
            return await ctx.send("🔧 La Ruleta Rusa se encuentra desactivada.")

        if monto is None or monto <= 0:
            return await ctx.send(
                f"❌ {ctx.author.mention} Formato correcto: `!rr {{monto}}`"
            )

        if monto > rr_config["max_apuesta"]:
            return await ctx.send(
                f"❌ {ctx.author.mention} No puedes apostar más de **{rr_config['max_apuesta']} {COIN}**."
            )

        user = await get_user(ctx.author.id)
        if monto > user["balance"]:
            return await ctx.send(
                f"❌ {ctx.author.mention} No tienes suficiente balance para esta apuesta."
            )

        if ctx.author.id in rr_games:
            return await ctx.send(
                f"❌ {ctx.author.mention} Ya tienes una partida activa de Ruleta Rusa."
            )

        now = time.time()
        expira_en = cache.get_game_cooldown_cache(ctx.author.id, "rr")
        if expira_en == 0:
            from core.database import get_game_cooldown
            expira_en = await get_game_cooldown(ctx.author.id, "rr")
            if expira_en:
                cache.set_game_cooldown_cache(ctx.author.id, "rr", expira_en)

        if expira_en > now:
            return await ctx.send(
                f"⏳ {ctx.author.mention} Espera <t:{int(expira_en)}:R> para volver a jugar Ruleta Rusa."
            )

        expira_en = now + rr_config["cooldown"]
        cache.set_game_cooldown_cache(ctx.author.id, "rr", expira_en)
        from core.database import set_game_cooldown
        await set_game_cooldown(ctx.author.id, "rr", expira_en)
        game = RRGameState(ctx.author.id, monto, ctx.author.display_name)
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
            thumbnail=WAIT_IMAGE
        )

        view = RRView(game, ctx.author.id)
        message = await ctx.send(embed=initial_embed, view=view)
        game.message = message


async def setup(bot):
    await bot.add_cog(RussianRoulette(bot))
