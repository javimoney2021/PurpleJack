import re

import discord


_MEMBER_MENTION_RE = re.compile(r"^<@!?(\d{15,22})>$")
_MEMBER_ID_RE = re.compile(r"^\d{15,22}$")


def parse_member_id(value: str | None) -> int | None:
    """Extrae únicamente una mención completa o un snowflake explícito."""
    if value is None:
        return None
    value = value.strip()
    match = _MEMBER_MENTION_RE.fullmatch(value)
    if match:
        return int(match.group(1))
    if _MEMBER_ID_RE.fullmatch(value):
        return int(value)
    return None


async def resolve_guild_member(
    guild: discord.Guild | None,
    user_id: int,
) -> discord.Member | None:
    """Resuelve un solo miembro por caché y usa HTTP como respaldo."""
    if guild is None:
        return None

    member = guild.get_member(int(user_id))
    if member is not None:
        return member

    try:
        return await guild.fetch_member(int(user_id))
    except (
        discord.NotFound,
        discord.Forbidden,
        discord.HTTPException,
        discord.ClientException,
    ):
        return None


async def resolve_explicit_member(
    guild: discord.Guild | None,
    value: str | None,
) -> discord.Member | None:
    """Resuelve exclusivamente menciones o IDs, nunca búsquedas masivas por nombre."""
    user_id = parse_member_id(value)
    if user_id is None:
        return None
    return await resolve_guild_member(guild, user_id)


async def resolve_reply_member(ctx) -> discord.Member | None:
    """Obtiene y verifica individualmente al autor del mensaje respondido."""
    if ctx.guild is None:
        return None

    reference = ctx.message.reference
    if reference is None or reference.message_id is None:
        return None

    message = reference.resolved
    if not isinstance(message, discord.Message):
        channel = ctx.guild.get_channel(reference.channel_id) or ctx.channel
        try:
            message = await channel.fetch_message(reference.message_id)
        except (
            discord.NotFound,
            discord.Forbidden,
            discord.HTTPException,
            AttributeError,
        ):
            return None

    return await resolve_guild_member(ctx.guild, message.author.id)
