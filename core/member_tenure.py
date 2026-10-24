"""Regla de antigüedad basada solo en el miembro que interactúa."""

from datetime import datetime, timedelta, timezone


MINIMUM_SERVER_TENURE = timedelta(days=15)
NEW_MEMBER_MESSAGE = (
    "Eres nuevo aca? necesitas minimo 15 dias de antiguedad en el servidor "
    "para participar de la Nave!"
)


def has_minimum_server_tenure(
    joined_at: datetime,
    now: datetime | None = None,
) -> bool:
    """Cumple al alcanzar 15 días completos desde el último ingreso."""
    if joined_at.tzinfo is None:
        joined_at = joined_at.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return current - joined_at >= MINIMUM_SERVER_TENURE
