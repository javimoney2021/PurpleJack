import discord

from core import cache
from core import database


async def get_guild_balance_ranking(
    guild: discord.Guild,
    limit: int = 15,
) -> list[tuple[discord.Member, int]]:
    """Devuelve el ranking de balance compuesto solo por miembros actuales."""
    await cache.flush_to_db()

    if not guild.chunked:
        try:
            await guild.chunk(cache=True)
        except (discord.Forbidden, discord.HTTPException, discord.ClientException):
            # Si Discord no permite completar el caché, se trabaja con los
            # miembros disponibles sin introducir menciones inválidas.
            pass

    members_by_id = {member.id: member for member in guild.members}
    if not members_by_id:
        return []

    async with database.pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, balance FROM users")

    user_cache = cache.get_all_cache()
    candidates = [
        (
            members_by_id[row["id"]],
            user_cache.get(row["id"], {}).get("balance", row["balance"]),
        )
        for row in rows
        if row["id"] in members_by_id
    ]
    candidates.sort(key=lambda entry: entry[1], reverse=True)
    return candidates[:limit]
