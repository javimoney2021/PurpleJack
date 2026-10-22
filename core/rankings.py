"""Rankings económicos sin consultas HTTP a Discord al mostrarlos."""

from core import cache


def get_balance_ranking(limit: int = 15) -> list[tuple[int, int]]:
    """Devuelve IDs y saldos vivos; !top y !rob comparten el mismo orden."""
    limit = max(1, min(int(limit), 25))
    _, candidates = cache.get_balance_index_snapshot(limit)
    return list(candidates)
