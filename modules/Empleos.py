import ast
import random
import time
import discord
from discord.ext import commands
from discord import ui, ButtonStyle, Interaction

from core.database import pool
from core.database import update_bank
from core.config import COIN


EMPLEOS = {
    "limpador": {
        "salario_min": 300,
        "salario_max": 500,
        "dificultad": "Fácil",
        "ganancia_prob": 0.80,
        "perdida_prob": 0.20,
        "pago_perdida": 300,
        "mensajes_exito": [
            "Has limpiado una oficina impecablemente.",
            "Has dejado reluciente un edificio completo.",
            "Tu excelente trabajo impresionó a tus supervisores."
        ],
        "mensajes_fracaso": [
            "Rompiste una ventana durante la limpieza.",
            "Dañaste equipo de mantenimiento.",
            "Generaste gastos inesperados en la empresa."
        ]
    },
    "ingeniero": {
        "salario_min": 500,
        "salario_max": 800,
        "dificultad": "Media",
        "ganancia_prob": 0.70,
        "perdida_prob": 0.30,
        "pago_perdida": 500,
        "mensajes_exito": [
            "Has reparado una máquina industrial.",
            "Tu proyecto fue aprobado exitosamente.",
            "Optimizaste un sistema crítico."
        ],
        "mensajes_fracaso": [
            "Tu diseño presentó errores costosos.",
            "Una reparación salió mal.",
            "El proyecto fue rechazado."
        ]
    },
    "plomero": {
        "salario_min": 1000,
        "salario_max": 1200,
        "dificultad": "Difícil",
        "ganancia_prob": 0.60,
        "perdida_prob": 0.40,
        "pago_perdida": 800,
        "mensajes_exito": [
            "Solucionaste una avería crítica.",
            "Completaste una instalación compleja.",
            "Tu trabajo evitó una gran emergencia."
        ],
        "mensajes_fracaso": [
            "Una tubería colapsó durante la reparación.",
            "Se produjo una inundación accidental.",
            "Los daños generaron costos importantes."
        ]
    }
}


_EMPLEOS_CACHE = {}


def normalizar_empleo(nombre: str) -> str:
    texto = nombre.lower().strip()
    texto = texto.replace("í", "i").replace("á", "a").replace("é", "e").replace("ó", "o").replace("ú", "u")
    texto = texto.replace("limpiador", "limpador")
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
            despedido_inactividad BOOLEAN DEFAULT FALSE
        )
        """)
        await conn.execute("ALTER TABLE empleos_users ADD COLUMN IF NOT EXISTS ultimo_empleo TEXT DEFAULT NULL")
        await conn.execute("ALTER TABLE empleos_users ADD COLUMN IF NOT EXISTS progreso_requisito DOUBLE PRECISION DEFAULT 0")
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
                ultimo_empleo, progreso_requisito, despedido_inactividad
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
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
                despedido_inactividad=EXCLUDED.despedido_inactividad
        """, data["user_id"], data.get("empleo_actual"), data.get("dificultad"), data.get("fecha_contratacion", 0),
             data.get("ultimo_trabajo", 0), hist_json, data.get("cooldown_renuncia", 0),
             data.get("progreso_permanencia", 0), data.get("ultimo_empleo"),
             data.get("progreso_requisito", 0), data.get("despedido_inactividad", False))
    _EMPLEOS_CACHE[data["user_id"]] = data


async def delete_empleo_user(user_id):
    if not pool:
        return
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM empleos_users WHERE user_id=$1", user_id)
    _EMPLEOS_CACHE.pop(user_id, None)


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


class ConfirmarEmpleoView(ui.View):
    def __init__(self, bot, user_id, empleo, salario_rango, ganancia_prob, perdida_prob):
        super().__init__(timeout=60)
        self.bot = bot
        self.user_id = user_id
        self.empleo = empleo
        self.salario_rango = salario_rango
        self.ganancia_prob = ganancia_prob
        self.perdida_prob = perdida_prob

    @ui.button(label="Aceptar empleo", style=ButtonStyle.green)
    async def aceptar(self, interaction: Interaction, button: ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ No es tu confirmación.", ephemeral=True)

        data = await get_empleo_user(self.user_id)
        if data and data.get("empleo_actual"):
            return await interaction.response.send_message(
                f"❌ Ya posees un empleo como **{data['empleo_actual']}**. Usa `!renunciar` antes de aplicar a otro trabajo.",
                ephemeral=True
            )

        now = time.time()
        if data and data.get("cooldown_renuncia", 0) > now:
            return await interaction.response.send_message(
                f"❌ Debes esperar antes de aplicar a un nuevo empleo.",
                ephemeral=True
            )

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
        }
        data.update({
            "empleo_actual": self.empleo,
            "dificultad": EMPLEOS[self.empleo]['dificultad'],
            "fecha_contratacion": now,
            "ultimo_trabajo": 0,
            "historial_reciente_de_jornadas": [],
            "cooldown_renuncia": 0,
            "progreso_permanencia": 0,
            "despedido_inactividad": False,
        })
        await save_empleo_user(data)
        await interaction.response.send_message(
            f"🎉 Felicidades {interaction.user.mention}. Ahora eres **{self.empleo.title()}**. Usa `!trabajar` cada 6 horas para recibir tu paga según el rango de tu empleo.",
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

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.CommandOnCooldown):
            retry = int(error.retry_after)
            await ctx.send(f"⏳ Podrás volver a usar este comando en **{retry}** segundos.", delete_after=10)
        else:
            raise error

    @commands.command(name="empleos")
    async def empleos(self, ctx):
        embed = discord.Embed(
            title="💼 Empleos Disponibles - Jornadas de 6h",
            color=discord.Color.blue()
        )
        for nombre, info in EMPLEOS.items():
            embed.add_field(
                name=nombre.title(),
                value=(
                    f"• Salario: {info['salario_min']} - {info['salario_max']} {COIN}\n"
                    f"• Dificultad: {info['dificultad']}"
                ),
                inline=False
            )
        embed.set_footer(text="Aplica a un trabajo con: !aplicar {empleo}")
        await ctx.send(embed=embed)

    @commands.command(name="aplicar")
    async def aplicar(self, ctx, empleo: str = None):
        if not empleo:
            return await ctx.send("❌ Usa: `!aplicar {empleo}`")

        empleo = normalizar_empleo(empleo)
        if empleo not in EMPLEOS:
            return await ctx.send("❌ Empleo no disponible.")

        data = await get_empleo_user(ctx.author.id)
        if data and data.get("empleo_actual"):
            return await ctx.send(f"❌ Ya posees un empleo como **{data['empleo_actual'].title()}**. Usa `!renunciar` antes de aplicar a otro trabajo.")

        if empleo == "ingeniero" and not (data and data.get("ultimo_empleo") == "limpador" and data.get("progreso_requisito", 0) >= 72 * 3600):
            return await ctx.send("❌ Debes haber trabajado como **Limpador** durante al menos 72 horas consecutivas para aplicar a este empleo.")

        if empleo == "plomero" and not (data and data.get("ultimo_empleo") == "ingeniero" and data.get("progreso_requisito", 0) >= 72 * 3600):
            return await ctx.send("❌ Debes haber trabajado como **Ingeniero** durante al menos 72 horas consecutivas para aplicar a este empleo.")

        if data and data.get("cooldown_renuncia", 0) > time.time():
            return await ctx.send("❌ Debes esperar 1 hora antes de aplicar a un nuevo empleo.")

        info = EMPLEOS[empleo]
        salario_min = info['salario_min']
        salario_max = info['salario_max']
        embed = discord.Embed(
            title=f"¿Deseas aplicar como {empleo.title()}?",
            description=(
                f"Este empleo paga entre **{salario_min} y {salario_max} {COIN}** por jornada de 6 horas.\n"
                f"Probabilidad de ganancia: **{int(info['ganancia_prob'] * 100)}%**\n"
                f"Probabilidad de pérdida: **{int(info['perdida_prob'] * 100)}%**"
            ),
            color=discord.Color.blurple()
        )
        await ctx.send(embed=embed, view=ConfirmarEmpleoView(self.bot, ctx.author.id, empleo, (salario_min, salario_max), info['ganancia_prob'], info['perdida_prob']))

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

    @commands.command(name="trabajar")
    async def trabajar(self, ctx):
        data = await get_empleo_user(ctx.author.id)
        if not data or not data.get("empleo_actual"):
            return await ctx.send(f"❌ {ctx.author.mention} No posees ningún trabajo.")

        now = time.time()
        if data.get("ultimo_trabajo", 0) and (now - data['ultimo_trabajo']) < 6 * 3600:
            return await ctx.send("⏳ Debes esperar 6 horas para volver a trabajar.")

        # Despido por inactividad
        if data.get("ultimo_trabajo", 0) and (now - data['ultimo_trabajo']) > 24 * 3600:
            data["despedido_inactividad"] = True
            data["ultimo_empleo"] = data.get("empleo_actual")
            data["progreso_requisito"] = 0
            data["progreso_permanencia"] = 0
            data["empleo_actual"] = None
            data["dificultad"] = None
            data["fecha_contratacion"] = 0
            data["ultimo_trabajo"] = 0
            data["historial_reciente_de_jornadas"] = []
            await save_empleo_user(data)
            await ctx.send("❌ Has perdido tu empleo debido a inactividad laboral.")
            return

        # Penalización por ausencia
        if data.get("ultimo_trabajo", 0) and (now - data['ultimo_trabajo']) > 15 * 3600:
            await update_bank(ctx.author.id, -500)
            await ctx.send("⚠️ Has faltado al trabajo y tu jefe se ha molestado. Recibes una penalización de -500 PurpleCoins.")

        empleo = data['empleo_actual'].lower()
        info = EMPLEOS[empleo]
        exito = random.random() < info['ganancia_prob']
        if exito:
            pago = random.randint(info['salario_min'], info['salario_max'])
            bonus = 0
            historial = data.get('historial_reciente_de_jornadas', [])
            historial.append(now)
            if len(historial) >= 3:
                recientes = [t for t in historial if now - t <= 13 * 3600]
                if len(recientes) >= 3 and (max(recientes) - min(recientes)) <= 13 * 3600:
                    bonus = int(pago * 0.20)
                    pago += bonus
            await update_bank(ctx.author.id, pago)
            data['ultimo_trabajo'] = now
            data['progreso_permanencia'] = max(data.get('progreso_permanencia', 0), now - data.get('fecha_contratacion', now))
            data['historial_reciente_de_jornadas'] = historial[-10:]
            await save_empleo_user(data)
            await append_historial(ctx.author.id, empleo, True, pago, random.choice(info['mensajes_exito']))
            await ctx.send(f"✅ {ctx.author.mention} {random.choice(info['mensajes_exito'])}\n💰 Ganaste **{pago}** PurpleCoins.{'\n✨ Por tu gran rendimiento ... 20% adicional.' if bonus else ''}")
        else:
            pago = -info['pago_perdida']
            await update_bank(ctx.author.id, pago)
            data['ultimo_trabajo'] = now
            data['progreso_permanencia'] = max(data.get('progreso_permanencia', 0), now - data.get('fecha_contratacion', now))
            data['historial_reciente_de_jornadas'] = (data.get('historial_reciente_de_jornadas', []) + [now])[-10:]
            await save_empleo_user(data)
            await append_historial(ctx.author.id, empleo, False, pago, random.choice(info['mensajes_fracaso']))
            await ctx.send(f"❌ {ctx.author.mention} {random.choice(info['mensajes_fracaso'])}\n💸 Perdiste **{abs(pago)}** PurpleCoins.")


async def setup(bot):
    await init_empleos_tables()
    await bot.add_cog(Empleos(bot))
