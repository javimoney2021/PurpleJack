import discord
import asyncio
from io import BytesIO
from math import ceil
from discord.ext import commands
from discord import app_commands
from openpyxl import Workbook
from core.database import (
    get_user, update_balance, update_bank, pool,
    add_item, edit_item, delete_item,
    get_item_by_name, add_to_inventory, get_all_users_net_worth,
    get_all_inventarios, load_items_to_cache, add_stock,
    upsert_collect_config_db, delete_collect_config_db,
    save_game_config, save_rr_config, save_ruleta_config,
    save_rob_config, clear_game_cooldowns
)
from core import cache
from core.config import ruleta_config, rr_config, game_config, COIN

STAFF_ROLE = "Equipo de Eventos"
COORDINADOR_ROLE = "Coordinador-ES"

# ── ANUNCIOS (RAM only) ────────────────────────────────
_pending_announcements = {}
# {user_id: {"channel": channel_obj, "expires": float, "content": str|None}}

# ── NAVE EDIT (RAM only) ───────────────────────────────
_pending_nave = {}
# {user_id: {"channel": channel_obj, "expires": float, "content": str|None}}


def is_staff():
    async def predicate(interaction: discord.Interaction):
        role = discord.utils.get(interaction.user.roles, name=STAFF_ROLE)
        if not role:
            await interaction.response.send_message(
                "❌ No tienes permisos para usar este comando.", ephemeral=True
            )
            return False
        return True
    return app_commands.check(predicate)


def create_economia_menu_embed():
    return discord.Embed(
        title="📊 Economía",
        description="Selecciona una opción para examinar la economía de PurpleJack.",
        color=discord.Color.dark_purple()
    )


# ── MODAL RESET ────────────────────────────────────────

class ResetAllModal(discord.ui.Modal, title="Confirmar Reset Global"):
    confirmacion = discord.ui.TextInput(label='Escribe "Reset" para confirmar', placeholder="Reset", max_length=5)

    async def on_submit(self, interaction: discord.Interaction):
        if self.confirmacion.value != "Reset":
            return await interaction.response.send_message("❌ Confirmación incorrecta.", ephemeral=True)
        async with pool.acquire() as conn:
            await conn.execute("UPDATE users SET balance=0, bank=0")
        cache._cache.clear()
        cache._dirty.clear()
        await interaction.response.send_message("✅ Reset global completado.", ephemeral=False)

# ── ANUNCIO CONFIRM VIEW ───────────────────────────────

class AnuncioConfirmView(discord.ui.View):
    def __init__(self, user_id, channel, content):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.channel = channel
        self.content = content

    @discord.ui.button(label="✅ Aceptar", style=discord.ButtonStyle.success)
    async def aceptar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ No es tu confirmación.", ephemeral=True)
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            embed=discord.Embed(
                description=f"✅ Anuncio enviado a {self.channel.mention}.",
                color=discord.Color.green()
            ),
            view=self
        )
        await self.channel.send(self.content)
        _pending_announcements.pop(self.user_id, None)

    @discord.ui.button(label="❌ Cancelar", style=discord.ButtonStyle.danger)
    async def cancelar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ No es tu confirmación.", ephemeral=True)
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            embed=discord.Embed(
                description="🚫 Anuncio cancelado. No se envió nada.",
                color=discord.Color.red()
            ),
            view=self
        )
        _pending_announcements.pop(self.user_id, None)

# ── NAVE CONFIRM VIEW ──────────────────────────────────

class NaveConfirmView(discord.ui.View):
    def __init__(self, user_id, contenido, author_message):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.contenido = contenido
        self.author_message = author_message

    @discord.ui.button(label="✅ Enviar", style=discord.ButtonStyle.success)
    async def enviar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ No es tu confirmación.", ephemeral=True)
        from core.database import save_nave_contenido
        await save_nave_contenido(self.contenido)
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            embed=discord.Embed(
                description="⏳ Guardando...",
                color=discord.Color.teal()
            ),
            view=self
        )
        await asyncio.sleep(5)
        try:
            await self.author_message.delete()
        except Exception:
            pass
        _pending_nave.pop(self.user_id, None)
        await interaction.channel.send(
            embed=discord.Embed(
                title="📋 Guía de la Nave-Sus Actualizada",
                description="✅ **Comando de la Nave Actualizado!**",
                color=discord.Color.teal()
            )
        )

    @discord.ui.button(label="❌ Cancelar", style=discord.ButtonStyle.danger)
    async def cancelar(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ No es tu confirmación.", ephemeral=True)
        for item in self.children:
            item.disabled = True
        _pending_nave.pop(self.user_id, None)
        await interaction.response.edit_message(
            embed=discord.Embed(
                description="🚫 Edición cancelada. No se guardó nada.",
                color=discord.Color.red()
            ),
            view=self
        )


class SaldosPaginationView(discord.ui.View):
    def __init__(self, author_id, users):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.users = sorted(
            users,
            key=lambda u: u["balance"] + u["bank"],
            reverse=True
        )
        self.page = 0

    def get_embed(self):
        total_items = len(self.users)
        total_pages = max(1, ceil(total_items / 10))
        start = self.page * 10
        end = start + 10
        page_users = self.users[start:end]

        embed = discord.Embed(
            title="📊 Saldos Netos",
            color=discord.Color.gold()
        )

        if not page_users:
            embed.description = "No hay usuarios con neto ≥ 100 Purple Coins."
        else:
            embed.description = "\n".join(
                f"**{idx}.** <@{user['id']}> — **{user['balance'] + user['bank']}** {COIN}"
                for idx, user in enumerate(page_users, start=start + 1)
            )

        embed.set_footer(text=f"Página {self.page + 1}/{total_pages}")
        return embed

    @discord.ui.button(label="Anterior", style=discord.ButtonStyle.secondary)
    async def anterior(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message("❌ Solo el creador puede usar estos botones.", ephemeral=True)
        if self.page > 0:
            self.page -= 1
            await interaction.response.edit_message(embed=self.get_embed(), view=self)
        else:
            await interaction.response.send_message("📌 Ya estás en la primera página.", ephemeral=True)

    @discord.ui.button(label="Siguiente", style=discord.ButtonStyle.secondary)
    async def siguiente(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message("❌ Solo el creador puede usar estos botones.", ephemeral=True)
        total_pages = max(1, ceil(len(self.users) / 10))
        if self.page < total_pages - 1:
            self.page += 1
            await interaction.response.edit_message(embed=self.get_embed(), view=self)
        else:
            await interaction.response.send_message("📌 Ya estás en la última página.", ephemeral=True)

    @discord.ui.button(label="Atrás", style=discord.ButtonStyle.danger)
    async def atras(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message("❌ Solo el creador puede usar estos botones.", ephemeral=True)
        await interaction.response.edit_message(embed=create_economia_menu_embed(), view=EconomiaView(self.author_id))


class EconomiaView(discord.ui.View):
    def __init__(self, author_id):
        super().__init__(timeout=180)
        self.author_id = author_id

    @discord.ui.button(label="Saldos Netos", style=discord.ButtonStyle.primary)
    async def saldos_netos(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message("❌ Solo el creador puede usar estos botones.", ephemeral=True)

        await interaction.response.defer()
        users = await get_all_users_net_worth(100)
        view = SaldosPaginationView(self.author_id, users)
        await interaction.edit_original_response(content=None, embed=view.get_embed(), view=view)

    @discord.ui.button(label="Inventarios", style=discord.ButtonStyle.success)
    async def inventarios(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            return await interaction.response.send_message("❌ Solo el creador puede usar estos botones.", ephemeral=True)

        await interaction.response.defer()
        await asyncio.sleep(10)

        rows = await get_all_inventarios()
        wb = Workbook()
        ws = wb.active
        ws.title = "Inventarios"

        ws.append(["Usuario", "Item", "Cantidad"])
        current_user = None
        user_index = 1
        user_names = {}

        for row in rows:
            user_id = row["user_id"]
            if user_id not in user_names:
                member = None
                if interaction.guild:
                    member = interaction.guild.get_member(user_id)
                    if member is None:
                        try:
                            member = await interaction.guild.fetch_member(user_id)
                        except Exception:
                            member = None
                if member:
                    user_names[user_id] = member.nick or member.name
                else:
                    user_obj = interaction.client.get_user(user_id)
                    if not user_obj:
                        try:
                            user_obj = await interaction.client.fetch_user(user_id)
                        except Exception:
                            user_obj = None
                    user_names[user_id] = user_obj.name if user_obj else f"Usuario {user_id}"

            if current_user != user_id:
                current_user = user_id
                ws.append([])
                ws.append([f"{user_index}. {user_names[user_id]}", "", ""])
                user_index += 1
            ws.append(["", row["nombre"], row["cantidad"]])

        buffer = BytesIO()
        wb.save(buffer)
        buffer.seek(0)

        dispositivo = discord.File(buffer, filename="Inv_actual.xlsx")
        await interaction.followup.send(
            content="✅ Inventarios generados. Descarga el archivo a continuación.",
            file=dispositivo,
            ephemeral=False
        )


# ── STAFF COG ──────────────────────────────────────────

class Staff(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def parse_cooldown(self, value: str):
        value = value.lower().strip()
        if value.endswith("h"):
            return int(value[:-1]) * 3600
        if value.endswith("m"):
            return int(value[:-1]) * 60
        if value.endswith("s"):
            return int(value[:-1])
        raise ValueError("Formato inválido")

    @app_commands.command(name="balance", description="Ver balance y banco de un miembro")
    @app_commands.describe(usuario="Miembro a consultar")
    @is_staff()
    async def balance_staff(self, interaction, usuario: discord.Member):
        user = await get_user(usuario.id)
        embed = discord.Embed(title=f"💰 Finanzas de {usuario.display_name}", color=discord.Color.gold())
        embed.add_field(name=f"{COIN} Balance", value=f"{user['balance']} {COIN}", inline=True)
        embed.add_field(name="🏦 Banco", value=f"{user['bank']} {COIN}", inline=True)
        embed.set_thumbnail(url=usuario.display_avatar.url)
        await interaction.response.send_message(embed=embed, ephemeral=False)

    @app_commands.command(name="addcoins", description="Añade PurpleCoins a un miembro")
    @app_commands.describe(usuario="Miembro", cantidad="Cantidad a añadir", destino="Balance o Banco")
    @app_commands.choices(destino=[
        app_commands.Choice(name="Balance", value="balance"),
        app_commands.Choice(name="Banco", value="banco"),
    ])
    @is_staff()
    async def addcoins(self, interaction, usuario: discord.Member, cantidad: int, destino: app_commands.Choice[str]):
        if cantidad <= 0:
            return await interaction.response.send_message("❌ La cantidad debe ser mayor a 0.", ephemeral=False)
        if destino.value == "balance":
            await update_balance(usuario.id, cantidad)
        else:
            await update_bank(usuario.id, cantidad)
        async with pool.acquire() as conn:
            data = cache.get_cached(usuario.id)
            if data:
                await conn.execute(
                    "UPDATE users SET balance=$1, bank=$2 WHERE id=$3",
                    data["balance"], data["bank"], usuario.id
                )
        await interaction.response.send_message(
            f"✅ Se añadieron **{cantidad}** {COIN} al **{destino.name}** de {usuario.mention}.", ephemeral=False
        )

    @app_commands.command(name="removecoins", description="Remueve PurpleCoins a un miembro")
    @app_commands.describe(usuario="Miembro", cantidad="Cantidad a remover", destino="Balance o Banco")
    @app_commands.choices(destino=[
        app_commands.Choice(name="Balance", value="balance"),
        app_commands.Choice(name="Banco", value="banco"),
    ])
    @is_staff()
    async def removecoins(self, interaction, usuario: discord.Member, cantidad: int, destino: app_commands.Choice[str]):
        if cantidad <= 0:
            return await interaction.response.send_message("❌ La cantidad debe ser mayor a 0.", ephemeral=False)
        if destino.value == "balance":
            await update_balance(usuario.id, -cantidad)
        else:
            await update_bank(usuario.id, -cantidad)
        async with pool.acquire() as conn:
            data = cache.get_cached(usuario.id)
            if data:
                await conn.execute(
                    "UPDATE users SET balance=$1, bank=$2 WHERE id=$3",
                    data["balance"], data["bank"], usuario.id
                )
        await interaction.response.send_message(
            f"✅ Se removieron **{cantidad}** {COIN} del **{destino.name}** de {usuario.mention}.", ephemeral=False
        )

    @app_commands.command(name="resetallcoins", description="Resetea las PurpleCoins de TODOS")
    @is_staff()
    async def resetallcoins(self, interaction):
        await interaction.response.send_modal(ResetAllModal())

    @app_commands.command(name="ruleta_apuesta_max", description="Configura la ruleta")
    @app_commands.describe(monto="Apuesta máxima", cooldown="Cooldown: ej 30s, 5m, 1h")
    @is_staff()
    async def ruleta_apuesta_max(self, interaction, monto: int, cooldown: str):
        if monto <= 0:
            return await interaction.response.send_message("❌ El monto debe ser mayor a 0.", ephemeral=True)
        try:
            cooldown_seconds = self.parse_cooldown(cooldown)
        except ValueError:
            return await interaction.response.send_message(
                "❌ Formato inválido. Usa ejemplos como: 30s, 5m, 1h", ephemeral=True
            )
        ruleta_config["max_apuesta"] = monto
        ruleta_config["cooldown"] = cooldown_seconds
        await save_ruleta_config()
        await clear_game_cooldowns("ruleta")
        cache.clear_game_cooldowns_cache("ruleta")
        await interaction.response.send_message(
            f"✅ Ruleta configurada:\n• Apuesta máxima: **{monto}** {COIN}\n• Cooldown: **{cooldown}**",
            ephemeral=False
        )

    @app_commands.command(name="ruleta_alternar", description="Activa o desactiva la ruleta")
    @is_staff()
    async def ruleta_alternar(self, interaction):
        ruleta_config["activa"] = not ruleta_config["activa"]
        await save_ruleta_config()
        estado = "✅ activada" if ruleta_config["activa"] else "🔧 desactivada"
        await interaction.response.send_message(f"La ruleta ha sido **{estado}**.", ephemeral=False)

    @app_commands.command(name="rr_max_apuesta", description="Configura la ruleta rusa")
    @app_commands.describe(
        monto="Apuesta máxima",
        cooldown="Cooldown: ej 30s, 5m, 1h",
        ganar_prob="Probabilidad de victoria (%)",
        perder_prob="Probabilidad de pérdida (%)"
    )
    @is_staff()
    async def rr_max_apuesta(self, interaction, monto: int, cooldown: str, ganar_prob: float = None, perder_prob: float = None):
        if monto <= 0:
            return await interaction.response.send_message("❌ El monto debe ser mayor a 0.", ephemeral=True)
        try:
            cooldown_seconds = self.parse_cooldown(cooldown)
        except ValueError:
            return await interaction.response.send_message(
                "❌ Formato inválido. Usa ejemplos como: 30s, 5m, 1h", ephemeral=True
            )

        def parse_probability(value: float):
            if value is None:
                return None
            if value > 1:
                if value > 100:
                    raise ValueError
                value = value / 100
            if value <= 0 or value >= 1:
                raise ValueError
            return value

        try:
            ganar = parse_probability(ganar_prob)
            perder = parse_probability(perder_prob)
        except ValueError:
            return await interaction.response.send_message(
                "❌ Probabilidades inválidas. Usa valores en % como 70 o en fracción 0.7.", ephemeral=True
            )

        if ganar is None and perder is None:
            ganar = rr_config["ganar_prob"]
            perder = rr_config["perder_prob"]
        elif ganar is None:
            ganar = 1 - perder
        elif perder is None:
            perder = 1 - ganar

        if abs((ganar + perder) - 1) > 0.01:
            return await interaction.response.send_message(
                "❌ Las probabilidades deben sumar 100%.", ephemeral=True
            )

        rr_config["max_apuesta"] = monto
        rr_config["cooldown"] = cooldown_seconds
        rr_config["ganar_prob"] = ganar
        rr_config["perder_prob"] = perder
        await save_rr_config()
        await clear_game_cooldowns("rr")
        cache.clear_game_cooldowns_cache("rr")
        await interaction.response.send_message(
            f"✅ Ruleta Rusa configurada:\n"
            f"• Apuesta máxima: **{monto}** {COIN}\n"
            f"• Cooldown: **{cooldown}**\n"
            f"• Probabilidad de victoria: **{int(ganar*100)}%**\n"
            f"• Probabilidad de pérdida: **{int(perder*100)}%**",
            ephemeral=False
        )

    @app_commands.command(name="rr_alternar", description="Activa o desactiva la ruleta rusa")
    @is_staff()
    async def rr_alternar(self, interaction):
        rr_config["activa"] = not rr_config["activa"]
        await save_rr_config()
        estado = "✅ activada" if rr_config["activa"] else "🔧 desactivada"
        await interaction.response.send_message(f"La Ruleta Rusa ha sido **{estado}**.", ephemeral=False)

    # ── ADD ITEM (nuevo flujo: campos de texto directos) ───
    @app_commands.command(name="add_item", description="Agrega un nuevo item a la tienda")
    @app_commands.describe(
        nombre="Nombre del item",
        desc="Descripción corta (visible en !tienda)",
        precio="Precio en PurpleCoins",
        stock="Stock total (-1 = ilimitado)",
        icono="Emoji del servidor o unicode. Ej: <:nombre:123456> o 🌟",
        desc_larga="Descripción larga visible en !info. Opcional.",
        rol="Rol que se otorga al usar el item. Opcional.",
        duracion_rol="Duración del rol: ej 30m, 12h, 7d. Vacío = permanente. Opcional.",
        cantid_por_user="Límite de compras por usuario. Vacío = ilimitado. Opcional."
    )
    @is_staff()
    async def item_new(self, interaction,
                       nombre: str,
                       desc: str,
                       precio: int,
                       stock: int,
                       icono: str = "",
                       desc_larga: str = "",
                       rol: discord.Role = None,
                       duracion_rol: str = "",
                       cantid_por_user: int = 0):

        if precio <= 0:
            return await interaction.response.send_message("❌ El precio debe ser mayor a 0.", ephemeral=True)

        existing = await get_item_by_name(nombre.strip())
        if existing:
            return await interaction.response.send_message(
                f"❌ Ya existe un item llamado **{nombre}**.", ephemeral=True
            )

        # Parsear duración
        duracion_segundos = 0
        if duracion_rol:
            try:
                val = duracion_rol.strip().lower()
                if val.endswith("d"):
                    duracion_segundos = int(val[:-1]) * 86400
                elif val.endswith("h"):
                    duracion_segundos = int(val[:-1]) * 3600
                elif val.endswith("m"):
                    duracion_segundos = int(val[:-1]) * 60
                else:
                    return await interaction.response.send_message(
                        "❌ Formato de duración inválido. Usa: 30m, 12h, 7d", ephemeral=True
                    )
            except ValueError:
                return await interaction.response.send_message(
                    "❌ Duración inválida.", ephemeral=True
                )

        rol_id = rol.id if rol else None

        await add_item(
            nombre=nombre.strip(),
            descripcion=desc.strip(),
            descripcion_larga=desc_larga.strip(),
            precio=precio,
            cantidad=1,
            stock=stock,
            icono=icono.strip(),
            utilizable=True,
            mensaje_uso="",
            rol_id=rol_id,
            duracion=duracion_segundos,
            limite_por_usuario=cantid_por_user if cantid_por_user > 0 else 0
        )

        dur_txt = duracion_rol if duracion_rol else "Permanente"
        rol_txt = rol.mention if rol else "Ninguno"
        icono_display = icono if icono else "🔹"
        limite_txt = str(cantid_por_user) if cantid_por_user > 0 else "∞"

        await interaction.response.send_message(
            f"✅ Item **{icono_display} {nombre}** añadido a la tienda.\n"
            f"• Precio: **{precio}** {COIN}\n"
            f"• Stock total: **{'∞' if stock == -1 else stock}**\n"
            f"• Límite por usuario: **{limite_txt}**\n"
            f"• Rol: {rol_txt}  •  Duración: {dur_txt}",
            ephemeral=False
        )

    @app_commands.command(name="editar_item", description="Edita un item de la tienda")
    @app_commands.describe(
        item="Selecciona el item a editar",
        nuevo_nombre="Nuevo nombre. Vacío = sin cambio. Opcional.",
        nuevo_precio="Nuevo precio. Vacío = sin cambio. Opcional.",
        nueva_desc="Nueva descripción corta. Vacío = sin cambio. Opcional."
    )
    @is_staff()
    async def editar_item(self, interaction,
                          item: str,
                          nuevo_nombre: str = "",
                          nuevo_precio: int = None,
                          nueva_desc: str = ""):

        await interaction.response.defer()

        try:
            found = await get_item_by_name(item.strip())
            if not found:
                return await interaction.followup.send(
                    f"❌ Item **{item}** no encontrado.", ephemeral=True
                )

            if not any([nuevo_nombre, nuevo_precio is not None, nueva_desc]):
                return await interaction.followup.send(
                    "❌ Debes cambiar al menos un campo.", ephemeral=True
                )

            nombre = nuevo_nombre.strip() or None
            precio = nuevo_precio if nuevo_precio is not None else None
            desc = nueva_desc.strip() or None

            await edit_item(found["id"], nombre=nombre, precio=precio, descripcion=desc)

            cambios = []
            if nombre:
                cambios.append(f"• Nombre: **{found['nombre']}** → **{nombre}**")
            if precio is not None:
                cambios.append(f"• Precio: **{found['precio']}** → **{precio}** {COIN}")
            if desc:
                cambios.append(f"• Descripción: **{desc}**")

            icono = found["icono"] if found["icono"] else "🔹"
            await interaction.followup.send(
                f"✅ Item **{icono} {found['nombre']}** actualizado:\n" + "\n".join(cambios),
                ephemeral=False
            )
        except Exception as e:
            print(f"ERROR editar_item: {e}")
            return await interaction.followup.send(
                "❌ Ocurrió un error al editar el item. Intenta de nuevo más tarde.",
                ephemeral=True
            )

    @editar_item.autocomplete("item")
    async def editar_item_autocomplete(self, interaction: discord.Interaction, current: str):
        items = cache.get_items_cache()
        return [
            app_commands.Choice(name=i["nombre"], value=i["nombre"])
            for i in items
            if current.lower() in i["nombre"].lower()
        ][:25]

    @app_commands.command(name="eliminar_item", description="Elimina un item de la tienda")
    @app_commands.describe(item="Selecciona el item a eliminar")
    @is_staff()
    async def eliminar_item(self, interaction, item: str):
        found = await get_item_by_name(item.strip())
        if not found:
            return await interaction.response.send_message(
                f"❌ Item **{item}** no encontrado.", ephemeral=True
            )
        await delete_item(found["id"])
        icono = found["icono"] if found["icono"] else "🔹"
        await interaction.response.send_message(
            f"✅ Item **{icono} {found['nombre']}** eliminado de la tienda.",
            ephemeral=False
        )

    @eliminar_item.autocomplete("item")
    async def eliminar_item_autocomplete(self, interaction: discord.Interaction, current: str):
        items = cache.get_items_cache()
        return [
            app_commands.Choice(name=i['nombre'], value=i['nombre'])
            for i in items
            if current.lower() in i['nombre'].lower()
        ][:25]

    @app_commands.command(name="dropar_item", description="Agrega un item de la tienda al inventario de un usuario")
    @app_commands.describe(
        usuario="Selecciona el usuario destinatario",
        item="Selecciona el item a dropar",
        cantidad="Cantidad a agregar"
    )
    @is_staff()
    async def dropar_item(self, interaction, usuario: discord.Member, item: str, cantidad: int):
        if cantidad <= 0:
            return await interaction.response.send_message(
                "❌ La cantidad debe ser mayor a 0.", ephemeral=True
            )

        found = await get_item_by_name(item.strip())
        if not found:
            return await interaction.response.send_message(
                f"❌ Item **{item}** no encontrado.", ephemeral=True
            )

        await add_to_inventory(usuario.id, found["id"], cantidad)
        cache.add_to_inventory_cache(usuario.id, {
            "id": found["id"],
            "nombre": found["nombre"],
            "icono": found["icono"] or "",
            "utilizable": found["utilizable"],
            "mensaje_uso": found["mensaje_uso"],
            "rol_id": found["rol_id"],
            "duracion": found.get("duracion", 0),
            "cantidad": cantidad
        })

        icono = found["icono"] if found["icono"] else "🔹"
        await interaction.response.send_message(
            f"✅ Se han agregado {cantidad}x {icono} {found['nombre']} al inventario de {usuario.mention}.",
            ephemeral=False
        )

    @dropar_item.autocomplete("item")
    async def dropar_item_autocomplete(self, interaction: discord.Interaction, current: str):
        items = cache.get_items_cache()
        return [
            app_commands.Choice(name=i["nombre"], value=i["nombre"])
            for i in items
            if current.lower() in i["nombre"].lower()
        ][:25]

    @app_commands.command(name="economia", description="Examina la economía completa")
    @is_staff()
    async def economia(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="📊 Economía",
            description="Selecciona una opción para examinar la economía de PurpleJack.",
            color=discord.Color.dark_purple()
        )
        await interaction.response.send_message(
            embed=embed,
            view=EconomiaView(interaction.user.id),
            ephemeral=False
        )

    @app_commands.command(name="stock_add", description="Añade stock a un item de la tienda")
    @app_commands.describe(nombre="Selecciona el item", cantidad="Cantidad de stock a añadir")
    @is_staff()
    async def stock_add(self, interaction, nombre: str, cantidad: int):
        if cantidad <= 0:
            return await interaction.response.send_message("❌ La cantidad debe ser mayor a 0.", ephemeral=True)
        item = await get_item_by_name(nombre.strip())
        if not item:
            return await interaction.response.send_message(f"❌ Item **{nombre}** no encontrado.", ephemeral=True)
        if item["stock"] == -1:
            return await interaction.response.send_message(f"❌ **{item['nombre']}** tiene stock infinito, no aplica.", ephemeral=True)
        await add_stock(item["id"], cantidad)
        nuevo_stock = item["stock"] + cantidad
        await interaction.response.send_message(
            f"✅ Stock de **{item['nombre']}** actualizado: `{item['stock']}` → `{nuevo_stock}`",
            ephemeral=False
        )

    @stock_add.autocomplete("nombre")
    async def stock_add_autocomplete(self, interaction: discord.Interaction, current: str):
        items = cache.get_items_cache()
        return [
            app_commands.Choice(name=i["nombre"], value=i["nombre"])
            for i in items
            if current.lower() in i["nombre"].lower()
        ][:25]

    @app_commands.command(name="collect_config", description="Configura collect para un rol")
    @app_commands.describe(rol="Rol", cantidad="PurpleCoins a otorgar", cooldown="Cooldown: ej 2h o 30m")
    @is_staff()
    async def collect_config(self, interaction, rol: discord.Role, cantidad: int, cooldown: str):
        if cantidad <= 0:
            return await interaction.response.send_message("❌ Cantidad debe ser mayor a 0.", ephemeral=True)
        cooldown = cooldown.strip().lower()
        if cooldown.endswith("h"):
            try:
                cooldown_horas = int(cooldown[:-1])
            except ValueError:
                return await interaction.response.send_message("❌ Formato inválido. Usa: 2h o 30m", ephemeral=True)
        elif cooldown.endswith("m"):
            try:
                minutos = int(cooldown[:-1])
                cooldown_horas = minutos / 60
            except ValueError:
                return await interaction.response.send_message("❌ Formato inválido. Usa: 2h o 30m", ephemeral=True)
        else:
            return await interaction.response.send_message("❌ Formato inválido. Usa: 2h o 30m", ephemeral=True)

        if cooldown_horas <= 0:
            return await interaction.response.send_message("❌ El cooldown debe ser mayor a 0.", ephemeral=True)

        await upsert_collect_config_db(rol.id, cantidad, cooldown_horas)
        await interaction.response.send_message(
            f"✅ Collect configurado: {rol.mention} → {COIN} **{cantidad}** cada **{cooldown}**.", ephemeral=False
        )

    @app_commands.command(name="collect_eliminar", description="Elimina el collect de un rol")
    @app_commands.describe(rol="Rol a eliminar")
    @is_staff()
    async def collect_eliminar(self, interaction, rol: discord.Role):
        config = cache.get_collect_config()
        if rol.id not in config:
            return await interaction.response.send_message(f"❌ {rol.mention} no tiene collect configurado.", ephemeral=True)
        await delete_collect_config_db(rol.id)
        await interaction.response.send_message(f"✅ Collect de {rol.mention} eliminado.", ephemeral=False)

    @app_commands.command(name="work_edit", description="Edita configuración de !work")
    @app_commands.describe(minimo="Mínimo de coins", maximo="Máximo de coins", cooldown="Cooldown: ej 4h o 30m")
    @is_staff()
    async def work_edit(self, interaction, minimo: int, maximo: int, cooldown: str):
        try:
            seconds = self.parse_cooldown(cooldown)
        except:
            return await interaction.response.send_message(
                "❌ Formato inválido. Usa ejemplos como: 6h o 30m", ephemeral=True
            )
        game_config["work"]["min"] = minimo
        game_config["work"]["max"] = maximo
        game_config["work"]["cooldown"] = seconds
        await save_game_config()
        await interaction.response.send_message(
            f"✅ Work actualizado:\n• Min: {minimo}\n• Max: {maximo}\n• Cooldown: {cooldown}",
            ephemeral=False
        )

    @app_commands.command(name="crime_edit", description="Edita configuración de !crime")
    @app_commands.describe(
        minimo="Min coins",
        maximo="Max coins",
        cooldown="CD: ej 8h o 30m",
        ganar_prob="Prob. éxito en % (ej: 100 o 70).",
        perder_prob="Prob. fallo en % (ej: 0 o 30)."
    )
    @is_staff()
    async def crime_edit(self, interaction, minimo: int, maximo: int, cooldown: str,
                         ganar_prob: float = None, perder_prob: float = None):
        try:
            seconds = self.parse_cooldown(cooldown)
        except:
            return await interaction.response.send_message(
                "❌ Formato inválido. Usa ejemplos como: 6h o 30m", ephemeral=True
            )

        def parse_prob(value):
            if value is None:
                return None
            if value > 1:
                if value > 100:
                    raise ValueError
                value = value / 100
            if value < 0 or value > 1:
                raise ValueError
            return value

        try:
            ganar = parse_prob(ganar_prob)
            perder = parse_prob(perder_prob)
        except ValueError:
            return await interaction.response.send_message(
                "❌ Probabilidades inválidas. Usa % como 70 o fracción 0.7.", ephemeral=True
            )

        if ganar is None and perder is None:
            ganar = game_config["crime"]["ganar_prob"]
            perder = game_config["crime"]["perder_prob"]
        elif ganar is None:
            ganar = 1 - perder
        elif perder is None:
            perder = 1 - ganar

        if abs((ganar + perder) - 1) > 0.01:
            return await interaction.response.send_message(
                "❌ Las probabilidades deben sumar 100%.", ephemeral=True
            )

        game_config["crime"]["min"] = minimo
        game_config["crime"]["max"] = maximo
        game_config["crime"]["cooldown"] = seconds
        game_config["crime"]["ganar_prob"] = ganar
        game_config["crime"]["perder_prob"] = perder
        await save_game_config()
        await interaction.response.send_message(
            f"✅ Crime actualizado:\n• Min: {minimo}\n• Max: {maximo}\n• CD: {cooldown}\n• Éxito: {int(ganar*100)}%\n• Fallo: {int(perder*100)}%",
            ephemeral=False
        )

    @app_commands.command(name="rob_alternar", description="Activa o desactiva el sistema de robos")
    @is_staff()
    async def rob_alternar(self, interaction):
        from core.config import rob_config
        rob_config["activa"] = not rob_config["activa"]
        await save_rob_config()
        estado = "✅ activado" if rob_config["activa"] else "🔧 desactivado"
        mensaje = "Ejecuta **/rob_alternar** para activar de nuevo." if not rob_config["activa"] else "El sistema de robos ha sido reactivado."
        await interaction.response.send_message(f"El sistema de robos ha sido **{estado}**. {mensaje}", ephemeral=False)

    @app_commands.command(name="rob_edit", description="Configura el sistema de robos")
    @app_commands.describe(
        cooldown="CD: ej 30s, 5m, 1h",
        exito_prob="Prob. éxito en % (ej: 50). Opcional.",
        fallo_prob="Prob. fallo en % (ej: 50). Opcional."
    )
    @is_staff()
    async def rob_edit(self, interaction, cooldown: str, exito_prob: float = None, fallo_prob: float = None):
        from core.config import rob_config
        try:
            seconds = self.parse_cooldown(cooldown)
        except ValueError:
            return await interaction.response.send_message(
                "❌ Formato inválido. Usa ejemplos como: 30s, 5m, 1h", ephemeral=True
            )

        if seconds <= 0:
            return await interaction.response.send_message("❌ El cooldown debe ser mayor a 0.", ephemeral=True)

        def parse_prob(value):
            if value is None:
                return None
            if value > 1:
                if value > 100:
                    raise ValueError
                value = value / 100
            if value < 0 or value > 1:
                raise ValueError
            return value

        try:
            exito = parse_prob(exito_prob)
            fallo = parse_prob(fallo_prob)
        except ValueError:
            return await interaction.response.send_message(
                "❌ Probabilidades inválidas. Usa % como 50 o fracción 0.5.", ephemeral=True
            )

        if exito is None and fallo is None:
            exito = rob_config["exito_prob"]
            fallo = rob_config["fallo_prob"]
        elif exito is None:
            exito = 1 - fallo
        elif fallo is None:
            fallo = 1 - exito

        if abs((exito + fallo) - 1) > 0.01:
            return await interaction.response.send_message(
                "❌ Las probabilidades deben sumar 100%.", ephemeral=True
            )

        rob_config["cooldown"] = seconds
        rob_config["exito_prob"] = exito
        rob_config["fallo_prob"] = fallo
        await save_rob_config()
        cache.clear_rob_cooldowns_cache()
        await interaction.response.send_message(
            f"✅ Rob actualizado:\n• CD: {cooldown}\n• Éxito: {int(exito*100)}%\n• Fallo: {int(fallo*100)}%",
            ephemeral=False
        )

    @app_commands.command(name="anunciar", description="Envía un anuncio a un canal escogido")
    @app_commands.describe(canal="Canal donde se enviará el anuncio")
    @is_staff()
    async def anunciar(self, interaction: discord.Interaction, canal: discord.TextChannel):
        try:
            role = discord.utils.get(interaction.user.roles, name=COORDINADOR_ROLE)
            if not role:
                return await interaction.response.send_message(
                    "❌ No tienes permisos para usar Anuncios.", ephemeral=True
                )
            import time
            user_id = interaction.user.id
            _pending_announcements[user_id] = {
                "channel": canal,
                "expires": time.time() + 300,
                "content": None
            }
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="📣 Anunciador listo",
                    description=(
                        f"Escribe tu mensaje **en este canal** y lo reenviaré a {canal.mention}.\n\n"
                        f"⏳ Tienes **5 minutos**. Escribe normalmente, nadie más lo verá."
                    ),
                    color=discord.Color.purple()
                ),
                ephemeral=True
            )

            async def auto_clear():
                await asyncio.sleep(300)
                entry = _pending_announcements.get(user_id)
                if entry and entry["content"] is None:
                    _pending_announcements.pop(user_id, None)

            asyncio.create_task(auto_clear())

        except Exception as e:
            import traceback
            traceback.print_exc()
            try:
                await interaction.response.send_message(f"❌ Error interno: {e}", ephemeral=True)
            except Exception:
                pass

    @app_commands.command(name="nave_edit", description="Actualiza la Guía de la Nave-Sus")
    @is_staff()
    async def nave_edit(self, interaction: discord.Interaction):
        try:
            role = discord.utils.get(interaction.user.roles, name=COORDINADOR_ROLE)
            if not role:
                return await interaction.response.send_message(
                    "❌ No tienes permisos para usar este comando.", ephemeral=True
                )
            import time
            user_id = interaction.user.id
            ahora = time.time()

            _pending_nave[user_id] = {
                "channel": interaction.channel,
                "expires": ahora + 300,
                "content": None
            }

            await interaction.response.send_message(
                embed=discord.Embed(
                    title="📋 Guía de la Nave-Sus — Edición",
                    description=(
                        "Escribe el contenido de la guía **en este canal**.\n\n"
                        "⏳ Tienes **5 minutos**. Tu mensaje será borrado automáticamente."
                    ),
                    color=discord.Color.teal()
                ),
                ephemeral=True
            )

            async def auto_clear():
                await asyncio.sleep(300)
                entry = _pending_nave.get(user_id)
                if entry and entry["content"] is None:
                    _pending_nave.pop(user_id, None)

            asyncio.create_task(auto_clear())

        except Exception as e:
            import traceback
            traceback.print_exc()
            try:
                await interaction.response.send_message(f"❌ Error interno: {e}", ephemeral=True)
            except Exception:
                pass

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        import time
        user_id = message.author.id

        # ── Nave edit listener ─────────────────────────
        nave_entry = _pending_nave.get(user_id)
        if nave_entry and nave_entry["content"] is None:
            if time.time() > nave_entry["expires"]:
                _pending_nave.pop(user_id, None)
            elif message.channel.id == nave_entry["channel"].id:
                nave_entry["content"] = message.content
                confirm_embed = discord.Embed(
                    title="📋 Confirma el contenido de la Guía",
                    description=f"**Contenido a guardar:**\n\n{message.content}",
                    color=discord.Color.teal()
                )
                confirm_embed.set_footer(text="Tienes 60 segundos para confirmar.")
                await message.channel.send(
                    embed=confirm_embed,
                    view=NaveConfirmView(user_id, message.content, message),
                    delete_after=65
                )
                return

        # ── Anuncio listener ───────────────────────────
        entry = _pending_announcements.get(user_id)
        if not entry or entry["content"] is not None:
            return
        if time.time() > entry["expires"]:
            _pending_announcements.pop(user_id, None)
            return

        content = message.content
        entry["content"] = content

        try:
            await message.delete()
        except Exception:
            pass

        confirm_embed = discord.Embed(
            title="📋 Confirma tu anuncio",
            description=(
                f"**Canal destino:** {entry['channel'].mention}\n\n"
                f"**Mensaje:**\n{content}"
            ),
            color=discord.Color.orange()
        )
        confirm_embed.set_footer(text="Tienes 60 segundos para confirmar.")

        try:
            await message.author.send(
                embed=confirm_embed,
                view=AnuncioConfirmView(user_id, entry["channel"], content)
            )
        except discord.Forbidden:
            await message.channel.send(
                embed=confirm_embed,
                view=AnuncioConfirmView(user_id, entry["channel"], content),
                delete_after=60
            )


    @app_commands.command(name="retar_edit", description="Cambia el cooldown del comando !retar")
    @app_commands.describe(cooldown="Cooldown en segundos")
    @is_staff()
    async def retar_edit(self, interaction, cooldown: int):
        if cooldown < 0:
            return await interaction.response.send_message("❌ El cooldown no puede ser negativo.", ephemeral=True)
        
        from modules.duels import _duel_cooldowns
        _duel_cooldowns[interaction.guild.id] = cooldown
        await interaction.response.send_message(f"✅ Cooldown de !retar cambiado a {cooldown} segundos.", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Staff(bot))
