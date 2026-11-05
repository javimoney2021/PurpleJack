"""Recordatorios opt-in de cooldown de juegos, persistidos para reinicios."""

import asyncio
import logging
import time

import discord

from core import database

logger = logging.getLogger(__name__)
GAME_NAMES = {
    "ruleta": "Ruleta",
    "dados": "Dados",
    "bj": "Blackjack",
    "memo": "Memoria",
    "trabajar": "Trabajar",
}


class ReminderPrompt(discord.ui.LayoutView):
    """Oferta temporal de aviso, mostrada solo tras finalizar una partida."""

    def __init__(self, user_id: int, game: str, expires_at: float):
        super().__init__(timeout=15)
        self.user_id = user_id
        self.game = game
        self.expires_at = expires_at
        self.message: discord.Message | None = None
        self._deletion_task: asyncio.Task | None = None
        container = discord.ui.Container(accent_color=0x7B42D6)
        activity = (
            "volver a trabajar" if game == "trabajar"
            else f"jugar **{GAME_NAMES[game]}**"
        )
        container.add_item(discord.ui.TextDisplay(
            f"¿Quieres recibir un aviso por DM cuando puedas "
            f"{activity} <t:{int(expires_at)}:R>?"
        ))
        row = discord.ui.ActionRow()
        row.add_item(ReminderAcceptButton())
        container.add_item(row)
        self.add_item(container)

async def _delete_prompt_after(prompt: ReminderPrompt, delay: int) -> None:
    await asyncio.sleep(delay)
    prompt.stop()
    if prompt.message is not None:
        try:
            await prompt.message.delete()
        except (discord.HTTPException, discord.NotFound):
            pass


class ReminderAcceptButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Avisarme por DM", emoji="🔔",
                         style=discord.ButtonStyle.success)

    async def callback(self, interaction: discord.Interaction):
        prompt: ReminderPrompt = self.view
        if interaction.user.id != prompt.user_id:
            await interaction.response.send_message(
                "Este recordatorio no es para ti.", ephemeral=True,
            )
            return
        await interaction.response.defer()
        try:
            current_expiry = await _current_cooldown_expiry(prompt.user_id, prompt.game)
            if current_expiry != prompt.expires_at or current_expiry <= time.time():
                await interaction.followup.send(
                    "El cooldown de esta partida ya cambió o terminó.", ephemeral=True,
                )
                return
            await set_reminder(prompt.user_id, prompt.game, prompt.expires_at)
        except Exception:
            logger.exception("No se pudo activar aviso de %s para %s", prompt.game, prompt.user_id)
            await interaction.followup.send(
                "No pude activar el aviso. Inténtalo más tarde.", ephemeral=True,
            )
            return
        prompt.stop()
        if prompt._deletion_task is not None:
            prompt._deletion_task.cancel()
        prompt.clear_items()
        container = discord.ui.Container(accent_color=0x43B581)
        activity = (
            "volver a trabajar" if prompt.game == "trabajar"
            else f"jugar **{GAME_NAMES[prompt.game]}**"
        )
        container.add_item(discord.ui.TextDisplay(
            f"✅ <@{prompt.user_id}> Te avisaré por DM cuando puedas "
            f"{activity}."
        ))
        prompt.add_item(container)
        try:
            await prompt.message.edit(view=prompt)
        except (discord.HTTPException, discord.NotFound):
            pass
        await asyncio.sleep(3)
        try:
            await prompt.message.delete()
        except (discord.HTTPException, discord.NotFound):
            pass


async def send_game_reminder_prompt(interaction: discord.Interaction | None,
                                    user_id: int, game: str,
                                    expires_at: float, *, delay: float = 2) -> None:
    """No afecta la liquidación del juego si Discord o el aviso fallan."""
    if game != "trabajar":
        try:
            from modules.player_panels import notify_game_finished
            await notify_game_finished(user_id, game, expires_at)
        except Exception:
            logger.exception("No se pudo refrescar /jugar tras %s de %s", game, user_id)
    if interaction is None or expires_at <= time.time():
        return
    try:
        if delay:
            await asyncio.sleep(delay)
        if expires_at <= time.time():
            return
        if await reminder_enabled(user_id, game, expires_at):
            return
        prompt = ReminderPrompt(user_id, game, expires_at)
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        prompt.message = await interaction.followup.send(
            view=prompt, ephemeral=True, wait=True,
        )
        prompt._deletion_task = asyncio.create_task(_delete_prompt_after(prompt, 15))
    except Exception:
        logger.exception("No se pudo ofrecer aviso de %s a %s", game, user_id)


async def _work_cooldown_expiry(user_id: int) -> float:
    from modules.Empleos import EMPLEOS, EMPLEOS_MAESTRIA, get_empleo_user, normalizar_empleo

    data = await get_empleo_user(user_id, force_refresh=True)
    if not data:
        return 0
    empleo = normalizar_empleo(data.get("empleo_actual") or "")
    info = EMPLEOS.get(empleo) or EMPLEOS_MAESTRIA.get(empleo)
    if not info:
        return 0
    return float(data.get("ultimo_trabajo") or 0) + info["duracion_horas"] * 3600


async def _current_cooldown_expiry(user_id: int, game: str) -> float:
    if game == "memo":
        return await database.get_command_cooldown("user", user_id, "memo")
    if game == "trabajar":
        return await _work_cooldown_expiry(user_id)
    return await database.get_game_cooldown(user_id, game)


async def init_game_reminders() -> None:
    async with database.pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS game_cooldown_reminders (
                user_id BIGINT NOT NULL,
                game TEXT NOT NULL,
                expires_at DOUBLE PRECISION NOT NULL,
                sent_at DOUBLE PRECISION,
                claim_until DOUBLE PRECISION NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, game)
            )
        """)


async def reminder_enabled(user_id: int, game: str, expires_at: float) -> bool:
    async with database.pool.acquire() as conn:
        value = await conn.fetchval(
            "SELECT 1 FROM game_cooldown_reminders "
            "WHERE user_id=$1 AND game=$2 AND expires_at=$3 AND sent_at IS NULL",
            user_id, game, expires_at,
        )
    return bool(value)


async def set_reminder(user_id: int, game: str, expires_at: float) -> None:
    if game not in GAME_NAMES or expires_at <= time.time():
        raise ValueError("El cooldown ya no está activo.")
    async with database.pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO game_cooldown_reminders
                (user_id, game, expires_at, sent_at, claim_until)
            VALUES ($1, $2, $3, NULL, 0)
            ON CONFLICT (user_id, game) DO UPDATE
            SET expires_at=EXCLUDED.expires_at, sent_at=NULL, claim_until=0
        """, user_id, game, expires_at)


async def cancel_reminder(user_id: int, game: str) -> None:
    async with database.pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM game_cooldown_reminders WHERE user_id=$1 AND game=$2",
            user_id, game,
        )


async def _claim_due(now: float) -> list[dict]:
    async with database.pool.acquire() as conn:
        rows = await conn.fetch("""
            UPDATE game_cooldown_reminders AS reminder
            SET claim_until=$1 + 300
            WHERE (user_id, game) IN (
                SELECT user_id, game FROM game_cooldown_reminders
                WHERE sent_at IS NULL AND expires_at <= $1 AND claim_until <= $1
                ORDER BY expires_at LIMIT 20 FOR UPDATE SKIP LOCKED
            )
            RETURNING reminder.user_id, reminder.game, reminder.expires_at
        """, now)
    return [dict(row) for row in rows]


async def game_reminder_worker(bot: discord.Client) -> None:
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            for reminder in await _claim_due(time.time()):
                user_id, game, expires_at = (
                    reminder["user_id"], reminder["game"], reminder["expires_at"]
                )
                current_expiry = await _current_cooldown_expiry(user_id, game)
                if current_expiry != expires_at:
                    async with database.pool.acquire() as conn:
                        if current_expiry > time.time():
                            await conn.execute(
                                "UPDATE game_cooldown_reminders "
                                "SET expires_at=$4, claim_until=0 "
                                "WHERE user_id=$1 AND game=$2 AND expires_at=$3",
                                user_id, game, expires_at, current_expiry,
                            )
                        else:
                            await conn.execute(
                                "DELETE FROM game_cooldown_reminders "
                                "WHERE user_id=$1 AND game=$2 AND expires_at=$3",
                                user_id, game, expires_at,
                            )
                    continue
                if game == "trabajar":
                    from modules.Empleos import get_jornada_activa
                    if await get_jornada_activa(user_id):
                        continue  # La jornada en curso se comprobará al caducar el lease.
                try:
                    user = await bot.fetch_user(user_id)
                    if game == "trabajar":
                        await user.send("🔔 Ya puedes volver a trabajar en PurpleJack.")
                    else:
                        await user.send(
                            f"🔔 Ya puedes volver a jugar **{GAME_NAMES[game]}** "
                            "en PurpleJack."
                        )
                    result = time.time()
                except (discord.Forbidden, discord.NotFound):
                    result = -1  # DM cerrados: no insistir ni generar spam.
                except Exception:
                    logger.exception("Fallo al avisar cooldown de %s a %s", game, user_id)
                    continue  # El lease permitirá reintentar después de 5 minutos.
                async with database.pool.acquire() as conn:
                    await conn.execute("""
                        UPDATE game_cooldown_reminders SET sent_at=$4
                        WHERE user_id=$1 AND game=$2 AND expires_at=$3
                    """, user_id, game, expires_at, result)
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Fallo en worker de recordatorios de juegos")
        await asyncio.sleep(30)
