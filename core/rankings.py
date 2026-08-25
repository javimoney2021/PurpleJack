import time

import discord

from core import cache


MEMBER_POSITIVE_TTL = 10 * 60
MEMBER_NEGATIVE_TTL = 60
MAX_RANKING_CANDIDATES = 75
MAX_BALANCE_RETRIES = 3

_member_resolution_cache = {}


def invalidate_guild_member(guild_id: int, user_id: int) -> None:
    """Invalida una verificación individual tras una entrada o salida."""
    _member_resolution_cache.pop((int(guild_id), int(user_id)), None)


def clear_guild_membership_cache(guild_id: int) -> None:
    guild_id = int(guild_id)
    for key in [key for key in _member_resolution_cache if key[0] == guild_id]:
        _member_resolution_cache.pop(key, None)


async def _resolve_current_member(
    guild: discord.Guild,
    user_id: int,
) -> discord.Member | None:
    """Resuelve un candidato por ID sin solicitar la lista completa del servidor."""
    user_id = int(user_id)
    member = guild.get_member(user_id)
    if member is not None:
        _member_resolution_cache[(guild.id, user_id)] = (
            time.monotonic() + MEMBER_POSITIVE_TTL,
            member,
        )
        return member

    key = (guild.id, user_id)
    cached = _member_resolution_cache.get(key)
    now = time.monotonic()
    if cached and cached[0] > now:
        return cached[1]
    if cached:
        _member_resolution_cache.pop(key, None)

    try:
        member = await guild.fetch_member(user_id)
    except discord.NotFound:
        _member_resolution_cache[key] = (
            now + MEMBER_NEGATIVE_TTL,
            None,
        )
        return None
    except (discord.Forbidden, discord.HTTPException, discord.ClientException):
        return None

    _member_resolution_cache[key] = (
        time.monotonic() + MEMBER_POSITIVE_TTL,
        member,
    )
    return member


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
