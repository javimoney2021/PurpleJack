"""Panel reutilizable de registros de eventos para discord.py 2.x.

Configura los valores de esta seccion antes de cargar el archivo como extension.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands


# ---------------------------------------------------------------------------
# CONFIGURACION DEL NUEVO BOT
# ---------------------------------------------------------------------------
STAFF_ROLE_NAME = "Staff"

# Reemplaza 0 por el ID del rol que recibiran los usuarios al registrarse.
# Dejalo en 0 para no asignar ningun rol.
ROL_REGISTRO_ID = 0

# IMPORTANTE: Reemplaza 0 por el ID del canal donde se enviaran las
# inscripciones del nuevo bot. Dejalo en 0 para no reenviar los datos.
CANAL_INSCRIPCIONES_ID = 0


logger = logging.getLogger(__name__)

# Estado en RAM: un panel activo por canal durante la sesion actual del bot.
_panel_activo: dict[int, tuple[discord.Message, str]] = {}


class RegistroModal(discord.ui.Modal, title="Registro de Evento"):
    nickname = discord.ui.TextInput(
        label="Nickname",
        placeholder="Tu nickname en el juego...",
        min_length=2,
        max_length=32,
        required=True,
    )
    id_espacial = discord.ui.TextInput(
        label="ID Espacial",
        placeholder="Tu ID Espacial...",
        min_length=2,
        max_length=30,
        required=True,
    )

    def __init__(self, bot: commands.Bot, nombre_evento: str):
        super().__init__()
        self.bot = bot
        self.nombre_evento = nombre_evento

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        if interaction.guild and ROL_REGISTRO_ID:
            rol = interaction.guild.get_role(ROL_REGISTRO_ID)
            if rol:
                try:
                    await interaction.user.add_roles(
                        rol,
                        reason=f"Registro: {self.nombre_evento}",
                    )
                except discord.Forbidden:
                    logger.warning("No se pudo asignar el rol de registro a %s.", interaction.user.id)

        await self._reenviar_inscripcion(interaction)
        await interaction.followup.send(
            "Registro exitoso. Nos vemos a la hora del evento.",
            ephemeral=True,
        )

    async def _reenviar_inscripcion(self, interaction: discord.Interaction):
        if not CANAL_INSCRIPCIONES_ID:
            return

        canal = self.bot.get_channel(CANAL_INSCRIPCIONES_ID)
        if canal is None:
            try:
                canal = await self.bot.fetch_channel(CANAL_INSCRIPCIONES_ID)
            except discord.HTTPException:
                logger.warning("No se encontro el canal de inscripciones configurado.")
                return

        embed = discord.Embed(
            title=f"Nuevo Registro - {self.nombre_evento}",
            color=discord.Color.gold(),
        )
        embed.add_field(name="Discord User", value=interaction.user.mention, inline=False)
        embed.add_field(name="Nickname", value=self.nickname.value, inline=False)
        embed.add_field(name="ID Espacial", value=self.id_espacial.value, inline=False)
        embed.set_thumbnail(url=interaction.user.display_avatar.url)

        try:
            await canal.send(embed=embed)
        except discord.HTTPException:
            logger.exception("No se pudo reenviar una inscripcion de evento.")


class RegistroView(discord.ui.View):
    def __init__(self, bot: commands.Bot, nombre_evento: str, abierto: bool = True):
        super().__init__(timeout=None)
        self.bot = bot
        self.nombre_evento = nombre_evento
        self.registrarse.disabled = not abierto
        self.registrarse.style = (
            discord.ButtonStyle.primary if abierto else discord.ButtonStyle.secondary
        )

    @discord.ui.button(
        label="Registrarse",
        style=discord.ButtonStyle.primary,
        custom_id="registro_evento:registrarse",
    )
    async def registrarse(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RegistroModal(self.bot, self.nombre_evento))


def embed_abierto(nombre_evento: str) -> discord.Embed:
    embed = discord.Embed(
        title=f"{nombre_evento} - Registro Abierto",
        description=(
            "El evento esta por comenzar.\n\n"
            "Presiona el boton **Registrarse** para apartar tu lugar.\n"
            "Se te pedira tu **Nickname** y tu **ID Espacial**."
        ),
        color=discord.Color.blurple(),
    )
    embed.set_footer(text="Cada usuario puede registrarse una sola vez.")
    return embed


def embed_cerrado(nombre_evento: str) -> discord.Embed:
    return discord.Embed(
        title=f"{nombre_evento} - Registro Cerrado",
        description="**Registro Cerrado, El evento esta por iniciar...!**",
        color=discord.Color.dark_gray(),
    )


class RegistroEventos(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="abrir_registro",
        description="Publica el panel de registro del evento en este canal.",
    )
    @app_commands.describe(nombre_evento="Nombre del evento que aparecera en el embed")
    @app_commands.checks.has_role(STAFF_ROLE_NAME)
    async def abrir_registro(self, interaction: discord.Interaction, nombre_evento: str):
        if interaction.channel_id in _panel_activo:
            return await interaction.response.send_message(
                "Ya hay un panel activo en este canal. Usa /cerrar_registro primero.",
                ephemeral=True,
            )
        if interaction.channel is None:
            return await interaction.response.send_message(
                "No se pudo identificar el canal actual.",
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True)
        view = RegistroView(self.bot, nombre_evento, abierto=True)
        mensaje = await interaction.channel.send(
            embed=embed_abierto(nombre_evento),
            view=view,
        )
        _panel_activo[interaction.channel_id] = (mensaje, nombre_evento)

        await interaction.followup.send(
            f"Panel de registro **{nombre_evento}** publicado.",
            ephemeral=True,
        )

    @app_commands.command(
        name="cerrar_registro",
        description="Cierra el registro del evento activo en este canal.",
    )
    @app_commands.checks.has_role(STAFF_ROLE_NAME)
    async def cerrar_registro(self, interaction: discord.Interaction):
        entry = _panel_activo.get(interaction.channel_id)
        if not entry:
            return await interaction.response.send_message(
                "No hay un panel activo en este canal.",
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True)
        mensaje, nombre_evento = entry
        vista = RegistroView(self.bot, nombre_evento, abierto=False)
        try:
            await mensaje.edit(embed=embed_cerrado(nombre_evento), view=vista)
        except discord.NotFound:
            pass

        _panel_activo.pop(interaction.channel_id, None)
        await interaction.followup.send(
            f"Registro de **{nombre_evento}** cerrado.",
            ephemeral=True,
        )

    @abrir_registro.error
    @cerrar_registro.error
    async def sin_permisos(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingRole):
            await interaction.response.send_message(
                "No tienes permisos para usar este comando.",
                ephemeral=True,
            )
        else:
            raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(RegistroEventos(bot))
