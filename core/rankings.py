import discord

from core import cache


MAX_RANKING_CANDIDATES = 75
MAX_BALANCE_RETRIES = 3

async def _resolve_current_member(
    guild: discord.Guild,
    user_id: int,
) -> discord.Member | None:
    """Valida por HTTP un candidato sin caché ni lista global de miembros."""
    try:
        return await guild.fetch_member(int(user_id))
    except discord.NotFound:
        return None
    except (discord.Forbidden, discord.HTTPException, discord.ClientException):
        return None


async def get_guild_balance_ranking(
    guild: discord.Guild,
    limit: int = 15,
) -> list[tuple[discord.Member, int]]:
    """Construye el ranking vivo desde RAM y valida solo sus candidatos."""
    if guild is None:
        return []

    limit = max(1, min(int(limit), 25))
    candidate_limit = max(limit, min(MAX_RANKING_CANDIDATES, limit * 5))
    last_results = []

    for _ in range(MAX_BALANCE_RETRIES):
        version, candidates = cache.get_balance_index_snapshot(candidate_limit)
        results = []
        for user_id, balance in candidates:
            member = await _resolve_current_member(guild, user_id)
            if member is None:
                continue
            results.append((member, balance))
            if len(results) >= limit:
                break

        last_results = results
        if cache.get_balance_index_version() == version:
            return results

    # Con actividad económica continua, entrega los miembros ya validados con
    # sus saldos más recientes en vez de forzar un flush o una consulta masiva.
    refreshed = [
        (member, cache.get_indexed_balance(member.id, balance))
        for member, balance in last_results
    ]
    refreshed.sort(key=lambda entry: (-entry[1], entry[0].id))
    return refreshed[:limit]
