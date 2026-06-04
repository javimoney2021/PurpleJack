import ast
import asyncio
import random
import time

import discord
from discord import ButtonStyle, Interaction, ui
from discord.ext import commands

from core.config import COIN
from core.database import pool, update_bank

EMPLEOS = {
    "Limpiador": {
        "salario_min": 250,
        "salario_max": 450,
        "dificultad": "Fácil",
        "xp_requisito": 0,
        "duracion_horas": 3,
        "penalizacion": -500,
        "prob_fallo": 0.20,
        "mensajes_exito": [
            "Has dejado reluciente el área de trabajo y recibes {monto} {COIN}.",
            "Tu limpieza fue impecable y el equipo te premia con {monto} {COIN}.",
            "La jornada quedó perfecta: ganas {monto} {COIN} por tu rendimiento."
        ],
        "mensajes_fallo": [
            "Un pequeño incidente de limpieza te hace perder {monto} {COIN}.",
            "La tarea terminó con un desajuste y pierdes {monto} {COIN}.",
            "Hubo un fallo en la rutina: recibes una penalización de {monto} {COIN}."
        ]
    },
    "ingeniero": {
        "salario_min": 450,
        "salario_max": 750,
        "dificultad": "Media",
        "xp_requisito": 20,
        "duracion_horas": 3,
        "penalizacion": -700,
        "prob_fallo": 0.20,
        "mensajes_exito": [
            "Has reparado con éxito los ajustes del sistema y ganas {monto} {COIN}.",
            "La revisión terminó bien: recibes {monto} {COIN} por tu trabajo.",
            "Tu análisis resolvió el problema y obtienes {monto} {COIN}."
        ],
        "mensajes_fallo": [
            "Un fallo técnico hizo colapsar la revisión y pierdes {monto} {COIN}.",
            "El sistema devolvió un error y tu pago se reduce en {monto} {COIN}.",
            "El ajuste salió mal: recibes una penalización de {monto} {COIN}."
        ]
    },
    "plomero": {
        "salario_min": 700,
        "salario_max": 1100,
        "dificultad": "Difícil",
        "xp_requisito": 30,
        "duracion_horas": 3,
        "penalizacion": -900,
        "prob_fallo": 0.20,
        "mensajes_exito": [
            "Tu revisión técnica dio resultado y ganas {monto} {COIN}.",
            "El mantenimiento quedó en orden: recibes {monto} {COIN}.",
            "Tu trabajo resolvió el problema y obtienes {monto} {COIN}."
        ],
        "mensajes_fallo": [
            "Un desajuste del sistema generó una pérdida de {monto} {COIN}.",
            "El plan falló en el último paso y pierdes {monto} {COIN}.",
            "La revisión terminó con un error y recibes {monto} {COIN} de penalización."
        ]
    }
}

_EMPLEOS_CACHE = {}


def normalizar_empleo(nombre: str) -> str:
    texto = nombre.lower().strip()
    texto = texto.replace("í", "i").replace("á", "a").replace("é", "e").replace("ó", "o").replace("ú", "u")
    return texto


async def init_empleos_tables():
    if not pool:
        return
    async with pool.acquire() as conn:
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS empleos_users (
            user_id BIGINT PRIMARY KEY,
            empleo_actual TEXT DEFAULT NULL,
            dificultad TEXT DEFAULT NULL,
            fecha_contratacion DOUBLE PRECISION DEFAULT 0,
            ultimo_trabajo DOUBLE PRECISION DEFAULT 0,
            historial_reciente_de_jornadas TEXT DEFAULT '[]',
            cooldown_renuncia DOUBLE PRECISION DEFAULT 0,
            progreso_permanencia DOUBLE PRECISION DEFAULT 0,
            ultimo_empleo TEXT DEFAULT NULL,
            progreso_requisito DOUBLE PRECISION DEFAULT 0,
            despedido_inactividad BOOLEAN DEFAULT FALSE,
            exp_laboral INTEGER DEFAULT 0,
            trabajos_exitosos INTEGER DEFAULT 0,
            trabajos_fallidos INTEGER DEFAULT 0,
            total_generado INTEGER DEFAULT 0,
            racha_exitos INTEGER DEFAULT 0
        )
        """)
        for column, definition in [
            ("exp_laboral", "INTEGER DEFAULT 0"),
            ("trabajos_exitosos", "INTEGER DEFAULT 0"),
            ("trabajos_fallidos", "INTEGER DEFAULT 0"),
            ("total_generado", "INTEGER DEFAULT 0"),
            ("racha_exitos", "INTEGER DEFAULT 0"),
        ]:
            await conn.execute(f"ALTER TABLE empleos_users ADD COLUMN IF NOT EXISTS {column} {definition}")
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS empleos_historial (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            empleo TEXT NOT NULL,
            timestamp DOUBLE PRECISION NOT NULL,
            exito BOOLEAN NOT NULL,
            pago INTEGER NOT NULL,
            motivo TEXT NOT NULL
        )
        """)


async def get_empleo_user(user_id):
    cached = _EMPLEOS_CACHE.get(user_id)
    if cached is not None:
        return cached
    if not pool:
        return None
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM empleos_users WHERE user_id=$1", user_id)
    if not row:
        data = {
            "user_id": user_id,
            "empleo_actual": None,
            "dificultad": None,
            "fecha_contratacion": 0,
            "ultimo_trabajo": 0,
            "historial_reciente_de_jornadas": [],
            "cooldown_renuncia": 0,
            "progreso_permanencia": 0,
            "ultimo_empleo": None,
            "progreso_requisito": 0,
            "despedido_inactividad": False,
            "exp_laboral": 0,
            "trabajos_exitosos": 0,
            "trabajos_fallidos": 0,
            "total_generado": 0,
            "racha_exitos": 0,
        }
    else:
        data = dict(row)
        try:
            data["historial_reciente_de_jornadas"] = ast.literal_eval(data["historial_reciente_de_jornadas"])
        except Exception:
            data["historial_reciente_de_jornadas"] = []
    _EMPLEOS_CACHE[user_id] = data
    return data


async def save_empleo_user(data):
    if not pool:
        return
    hist = data.get("historial_reciente_de_jornadas", [])
    hist_json = repr(hist)
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO empleos_users (
                user_id, empleo_actual, dificultad, fecha_contratacion,
                ultimo_trabajo, historial_reciente_de_jornadas,
                cooldown_renuncia, progreso_permanencia,
                ultimo_empleo, progreso_requisito, despedido_inactividad,
                exp_laboral, trabajos_exitosos, trabajos_fallidos,
                total_generado, racha_exitos
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)
            ON CONFLICT (user_id) DO UPDATE SET
                empleo_actual=EXCLUDED.empleo_actual,
                dificultad=EXCLUDED.dificultad,
                fecha_contratacion=EXCLUDED.fecha_contratacion,
                ultimo_trabajo=EXCLUDED.ultimo_trabajo,
                historial_reciente_de_jornadas=EXCLUDED.historial_reciente_de_jornadas,
                cooldown_renuncia=EXCLUDED.cooldown_renuncia,
                progreso_permanencia=EXCLUDED.progreso_permanencia,
                ultimo_empleo=EXCLUDED.ultimo_empleo,
                progreso_requisito=EXCLUDED.progreso_requisito,
                despedido_inactividad=EXCLUDED.despedido_inactividad,
                exp_laboral=EXCLUDED.exp_laboral,
                trabajos_exitosos=EXCLUDED.trabajos_exitosos,
                trabajos_fallidos=EXCLUDED.trabajos_fallidos,
                total_generado=EXCLUDED.total_generado,
                racha_exitos=EXCLUDED.racha_exitos
        """, data["user_id"], data.get("empleo_actual"), data.get("dificultad"), data.get("fecha_contratacion", 0),
             data.get("ultimo_trabajo", 0), hist_json, data.get("cooldown_renuncia", 0),
             data.get("progreso_permanencia", 0), data.get("ultimo_empleo"),
             data.get("progreso_requisito", 0), data.get("despedido_inactividad", False),
             data.get("exp_laboral", 0), data.get("trabajos_exitosos", 0), data.get("trabajos_fallidos", 0),
             data.get("total_generado", 0), data.get("racha_exitos", 0))
    _EMPLEOS_CACHE[data["user_id"]] = data


async def append_historial(user_id, empleo, exito, pago, motivo):
    if not pool:
        return
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO empleos_historial (user_id, empleo, timestamp, exito, pago, motivo)
            VALUES ($1, $2, $3, $4, $5, $6)
        """, user_id, empleo, time.time(), exito, pago, motivo)


async def limpiar_progreso(user_id):
    data = await get_empleo_user(user_id)
    if not data:
        return
    data["progreso_permanencia"] = 0
    data["progreso_requisito"] = 0
    data["fecha_contratacion"] = 0
    data["ultimo_trabajo"] = 0
    data["historial_reciente_de_jornadas"] = []
    data["empleo_actual"] = None
    data["dificultad"] = None
    await save_empleo_user(data)


async def registrar_resultado(user_id, empleo, exito, pago, motivo):
    data = await get_empleo_user(user_id)
    if not data:
        return
    ahora = time.time()
    data["ultimo_trabajo"] = ahora
    data["progreso_permanencia"] = max(data.get("progreso_permanencia", 0), ahora - data.get("fecha_contratacion", ahora))
    data["historial_reciente_de_jornadas"] = (data.get("historial_reciente_de_jornadas", []) + [ahora])[-10:]
    if exito:
        data["trabajos_exitosos"] = data.get("trabajos_exitosos", 0) + 1
        data["exp_laboral"] = data.get("exp_laboral", 0) + 2
        data["racha_exitos"] = data.get("racha_exitos", 0) + 1
        if data["racha_exitos"] % 5 == 0:
            data["exp_laboral"] += 5
            data["racha_exitos"] += 0
        data["total_generado"] = data.get("total_generado", 0) + max(0, pago)
    else:
        data["trabajos_fallidos"] = data.get("trabajos_fallidos", 0) + 1
        data["racha_exitos"] = 0
    await save_empleo_user(data)
    await append_historial(user_id, empleo, exito, pago, motivo)


class ConfirmarEmpleoView(ui.View):
    def __init__(self, bot, user_id, empleo):
        super().__init__(timeout=60)
        self.bot = bot
        self.user_id = user_id
        self.empleo = empleo

    @ui.button(label="Aceptar empleo", style=ButtonStyle.green)
    async def aceptar(self, interaction: Interaction, button: ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ No es tu confirmación.", ephemeral=True)

        data = await get_empleo_user(self.user_id) or {
            "user_id": self.user_id,
            "empleo_actual": None,
            "dificultad": None,
            "fecha_contratacion": 0,
            "ultimo_trabajo": 0,
            "historial_reciente_de_jornadas": [],
            "cooldown_renuncia": 0,
            "progreso_permanencia": 0,
            "ultimo_empleo": None,
            "progreso_requisito": 0,
            "despedido_inactividad": False,
            "exp_laboral": 0,
            "trabajos_exitosos": 0,
            "trabajos_fallidos": 0,
            "total_generado": 0,
            "racha_exitos": 0,
        }
        if data.get("empleo_actual"):
            return await interaction.response.send_message(
                f"❌ Ya posees un empleo como **{data['empleo_actual']}**. Usa `!renunciar` antes de aplicar a otro trabajo.",
                ephemeral=True
            )

        info = EMPLEOS[self.empleo]
        if data.get("exp_laboral", 0) < info["xp_requisito"]:
            return await interaction.response.send_message(
                f"❌ {interaction.user.mention} necesitas **{info['xp_requisito']}** puntos de Experiencia Laboral para aplicar a **{self.empleo.title()}**.",
                ephemeral=True
            )

        now = time.time()
        data.update({
            "empleo_actual": self.empleo,
            "dificultad": info['dificultad'],
            "fecha_contratacion": now,
            "ultimo_trabajo": 0,
            "historial_reciente_de_jornadas": [],
            "cooldown_renuncia": 0,
            "progreso_permanencia": 0,
            "despedido_inactividad": False,
        })
        await save_empleo_user(data)
        await interaction.response.send_message(
            f"🎉 {interaction.user.mention} ahora eres **{self.empleo.title()}**. Usa `!trabajar` para iniciar tu jornada de 3 horas.",
            ephemeral=False
        )
        try:
            await interaction.message.delete(delay=1)
        except Exception:
            pass

    @ui.button(label="Rechazar empleo", style=ButtonStyle.red)
    async def rechazar(self, interaction: Interaction, button: ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ No es tu confirmación.", ephemeral=True)
        await interaction.response.send_message("❌ Has rechazado aplicar a este empleo.", ephemeral=True)
        try:
            await interaction.message.delete(delay=1)
        except Exception:
            pass


class Empleos(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        await init_empleos_tables()

    @commands.command(name="empleos")
    async def empleos(self, ctx):
        embed = discord.Embed(title="💼 Empleos Disponibles", color=discord.Color.blue())
        for nombre, info in EMPLEOS.items():
            embed.add_field(
                name=f"{nombre.title()}",
                value=(
                    f"• Salario: {info['salario_min']} - {info['salario_max']} {COIN}\n"
                    f"• Dificultad: {info['dificultad']}\n"
                    f"• XP requerida: {info['xp_requisito']}\n"
                    f"• Jornada: {info['duracion_horas']} horas"
                ),
                inline=False,
            )
        embed.set_footer(text="Aplica con: !aplicar <empleo>")
        await ctx.send(embed=embed)

    @commands.command(name="aplicar")
    async def aplicar(self, ctx, empleo: str = None):
        if not empleo:
            return await ctx.send("❌ Usa: `!aplicar <empleo>`")
        empleo = normalizar_empleo(empleo)
        if empleo not in EMPLEOS:
            return await ctx.send("❌ Empleo no disponible.")

        data = await get_empleo_user(ctx.author.id)
        if data and data.get("empleo_actual"):
            return await ctx.send(f"❌ Ya posees un empleo como **{data['empleo_actual'].title()}**. Usa `!renunciar` antes de aplicar a otro trabajo.")

        info = EMPLEOS[empleo]
        if data and data.get("exp_laboral", 0) < info["xp_requisito"]:
            return await ctx.send(f"❌ {ctx.author.mention} necesitas **{info['xp_requisito']}** puntos de Experiencia Laboral para aplicar a **{empleo.title()}**.")

        embed = discord.Embed(
            title=f"¿Deseas aplicar como {empleo.title()}?",
            description=(
                f"Requiere **{info['xp_requisito']} XP Laboral**\n"
                f"Pago estimado: **{info['salario_min']} - {info['salario_max']} {COIN}** por jornada de 3 horas"
            ),
            color=discord.Color.blurple(),
        )
        await ctx.send(embed=embed, view=ConfirmarEmpleoView(self.bot, ctx.author.id, empleo))

    @commands.command(name="renunciar")
    async def renunciar(self, ctx):
        data = await get_empleo_user(ctx.author.id)
        if not data or not data.get("empleo_actual"):
            return await ctx.send("❌ No posees un empleo activo.")
        data["ultimo_empleo"] = data.get("empleo_actual")
        data["progreso_requisito"] = data.get("progreso_permanencia", 0)
        data["cooldown_renuncia"] = time.time() + 3600
        data["empleo_actual"] = None
        data["dificultad"] = None
        data["fecha_contratacion"] = 0
        data["ultimo_trabajo"] = 0
        data["historial_reciente_de_jornadas"] = []
        data["despedido_inactividad"] = False
        await save_empleo_user(data)
        await ctx.send("🛑 Has renunciado a tu empleo. Podrás aplicar a un nuevo trabajo dentro de 1 hora.")

    @commands.command(name="exp")
    async def exp(self, ctx):
        data = await get_empleo_user(ctx.author.id)
        empleo = data.get("empleo_actual") or "Sin empleo"
        embed = discord.Embed(title="📊 Experiencia Laboral", color=discord.Color.gold())
        embed.add_field(name="Empleo actual", value=empleo.title() if empleo != "Sin empleo" else empleo, inline=False)
        embed.add_field(name="XP Laboral", value=str(data.get("exp_laboral", 0)), inline=True)
        embed.add_field(name="Trabajos exitosos", value=str(data.get("trabajos_exitosos", 0)), inline=True)
        embed.add_field(name="Trabajos fallidos", value=str(data.get("trabajos_fallidos", 0)), inline=True)
        embed.add_field(name="Total generado", value=f"{data.get('total_generado', 0)} {COIN}", inline=False)
        embed.set_footer(text="Tu progreso laboral se actualiza al terminar cada jornada.")
        await ctx.send(embed=embed)

    @commands.command(name="trabajar")
    async def trabajar(self, ctx):
        data = await get_empleo_user(ctx.author.id)
        if not data or not data.get("empleo_actual"):
            return await ctx.send(f"❌ {ctx.author.mention} No tienes un empleo activo.")

        empleo = data["empleo_actual"].lower()
        info = EMPLEOS[empleo]
        now = time.time()
        if data.get("ultimo_trabajo", 0) and (now - data["ultimo_trabajo"]) < info["duracion_horas"] * 3600:
            return await ctx.send("⏳ Debes esperar 3 horas para volver a trabajar.")

        if empleo == "Limpiador":
            view = LimpiadorView(self.bot, ctx.author, info)
        elif empleo == "ingeniero":
            view = IngenieroView(self.bot, ctx.author, info)
        else:
            view = PlomeroView(self.bot, ctx.author, info)

        msg = await ctx.send(embed=view.build_embed(), view=view)
        view.message = msg


class LimpiadorView(ui.View):
    def __init__(self, bot, author, info):
        super().__init__(timeout=180)
        self.bot = bot
        self.author = author
        self.info = info
        self.start_time = time.time()
        self.revelados = [False] * 16
        self.basura = 3
        self.message = None
        self.puntos = 0
        self._build()

    def _build(self):
        self.clear_items()
        emojis = ["🗑️", "🧹", "🧺", "🧼", "🪴", "📚", "🪟", "📦", "🧻", "🧽", "🫧", "🪣", "🖥️", "🪙", "🧲", "🧼"]
        tablero = emojis[:]
        random.shuffle(tablero)
        self.tablero = ["🗑️"] * 3 + tablero[3:16]
        random.shuffle(self.tablero)
        for i, emoji in enumerate(self.tablero):
            row = i // 4
            btn = ui.Button(label="⬜", style=ButtonStyle.secondary, row=row, custom_id=f"limp_{i}")
            btn.callback = self._make_callback(i)
            self.add_item(btn)

    def _make_callback(self, idx):
        async def callback(interaction: Interaction):
            if interaction.user.id != self.author.id:
                return await interaction.response.send_message("❌ Este tablero no es tuyo.", ephemeral=True)
            if self.revelados[idx]:
                return await interaction.response.send_message("✅ Esa casilla ya está descubierta.", ephemeral=True)
            self.revelados[idx] = True
            self.puntos += 1
            self._build()
            for item in self.children:
                cid = int(item.custom_id.split("_")[1])
                if self.revelados[cid]:
                    item.label = self.tablero[cid]
                    item.style = ButtonStyle.success
            await interaction.response.edit_message(embed=self.build_embed(), view=self)
            if self.tablero[idx] == "🗑️":
                self.basura -= 1
            if self.basura == 0:
                await self._terminar(interaction, exito=True)
        return callback

    def build_embed(self):
        descubiertos = sum(self.revelados)
        embed = discord.Embed(title="🧹 Limpieza en progreso", color=discord.Color.green())
        embed.add_field(name="Objetivo", value="Descubre los 3 símbolos de reciclaje para completar la tarea.", inline=False)
        embed.add_field(name="Celdas abiertas", value=str(descubiertos), inline=True)
        embed.add_field(name="Símbolos de reciclaje restantes", value=str(self.basura), inline=True)
        embed.set_footer(text=f"Tablero de {self.author.display_name} • Se elimina en 180 segundos")
        return embed

    async def _terminar(self, interaction, exito):
        tiempo = int(time.time() - self.start_time)
        base = random.randint(self.info['salario_min'], self.info['salario_max'])
        ratio = 1.0 + max(0.0, 45 - tiempo) / 45.0 * 0.35
        pago = int(base * ratio)
        exito_real = True
        if random.random() < self.info['prob_fallo']:
            exito_real = False
            pago = self.info['penalizacion']
            mensaje = random.choice(self.info['mensajes_fallo']).format(monto=abs(self.info['penalizacion']), COIN=COIN)
            await update_bank(self.author.id, self.info['penalizacion'])
            await registrar_resultado(self.author.id, 'Limpiador', False, self.info['penalizacion'], mensaje)
            await interaction.edit_original_response(embed=discord.Embed(title="🧹 Resultado", description=f"{self.author.mention} {mensaje}", color=discord.Color.red()), view=None)
            await self._cleanup()
            return
        mensaje = random.choice(self.info['mensajes_exito']).format(monto=pago, COIN=COIN)
        await update_bank(self.author.id, pago)
        await registrar_resultado(self.author.id, 'Limpiador', True, pago, mensaje)
        await interaction.edit_original_response(embed=discord.Embed(title="🧹 Resultado", description=f"{self.author.mention} {mensaje}", color=discord.Color.green()), view=None)
        await self._cleanup()

    async def _cleanup(self):
        await asyncio.sleep(180)
        try:
            if self.message:
                await self.message.delete()
        except Exception:
            pass


class IngenieroView(ui.View):
    def __init__(self, bot, author, info):
        super().__init__(timeout=180)
        self.bot = bot
        self.author = author
        self.info = info
        self.start_time = time.time()
        self.message = None
        self.revelados = [False] * 8
        self.seleccion = []
        self.pares = 0
        self.bloqueado = False
        self._build()

    def _build(self):
        self.clear_items()
        emojis = ["📡", "💡", "🔌", "🔧"] * 2
        random.shuffle(emojis)
        self.tablero = emojis
        for i, emoji in enumerate(self.tablero):
            row = i // 4
            btn = ui.Button(label="⬜", style=ButtonStyle.secondary, row=row, custom_id=f"ing_{i}")
            btn.callback = self._make_callback(i)
            self.add_item(btn)

    def _make_callback(self, idx):
        async def callback(interaction: Interaction):
            if interaction.user.id != self.author.id:
                return await interaction.response.send_message("❌ Este tablero no es tuyo.", ephemeral=True)
            if self.bloqueado or self.revelados[idx] or idx in self.seleccion:
                return
            self.seleccion.append(idx)
            self._build()
            for item in self.children:
                cid = int(item.custom_id.split("_")[1])
                if cid in self.seleccion:
                    item.label = self.tablero[cid]
                    item.style = ButtonStyle.primary
            await interaction.response.edit_message(embed=self.build_embed(), view=self)
            if len(self.seleccion) == 2:
                self.bloqueado = True
                i1, i2 = self.seleccion
                await asyncio.sleep(1)
                if self.tablero[i1] == self.tablero[i2]:
                    self.revelados[i1] = True
                    self.revelados[i2] = True
                    self.pares += 1
                    if self.pares == 4:
                        await self._terminar(interaction, exito=True)
                        return
                else:
                    self._build()
                self.seleccion = []
                self.bloqueado = False
                await interaction.edit_original_response(embed=self.build_embed(), view=self)
        return callback

    def build_embed(self):
        embed = discord.Embed(title="🔧 Modo ingeniería", color=discord.Color.blurple())
        embed.add_field(name="Objetivo", value="Encuentra los 4 pares de símbolos para completar la revisión.", inline=False)
        embed.add_field(name="Pares encontrados", value=str(self.pares), inline=True)
        embed.set_footer(text=f"Tablero de {self.author.display_name} • Se elimina en 180 segundos")
        return embed

    async def _terminar(self, interaction, exito):
        tiempo = int(time.time() - self.start_time)
        base = random.randint(self.info['salario_min'], self.info['salario_max'])
        ratio = 1.0 + max(0.0, 45 - tiempo) / 45.0 * 0.35
        pago = int(base * ratio)
        if random.random() < self.info['prob_fallo']:
            await update_bank(self.author.id, self.info['penalizacion'])
            mensaje = random.choice(self.info['mensajes_fallo']).format(monto=abs(self.info['penalizacion']), COIN=COIN)
            await registrar_resultado(self.author.id, 'ingeniero', False, self.info['penalizacion'], mensaje)
            await interaction.edit_original_response(embed=discord.Embed(title="🔧 Resultado", description=f"{self.author.mention} {mensaje}", color=discord.Color.red()), view=None)
        else:
            await update_bank(self.author.id, pago)
            mensaje = random.choice(self.info['mensajes_exito']).format(monto=pago, COIN=COIN)
            await registrar_resultado(self.author.id, 'ingeniero', True, pago, mensaje)
            await interaction.edit_original_response(embed=discord.Embed(title="🔧 Resultado", description=f"{self.author.mention} {mensaje}", color=discord.Color.green()), view=None)
        await asyncio.sleep(180)
        try:
            if self.message:
                await self.message.delete()
        except Exception:
            pass


class PlomeroView(ui.View):
    def __init__(self, bot, author, info):
        super().__init__(timeout=180)
        self.bot = bot
        self.author = author
        self.info = info
        self.start_time = time.time()
        self.message = None
        self.revelados = [False] * 9
        self.intentos = 0
        self.hallazgos = 0
        self._build()

    def _build(self):
        self.clear_items()
        emojis = ["⚠️"] * 3 + ["🪨"] * 6
        random.shuffle(emojis)
        self.tablero = emojis
        for i, emoji in enumerate(self.tablero):
            row = i // 3
            btn = ui.Button(label="⬜", style=ButtonStyle.secondary, row=row, custom_id=f"plo_{i}")
            btn.callback = self._make_callback(i)
            self.add_item(btn)

    def _make_callback(self, idx):
        async def callback(interaction: Interaction):
            if interaction.user.id != self.author.id:
                return await interaction.response.send_message("❌ Este tablero no es tuyo.", ephemeral=True)
            if self.revelados[idx]:
                return await interaction.response.send_message("✅ Esa casilla ya está abierta.", ephemeral=True)
            self.intentos += 1
            self.revelados[idx] = True
            self._build()
            for item in self.children:
                cid = int(item.custom_id.split("_")[1])
                if self.revelados[cid]:
                    item.label = self.tablero[cid]
                    item.style = ButtonStyle.success if self.tablero[cid] == "⚠️" else ButtonStyle.danger
            await interaction.response.edit_message(embed=self.build_embed(), view=self)
            if self.tablero[idx] == "⚠️":
                self.hallazgos += 1
            if self.hallazgos >= 3:
                await self._terminar(interaction, exito=True)
            elif self.intentos >= 5:
                await self._terminar(interaction, exito=False)
        return callback

    def build_embed(self):
        embed = discord.Embed(title="🛠️ Revisión técnica", color=discord.Color.orange())
        embed.add_field(name="Objetivo", value="Encuentra 3 señales de riesgo en 5 intentos.", inline=False)
        embed.add_field(name="Intentos usados", value=str(self.intentos), inline=True)
        embed.add_field(name="Señales encontradas", value=str(self.hallazgos), inline=True)
        embed.set_footer(text=f"Tablero de {self.author.display_name} • Se elimina en 180 segundos")
        return embed

    async def _terminar(self, interaction, exito):
        tiempo = int(time.time() - self.start_time)
        base = random.randint(self.info['salario_min'], self.info['salario_max'])
        ratio = 1.0 + max(0.0, 45 - tiempo) / 45.0 * 0.35
        pago = int(base * ratio)
        if not exito or random.random() < self.info['prob_fallo']:
            await update_bank(self.author.id, self.info['penalizacion'])
            mensaje = random.choice(self.info['mensajes_fallo']).format(monto=abs(self.info['penalizacion']), COIN=COIN)
            await registrar_resultado(self.author.id, 'plomero', False, self.info['penalizacion'], mensaje)
            await interaction.edit_original_response(embed=discord.Embed(title="🛠️ Resultado", description=f"{self.author.mention} {mensaje}", color=discord.Color.red()), view=None)
        else:
            await update_bank(self.author.id, pago)
            mensaje = random.choice(self.info['mensajes_exito']).format(monto=pago, COIN=COIN)
            await registrar_resultado(self.author.id, 'plomero', True, pago, mensaje)
            await interaction.edit_original_response(embed=discord.Embed(title="🛠️ Resultado", description=f"{self.author.mention} {mensaje}", color=discord.Color.green()), view=None)
        await asyncio.sleep(180)
        try:
            if self.message:
                await self.message.delete()
        except Exception:
            pass


async def setup(bot):
    await init_empleos_tables()
    await bot.add_cog(Empleos(bot))
