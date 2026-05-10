import discord
from discord.ext import commands
from discord import app_commands
from core.database import (
    get_user, update_balance, update_bank, pool,
    add_item, edit_item, delete_item,
    get_item_by_name, load_items_to_cache,
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

# ── ITEM NEW STATE ─────────────────────────────────────

class ItemState:
    def __init__(self):
        self.nombre = None
        self.descripcion = None
        self.descripcion_larga = None
        self.precio = None
        self.cantidad = 1
        self.stock = -1
        self.icono = ""
        self.utilizable = False
        self.mensaje_uso = ""
        self.rol_id = None
        self.duracion = 0

    def resumen(self):
        campos = [
            ("Nombre",         self.nombre or "❌ No definido"),
            ("Descripción",    self.descripcion or "❌ No definido"),
            ("Precio",         f"{self.precio} {COIN}" if self.precio is not None else "❌ No definido"),
            ("Cantidad",       str(self.cantidad)),
            ("Desc. Larga",    "✅ Definida" if self.descripcion_larga else "❌ No definida"),
            ("Stock",          "∞ Ilimitado" if self.stock == -1 else ("❌ Agotado" if self.stock == 0 else str(self.stock))),
            ("Icono",          self.icono if self.icono else "❌ No definido"),
            ("Usable",         "✅ Sí" if self.utilizable else "❌ No"),
            ("Cargo (Rol ID)", str(self.rol_id) if self.rol_id else "❌ No definido"),
            ("Duración",       f"{self.duracion}d" if self.duracion else "Permanente"),
        ]
        return "\n".join(f"**{k}:** {v}" for k, v in campos)

    def listo(self):
        return self.nombre and self.descripcion and self.precio is not None


# ── MODALES ITEM NEW ───────────────────────────────────

class ModalNombre(discord.ui.Modal, title="Nombre del Item"):
    nombre = discord.ui.TextInput(label="Nombre", placeholder="Ej: Estrella Dorada", max_length=50)

    def __init__(self, state, view):
        super().__init__()
        self.state = state
        self._view = view

    async def on_submit(self, interaction):
        self.state.nombre = self.nombre.value.strip()
        await interaction.response.edit_message(content=self.state.resumen(), view=self._view)


class ModalDescripcion(discord.ui.Modal, title="Descripción Corta"):
    descripcion = discord.ui.TextInput(label="Descripción corta", placeholder="Visible en !tienda", max_length=100)

    def __init__(self, state, view):
        super().__init__()
        self.state = state
        self._view = view

    async def on_submit(self, interaction):
        self.state.descripcion = self.descripcion.value.strip()
        await interaction.response.edit_message(content=self.state.resumen(), view=self._view)


class ModalPrecio(discord.ui.Modal, title="Precio del Item"):
    precio = discord.ui.TextInput(label="Precio", placeholder="Ej: 500")
    cantidad = discord.ui.TextInput(label="Cantidad al inventario", placeholder="Ej: 1", default="1")

    def __init__(self, state, view):
        super().__init__()
        self.state = state
        self._view = view

    async def on_submit(self, interaction):
        try:
            self.state.precio = int(self.precio.value.strip())
            self.state.cantidad = int(self.cantidad.value.strip()) if self.cantidad.value.strip().isdigit() else 1
        except ValueError:
            return await interaction.response.send_message("❌ Precio inválido.", ephemeral=True)
        await interaction.response.edit_message(content=self.state.resumen(), view=self._view)


class ModalDescLarga(discord.ui.Modal, title="Descripción Larga"):
    descripcion_larga = discord.ui.TextInput(
        label="Descripción larga", placeholder="Detalle completo visible en !info",
        style=discord.TextStyle.paragraph, max_length=1000, required=False
    )

    def __init__(self, state, view):
        super().__init__()
        self.state = state
        self._view = view

    async def on_submit(self, interaction):
        self.state.descripcion_larga = self.descripcion_larga.value.strip()
        await interaction.response.edit_message(content=self.state.resumen(), view=self._view)


class ModalStock(discord.ui.Modal, title="Stock del Item"):
    stock = discord.ui.TextInput(label="Stock (-1=ilimitado, 0=agotado, n=cantidad)", placeholder="Ej: 10 o -1")

    def __init__(self, state, view):
        super().__init__()
        self.state = state
        self._view = view

    async def on_submit(self, interaction):
        try:
            self.state.stock = int(self.stock.value.strip())
        except ValueError:
            return await interaction.response.send_message("❌ Stock inválido.", ephemeral=True)
        await interaction.response.edit_message(content=self.state.resumen(), view=self._view)


class ModalIcono(discord.ui.Modal, title="Ícono del Item"):
    icono = discord.ui.TextInput(label="ID del emoji", placeholder="Ej: <:nombre:123456789>", required=False)

    def __init__(self, state, view):
        super().__init__()
        self.state = state
        self._view = view

    async def on_submit(self, interaction):
        self.state.icono = self.icono.value.strip()
        await interaction.response.edit_message(content=self.state.resumen(), view=self._view)


class ModalUsable(discord.ui.Modal, title="¿Item Usable?"):
    usable = discord.ui.TextInput(label="¿Usable? (si/no)", placeholder="si o no")
    mensaje_uso = discord.ui.TextInput(label="Mensaje al usar", placeholder="Ej: ¡Usaste una estrella!", required=False)

    def __init__(self, state, view):
        super().__init__()
        self.state = state
        self._view = view

    async def on_submit(self, interaction):
        self.state.utilizable = self.usable.value.lower().strip() == "si"
        self.state.mensaje_uso = self.mensaje_uso.value.strip()
        await interaction.response.edit_message(content=self.state.resumen(), view=self._view)


class ModalCargo(discord.ui.Modal, title="Cargo (Rol)"):
    rol_id = discord.ui.TextInput(label="ID del Rol", placeholder="Ej: 123456789012345678", required=False)
    duracion = discord.ui.TextInput(label="Duración en días (0=permanente)", placeholder="Ej: 30 o 0", default="0")

    def __init__(self, state, view):
        super().__init__()
        self.state = state
        self._view = view

    async def on_submit(self, interaction):
        import re
        rid = self.rol_id.value.strip()
        match = re.search(r'\d+', rid)
        self.state.rol_id = int(match.group()) if match else None
        try:
            self.state.duracion = int(self.duracion.value.strip())
        except ValueError:
            self.state.duracion = 0
        await interaction.response.edit_message(content=self.state.resumen(), view=self._view)


# ── VISTA ITEM NEW ─────────────────────────────────────

class ItemNewView(discord.ui.View):
    def __init__(self, author_id):
        super().__init__(timeout=300)
        self.author_id = author_id
        self.state = ItemState()

    async def interaction_check(self, interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Este panel no fue generado por ti.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="✏️ Nombre", style=discord.ButtonStyle.secondary, row=0)
    async def btn_nombre(self, interaction, button):
        await interaction.response.send_modal(ModalNombre(self.state, self))

    @discord.ui.button(label="📋 Descripción", style=discord.ButtonStyle.secondary, row=0)
    async def btn_descripcion(self, interaction, button):
        await interaction.response.send_modal(ModalDescripcion(self.state, self))

    @discord.ui.button(label="💰 Precio", style=discord.ButtonStyle.secondary, row=0)
    async def btn_precio(self, interaction, button):
        await interaction.response.send_modal(ModalPrecio(self.state, self))

    @discord.ui.button(label="📖 Desc. Larga", style=discord.ButtonStyle.secondary, row=1)
    async def btn_desc_larga(self, interaction, button):
        await interaction.response.send_modal(ModalDescLarga(self.state, self))

    @discord.ui.button(label="📦 Stock", style=discord.ButtonStyle.secondary, row=1)
    async def btn_stock(self, interaction, button):
        await interaction.response.send_modal(ModalStock(self.state, self))

    @discord.ui.button(label="🖼️ Ícono", style=discord.ButtonStyle.secondary, row=1)
    async def btn_icono(self, interaction, button):
        await interaction.response.send_modal(ModalIcono(self.state, self))

    @discord.ui.button(label="🎯 Usable", style=discord.ButtonStyle.secondary, row=2)
    async def btn_usable(self, interaction, button):
        await interaction.response.send_modal(ModalUsable(self.state, self))

    @discord.ui.button(label="👤 Cargo", style=discord.ButtonStyle.secondary, row=2)
    async def btn_cargo(self, interaction, button):
        await interaction.response.send_modal(ModalCargo(self.state, self))

    @discord.ui.button(label="✅ Agregar Item", style=discord.ButtonStyle.success, row=3)
    async def btn_agregar(self, interaction, button):
        if not self.state.listo():
            return await interaction.response.send_message(
                "❌ Faltan campos obligatorios: **Nombre**, **Descripción** y **Precio**.", ephemeral=True
            )
        existing = await get_item_by_name(self.state.nombre)
        if existing:
            return await interaction.response.send_message(
                f"❌ Ya existe un item llamado **{self.state.nombre}**.", ephemeral=True
            )
        await add_item(
            nombre=self.state.nombre,
            descripcion=self.state.descripcion,
            descripcion_larga=self.state.descripcion_larga or "",
            precio=self.state.precio,
            cantidad=self.state.cantidad,
            stock=self.state.stock,
            icono=self.state.icono,
            utilizable=self.state.utilizable,
            mensaje_uso=self.state.mensaje_uso,
            rol_id=self.state.rol_id,
            duracion=self.state.duracion
        )
        self.stop()
        # Solo una respuesta — edit_message es suficiente
        await interaction.response.edit_message(
            content=f"✅ Item **{self.state.nombre}** agregado a la tienda exitosamente.", view=None
        )

    @discord.ui.button(label="🚫 Cancelar", style=discord.ButtonStyle.danger, row=3)
    async def btn_cancelar(self, interaction, button):
        self.stop()
        await interaction.response.edit_message(content="❌ Creación de item cancelada.", view=None)


# ── MODALES EDITAR/ELIMINAR ────────────────────────────

class EditItemModal(discord.ui.Modal, title="Editar Item"):
    nombre_actual = discord.ui.TextInput(label="Nombre actual del item")
    nuevo_nombre = discord.ui.TextInput(label="Nuevo nombre (vacío = sin cambio)", required=False)
    nuevo_precio = discord.ui.TextInput(label="Nuevo precio (vacío = sin cambio)", required=False)
    nuevo_stock = discord.ui.TextInput(label="Nuevo stock (vacío = sin cambio)", required=False)

    async def on_submit(self, interaction):
        item = await get_item_by_name(self.nombre_actual.value.strip())
        if not item:
            return await interaction.response.send_message(f"❌ Item no encontrado.", ephemeral=True)
        nombre = self.nuevo_nombre.value.strip() or None
        precio = int(self.nuevo_precio.value.strip()) if self.nuevo_precio.value.strip().isdigit() else None
        stock = int(self.nuevo_stock.value.strip()) if self.nuevo_stock.value.strip().lstrip("-").isdigit() else None
        await edit_item(item["id"], nombre=nombre, precio=precio, stock=stock)
        await interaction.response.send_message(f"✅ Item actualizado.", ephemeral=True)


class DeleteItemModal(discord.ui.Modal, title="Eliminar Item"):
    nombre = discord.ui.TextInput(label="Nombre del item a eliminar")

    async def on_submit(self, interaction):
        item = await get_item_by_name(self.nombre.value.strip())
        if not item:
            return await interaction.response.send_message(f"❌ Item no encontrado.", ephemeral=True)
        await delete_item(item["id"])
        await interaction.response.send_message(f"✅ Item eliminado.", ephemeral=True)


# ── STAFF COG ──────────────────────────────────────────

class Staff(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

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
        # Flush inmediato para operaciones administrativas
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

    @app_commands.command(name="ruleta_apuesta_max", description="Configura la apuesta máxima de la ruleta")
    @app_commands.describe(valor="Nuevo valor máximo")
    @is_staff()
    async def ruleta_apuesta_max(self, interaction, valor: int):
        if valor <= 0:
            return await interaction.response.send_message("❌ El valor debe ser mayor a 0.", ephemeral=True)
        ruleta_config["max_apuesta"] = valor
        await interaction.response.send_message(f"✅ Apuesta máxima actualizada a **{valor}** {COIN}.", ephemeral=False)

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
    async def rr_max_apuesta(
        self,
        interaction,
        monto: int,
        cooldown: str,
        ganar_prob: float = None,
        perder_prob: float = None
    ):
        if monto <= 0:
            return await interaction.response.send_message("❌ El monto debe ser mayor a 0.", ephemeral=True)

        try:
            cooldown_seconds = self.parse_cooldown(cooldown)
        except ValueError:
            return await interaction.response.send_message(
                "❌ Formato inválido. Usa ejemplos como: 30s, 5m, 1h", ephemeral=True
            )

        if ganar_prob is not None and ganar_prob <= 0:
            return await interaction.response.send_message(
                "❌ Gan_Prob debe ser mayor a 0.", ephemeral=True
            )
        if perder_prob is not None and perder_prob <= 0:
            return await interaction.response.send_message(
                "❌ Perd_Prob debe ser mayor a 0.", ephemeral=True
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
                "❌ Probabilidades inválidas. Usa valores en % como 70 o en fracción 0.7.",
                ephemeral=True
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

    @app_commands.command(name="ruleta_apuesta_max", description="Configura la ruleta")
    @app_commands.describe(
        monto="Apuesta máxima",
        cooldown="Cooldown: ej 30s, 5m, 1h"
    )
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
            f"✅ Ruleta configurada:\n"
            f"• Apuesta máxima: **{monto}** {COIN}\n"
            f"• Cooldown: **{cooldown}**",
            ephemeral=False
        )

    @app_commands.command(name="add_item", description="Agrega un nuevo item a la tienda")
    @is_staff()
    async def item_new(self, interaction):
        view = ItemNewView(interaction.user.id)
        await interaction.response.send_message(content=view.state.resumen(), view=view, ephemeral=True)

    @app_commands.command(name="editar_item", description="Edita un item de la tienda")
    @is_staff()
    async def editar_item(self, interaction):
        await interaction.response.send_modal(EditItemModal())

    @app_commands.command(name="eliminar_item", description="Elimina un item de la tienda")
    @is_staff()
    async def eliminar_item(self, interaction):
        await interaction.response.send_modal(DeleteItemModal())

    @app_commands.command(name="collect_config", description="Configura collect para un rol")
    @app_commands.describe(rol="Rol", cantidad="PurpleCoins a otorgar", cooldown="Cooldown en horas")
    @is_staff()
    async def collect_config(self, interaction, rol: discord.Role, cantidad: int, cooldown: int):
        if cantidad <= 0 or cooldown <= 0:
            return await interaction.response.send_message("❌ Cantidad y cooldown deben ser mayores a 0.", ephemeral=True)
        await upsert_collect_config_db(rol.id, cantidad, cooldown)
        await interaction.response.send_message(
            f"✅ Collect configurado: {rol.mention} → {COIN} **{cantidad}** cada **{cooldown}h**.", ephemeral=False
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
        

    def parse_cooldown(self, value: str):
        value = value.lower().strip()
        if value.endswith("h"):
            return int(value[:-1]) * 3600
        if value.endswith("m"):
            return int(value[:-1]) * 60
        if value.endswith("s"):
            return int(value[:-1])
        raise ValueError("Formato inválido")

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
        await save_game_config()  # persiste en DB
        await interaction.response.send_message(
            f"✅ Work actualizado:\n• Min: {minimo}\n• Max: {maximo}\n• Cooldown: {cooldown}",
            ephemeral=False
        )

    @app_commands.command(name="crime_edit", description="Edita configuración de !crime")
    @app_commands.describe(minimo="Mínimo de coins", maximo="Máximo de coins", cooldown="Cooldown: ej 8h o 30m")
    @is_staff()
    async def crime_edit(self, interaction, minimo: int, maximo: int, cooldown: str):
        try:
            seconds = self.parse_cooldown(cooldown)
        except:
            return await interaction.response.send_message(
                "❌ Formato inválido. Usa ejemplos como: 6h o 30m", ephemeral=True
            )
        game_config["crime"]["min"] = minimo
        game_config["crime"]["max"] = maximo
        game_config["crime"]["cooldown"] = seconds
        await save_game_config()  # persiste en DB
        await interaction.response.send_message(
            f"✅ Crime actualizado:\n• Min: {minimo}\n• Max: {maximo}\n• Cooldown: {cooldown}",
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

    @app_commands.command(name="rob_edit", description="Configura el cooldown de robos")
    @app_commands.describe(cooldown="Cooldown: ej 30s, 5m, 1h")
    @is_staff()
    async def rob_edit(self, interaction, cooldown: str):
        from core.config import rob_config
        try:
            seconds = self.parse_cooldown(cooldown)
        except ValueError:
            return await interaction.response.send_message(
                "❌ Formato inválido. Usa ejemplos como: 30s, 5m, 1h", ephemeral=True
            )
        if seconds <= 0:
            return await interaction.response.send_message("❌ El cooldown debe ser mayor a 0.", ephemeral=True)
        rob_config["cooldown"] = seconds
        await save_rob_config()
        cache.clear_rob_cooldowns_cache()
        await interaction.response.send_message(
            f"✅ Cooldown de robos actualizado a **{cooldown}**.", ephemeral=False
        )
    @app_commands.command(name="anunciar", description="Envía un anuncio a un canal escogido")
    @app_commands.describe(canal="Canal donde se enviará el anuncio")
    async def anunciar(self, interaction: discord.Interaction, canal: discord.TextChannel):
        role = discord.utils.get(interaction.user.roles, name=COORDINADOR_ROLE)
        if not role:
            return await interaction.response.send_message(
                "❌ No tienes permisos para usar Anuncios.", ephemeral=False
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

        # Auto-limpiar si expira sin mensaje
        async def auto_clear():
            import asyncio
            await asyncio.sleep(300)
            entry = _pending_announcements.get(user_id)
            if entry and entry["content"] is None:
                _pending_announcements.pop(user_id, None)

        import asyncio
        asyncio.create_task(auto_clear())

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        import time
        user_id = message.author.id
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
            # Si tiene DMs cerrados, responde efímero en el canal
            await message.channel.send(
                embed=confirm_embed,
                view=AnuncioConfirmView(user_id, entry["channel"], content),
                delete_after=60
            )

async def setup(bot):
    await bot.add_cog(Staff(bot))


