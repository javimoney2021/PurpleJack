from discord.ext import commands
from discord import app_commands, ui, ButtonStyle, Interaction
import discord
import asyncio
import time

from core.database import get_user, update_balance, update_bank
from core import cache
from core.config import (
    COIN, game_config, ruleta_config, rob_config, dados_config,
    memo_config, blackjack_config,
)
from core.cache import MAX_BANK

TOP_COOLDOWN = 300
EVENTO_THUMBNAIL_URL = "https://pub-a09b3609b6b34dfab5c7aa7742cd1a8a.r2.dev/Purple%20jack%20Harcode/PurpleThumb.png"
EVENTO_TASA_DEPOSITO = 30
EVENTO_TOP_ICON = "<:ygoldstar:1004555717610590258>"

NAVE_INFO_DESCRIPTION = (
    "PurpleJack es una experiencia de economía y entretenimiento donde puedes "
    "trabajar, competir, jugar, coleccionar artículos y progresar junto a la comunidad.\n\n"
    "Antes de comenzar, consulta nuestros "
    "[Términos de Servicio](https://purplejack.online/Terms.html) y nuestra "
    "[Política de Privacidad](https://purplejack.online/Privacy.html) para conocer "
    "las reglas y el tratamiento de tus datos."
)

NAVE_COMMAND_PAGES = (
    (
        "💰 Economía",
        "Consulta tus recursos, posiciones y tiempos dentro de PurpleJack.",
        (
            "!bal       Consulta tu balance y banco.\n"
            "!cd        Muestra tus cooldowns activos.\n"
            "!top       Consulta la clasificación económica.\n"
            "!evento    Consulta el ranking del evento.\n"
            "!prob      Muestra las probabilidades actuales.\n"
            "!collect   Reclama la recompensa de tu rol."
        ),
    ),
    (
        "🧰 Empleos, tienda e inventario",
        "Trabaja, progresa y administra tus artículos.",
        (
            "!work      Realiza un trabajo económico básico.\n"
            "!crime     Intenta cometer un crimen.\n"
            "!empleos   Consulta los empleos disponibles.\n"
            "!aplicar   Solicita un empleo.\n"
            "!renunciar Abandona tu empleo actual.\n"
            "!exp       Consulta tu experiencia laboral.\n"
            "!oficina   Accede a empleos avanzados.\n"
            "!trabajar  Cumple la jornada de tu empleo.\n"
            "!tienda    Abre la tienda.\n"
            "!info      Consulta un artículo.\n"
            "!inv       Abre tu inventario.\n"
            "!time      Consulta la duración de tus artículos."
        ),
    ),
    (
        "🎮 Juegos y competición",
        "Participa en los juegos y desafíos disponibles.",
        (
            "!dados     Juega una partida de dados.\n"
            "!ruleta    Realiza una apuesta en la ruleta.\n"
            "!bj        Juega Blackjack.\n"
            "!memo      Inicia el juego de memoria.\n"
            "!carrera   Participa en una carrera.\n"
            "!retar     Desafía a otro jugador.\n"
            "!rob       Intenta robar a otro miembro."
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
    embed.set_footer(text=f"Página {page + 1}/{len(NAVE_COMMAND_PAGES)} · Prefijo de usuario: !")
    return embed


class NaveHelpView(ui.View):
    def __init__(self, author_id: int):
        super().__init__(timeout=300)
        self.author_id = author_id
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
        self.page_indicator.disabled = True
        self.page_indicator.label = (
            f"Página {self.page + 1}/{len(NAVE_COMMAND_PAGES)}"
            if showing_commands
            else "Inicio"
        )
        self.next.disabled = not showing_commands or self.page == len(NAVE_COMMAND_PAGES) - 1

    @ui.select(
        placeholder="Selecciona una categoría",
        min_values=1,
        max_values=1,
        options=[
            discord.SelectOption(label="Inicio", value="inicio", emoji="🚀"),
            discord.SelectOption(label="Comandos", value="comandos", emoji="📚"),
        ],
    )
    async def category(self, interaction: Interaction, select: ui.Select):
        if select.values[0] == "inicio":
            self.page = None
            embed = _build_nave_inicio_embed()
        else:
            self.page = 0
            embed = _build_nave_commands_embed(self.page)
        self._sync_pagination()
        await interaction.response.edit_message(embed=embed, view=self)

    @ui.button(label="◀ Anterior", style=ButtonStyle.primary, row=1)
    async def previous(self, interaction: Interaction, button: ui.Button):
        if self.page is None:
            return await interaction.response.defer()
        self.page = max(0, self.page - 1)
        self._sync_pagination()
        await interaction.response.edit_message(embed=_build_nave_commands_embed(self.page), view=self)

    @ui.button(label="Inicio", style=ButtonStyle.secondary, disabled=True, row=1)
    async def page_indicator(self, interaction: Interaction, button: ui.Button):
        pass

    @ui.button(label="Siguiente ▶", style=ButtonStyle.primary, row=1)
    async def next(self, interaction: Interaction, button: ui.Button):
        if self.page is None:
            return await interaction.response.defer()
        self.page = min(len(NAVE_COMMAND_PAGES) - 1, self.page + 1)
        self._sync_pagination()
        await interaction.response.edit_message(embed=_build_nave_commands_embed(self.page), view=self)


def _format_cooldown(seconds: int) -> str:
    if seconds >= 3600:
        return f"{seconds // 3600}h"
    if seconds >= 60:
        return f"{seconds // 60}m"
    return f"{seconds}s"


async def _finalizar_consulta_evento(message: discord.Message) -> None:
    await asyncio.sleep(60)
    try:
        await message.edit(
            embed=discord.Embed(
                title="**🔰 Consulta del Evento PurpleCoins Finalizada...**",
                color=discord.Color.purple(),
            )
        )
    except (discord.NotFound, discord.HTTPException):
        pass


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
        f"**!work**     — Cada {work_cd}\n"
        f"**!crime**    — Cada {crime_cd}\n"
        f"**!ruleta**   — Cada {ruleta_cd}\n"
        f"**!rob**      — Cada {rob_cd}\n"
        f"**!dados**    — Cada {dados_cd}\n"
        f"**!memo**     — Cada {memo_cd}\n"
        f"**!bj**       — Cada {blackjack_cd}\n"
        f"**!retar**    — Cada {retar_cd}"
    )
    return embed


def _build_collects_embed() -> discord.Embed:
    embed = discord.Embed(title="💷 Roles con Collects Activos", color=discord.Color.blurple())
    _add_collect_fields(embed)
    return embed


class CooldownsPanelView(ui.View):
    def __init__(self):
        super().__init__(timeout=40)
        self.message: discord.Message | None = None

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
        if self.message is None:
            return

        expired_embed = discord.Embed(
            title="*Consulta **!cd** para ver la info de cooldowns y collects.*",
            color=discord.Color.purple(),
        )
        try:
            await self.message.edit(embed=expired_embed, view=None)
        except discord.HTTPException:
            return

        asyncio.create_task(self._delete_expired_message())

    async def _delete_expired_message(self):
        await asyncio.sleep(60)
        try:
            await self.message.delete()
        except discord.HTTPException:
            pass


class FinanceView(ui.View):
    def __init__(self, user_id):
        super().__init__(timeout=60)
        self.user_id = user_id

    @ui.button(label="Depositar", style=ButtonStyle.green)
    async def depositar(self, interaction: Interaction, button: ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message(
                "❌ No es tu menú.", ephemeral=True
            )
        await interaction.response.send_modal(
            DepositModal(self.user_id, interaction.message)
        )

    @ui.button(label="Retirar", style=ButtonStyle.primary)
    async def retirar(self, interaction: Interaction, button: ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message(
                "❌ No es tu menú.", ephemeral=True
            )
        await interaction.response.send_modal(
            WithdrawModal(self.user_id, interaction.message)
        )

    @ui.button(label="Salir", style=ButtonStyle.danger)
    async def salir(self, interaction: Interaction, button: ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message(
                "❌ No es tu menú.", ephemeral=True
            )
        await interaction.message.delete()
        await interaction.response.defer()


class DepositModal(ui.Modal, title="Depositar al Banco"):
    amount = ui.TextInput(label="¿Cuánto deseas depositar?", placeholder="Ej: 500 o All")

    def __init__(self, user_id, message):
        super().__init__()
        self.user_id = user_id
        self.message = message

    async def on_submit(self, interaction: Interaction):
        try:
            # Una sola llamada — get_user es cache-first, sin hit extra a DB
            user = await get_user(self.user_id)
            raw  = self.amount.value.strip().lower()
            amount = user["balance"] if raw == "all" else int(raw)

            if amount <= 0:
                return await interaction.response.send_message(
                    "❌ Cantidad inválida.", ephemeral=True
                )
            if amount > user["balance"]:
                return await interaction.response.send_message(
                    "❌ No tienes suficiente balance.", ephemeral=True
                )

            # Validar límite de banco antes de proceder
            banco_actual = user["bank"]
            if banco_actual >= MAX_BANK:
                return await interaction.response.send_message(
                    f"🏦 Tu banco ya está al límite máximo ({MAX_BANK:,} {COIN}).\n"
                    f"Retira fondos antes de depositar.",
                    ephemeral=True
                )

            # Calcular cuánto cabe realmente en el banco
            espacio_disponible = MAX_BANK - banco_actual
            aplicado_banco    = min(amount, espacio_disponible)
            excedente_balance = amount - aplicado_banco

            # El depósito no descuenta el total del evento: solo el 30 % de
            # lo que realmente pudo entrar al banco.
            await update_balance(self.user_id, -amount, track_event=False)
            aplicado_banco = await update_bank(self.user_id, amount, track_event=False)
            descuento_evento = aplicado_banco * EVENTO_TASA_DEPOSITO // 100
            cache.record_evento_balance_delta(self.user_id, -descuento_evento)

            # Refrescar datos para actualizar el embed
            user = await get_user(self.user_id)
            embed = self.message.embeds[0]
            embed.set_field_at(0, name=embed.fields[0].name,
                               value=f"{user['balance']} {COIN}", inline=True)
            embed.set_field_at(1, name=embed.fields[1].name,
                               value=f"{user['bank']} {COIN}",    inline=True)
            await self.message.edit(embed=embed)

            # Informar distribución si el banco se llenó durante el depósito
            if excedente_balance > 0:
                await interaction.response.send_message(
                    f"🏦 Banco lleno: **{aplicado_banco:,}** {COIN} depositados al banco.\n"
                    f"💰 **{excedente_balance:,}** {COIN} quedaron en tu balance.",
                    ephemeral=True
                )
            else:
                await interaction.response.defer()
        except ValueError:
            await interaction.response.send_message(
                "❌ Ingresa un número válido.", ephemeral=True
            )


class WithdrawModal(ui.Modal, title="Retirar del Banco"):
    amount = ui.TextInput(label="¿Cuánto deseas retirar?", placeholder="Ej: 500 o All")

    def __init__(self, user_id, message):
        super().__init__()
        self.user_id = user_id
        self.message = message

    async def on_submit(self, interaction: Interaction):
        try:
            # Una sola llamada — get_user es cache-first, sin hit extra a DB
            user = await get_user(self.user_id)
            raw  = self.amount.value.strip().lower()
            amount = user["bank"] if raw == "all" else int(raw)

            if amount <= 0:
                return await interaction.response.send_message(
                    "❌ Cantidad inválida.", ephemeral=True
                )
            if amount > user["bank"]:
                return await interaction.response.send_message(
                    "❌ No tienes suficiente en el banco.", ephemeral=True
                )

            await update_bank(self.user_id, -amount)
            await update_balance(self.user_id, amount, track_event=False)

            user = await get_user(self.user_id)
            embed = self.message.embeds[0]
            embed.set_field_at(0, name=embed.fields[0].name,
                               value=f"{user['balance']} {COIN}", inline=True)
            embed.set_field_at(1, name=embed.fields[1].name,
                               value=f"{user['bank']} {COIN}",    inline=True)
            await self.message.edit(embed=embed)
            await interaction.response.defer()
        except ValueError:
            await interaction.response.send_message(
                "❌ Ingresa un número válido.", ephemeral=True
            )


class Economy(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="bal")
    async def balance(self, ctx):
        user = await get_user(ctx.author.id)
        embed = discord.Embed(
            title=f"💰 Finanzas de {ctx.author.display_name}",
            color=discord.Color.purple(),
        )
        embed.add_field(name=f"{COIN} Balance", value=f"{user['balance']} {COIN}", inline=True)
        embed.add_field(name="🏦 Banco",        value=f"{user['bank']} {COIN}",    inline=True)
        embed.set_thumbnail(url=ctx.author.display_avatar.url)
        await ctx.message.reply(embed=embed, view=FinanceView(ctx.author.id), delete_after=120)

    def format_cooldown(self, seconds: int) -> str:
        return _format_cooldown(seconds)

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.CommandOnCooldown):
            retry = int(error.retry_after)
            if retry >= 60:
                tiempo = f"{retry // 60}m {retry % 60}s" if retry % 60 else f"{retry // 60}m"
            else:
                tiempo = f"{retry}s"
            await ctx.send(
                f"⏳ {ctx.author.mention} Podrás usar este comando de nuevo en **{tiempo}**.",
                delete_after=10,
            )
        else:
            raise error

    @commands.command(name="cd")
    @commands.cooldown(1, 30, commands.BucketType.user)
    async def cooldowns(self, ctx):
        embed = discord.Embed(
            title="**INFO:** CD y Collects Activos",
            description=(
                "Haz click en los botones de abajo para mostrar la información de los "
                "tiempos de espera y Roles con Collects..."
            ),
            color=discord.Color.purple(),
        )
        embed.set_footer(text=">> Cualquiera puede usar este panel <<")
        view = CooldownsPanelView()
        message = await ctx.send(embed=embed, view=view)
        view.message = message

    @commands.command()
    async def top(self, ctx):
        user_id = ctx.author.id

        if cache.check_top_cooldown(user_id):
            return await ctx.send(
                f"⏳ {ctx.author.mention} Espera antes de consultar el top de nuevo.",
                delete_after=10,
            )

        cache.set_top_cooldown(user_id)
        await cache.flush_to_db()

        from core.database import pool

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, balance FROM users ORDER BY balance DESC LIMIT 15"
            )

        # Usar la función pública get_all_cache() en lugar de importar _cache
        user_cache = cache.get_all_cache()

        resultados = []
        for row in rows:
            uid = row["id"]
            if uid in user_cache:
                total = user_cache[uid]["balance"]
            else:
                total = row["balance"]
            resultados.append((uid, total))

        resultados.sort(key=lambda x: x[1], reverse=True)

        medallas    = ["🥇", "🥈", "🥉"]
        descripcion = ""

        for i, (uid, balance) in enumerate(resultados):
            member = ctx.guild.get_member(uid)
            nombre = member.display_name if member else f"<@{uid}>"
            posicion     = medallas[i] if i < 3 else f"**{i+1}.**"
            descripcion += f"{posicion} {nombre} —— {COIN} **{balance}**\n"

        embed = discord.Embed(
            title=f"{COIN} TOP BALANCES MAS RICOS {COIN}",
            description=descripcion,
            color=discord.Color.blue(),
        )
        embed.set_footer(text="Solo se muestra el Top 15 de los más ricos.")
        await ctx.send(embed=embed, delete_after=60)

    @commands.command(name="evento")
    async def evento(self, ctx):
        if not cache.is_evento_activo():
            resultados_anteriores = cache.get_evento_top(4)
            if not resultados_anteriores:
                return await ctx.reply(
                    "❌ No hay un evento de Purple Coins activo actualmente.",
                    delete_after=15,
                )

            lineas = []
            for user_id, puntos in resultados_anteriores:
                member = ctx.guild.get_member(user_id) if ctx.guild else None
                nombre = (member.nick or member.display_name) if member else f"Usuario {user_id}"
                lineas.append(f"{COIN} {nombre} —— {COIN} **{puntos}**")

            embed = discord.Embed(
                title="Ultimos Ganadores del Evento",
                description="\n".join(lineas),
                color=discord.Color.purple(),
            )
            embed.set_footer(text="No hay Evento Activo, Espera el Proximo Anuncio...")
            embed.set_thumbnail(url=EVENTO_THUMBNAIL_URL)
            return await ctx.send(embed=embed, delete_after=60)

        resultados = cache.get_evento_top(10)
        if resultados:
            lineas = []
            for indice, (user_id, puntos) in enumerate(resultados):
                member = ctx.guild.get_member(user_id) if ctx.guild else None
                nombre = (member.nick or member.display_name) if member else f"Usuario {user_id}"
                posicion = EVENTO_TOP_ICON if indice < 4 else f"**{indice + 1}.**"
                lineas.append(f"{posicion} {nombre} —— {COIN} **{puntos}**")
            descripcion = "\n".join(lineas)
        else:
            descripcion = "Aún no se registran ingresos en este evento."

        embed = discord.Embed(
            title="TOP EVENTO PURPLE COINS",
            description=descripcion,
            color=discord.Color.gold(),
        )
        embed.set_footer(text="Retiros del banco y collects no suman puntos.")
        embed.set_thumbnail(url=EVENTO_THUMBNAIL_URL)
        message = await ctx.send(embed=embed)
        asyncio.create_task(_finalizar_consulta_evento(message))

    @app_commands.command(name="ayuda_nave", description="Presentación y guía de comandos de PurpleJack")
    async def ayuda_nave(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=_build_nave_inicio_embed(),
            view=NaveHelpView(interaction.user.id),
            ephemeral=True,
        )

    @commands.command(name="prob")
    @commands.cooldown(1, 60, commands.BucketType.user)
    async def probabilidades(self, ctx):
        crime_exito  = int(game_config["crime"]["ganar_prob"] * 100)
        crime_fallo  = int(game_config["crime"]["perder_prob"] * 100)
        rob_exito    = int(rob_config["exito_prob"] * 100)
        rob_fallo    = int(rob_config["fallo_prob"] * 100)
        dados_exito  = int(dados_config["exito_prob"] * 100)
        dados_fallo  = int(dados_config["fallo_prob"] * 100)

        embed = discord.Embed(
            title="🍀 Probabilidades Actuales",
            color=discord.Color.purple(),
        )
        embed.add_field(
            name="",
            value=(
                f"**!crime** — Éxito: `{crime_exito}%` · Fallo: `{crime_fallo}%`\n"
                f"**!rob** — Éxito: `{rob_exito}%` · Fallo: `{rob_fallo}%`\n"
                f"**!dados** — Éxito: `{dados_exito}%` · Fallo: `{dados_fallo}%`"
            ),
            inline=False,
        )
        await ctx.send(embed=embed, delete_after=25)


async def setup(bot):
    await bot.add_cog(Economy(bot))
