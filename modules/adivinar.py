import logging
import unicodedata
from dataclasses import dataclass

import discord
from discord import app_commands
from discord.ext import commands

from core.config import COORDINADOR_ROLE_ID, STAFF_ROLE_ID


logger = logging.getLogger("purplejack.adivinar")


def normalize_text(text: str) -> str:
    """Compara palabras sin distinguir mayusculas, tildes ni espacios extremos."""
    normalized = unicodedata.normalize("NFD", text.lower().strip())
    return "".join(char for char in normalized if unicodedata.category(char) != "Mn")


@dataclass
class PendingSetup:
    destination_channel_id: int
    origin_channel_id: int
    word: str
    original_word: str
    step: int = 1
    start_announcement: str | None = None


@dataclass
class ActiveGuess:
    word: str
    original_word: str
    winner_announcement: str


class Adivinar(commands.Cog):
    """Dinamica de adivinar una palabra, con una partida activa por canal."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.pending_setups: dict[tuple[int, int], PendingSetup] = {}
        self.active_guesses: dict[tuple[int, int], ActiveGuess] = {}

    @app_commands.command(
        name="adivinar",
        description="Configura una dinamica para adivinar una palabra.",
    )
    @app_commands.describe(
        palabra="La palabra que deben escribir",
        canal="Canal donde se publicara y se adivinara la palabra",
    )
    @app_commands.checks.has_any_role(STAFF_ROLE_ID, COORDINADOR_ROLE_ID)
    async def adivinar(
        self,
        interaction: discord.Interaction,
        palabra: str,
        canal: discord.TextChannel,
    ):
        if not interaction.guild_id or not interaction.channel_id:
            return await interaction.response.send_message(
                "Este comando solo puede usarse dentro de un servidor.",
                ephemeral=True,
            )

        active_key = (interaction.guild_id, canal.id)
        if active_key in self.active_guesses:
            return await interaction.response.send_message(
                "Ya hay una dinamica activa en ese canal.",
                ephemeral=True,
            )

        key = (interaction.guild_id, interaction.user.id)
        self.pending_setups[key] = PendingSetup(
            destination_channel_id=canal.id,
            origin_channel_id=interaction.channel_id,
            word=normalize_text(palabra),
            original_word=palabra.strip(),
        )

        await interaction.response.send_message(
            "Dinamica iniciada (Paso 1/2)\n"
            f"Palabra: `{palabra.strip()}` | Canal: {canal.mention}\n\n"
            "Escribe el anuncio de **Inicio** de la dinamica.",
            ephemeral=True,
        )

    @adivinar.error
    async def adivinar_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingAnyRole):
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "No tienes permisos para usar este comando.",
                    ephemeral=True,
                )
            return
        raise error

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return

        if await self._handle_setup_message(message):
            return

        await self._handle_guess_message(message)

    async def _handle_setup_message(self, message: discord.Message) -> bool:
        key = (message.guild.id, message.author.id)
        setup = self.pending_setups.get(key)
        if setup is None or setup.origin_channel_id != message.channel.id:
            return False

        # Nunca guardar comandos de prefijo como textos de configuracion.
        prefixes = await self.bot.get_prefix(message)
        if isinstance(prefixes, str):
            prefixes = [prefixes]
        if any(message.content.startswith(prefix) for prefix in prefixes):
            return True

        if setup.step == 1:
            setup.start_announcement = message.content
            setup.step = 2
            await message.reply(
                "Anuncio de Inicio guardado.\n\n"
                "Escribe el Anuncio de Ganadores. Usa `{word}` para la palabra y "
                "`{winner}` para mencionar al ganador.",
            )
            return True

        self.pending_setups.pop(key, None)
        channel = message.guild.get_channel(setup.destination_channel_id)
        if not isinstance(channel, discord.TextChannel):
            await message.reply(
                "No pude encontrar el canal seleccionado. Vuelve a iniciar la dinamica."
            )
            return True

        active_key = (message.guild.id, channel.id)
        if active_key in self.active_guesses:
            await message.reply("Ya hay una dinamica activa en ese canal.")
            return True

        self.active_guesses[active_key] = ActiveGuess(
            word=setup.word,
            original_word=setup.original_word,
            winner_announcement=message.content,
        )
        try:
            await channel.send(setup.start_announcement or "")
        except discord.HTTPException:
            self.active_guesses.pop(active_key, None)
            logger.exception("No se pudo publicar el inicio de la dinamica.")
            await message.reply("No pude publicar el anuncio en el canal seleccionado.")
            return True

        await message.reply(
            f"Dinamica activada. El anuncio de inicio fue enviado a {channel.mention}."
        )
        return True

    async def _handle_guess_message(self, message: discord.Message):
        active_key = (message.guild.id, message.channel.id)
        guess = self.active_guesses.get(active_key)
        if guess is None or normalize_text(message.content) != guess.word:
            return

        self.active_guesses.pop(active_key, None)
        announcement = (
            guess.winner_announcement
            .replace("{winner}", message.author.mention)
            .replace("{word}", guess.original_word)
        )
        await message.channel.send(announcement)


async def setup(bot: commands.Bot):
    await bot.add_cog(Adivinar(bot))
