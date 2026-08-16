import discord
import asyncio
import logging
import random
import time
from discord.ext import commands
from core.database import update_balance
from core.config import COIN, STAFF_ROLE

logger = logging.getLogger(__name__)

# ── CONFIG BASE ────────────────────────────────────────
GOLPEAR_GIF = "https://pub-a09b3609b6b34dfab5c7aa7742cd1a8a.r2.dev/Purple%20jack%20Harcode/cofre1.gif"
MAX_GOLPES = 3
COFRE_TIMEOUT = 6

# ── ESTADO GLOBAL ──────────────────────────────────────
_golpear_config = {
    "activo": False,
    "canal_id": None,
    "min_time": 600,
    "max_time": 3600,
    "min_ganancia": 150,
    "max_ganancia": 800,
    "next_spawn_at": 0.0,
}

# Evento de señal: se dispara cuando el sistema se activa externamente,
# permitiendo que el loop salga del sleep largo inmediatamente.
_cambio_event: asyncio.Event = asyncio.Event()
_loop_health = {
    "estado": "iniciando",
    "ultimo_error": None,
    "ultimo_spawn": 0.0,
}


def señalar_cambio():
    """Llamar desde staff.py ante cualquier cambio de config (activar, desactivar, editar).
    Interrumpe inmediatamente el sleep del loop para que re-evalue el estado."""
    _cambio_event.set()


def permisos_faltantes(canal: discord.TextChannel, miembro: discord.Member) -> list[str]:
    """Devuelve permisos indispensables para publicar y actualizar el cofre."""
    permisos = canal.permissions_for(miembro)
    requeridos = (
        ("view_channel", "Ver canal"),
        ("send_messages", "Enviar mensajes"),
        ("embed_links", "Insertar enlaces"),
    )
    return [etiqueta for atributo, etiqueta in requeridos if not getattr(permisos, atributo)]


async def _esperar_cambio_o_timeout(timeout: float) -> bool:
    """Espera sin perder señales que ocurran justo antes de limpiar el evento."""
    if _cambio_event.is_set():
        _cambio_event.clear()
        return True
    _cambio_event.clear()
    try:
        await asyncio.wait_for(_cambio_event.wait(), timeout=max(0.05, timeout))
        return True
    except asyncio.TimeoutError:
        return False


# ── VIEW ───────────────────────────────────────────────
class GolpearView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=COFRE_TIMEOUT)
        self.golpeadores = []
        self.terminado = False
        self.message = None
        self._golpe_lock = asyncio.Lock()

    async def on_timeout(self):
        async with self._golpe_lock:
            if self.terminado:
                return
            self.terminado = True
            for item in self.children:
                item.disabled = True
            golpeadores = list(self.golpeadores)

        if golpeadores:
            await asyncio.sleep(3)
            lineas = "\n".join(
                f"**{u.display_name}** obtuvo **{m}** {COIN}"
                for u, m in golpeadores
            )
            embed = discord.Embed(
                title="💥 ¡Cofre Destruido!",
                description=f"Los aventureros que golpearon primero:\n\n{lineas}",
                color=discord.Color.gold()
            )
            embed.set_image(url=GOLPEAR_GIF)
            embed.set_footer(text="Este mensaje se eliminará en breve.")
        else:
            embed = discord.Embed(
                title="💨 Cofre Vencido",
                description="El cofre desapareció... Nadie golpeó a tiempo.",
                color=discord.Color.dark_gray()
            )
            embed.set_image(url=GOLPEAR_GIF)

        try:
            await self.message.edit(embed=embed, view=self)
        except Exception as error:
            logger.warning(
                "[COFRES:E_EDIT_TIMEOUT] No se pudo actualizar el cofre vencido: %s",
                error,
            )

        await asyncio.sleep(240)
        try:
            await self.message.delete()
        except discord.NotFound:
            pass
        except Exception as error:
            logger.warning(
                "[COFRES:E_DELETE] No se pudo eliminar el cofre vencido: %s",
                error,
            )

    @discord.ui.button(label="💥 Golpear", style=discord.ButtonStyle.danger)
    async def golpear(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        debe_cerrar = False
        async with self._golpe_lock:
            if self.terminado:
                return await interaction.followup.send(
                    "❌ El cofre ya fue reclamado.", ephemeral=True
                )

            if any(u.id == interaction.user.id for u, _ in self.golpeadores):
                return await interaction.followup.send(
                    "❌ Ya golpeaste este cofre.", ephemeral=True
                )

            monto = random.randint(
                _golpear_config["min_ganancia"],
                _golpear_config["max_ganancia"],
            )
            try:
                await update_balance(interaction.user.id, monto)
            except Exception:
                logger.exception(
                    "[COFRES:E_PAGO] No se pudo entregar el premio a %s",
                    interaction.user.id,
                )
                return await interaction.followup.send(
                    "❌ No pude acreditar tu premio. Tu golpe no fue consumido; inténtalo otra vez.",
                    ephemeral=True,
                )

            self.golpeadores.append((interaction.user, monto))
            if len(self.golpeadores) >= MAX_GOLPES:
                self.terminado = True
                self.stop()
                for item in self.children:
                    item.disabled = True
                debe_cerrar = True

        if debe_cerrar:
            await self._mostrar_resultado(interaction.message)

    async def _mostrar_resultado(self, message):

        await asyncio.sleep(3)

        lineas = "\n".join(
            f"**{u.display_name}** obtuvo **{m}** {COIN}"
            for u, m in self.golpeadores
        )

        embed = discord.Embed(
            title="💥 ¡Cofre Destruido!",
            description=f"Los aventureros que golpearon primero:\n\n{lineas}",
            color=discord.Color.gold()
        )
        embed.set_image(url=GOLPEAR_GIF)
        embed.set_footer(text="Ganancias entregadas....")

        try:
            await message.edit(embed=embed, view=self)
        except Exception as error:
            logger.warning(
                "[COFRES:E_EDIT_RESULT] No se pudo mostrar el resultado del cofre: %s",
                error,
            )

        await asyncio.sleep(240)
        try:
            await message.delete()
        except discord.NotFound:
            pass
        except Exception as error:
            logger.warning(
                "[COFRES:E_DELETE] No se pudo eliminar el cofre finalizado: %s",
                error,
            )


# ── SPAWN ──────────────────────────────────────────────
async def spawn_cofre(canal: discord.TextChannel):
    embed = discord.Embed(
        title="💥 ¡Cofre Misterioso!",
        description="¡Un cofre misterioso ha aparecido!\n\n¡Sé de los primeros en golpearlo!",
        color=discord.Color.purple()
    )
    embed.set_image(url=GOLPEAR_GIF)
    embed.set_footer(text="¡Date prisa antes de que desaparezca!")

    view = GolpearView()
    msg = await canal.send(embed=embed, view=view)
    view.message = msg


# ── TASK ───────────────────────────────────────────────
async def golpear_loop(bot):
    await bot.wait_until_ready()
    logger.info("[COFRES:OK_WORKER] Monitor de cofres iniciado.")

    from core.database import (
        claim_golpear_spawn,
        load_golpear_config_to_cache,
        save_golpear_next_spawn,
    )

    while not bot.is_closed():
        try:
            datos_db = await load_golpear_config_to_cache()
            if not datos_db:
                raise RuntimeError("No existe una fila de configuración de cofres")
            _golpear_config.update(datos_db)
            break
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _loop_health["estado"] = "error_config"
            _loop_health["ultimo_error"] = f"{type(error).__name__}: {error}"
            logger.exception(
                "[COFRES:E_CONFIG_DB] No se pudo cargar la configuración; "
                "nuevo intento en 30s."
            )
            await asyncio.sleep(30)

    while not bot.is_closed():
        try:
            if not _golpear_config["activo"] or not _golpear_config["canal_id"]:
                _loop_health["estado"] = (
                    "inactivo" if not _golpear_config["activo"] else "sin_canal"
                )
                _golpear_config["next_spawn_at"] = 0.0
                await _esperar_cambio_o_timeout(60)
                continue

            min_time = int(_golpear_config["min_time"])
            max_time = int(_golpear_config["max_time"])
            if min_time <= 0 or max_time <= min_time:
                raise ValueError(
                    f"intervalo inválido min_time={min_time}, max_time={max_time}"
                )

            now = time.time()
            next_spawn_at = float(_golpear_config.get("next_spawn_at") or 0)
            if next_spawn_at <= 0:
                next_spawn_at = now + random.randint(min_time, max_time)
                await save_golpear_next_spawn(next_spawn_at)
                _golpear_config["next_spawn_at"] = next_spawn_at

            _loop_health["estado"] = "esperando"
            _loop_health["ultimo_error"] = None
            if next_spawn_at > now:
                cambio = await _esperar_cambio_o_timeout(next_spawn_at - now)
                if cambio:
                    continue

            if not _golpear_config["activo"] or not _golpear_config["canal_id"]:
                continue

            canal_id = _golpear_config["canal_id"]
            canal = bot.get_channel(canal_id)
            if canal is None:
                try:
                    canal = await bot.fetch_channel(canal_id)
                except Exception as error:
                    raise RuntimeError(
                        f"no se pudo resolver el canal {canal_id}: {error}"
                    ) from error
            if not isinstance(canal, discord.TextChannel):
                raise TypeError(f"el canal configurado {canal_id} no es de texto")

            bot_member = canal.guild.me
            if bot_member is None:
                raise RuntimeError("no se pudo resolver al miembro del bot")
            faltantes = permisos_faltantes(canal, bot_member)
            if faltantes:
                raise RuntimeError(
                    "permisos faltantes en el canal: " + ", ".join(faltantes)
                )

            # Guardar primero la siguiente ejecución evita duplicar cofres si
            # SquareCloud reinicia justo después del envío actual.
            following_spawn = time.time() + random.randint(min_time, max_time)
            claimed = await claim_golpear_spawn(time.time(), following_spawn)
            if not claimed:
                datos_db = await load_golpear_config_to_cache()
                if datos_db:
                    _golpear_config.update(datos_db)
                continue
            _golpear_config["next_spawn_at"] = following_spawn
            _loop_health["estado"] = "publicando"
            await spawn_cofre(canal)
            _loop_health["ultimo_spawn"] = time.time()
            _loop_health["estado"] = "esperando"
            _loop_health["ultimo_error"] = None

        except asyncio.CancelledError:
            logger.info("[COFRES:OK_STOP] Monitor de cofres detenido limpiamente.")
            raise

        except Exception as error:
            fallo_publicando = _loop_health.get("estado") == "publicando"
            _loop_health["estado"] = "error"
            _loop_health["ultimo_error"] = f"{type(error).__name__}: {error}"
            if fallo_publicando:
                retry_spawn = time.time() + 60
                try:
                    await save_golpear_next_spawn(retry_spawn)
                    _golpear_config["next_spawn_at"] = retry_spawn
                except Exception:
                    logger.exception(
                        "[COFRES:E_RETRY_DB] No se pudo programar el reintento "
                        "del cofre fallido."
                    )
            logger.exception(
                "[COFRES:E_LOOP] Fallo detectado en el sistema de cofres; "
                "nuevo intento en 60s."
            )
            await _esperar_cambio_o_timeout(60)


async def golpear_supervisor(bot):
    """Reinicia el worker si termina de forma inesperada."""
    while not bot.is_closed():
        try:
            await golpear_loop(bot)
            if not bot.is_closed():
                raise RuntimeError("el worker terminó sin que el bot se cerrara")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _loop_health["estado"] = "reiniciando"
            _loop_health["ultimo_error"] = f"{type(error).__name__}: {error}"
            logger.exception(
                "[COFRES:E_TASK_STOPPED] El worker se detuvo inesperadamente; "
                "se reiniciará en 15s."
            )
            await asyncio.sleep(15)


# ── COG ────────────────────────────────────────────────
_loop_task: asyncio.Task | None = None  # Nivel de módulo para evitar tareas duplicadas en recargas


class Golpear(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def cog_unload(self):
        global _loop_task
        if _loop_task and not _loop_task.done():
            _loop_task.cancel()
            logger.info("[COFRES:OK_UNLOAD] Supervisor cancelado al descargar el módulo.")


async def setup(bot):
    global _loop_task
    await bot.add_cog(Golpear(bot))
    # Fix Bug #2: crear la tarea DESPUÉS de add_cog, igual que flush_loop y check_cargos_loop
    if _loop_task is None or _loop_task.done():
        _loop_task = asyncio.create_task(
            golpear_supervisor(bot),
            name="cofres-supervisor",
        )
