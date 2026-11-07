"""Saldo de dulces y catálogo de crafteo de Halloween."""

import asyncio
import logging

from core import database
from core.config import INVENTORY_ITEM_MAX_QUANTITY


logger = logging.getLogger(__name__)
CANDY = "<:Candy:1558963919492481055>"
_candy_balances: dict[int, int] = {}
_user_locks: dict[int, asyncio.Lock] = {}


def _get_user_lock(user_id: int) -> asyncio.Lock:
    lock = _user_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _user_locks[user_id] = lock
    return lock


def purge_candy_cache(user_id: int) -> None:
    _candy_balances.pop(user_id, None)
    _user_locks.pop(user_id, None)


async def init_halloween_tables() -> None:
    async with database.pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS halloween_candy_balances (
                user_id BIGINT PRIMARY KEY,
                balance INTEGER NOT NULL DEFAULT 0 CHECK (balance >= 0)
            )
        """)
        await conn.execute("DROP TABLE IF EXISTS halloween_candy_awards")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS halloween_craft_items (
                id SERIAL PRIMARY KEY,
                nombre TEXT NOT NULL UNIQUE,
                precio INTEGER NOT NULL CHECK (precio > 0),
                icono TEXT NOT NULL DEFAULT '',
                descripcion TEXT NOT NULL DEFAULT '',
                active BOOLEAN NOT NULL DEFAULT TRUE
            )
        """)
        await conn.execute("""
            ALTER TABLE halloween_craft_items
            ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS halloween_craft_inventory (
                user_id BIGINT NOT NULL,
                item_id INTEGER NOT NULL REFERENCES halloween_craft_items(id),
                cantidad INTEGER NOT NULL CHECK (cantidad BETWEEN 1 AND 3),
                PRIMARY KEY (user_id, item_id)
            )
        """)


async def get_candy_balance(user_id: int) -> int:
    """Lee la fuente persistente y sincroniza el saldo en RAM."""
    async with _get_user_lock(user_id):
        async with database.pool.acquire() as conn:
            balance = await conn.fetchval(
                "SELECT balance FROM halloween_candy_balances WHERE user_id=$1",
                user_id,
            )
        amount = int(balance or 0)
        _candy_balances[user_id] = amount
        return amount


async def get_candy_ranking_page(offset: int, limit: int = 10) -> tuple[list[dict], int]:
    """Consulta el ranking persistente sin solicitar la lista completa del servidor."""
    async with database.pool.acquire() as conn:
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM halloween_candy_balances WHERE balance > 0"
        )
        rows = await conn.fetch(
            """
            SELECT user_id, balance
            FROM halloween_candy_balances
            WHERE balance > 0
            ORDER BY balance DESC, user_id ASC
            LIMIT $1 OFFSET $2
            """,
            limit,
            offset,
        )
    return [dict(row) for row in rows], int(total or 0)


async def get_craft_inventory_page(offset: int, limit: int = 5) -> tuple[list[dict], int]:
    """Lista propietarios de Craft sin consultar la lista de miembros de Discord."""
    async with database.pool.acquire() as conn:
        total = await conn.fetchval(
            "SELECT COUNT(DISTINCT user_id) FROM halloween_craft_inventory "
            "WHERE cantidad > 0"
        )
        rows = await conn.fetch(
            """
            WITH holders AS (
                SELECT user_id
                FROM halloween_craft_inventory
                WHERE cantidad > 0
                GROUP BY user_id
                ORDER BY user_id
                LIMIT $1 OFFSET $2
            )
            SELECT inv.user_id, craft.nombre, inv.cantidad
            FROM holders
            JOIN halloween_craft_inventory AS inv ON inv.user_id = holders.user_id
            JOIN halloween_craft_items AS craft ON craft.id = inv.item_id
            WHERE inv.cantidad > 0
            ORDER BY inv.user_id, craft.nombre, craft.id
            """,
            limit,
            offset,
        )
    users: dict[int, dict] = {}
    for row in rows:
        user_id = int(row["user_id"])
        user = users.setdefault(user_id, {"user_id": user_id, "items": []})
        user["items"].append({
            "nombre": row["nombre"],
            "cantidad": int(row["cantidad"]),
        })
    return list(users.values()), int(total or 0)


async def purge_craft_inventories() -> dict[str, int]:
    """Borra solo artículos canjeados; invalida RAM después del commit."""
    async with database.pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "LOCK TABLE halloween_craft_inventory IN SHARE ROW EXCLUSIVE MODE"
            )
            rows = await conn.fetch(
                "DELETE FROM halloween_craft_inventory RETURNING user_id, cantidad"
            )
    affected_users = {int(row["user_id"]) for row in rows}
    for user_id in affected_users:
        database.cache.invalidate_inventory_cache(user_id)
    return {
        "users": len(affected_users),
        "items": len(rows),
        "units": sum(int(row["cantidad"]) for row in rows),
    }


async def add_craft_item(nombre: str, precio: int, icono: str, descripcion: str) -> bool:
    async with database.pool.acquire() as conn:
        item_id = await conn.fetchval(
            """
            INSERT INTO halloween_craft_items (nombre, precio, icono, descripcion)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (nombre) DO UPDATE
            SET precio=EXCLUDED.precio,
                icono=EXCLUDED.icono,
                descripcion=EXCLUDED.descripcion,
                active=TRUE
            WHERE halloween_craft_items.active=FALSE
            RETURNING id
            """,
            nombre,
            precio,
            icono,
            descripcion,
        )
    return item_id is not None


async def get_craft_items() -> list[dict]:
    async with database.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, nombre, precio, icono, descripcion "
            "FROM halloween_craft_items WHERE active=TRUE "
            "ORDER BY precio ASC, id ASC"
        )
    return [dict(row) for row in rows]


async def delete_craft_item(item_id: int) -> str | None:
    """Retira el artículo del catálogo sin borrar copias ya canjeadas."""
    async with database.pool.acquire() as conn:
        return await conn.fetchval(
            "UPDATE halloween_craft_items SET active=FALSE "
            "WHERE id=$1 AND active=TRUE RETURNING nombre",
            item_id,
        )


async def purchase_craft_item(user_id: int, item_id: int) -> dict:
    """Canjea un artículo y descuenta dulces en la misma transacción."""
    async with _get_user_lock(user_id):
        async with database.pool.acquire() as conn:
            async with conn.transaction():
                item = await conn.fetchrow(
                    "SELECT id, nombre, precio FROM halloween_craft_items "
                    "WHERE id=$1 AND active=TRUE FOR UPDATE",
                    item_id,
                )
                if item is None:
                    return {"ok": False, "reason": "not_found"}
                balance = await conn.fetchval(
                    "SELECT balance FROM halloween_candy_balances "
                    "WHERE user_id=$1 FOR UPDATE",
                    user_id,
                )
                if balance is None or balance < item["precio"]:
                    return {"ok": False, "reason": "insufficient", "balance": int(balance or 0)}
                owned = await conn.fetchval(
                    "SELECT cantidad FROM halloween_craft_inventory "
                    "WHERE user_id=$1 AND item_id=$2 FOR UPDATE",
                    user_id,
                    item_id,
                )
                if int(owned or 0) >= INVENTORY_ITEM_MAX_QUANTITY:
                    return {"ok": False, "reason": "inventory_cap"}
                new_balance = await conn.fetchval(
                    """
                    UPDATE halloween_candy_balances
                    SET balance=balance-$2
                    WHERE user_id=$1 AND balance >= $2
                    RETURNING balance
                    """,
                    user_id,
                    item["precio"],
                )
                await conn.execute(
                    """
                    INSERT INTO halloween_craft_inventory (user_id, item_id, cantidad)
                    VALUES ($1, $2, 1)
                    ON CONFLICT (user_id, item_id) DO UPDATE
                    SET cantidad=halloween_craft_inventory.cantidad+1
                    """,
                    user_id,
                    item_id,
                )
        _candy_balances[user_id] = int(new_balance)
        database.cache.invalidate_inventory_cache(user_id)
        return {
            "ok": True,
            "balance": int(new_balance),
            "item_name": item["nombre"],
        }
