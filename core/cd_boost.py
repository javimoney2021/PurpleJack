import logging

import discord

from core.config import CD_BOOST_MULTIPLIER
from core.database import get_active_cd_boost


logger = logging.getLogger(__name__)


async def resolve_cd_boost(user_id: int, base_cooldown: int):
    """Devuelve duración efectiva y datos del beneficio activo."""
    boost = await get_active_cd_boost(user_id)
    if not boost:
        return int(base_cooldown), None
    effective = max(1, int(round(base_cooldown * CD_BOOST_MULTIPLIER)))
    return effective, boost


async def send_cd_boost_notice(message, member, boost) -> None:
    """Responde al resultado sin convertir un fallo visual en fallo del comando."""
    if message is None or not boost:
        return
    try:
        await message.reply(
            f"{member.mention} Tus cooldowns están reducidos gracias a la "
            "**Bebida Energetica**",
            mention_author=False,
        )
    except (discord.HTTPException, discord.NotFound):
        logger.warning(
            "No se pudo enviar aviso cd_boost para usuario %s en mensaje %s",
            member.id,
            getattr(message, "id", "?"),
        )
