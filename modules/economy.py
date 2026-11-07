# Deployment refresh: 2026-08-17
from discord.ext import commands
from discord import app_commands, ui, ButtonStyle, Interaction
import discord
import asyncio
import logging
import time

from core.database import (
    get_user,
    get_user_deletion_blocker,
    get_user_temporary_roles,
    purge_user_data,
    update_balance,
    update_bank,
)
from core import cache
from core.config import (
    COIN, game_config, ruleta_config, rob_config, dados_config,
    memo_config, blackjack_config, COORDINADOR_ROLE_ID,
)
from core.cache import MAX_BANK
from core.rankings import get_balance_ranking
from core.halloween import CANDY, get_candy_balance

TOP_COOLDOWN = 300
NAVE_SUPPORT_URL = "https://canary.discord.com/channels/980073134411644939/1399742637426081913"
NAVE_DATA_CHANNEL_ID = 1505296434076057752
NAVE_BUGS_CHANNEL_ID = 1534744435814826205
DATA_DELETION_LOG_CHANNEL_ID = 1502119147046305962

logger = logging.getLogger(__name__)

NAVE_INFO_DESCRIPTION = (
    "PurpleJack es una experiencia de economía y entretenimiento donde puedes "
    "trabajar, competir, jugar, coleccionar artículos y progresar junto a la comunidad.\n\n"
    "Comienza con `/consultar` para ver tus recursos, `/trabajar` para elegir "
    "un empleo o `/jugar` para abrir los juegos disponibles.\n\n"
    "Antes de comenzar, consulta nuestros "
    "[Términos de Servicio](https://purplejack.online/terms.html) y nuestra "
    "[Política de Privacidad](https://purplejack.online/privacy.html) para conocer "
    "las reglas y el tratamiento de tus datos. Los registros operativos publicados "
    "por PurpleJack en sus canales de control se eliminan después de 24 horas.\n\n"
    "Para dudas o reportes de errores, visita nuestro "
    f"[#soporte]({NAVE_SUPPORT_URL})."
)

NAVE_COMMAND_PAGES = (
    (
        "💰 Economía y consultas",
        "Consulta tus recursos y utiliza las funciones económicas de la Nave.",
        (
            "/consultar   Panel privado: Balance, Inventario, CD,\n"
            "             Top, Time e Info.\n"
            "/work       Realiza una tarea y gana monedas.\n"
            "/crime      Intenta un crimen económico.\n"
            "/collect    Cobra las recompensas de tus roles.\n"
            "/tienda     Abre la tienda de artículos."
        ),
    ),
    (
        "🧰 Empleos",
        "Gestiona tu empleo desde un único panel privado.",
        (
            "/trabajar  Abre el centro laboral.\n"
            "  Trabajar  Inicia la jornada de tu empleo.\n"
            "  Aplicar   Solicita un empleo.\n"
            "  Renunciar Deja tu empleo actual.\n"
            "  Oficina   Accede a empleos de maestría.\n"
            "  Empleos   Muestra los trabajos disponibles.\n"
            "  XP laboral Consulta tu progreso laboral."
        ),
    ),
    (
        "🎮 Juegos y competición",
        "Abre juegos, carreras y desafíos desde comandos slash.",
        (
            "/jugar {apuesta}             Ruleta, Dados, BJ y Memo.\n"
            "/carrera {apuesta}           Convoca una carrera.\n"
            "/retar {usuario} {apuesta}   Desafía a un jugador.\n"
            "/rob                        Elige un objetivo del Top 10 para robar."
        ),
    ),
)


def _build_nave_inicio_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🚀 PurpleJack - Info",
        description=NAVE_INFO_DESCRIPTION,
        color=discord.Color.purple(),
    )
    embed.set_footer(text="Selecciona una categoría para explorar PurpleJack.")
    return embed


def _build_nave_commands_embed(page: int) -> discord.Embed:
    title, description, commands_text = NAVE_COMMAND_PAGES[page]
    embed = discord.Embed(
        title=f"📚 PurpleJack - Comandos | {title}",
        description=description,
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Comandos disponibles", value=f"```text\n{commands_text}\n```", inline=False)
    embed.set_footer(text=f"Página {page + 1}/{len(NAVE_COMMAND_PAGES)} · Comandos de usuario: /")
    return embed


def _build_nave_support_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🛟 PurpleJack - Soporte",
        description=(
            "¿Tienes una duda o necesitas asistencia? Dirígete al canal oficial "
            f"[#soporte]({NAVE_SUPPORT_URL}) y selecciona la **Opción 3**.\n\n"
            f"Para reportar bugs, utiliza <#{NAVE_BUGS_CHANNEL_ID}>."
        ),
        color=discord.Color.teal(),
    )
    embed.set_footer(text="Nunca compartas contraseñas, tokens ni credenciales.")
    return embed


def _build_nave_unsubscribe_embed() -> discord.Embed:
    embed = discord.Embed(
        title="❌ PurpleJack - Darme de baja",
        description=(
            "Como usuario puedes solicitar que tu información sea eliminada de los "
            "registros funcionales de PurpleJack. Ejecuta `/eliminar_mis_datos` desde "
            f"el canal <#{NAVE_DATA_CHANNEL_ID}> y completa las confirmaciones."
        ),
        color=discord.Color.red(),
    )
    embed.set_footer(text="La solicitud es privada y requiere doble confirmación.")
    return embed


class NaveHelpView(ui.View):
    def __init__(self, author_id: int):
        super().__init__(timeout=300)
        self.author_id = author_id
        self.section = "inicio"
        self.page: int | None = None
        self._sync_pagination()

    async def interaction_check(self, interaction: Interaction) -> bool:
        if interaction.user.id == self.author_id:
            return True
        await interaction.response.send_message(
            "❌ Este panel pertenece a otro usuario. Usa `/ayuda_nave` para abrir el tuyo.",
            ephemeral=True,
        )
        return False

    def _sync_pagination(self) -> None:
        showing_commands = self.page is not None
        self.previous.disabled = not showing_commands or self.page == 0
        self.next.disabled = not showing_commands or self.page == len(NAVE_COMMAND_PAGES) - 1
        self.back_to_start.disabled = self.section == "inicio"

    @ui.select(
        placeholder="Selecciona una categoría",
        min_values=1,
        max_values=1,
        options=[
            discord.SelectOption(label="Comandos", value="comandos", emoji="📚"),
            discord.SelectOption(label="Soporte", value="soporte", emoji="🛟"),
            discord.SelectOption(label="Darme de baja", value="baja", emoji="❌"),
        ],
    )
    async def category(self, interaction: Interaction, select: ui.Select):
        if select.values[0] == "comandos":
            self.section = "comandos"
            self.page = 0
            embed = _build_nave_commands_embed(self.page)
        elif select.values[0] == "soporte":
            self.section = "soporte"
            self.page = None
            embed = _build_nave_support_embed()
        else:
            self.section = "baja"
            self.page = None
            embed = _build_nave_unsubscribe_embed()
        self._sync_pagination()
        await interaction.response.edit_message(embed=embed, view=self)

    @ui.button(label="◀ Anterior", style=ButtonStyle.primary, row=1)
    async def previous(self, interaction: Interaction, button: ui.Button):
        if self.page is None:
            return await interaction.response.defer()
        self.page = max(0, self.page - 1)
        self._sync_pagination()
        await interaction.response.edit_message(embed=_build_nave_commands_embed(self.page), view=self)

    @ui.button(label="Siguiente ▶", style=ButtonStyle.primary, row=1)
    async def next(self, interaction: Interaction, button: ui.Button):
        if self.page is None:
            return await interaction.response.defer()
        self.page = min(len(NAVE_COMMAND_PAGES) - 1, self.page + 1)
        self._sync_pagination()
        await interaction.response.edit_message(embed=_build_nave_commands_embed(self.page), view=self)

    @ui.button(label="Volver al Inicio", style=ButtonStyle.success, row=1)
    async def back_to_start(self, interaction: Interaction, button: ui.Button):
        self.section = "inicio"
        self.page = None
        self._sync_pagination()
        await interaction.response.edit_message(embed=_build_nave_inicio_embed(), view=self)


def _deletion_summary_embed(final: bool = False) -> discord.Embed:
    if final:
        title = "⚠️ Confirmación definitiva"
        intro = "Esta es la última confirmación. La eliminación no se puede deshacer."
        color = discord.Color.red()
    else:
        title = "🗑️ Eliminar mis datos"
        intro = "Puedes eliminar toda la información funcional asociada a tu usuario."
        color = discord.Color.orange()

    embed = discord.Embed(title=title, description=intro, color=color)
    embed.add_field(
        name="Información que será eliminada",
        value=(
            "• Balance, banco, dulces y posición en rankings.\n"
            "• Inventario, artículos y registros de uso.\n"
            "• Empleo, experiencia, maestrías e historial laboral.\n"
            "• Cooldowns, protecciones, boosts y progreso de juegos.\n"
            "• Apuestas finalizadas, jornadas y registros funcionales.\n"
            "• Registros de roles temporales asociados a tus artículos."
        ),
        inline=False,
    )
    embed.add_field(
        name="Importante",
        value=(
            "No se elimina tu cuenta, tus mensajes ni datos administrados directamente "
            "por la plataforma. Si vuelves a utilizar PurpleJack, comenzarás con un perfil nuevo."
        ),
        inline=False,
    )
    return embed


async def _remove_roles_for_user(bot, user_id: int) -> bool:
    for cargo in await get_user_temporary_roles(user_id):
        guild = bot.get_guild(cargo["guild_id"])
        if guild is None:
            continue
        role = guild.get_role(cargo["rol_id"])
        if role is None:
            continue
        try:
            member = guild.get_member(user_id) or await guild.fetch_member(user_id)
        except discord.NotFound:
            continue
        try:
            if role in member.roles:
                await member.remove_roles(
                    role,
                    reason="Eliminación de datos solicitada por el usuario",
                )
        except (discord.Forbidden, discord.HTTPException):
            return False
    return True


async def _notify_completed_data_deletion(bot, user: discord.abc.User) -> None:
    from settings import TEST_MODE
    if TEST_MODE:
        return
    try:
        channel = bot.get_channel(DATA_DELETION_LOG_CHANNEL_ID)
        if channel is None:
            channel = await bot.fetch_channel(DATA_DELETION_LOG_CHANNEL_ID)
        nickname = getattr(user, "nick", None) or user.display_name
        nickname = discord.utils.escape_markdown(
            discord.utils.escape_mentions(nickname)
        )
        await channel.send(
            f"<@&{COORDINADOR_ROLE_ID}> El usuario **{nickname}** eliminó sus datos de PurpleJack.",
            allowed_mentions=discord.AllowedMentions(
                everyone=False,
                users=False,
                roles=True,
                replied_user=False,
            ),
        )
    except (discord.Forbidden, discord.HTTPException, discord.NotFound) as exc:
        logger.error(
            "No se pudo notificar la eliminación de datos del usuario %s en el canal %s: %s",
            user.id,
            DATA_DELETION_LOG_CHANNEL_ID,
            exc,
        )


class DeleteDataFinalView(ui.View):
    def __init__(self, bot, user_id: int):
        super().__init__(timeout=120)
        self.bot = bot
        self.user_id = user_id
        self.processing = False

    async def interaction_check(self, interaction: Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message("❌ Esta solicitud no te pertenece.", ephemeral=True)
        return False

    @ui.button(label="Eliminar definitivamente", emoji="🗑️", style=ButtonStyle.danger)
    async def delete_permanently(self, interaction: Interaction, button: ui.Button):
        if self.processing:
            return await interaction.response.send_message(
                "⌛ La eliminación ya está en proceso.", ephemeral=True
            )
        self.processing = True
        for child in self.children:
            child.disabled = True
        await interaction.response.defer()

        blocker = await get_user_deletion_blocker(self.user_id)
        if blocker:
            self.processing = False
            return await interaction.edit_original_response(
                embed=discord.Embed(
                    title="⌛ Eliminación pendiente",
                    description=(
                        f"No se puede eliminar el perfil mientras tengas una **{blocker}**. "
                        "Finalízala o espera su recuperación y vuelve a intentarlo."
                    ),
                    color=discord.Color.orange(),
                ),
                view=None,
            )

        if not await _remove_roles_for_user(self.bot, self.user_id):
            self.processing = False
            return await interaction.edit_original_response(
                embed=discord.Embed(
                    title="❌ No se pudo completar la eliminación",
                    description=(
                        "No pude retirar uno de tus roles temporales de forma segura. "
                        f"Solicita asistencia en [#soporte]({NAVE_SUPPORT_URL}) y vuelve a intentarlo."
                    ),
                    color=discord.Color.red(),
                ),
                view=None,
            )

        deleted = await purge_user_data(self.user_id)
        if not deleted:
            self.processing = False
            return await interaction.edit_original_response(
                embed=discord.Embed(
                    title="⌛ Eliminación pendiente",
                    description="Se detectó una operación activa. Finalízala y vuelve a intentarlo.",
                    color=discord.Color.orange(),
                ),
                view=None,
            )

        activity_cache = getattr(self.bot, "_user_activity_writes", None)
        if activity_cache is not None:
            activity_cache.pop(self.user_id, None)
        await interaction.edit_original_response(
            embed=discord.Embed(
                title="✅ Datos eliminados",
                description=(
                    "Tu información funcional fue eliminada del sistema. "
                    "Si vuelves a utilizar PurpleJack, comenzarás con un perfil nuevo."
                ),
                color=discord.Color.green(),
            ),
            view=None,
        )
        await _notify_completed_data_deletion(self.bot, interaction.user)

    @ui.button(label="Cancelar", style=ButtonStyle.secondary)
    async def cancel(self, interaction: Interaction, button: ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="Eliminación cancelada",
                description="No se eliminó ninguna información.",
                color=discord.Color.green(),
            ),
            view=self,
        )


class DeleteDataFirstView(ui.View):
    def __init__(self, bot, user_id: int):
        super().__init__(timeout=120)
        self.bot = bot
        self.user_id = user_id

    async def interaction_check(self, interaction: Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message("❌ Esta solicitud no te pertenece.", ephemeral=True)
        return False

    @ui.button(label="Continuar", style=ButtonStyle.danger)
    async def continue_deletion(self, interaction: Interaction, button: ui.Button):
        await interaction.response.edit_message(
            embed=_deletion_summary_embed(final=True),
            view=DeleteDataFinalView(self.bot, self.user_id),
        )

    @ui.button(label="Cancelar", style=ButtonStyle.secondary)
    async def cancel(self, interaction: Interaction, button: ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="Eliminación cancelada",
                description="No se eliminó ninguna información.",
                color=discord.Color.green(),
            ),
            view=self,
        )


def _format_cooldown(seconds: int) -> str:
    if seconds >= 3600:
        return f"{seconds // 3600}h"
    if seconds >= 60:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def _add_collect_fields(embed: discord.Embed):
    collect_config = cache.get_collect_config()
    if not collect_config:
        embed.description = "No hay roles con collect activo actualmente."
        return

    lineas_collect = [
        f"<@&{rol_id}>: **{cfg['cantidad']}** {COIN} "
        f"({_format_cooldown(int(round(cfg['cooldown_horas'] * 3600)))})"
        for rol_id, cfg in collect_config.items()
    ]
    bloques_collect = []
    bloque_actual = []
    longitud_actual = 0
    for linea in lineas_collect:
        longitud_linea = len(linea) + (1 if bloque_actual else 0)
        if bloque_actual and longitud_actual + longitud_linea > 1024:
            bloques_collect.append("\n".join(bloque_actual))
            bloque_actual = [linea]
            longitud_actual = len(linea)
        else:
            bloque_actual.append(linea)
            longitud_actual += longitud_linea
    if bloque_actual:
        bloques_collect.append("\n".join(bloque_actual))

    for indice, bloque in enumerate(bloques_collect):
        nombre_campo = (
            "**Cargos con Collect Activo**"
            if indice == 0
            else "**Cargos con Collect Activo (continuación)**"
        )
        embed.add_field(name=nombre_campo, value=bloque, inline=False)


def _build_cooldowns_embed(guild_id: int) -> discord.Embed:
    from modules.duels import DEFAULT_DUEL_COOLDOWN, _duel_cooldowns

    work_cd = _format_cooldown(game_config["work"]["cooldown"])
    crime_cd = _format_cooldown(game_config["crime"]["cooldown"])
    ruleta_cd = _format_cooldown(ruleta_config["cooldown"])
    rob_cd = _format_cooldown(rob_config["cooldown"])
    dados_cd = _format_cooldown(dados_config["cooldown"])
    memo_cd = _format_cooldown(memo_config["cooldown"])
    blackjack_cd = _format_cooldown(blackjack_config["cooldown"])
    retar_cd = _format_cooldown(_duel_cooldowns.get(guild_id, DEFAULT_DUEL_COOLDOWN))

    embed = discord.Embed(title="⏱️ Cooldowns de Juegos", color=discord.Color.purple())
    embed.set_thumbnail(
        url="https://pub-a09b3609b6b34dfab5c7aa7742cd1a8a.r2.dev/Purple%20jack%20Harcode/cdPJ.png"
    )
    embed.description = (
        f"**/work** — Cada {work_cd}\n"
        f"**/crime** — Cada {crime_cd}\n"
        f"**Ruleta (/jugar)** — Cada {ruleta_cd}\n"
        f"**/rob** — Cada {rob_cd}\n"
        f"**Dados (/jugar)** — Cada {dados_cd}\n"
        f"**Memo (/jugar)** — Cada {memo_cd}\n"
        f"**Blackjack (/jugar)** — Cada {blackjack_cd}\n"
        f"**/retar** — Cada {retar_cd}"
    )
    return embed


def _build_collects_embed() -> discord.Embed:
    embed = discord.Embed(title="💷 Roles con Collects Activos", color=discord.Color.blurple())
    _add_collect_fields(embed)
    return embed


class CooldownsPanelView(ui.View):
    def __init__(self, owner_id: int):
        super().__init__(timeout=40)
        self.owner_id = owner_id
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            from bot import check_player_access
            return await check_player_access(interaction)
        await interaction.response.send_message("Este panel no es tuyo.", ephemeral=True)
        return False

    @ui.button(label="Cooldowns", style=ButtonStyle.green)
    async def cooldowns(self, interaction: Interaction, button: ui.Button):
        await interaction.response.send_message(
            embed=_build_cooldowns_embed(interaction.guild_id or 0),
            ephemeral=True,
        )

    @ui.button(label="Roles Collects", style=ButtonStyle.primary)
    async def collects(self, interaction: Interaction, button: ui.Button):
        await interaction.response.send_message(embed=_build_collects_embed(), ephemeral=True)

    async def on_timeout(self):
        for button in self.children:
            button.disabled = True
        try:
            if self.message is not None:
                await self.message.edit(view=self)
        except discord.HTTPException:
            pass


class FinanceView(ui.View):
    def __init__(self, user_id):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: Interaction) -> bool:
        from bot import check_player_access
        return await check_player_access(interaction)

    @ui.button(label="Depositar", style=ButtonStyle.green)
    async def depositar(self, interaction: Interaction, button: ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message(
                "❌ No es tu menú.", ephemeral=True
            )
        await interaction.response.send_modal(
            DepositModal(self.user_id, self.message)
        )

    @ui.button(label="Retirar", style=ButtonStyle.primary)
    async def retirar(self, interaction: Interaction, button: ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message(
                "❌ No es tu menú.", ephemeral=True
            )
        await interaction.response.send_modal(
            WithdrawModal(self.user_id, self.message)
        )

    @ui.button(label="Craft", style=ButtonStyle.secondary)
    async def craft(self, interaction: Interaction, button: ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message(
                "❌ No es tu menú.", ephemeral=True,
            )
        from modules.craft import open_craft_panel
        await open_craft_panel(interaction, balance_message=self.message)

    @ui.button(label="Salir", style=ButtonStyle.danger)
    async def salir(self, interaction: Interaction, button: ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message(
                "❌ No es tu menú.", ephemeral=True
            )
        await interaction.response.defer()
        self.stop()
        try:
            if self.message is not None:
                await self.message.delete()
            else:
                await interaction.delete_original_response()
        except discord.NotFound:
            pass  # El panel ya fue cerrado o eliminado.
        except discord.HTTPException:
            logger.warning("No se pudo cerrar el panel de Balance de %s", self.user_id)


async def _refresh_finance_message(message, user: dict) -> bool:
    """Actualiza el webhook efímero; un panel vencido no revierte la operación."""
    if message is None or not message.embeds:
        return False
    embed = message.embeds[0].copy()
    embed.set_field_at(0, name=embed.fields[0].name,
                       value=f"{user['balance']} {COIN}", inline=True)
    embed.set_field_at(1, name=embed.fields[1].name,
                       value=f"{user['bank']} {COIN}", inline=True)
    try:
        await message.edit(embed=embed)
    except discord.HTTPException as error:
        logger.warning("No se pudo actualizar el panel de Balance: %s", error)
        return False
    return True


class DepositModal(ui.Modal, title="Depositar al Banco"):
    amount = ui.TextInput(label="¿Cuánto deseas depositar?", placeholder="Ej: 500 o All")

    def __init__(self, user_id, message):
        super().__init__()
        self.user_id = user_id
        self.message = message

    async def on_submit(self, interaction: Interaction):
        from bot import check_restricted_interaction
        if not await check_restricted_interaction(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            # Una sola llamada — get_user es cache-first, sin hit extra a DB
            user = await get_user(self.user_id)
            raw  = self.amount.value.strip().lower()
            amount = user["balance"] if raw == "all" else int(raw)

            if amount <= 0:
                return await interaction.followup.send(
                    "❌ Cantidad inválida.", ephemeral=True
                )
            if amount > user["balance"]:
                return await interaction.followup.send(
                    "❌ No tienes suficiente balance.", ephemeral=True
                )

            # Validar límite de banco antes de proceder
            banco_actual = user["bank"]
            if banco_actual >= MAX_BANK:
                return await interaction.followup.send(
                    f"🏦 Tu banco ya está al límite máximo ({MAX_BANK:,} {COIN}).\n"
                    f"Retira fondos antes de depositar.",
                    ephemeral=True
                )

            # Calcular cuánto cabe realmente en el banco
            espacio_disponible = MAX_BANK - banco_actual
            aplicado_banco    = min(amount, espacio_disponible)
            excedente_balance = amount - aplicado_banco

            await update_balance(self.user_id, -amount, track_event=False)
            aplicado_banco = await update_bank(self.user_id, amount, track_event=False)

            # Refrescar datos para actualizar el embed
            user = await get_user(self.user_id)
            refreshed = await _refresh_finance_message(self.message, user)

            # Informar distribución si el banco se llenó durante el depósito
            if excedente_balance > 0:
                await interaction.followup.send(
                    f"🏦 Banco lleno: **{aplicado_banco:,}** {COIN} depositados al banco.\n"
                    f"💰 **{excedente_balance:,}** {COIN} quedaron en tu balance.",
                    ephemeral=True
                )
            elif not refreshed:
                await interaction.followup.send(
                    f"✅ Depósito realizado. Balance: **{user['balance']}** {COIN} · "
                    f"Banco: **{user['bank']}** {COIN}.",
                    ephemeral=True,
                )
        except ValueError:
            await interaction.followup.send(
                "❌ Ingresa un número válido.", ephemeral=True
            )


class WithdrawModal(ui.Modal, title="Retirar del Banco"):
    amount = ui.TextInput(label="¿Cuánto deseas retirar?", placeholder="Ej: 500 o All")

    def __init__(self, user_id, message):
        super().__init__()
        self.user_id = user_id
        self.message = message

    async def on_submit(self, interaction: Interaction):
        from bot import check_restricted_interaction
        if not await check_restricted_interaction(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            # Una sola llamada — get_user es cache-first, sin hit extra a DB
            user = await get_user(self.user_id)
            raw  = self.amount.value.strip().lower()
            amount = user["bank"] if raw == "all" else int(raw)

            if amount <= 0:
                return await interaction.followup.send(
                    "❌ Cantidad inválida.", ephemeral=True
                )
            if amount > user["bank"]:
                return await interaction.followup.send(
                    "❌ No tienes suficiente en el banco.", ephemeral=True
                )

            await update_bank(self.user_id, -amount)
            await update_balance(self.user_id, amount, track_event=False)

            user = await get_user(self.user_id)
            if not await _refresh_finance_message(self.message, user):
                await interaction.followup.send(
                    f"✅ Retiro realizado. Balance: **{user['balance']}** {COIN} · "
                    f"Banco: **{user['bank']}** {COIN}.",
                    ephemeral=True,
                )
        except ValueError:
            await interaction.followup.send(
                "❌ Ingresa un número válido.", ephemeral=True
            )


class Economy(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def balance(self, ctx):
        user = await get_user(ctx.author.id)
        candy_balance = await get_candy_balance(ctx.author.id)
        embed = discord.Embed(
            title=f"💰 Finanzas de {ctx.author.display_name}",
            color=discord.Color.purple(),
        )
        embed.add_field(name=f"{COIN} Balance", value=f"{user['balance']} {COIN}", inline=True)
        embed.add_field(name="🏦 Banco",        value=f"{user['bank']} {COIN}",    inline=True)
        embed.add_field(name="🎃 Dulces", value=f"{candy_balance} {CANDY}", inline=True)
        embed.set_thumbnail(url=ctx.author.display_avatar.url)
        view = FinanceView(ctx.author.id)
        view.message = await ctx.message.reply(embed=embed, view=view, delete_after=120)

    def format_cooldown(self, seconds: int) -> str:
        return _format_cooldown(seconds)

    async def cooldowns(self, ctx):
        embed = discord.Embed(
            title="**INFO:** CD y Collects Activos",
            description=(
                "Haz click en los botones de abajo para mostrar la información de los "
                "tiempos de espera y Roles con Collects..."
            ),
            color=discord.Color.purple(),
        )
        embed.set_footer(text="Panel privado de consultas")
        view = CooldownsPanelView(ctx.author.id)
        message = await ctx.send(embed=embed, view=view)
        view.message = message

    async def top(self, ctx):
        if ctx.guild is None:
            return await ctx.send("❌ Este ranking solo está disponible en un servidor.")
        user_id = ctx.author.id

        if cache.check_top_cooldown(user_id):
            return await ctx.send(
                f"⏳ {ctx.author.mention} Espera antes de consultar el top de nuevo.",
                delete_after=10,
            )

        cache.set_top_cooldown(user_id)
        resultados = get_balance_ranking(limit=15)

        medallas    = ["🥇", "🥈", "🥉"]
        descripcion = ""

        for i, (ranked_user_id, balance) in enumerate(resultados):
            nombre_guardado = cache.get_member_name(ranked_user_id)
            nombre = (
                discord.utils.escape_markdown(discord.utils.escape_mentions(nombre_guardado))
                if nombre_guardado else f"<@{ranked_user_id}>"
            )
            posicion     = medallas[i] if i < 3 else f"**{i+1}.**"
            descripcion += f"{posicion} {nombre} —— {COIN} **{balance}**\n"

        embed = discord.Embed(
            title=f"{COIN} TOP BALANCES MAS RICOS {COIN}",
            description=descripcion,
            color=discord.Color.blue(),
        )
        embed.set_footer(text="Solo se muestra el Top 15 de los más ricos.")
        await ctx.send(
            embed=embed, delete_after=60,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="ayuda_nave", description="Presentación y guía de comandos de PurpleJack")
    async def ayuda_nave(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=_build_nave_inicio_embed(),
            view=NaveHelpView(interaction.user.id),
            ephemeral=True,
        )

    @app_commands.command(
        name="eliminar_mis_datos",
        description="Solicita la eliminación de tus datos funcionales de PurpleJack",
    )
    async def eliminar_mis_datos(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=_deletion_summary_embed(),
            view=DeleteDataFirstView(self.bot, interaction.user.id),
            ephemeral=True,
        )

async def setup(bot):
    await bot.add_cog(Economy(bot))
