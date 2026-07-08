import discord
from discord import app_commands
from discord.ext import commands
from core.config import STAFF_ROLE

# ── CONFIG ─────────────────────────────────────────────
ROL_REGISTRO_ID   = 1507215915396239410
CANAL_STAFF_ID    = 1523367841405993120
CANAL_VOZ_ID      = 1507791449985646723

# ── ESTADO GLOBAL (RAM) ────────────────────────────────
_panel_activo: dict[int, discord.Message] = {}
_registro_abierto: dict[int, bool] = {}


# ── MODAL ──────────────────────────────────────────────
class RegistroModal(discord.ui.Modal, title="Registro de Evento"):

    nickname = discord.ui.TextInput(
        label="Nickname",
        placeholder="Tu nickname en el juego...",
        min_length=2,
        max_length=32,
        required=True
    )

    id_espacial = discord.ui.TextInput(
        label="ID Espacial",
        placeholder="Tu ID Espacial...",
        min_length=2,
        max_length=30,
        required=True
    )

    def __init__(self, bot: commands.Bot, nombre_evento: str):
        super().__init__()
        self.bot = bot
        self.nombre_evento = nombre_evento

    async def on_submit(self, interaction: discord.Interaction):
        # ── Asignar rol ────────────────────────────────
        rol = interaction.guild.get_role(ROL_REGISTRO_ID)
        if rol:
            try:
                await interaction.user.add_roles(rol, reason=f"Registro {self.nombre_evento}")
            except discord.Forbidden:
                pass

        # ── Mensaje efímero al usuario ─────────────────
        await interaction.response.send_message(
            "🎖️ **¡Registro Exitoso!** Nos vemos a la hora del evento 🤝",
            ephemeral=True
        )

        # ── Reenviar datos al canal de staff (RAM, sin DB) ─────────
        canal_staff = self.bot.get_channel(CANAL_STAFF_ID)
        if canal_staff:
            embed = discord.Embed(
                title=f"✨ Nuevo Registro — {self.nombre_evento}",
                color=discord.Color.gold()
            )
            embed.add_field(name="Discord User", value=interaction.user.mention, inline=False)
            embed.add_field(name="Nickname",     value=self.nickname.value,      inline=False)
            embed.add_field(name="ID Espacial",  value=self.id_espacial.value,   inline=False)
            embed.set_thumbnail(url=interaction.user.display_avatar.url)
            await canal_staff.send(embed=embed)


# ── VIEW PANEL ─────────────────────────────────────────
class RegistroView(discord.ui.View):
    def __init__(self, bot: commands.Bot, nombre_evento: str, abierto: bool = True):
        super().__init__(timeout=None)
        self.bot = bot
        self.nombre_evento = nombre_evento
        self._set_estado(abierto)

    def _set_estado(self, abierto: bool):
        self.registrarse.disabled = not abierto
        self.registrarse.style = (
            discord.ButtonStyle.primary if abierto
            else discord.ButtonStyle.secondary
        )

    @discord.ui.button(label="Registrarse", emoji="✍️",
                       style=discord.ButtonStyle.primary,
                       custom_id="registro_evento:btn")
    async def registrarse(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(
            RegistroModal(self.bot, self.nombre_evento)
        )


# ── EMBEDS ─────────────────────────────────────────────
def embed_abierto(nombre_evento: str) -> discord.Embed:
    embed = discord.Embed(
        title=f"{nombre_evento} — Registro Abierto",
        description=(
            "¡El evento está por comenzar!\n\n"
            "Presiona el botón **Registrarse** para apartar tu lugar.\n"
            "Se te pedirá tu **Nickname** y tu **ID Espacial**."
        ),
        color=discord.Color.blurple()
    )
    embed.set_footer(text="Cada usuario puede registrarse una sola vez.")
    return embed


def embed_cerrado(nombre_evento: str) -> discord.Embed:
    embed = discord.Embed(
        title=f"{nombre_evento} — Registro Cerrado",
        description="**Registro Cerrado, El evento está por iniciar…!**",
        color=discord.Color.dark_gray()
    )
    return embed


# ── COG ────────────────────────────────────────────────
class RegistroRuleta(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="abrir_registro",
        description="Publica el panel de registro del evento en este canal."
    )
    @app_commands.describe(nombre_evento="Nombre del evento que aparecerá en el embed")
    @app_commands.checks.has_role(STAFF_ROLE)
    async def abrir_registro(self, interaction: discord.Interaction, nombre_evento: str):
        if interaction.channel_id in _panel_activo:
            return await interaction.response.send_message(
                "❌ Ya hay un panel activo en este canal. Usa `/cerrar_registro` primero.",
                ephemeral=True
            )

        view = RegistroView(self.bot, nombre_evento, abierto=True)
        msg  = await interaction.channel.send(embed=embed_abierto(nombre_evento), view=view)

        _panel_activo[interaction.channel_id]    = (msg, nombre_evento)
        _registro_abierto[interaction.channel_id] = True

        await interaction.response.send_message(
            f"✅ Panel de registro **{nombre_evento}** publicado.", ephemeral=True
        )

    @app_commands.command(
        name="cerrar_registro",
        description="Cierra el registro del evento activo en este canal."
    )
    @app_commands.checks.has_role(STAFF_ROLE)
    async def cerrar_registro(self, interaction: discord.Interaction):
        entry = _panel_activo.get(interaction.channel_id)
        if not entry:
            return await interaction.response.send_message(
                "❌ No hay un panel activo en este canal.", ephemeral=True
            )

        msg, nombre_evento = entry
        view = RegistroView(self.bot, nombre_evento, abierto=False)
        try:
            await msg.edit(embed=embed_cerrado(nombre_evento), view=view)
        except discord.NotFound:
            pass

        _panel_activo.pop(interaction.channel_id, None)
        _registro_abierto.pop(interaction.channel_id, None)

        await interaction.response.send_message(
            f"🔒 Registro de **{nombre_evento}** cerrado.", ephemeral=True
        )

    @abrir_registro.error
    @cerrar_registro.error
    async def sin_permisos(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.MissingRole):
            await interaction.response.send_message(
                "❌ No tienes permisos para usar este comando.", ephemeral=True
            )
        else:
            raise error


async def setup(bot: commands.Bot):
    await bot.add_cog(RegistroRuleta(bot))
