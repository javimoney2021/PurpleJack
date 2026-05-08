import discord
from discord.ext import commands
from discord import app_commands
from core.database import (
    get_user, update_balance, update_bank, pool,
    add_item, edit_item, delete_item, get_all_items,
    get_item_by_name, load_items_to_cache,
    upsert_collect_config_db, delete_collect_config_db
)
from core import cache
from core.config import game_config

STAFF_ROLE = "Equipo de Eventos"
COIN = "<:PurpleCoin:1501855737842892941>"


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


# ── MODALES ECONOMIA ───────────────────────────────────

class ResetAllModal(discord.ui.Modal, title="Confirmar Reset Global"):
    confirmacion = discord.ui.TextInput(
        label='Escribe "Reset" para confirmar',
        placeholder="Reset",
        max_length=5
    )

    async def on_submit(self, interaction: discord.Interaction):
        if self.confirmacion.value != "Reset":
            return await interaction.response.send_message(
                "❌ Confirmación incorrecta. Operación cancelada.", ephemeral=True
            )
        async with pool.acquire() as conn:
            await conn.execute("UPDATE users SET balance=0, bank=0")
        cache._cache.clear()
        cache._dirty.clear()
        await interaction.response.send_message(
            "✅ Reset global completado. Todas las PurpleCoins han sido reiniciadas.", ephemeral=False
        )


# ── ITEM NEW — ESTADO ──────────────────────────────────

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
            ("Nombre",            self.nombre or "❌ No definido"),
            ("Descripción",       self.descripcion or "❌ No definido"),
            ("Precio",            f"{self.precio} {COIN}" if self.precio is not None else "❌ No definido"),
            ("Cantidad",          str(self.cantidad)),
            ("Desc. Larga",       "✅ Definida" if self.descripcion_larga else "❌ No definida"),
            ("Stock",             "∞ Ilimitado" if self.stock == -1 else ("❌ Agotado" if self.stock == 0 else str(self.stock))),
            ("Icono",             self.icono if self.icono else "❌ No definido"),
            ("Usable",            "✅ Sí" if self.utilizable else "❌ No"),
            ("Cargo (Rol ID)",    str(self.rol_id) if self.rol_id else "❌ No definido"),
            ("Duración",          f"{self.duracion}d" if self.duracion else "Permanente"),
        ]
        return "\n".join(f"**{k}:** {v}" for k, v in campos)

    def listo(self):
        return self.nombre and self.descripcion and self.precio is not None


# ── MODALES ITEM NEW ───────────────────────────────────

class ModalNombre(discord.ui.Modal, title="Nombre del Item"):
    nombre = discord.ui.TextInput(label="Nombre", placeholder="Ej: Estrella Dorada", max_length=50)

    def __init__(self, state: ItemState, view):
        super().__init__()
        self.state = state
        self._view = view

    async def on_submit(self, interaction: discord.Interaction):
        self.state.nombre = self.nombre.value.strip()
        await interaction.response.edit_message(content=self.state.resumen(), view=self._view)


class ModalDescripcion(discord.ui.Modal, title="Descripción Corta"):
    descripcion = discord.ui.TextInput(label="Descripción corta", placeholder="Visible en !tienda", max_length=100)

    def __init__(self, state: ItemState, view):
        super().__init__()
        self.state = state
        self._view = view

    async def on_submit(self, interaction: discord.Interaction):
        self.state.descripcion = self.descripcion.value.strip()
        await interaction.response.edit_message(content=self.state.resumen(), view=self._view)


class ModalPrecio(discord.ui.Modal, title="Precio del Item"):
    precio = discord.ui.TextInput(label="Precio", placeholder="Ej: 500")
    cantidad = discord.ui.TextInput(label="Cantidad al inventario", placeholder="Ej: 1", default="1")

    def __init__(self, state: ItemState, view):
        super().__init__()
        self.state = state
        self._view = view

    async def on_submit(self, interaction: discord.Interaction):
        try:
            self.state.precio = int(self.precio.value.strip())
            self.state.cantidad = int(self.cantidad.value.strip()) if self.cantidad.value.strip().isdigit() else 1
        except ValueError:
            return await interaction.response.send_message("❌ Precio inválido.", ephemeral=True)
        await interaction.response.edit_message(content=self.state.resumen(), view=self._view)


class ModalDescLarga(discord.ui.Modal, title="Descripción Larga"):
    descripcion_larga = discord.ui.TextInput(
        label="Descripción larga",
        placeholder="Detalle completo visible en !info",
        style=discord.TextStyle.paragraph,
        max_length=1000,
        required=False
    )

    def __init__(self, state: ItemState, view):
        super().__init__()
        self.state = state
        self._view = view

    async def on_submit(self, interaction: discord.Interaction):
        self.state.descripcion_larga = self.descripcion_larga.value.strip()
        await interaction.response.edit_message(content=self.state.resumen(), view=self._view)


class ModalStock(discord.ui.Modal, title="Stock del Item"):
    stock = discord.ui.TextInput(label="Stock (-1=ilimitado, 0=agotado, n=cantidad)", placeholder="Ej: 10 o -1")

    def __init__(self, state: ItemState, view):
        super().__init__()
        self.state = state
        self._view = view

    async def on_submit(self, interaction: discord.Interaction):
        try:
            self.state.stock = int(self.stock.value.strip())
        except ValueError:
            return await interaction.response.send_message("❌ Stock inválido.", ephemeral=True)
        await interaction.response.edit_message(content=self.state.resumen(), view=self._view)


class ModalIcono(discord.ui.Modal, title="Ícono del Item"):
    icono = discord.ui.TextInput(label="ID del emoji", placeholder="Ej: <:nombre:123456789>", required=False)

    def __init__(self, state: ItemState, view):
        super().__init__()
        self.state = state
        self._view = view

    async def on_submit(self, interaction: discord.Interaction):
        self.state.icono = self.icono.value.strip()
        await interaction.response.edit_message(content=self.state.resumen(), view=self._view)


class ModalUsable(discord.ui.Modal, title="¿Item Usable?"):
    usable = discord.ui.TextInput(label="¿Usable? (si/no)", placeholder="si o no")
    mensaje_uso = discord.ui.TextInput(
        label="Mensaje al comprar/usar (si es usable)",
        placeholder="Ej: ¡Usaste una estrella!",
        required=False
    )

    def __init__(self, state: ItemState, view):
        super().__init__()
        self.state = state
        self._view = view

    async def on_submit(self, interaction: discord.Interaction):
        self.state.utilizable = self.usable.value.lower().strip() == "si"
        self.state.mensaje_uso = self.mensaje_uso.value.strip()
        await interaction.response.edit_message(content=self.state.resumen(), view=self._view)


class ModalCargo(discord.ui.Modal, title="Cargo (Rol)"):
    rol_id = discord.ui.TextInput(
        label="ID del Rol a otorgar",
        placeholder="Ej: 123456789012345678",
        required=False
    )
    duracion = discord.ui.TextInput(
        label="Duración en días (0 = permanente)",
        placeholder="Ej: 30 o 0",
        default="0"
    )

    def __init__(self, state: ItemState, view):
        super().__init__()
        self.state = state
        self._view = view

    async def on_submit(self, interaction: discord.Interaction):
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
    def __init__(self, author_id: int):
        super().__init__(timeout=300)
        self.author_id = author_id
        self.state = ItemState()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Este panel no fue generado por ti.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="✏️ Nombre", style=discord.ButtonStyle.secondary, row=0)
    async def btn_nombre(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ModalNombre(self.state, self))

    @discord.ui.button(label="📋 Descripción", style=discord.ButtonStyle.secondary, row=0)
    async def btn_descripcion(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ModalDescripcion(self.state, self))

    @discord.ui.button(label="💰 Precio", style=discord.ButtonStyle.secondary, row=0)
    async def btn_precio(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ModalPrecio(self.state, self))

    @discord.ui.button(label="📖 Desc. Larga", style=discord.ButtonStyle.secondary, row=1)
    async def btn_desc_larga(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ModalDescLarga(self.state, self))

    @discord.ui.button(label="📦 Stock", style=discord.ButtonStyle.secondary, row=1)
    async def btn_stock(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ModalStock(self.state, self))

    @discord.ui.button(label="🖼️ Ícono", style=discord.ButtonStyle.secondary, row=1)
    async def btn_icono(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ModalIcono(self.state, self))

    @discord.ui.button(label="🎯 Usable", style=discord.ButtonStyle.secondary, row=2)
    async def btn_usable(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ModalUsable(self.state, self))

    @discord.ui.button(label="👤 Cargo", style=discord.ButtonStyle.secondary, row=2)
    async def btn_cargo(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ModalCargo(self.state, self))

    @discord.ui.button(label="✅ Agregar Item", style=discord.ButtonStyle.success, row=3)
    async def btn_agregar(self, interaction: discord.Interaction, button: discord.ui.Button):
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
        await interaction.response.edit_message(
            content=f"✅ Item **{self.state.nombre}** agregado a la tienda exitosamente.", view=None
        )
        await interaction.followup.send(
            f"✅ Item **{self.state.nombre}** agregado a la tienda exitosamente.", ephemeral=False
        )

    @discord.ui.button(label="🚫 Cancelar", style=discord.ButtonStyle.danger, row=3)
    async def btn_cancelar(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="❌ Creación de item cancelada.", view=None)


# ── MODALES TIENDA EXISTENTES ──────────────────────────

class EditItemModal(discord.ui.Modal, title="Editar Item"):
    nombre_actual = discord.ui.TextInput(label="Nombre actual del item", placeholder="Ej: Estrella Dorada")
    nuevo_nombre = discord.ui.TextInput(label="Nuevo nombre (vacío = sin cambio)", required=False)
    nuevo_precio = discord.ui.TextInput(label="Nuevo precio (vacío = sin cambio)", required=False)
    nuevo_stock = discord.ui.TextInput(label="Nuevo stock (vacío = sin cambio)", required=False)

    async def on_submit(self, interaction: discord.Interaction):
        item = await get_item_by_name(self.nombre_actual.value.strip())
        if not item:
            return await interaction.response.send_message(
                f"❌ Item `{self.nombre_actual.value}` no encontrado.", ephemeral=True
            )
        nombre = self.nuevo_nombre.value.strip() or None
        precio = int(self.nuevo_precio.value.strip()) if self.nuevo_precio.value.strip().isdigit() else None
        stock = int(self.nuevo_stock.value.strip()) if self.nuevo_stock.value.strip().lstrip("-").isdigit() else None
        await edit_item(item["id"], nombre=nombre, precio=precio, stock=stock)
        await interaction.response.send_message(
            f"✅ Item **{self.nombre_actual.value.strip()}** actualizado.", ephemeral=True
        )


class DeleteItemModal(discord.ui.Modal, title="Eliminar Item"):
    nombre = discord.ui.TextInput(label="Nombre del item a eliminar", placeholder="Ej: Estrella Dorada")

    async def on_submit(self, interaction: discord.Interaction):
        item = await get_item_by_name(self.nombre.value.strip())
        if not item:
            return await interaction.response.send_message(
                f"❌ Item `{self.nombre.value}` no encontrado.", ephemeral=True
            )
        await delete_item(item["id"])
        await interaction.response.send_message(
            f"✅ Item **{self.nombre.value.strip()}** eliminado de la tienda.", ephemeral=True
        )


# ── STAFF COG ──────────────────────────────────────────

class Staff(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="balance", description="Ver balance y banco de un miembro")
    @app_commands.describe(usuario="Miembro a consultar")
    @is_staff()
    async def balance_staff(self, interaction: discord.Interaction, usuario: discord.Member):
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
    async def addcoins(self, interaction: discord.Interaction, usuario: discord.Member, cantidad: int, destino: app_commands.Choice[str]):
        if cantidad <= 0:
            return await interaction.response.send_message("❌ La cantidad debe ser mayor a 0.", ephemeral=False)
        if destino.value == "balance":
            await update_balance(usuario.id, cantidad)
        else:
            await update_bank(usuario.id, cantidad)
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
    async def removecoins(self, interaction: discord.Interaction, usuario: discord.Member, cantidad: int, destino: app_commands.Choice[str]):
        if cantidad <= 0:
            return await interaction.response.send_message("❌ La cantidad debe ser mayor a 0.", ephemeral=False)
        if destino.value == "balance":
            await update_balance(usuario.id, -cantidad)
        else:
            await update_bank(usuario.id, -cantidad)
        await interaction.response.send_message(
            f"✅ Se removieron **{cantidad}** {COIN} del **{destino.name}** de {usuario.mention}.", ephemeral=False
        )

    @app_commands.command(name="resetallcoins", description="Resetea las PurpleCoins de TODOS los miembros")
    @is_staff()
    async def resetallcoins(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ResetAllModal())

    @app_commands.command(name="ruleta_apuesta_max", description="Configura la apuesta máxima de la ruleta")
    @app_commands.describe(valor="Nuevo valor máximo de apuesta")
    @is_staff()
    async def ruleta_apuesta_max(self, interaction: discord.Interaction, valor: int):
        if valor <= 0:
            return await interaction.response.send_message("❌ El valor debe ser mayor a 0.", ephemeral=True)
        game_config["ruleta"]["max_apuesta"] = valor
        await interaction.response.send_message(
            f"✅ Apuesta máxima de la ruleta actualizada a **{valor}** {COIN}.", ephemeral=False
        )

    @app_commands.command(name="ruleta_alternar", description="Activa o desactiva la ruleta")
    @is_staff()
    async def ruleta_alternar(self, interaction: discord.Interaction):
        game_config["ruleta"]["activa"] = not game_config["ruleta"]["activa"]
        estado = "✅ activada" if game_config["ruleta"]["activa"] else "🔧 desactivada"
        await interaction.response.send_message(f"La ruleta ha sido **{estado}**.", ephemeral=False)

    @app_commands.command(name="item_new", description="Agrega un nuevo item a la tienda")
    @is_staff()
    async def item_new(self, interaction: discord.Interaction):
        view = ItemNewView(interaction.user.id)
        await interaction.response.send_message(content=view.state.resumen(), view=view, ephemeral=True)

    @app_commands.command(name="editar_item", description="Edita un item de la tienda")
    @is_staff()
    async def editar_item(self, interaction: discord.Interaction):
        await interaction.response.send_modal(EditItemModal())

    @app_commands.command(name="eliminar_item", description="Elimina un item de la tienda")
    @is_staff()
    async def eliminar_item(self, interaction: discord.Interaction):
        await interaction.response.send_modal(DeleteItemModal())

    @app_commands.command(name="collect_config", description="Configura cantidad y cooldown de collect para un rol")
    @app_commands.describe(
        rol="Rol a configurar",
        cantidad="PurpleCoins a otorgar",
        cooldown="Cooldown en horas"
    )
    @is_staff()
    async def collect_config(self, interaction: discord.Interaction, rol: discord.Role, cantidad: int, cooldown: int):
        if cantidad <= 0 or cooldown <= 0:
            return await interaction.response.send_message(
                "❌ Cantidad y cooldown deben ser mayores a 0.", ephemeral=True
            )
        await upsert_collect_config_db(rol.id, cantidad, cooldown)
        await interaction.response.send_message(
            f"✅ Collect configurado: {rol.mention} → {COIN} **{cantidad}** cada **{cooldown}h**.",
            ephemeral=False
        )

    @app_commands.command(name="collect_eliminar", description="Elimina el collect de un rol")
    @app_commands.describe(rol="Rol a eliminar del sistema de collect")
    @is_staff()
    async def collect_eliminar(self, interaction: discord.Interaction, rol: discord.Role):
        config = cache.get_collect_config()
        if rol.id not in config:
            return await interaction.response.send_message(
                f"❌ {rol.mention} no tiene collect configurado.", ephemeral=True
            )
        await delete_collect_config_db(rol.id)
        await interaction.response.send_message(
            f"✅ Collect de {rol.mention} eliminado.", ephemeral=False
        )


    def parse_cooldown(self, value: str):
        value = value.lower().strip()

        if value.endswith("h"):
            return int(value[:-1]) * 3600

        if value.endswith("m"):
            return int(value[:-1]) * 60

        raise ValueError("Formato inválido")

    @app_commands.command(name="work_edit", description="Edita configuración de !work")
    @is_staff()
    async def work_edit(
        self,
        interaction: discord.Interaction,
        minimo: int,
        maximo: int,
        cooldown: str
    ):
        try:
            seconds = self.parse_cooldown(cooldown)
        except:
            return await interaction.response.send_message(
                "❌ Formato inválido. Usa ejemplos como: 6h o 30m",
                ephemeral=True
            )

        game_config["work"]["min"] = minimo
        game_config["work"]["max"] = maximo
        game_config["work"]["cooldown"] = seconds

        await interaction.response.send_message(
            f"✅ Work actualizado:\n"
            f"• Min: {minimo}\n"
            f"• Max: {maximo}\n"
            f"• Cooldown: {cooldown}",
            ephemeral=False
        )

    @app_commands.command(name="crime_edit", description="Edita configuración de !crime")
    @is_staff()
    async def crime_edit(
        self,
        interaction: discord.Interaction,
        minimo: int,
        maximo: int,
        cooldown: str
    ):
        try:
            seconds = self.parse_cooldown(cooldown)
        except:
            return await interaction.response.send_message(
                "❌ Formato inválido. Usa ejemplos como: 6h o 30m",
                ephemeral=True
            )

        game_config["crime"]["min"] = minimo
        game_config["crime"]["max"] = maximo
        game_config["crime"]["cooldown"] = seconds

        await interaction.response.send_message(
            f"✅ Crime actualizado:\n"
            f"• Min: {minimo}\n"
            f"• Max: {maximo}\n"
            f"• Cooldown: {cooldown}",
            ephemeral=False
        )


async def setup(bot):
    await bot.add_cog(Staff(bot))
