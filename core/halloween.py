"""Saldo de dulces y catálogo de crafteo de Halloween."""

import asyncio
import logging
import random
import time

import discord

from core import database
from core.config import INVENTORY_ITEM_MAX_QUANTITY


logger = logging.getLogger(__name__)
CANDY = "<:Candy:1558963919492481055>"
GHOST = "<:fantasma:1556828340843974748>"
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
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS halloween_candy_awards (
                source_key TEXT PRIMARY KEY,
                user_id BIGINT NOT NULL,
                amount SMALLINT NOT NULL CHECK (amount BETWEEN 0 AND 3),
                created_at DOUBLE PRECISION NOT NULL
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS halloween_candy_awards_user_idx
            ON halloween_candy_awards (user_id)
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS halloween_craft_items (
                id SERIAL PRIMARY KEY,
                nombre TEXT NOT NULL UNIQUE,
                precio INTEGER NOT NULL CHECK (precio > 0),
                icono TEXT NOT NULL DEFAULT '',
                descripcion TEXT NOT NULL DEFAULT ''
            )
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


async def award_candy(user_id: int, source_key: str) -> int:
    """Decide el 50 % y persiste una sola tirada por resultado de juego."""
    amount = random.randint(1, 3) if random.random() < 0.5 else 0
    async with _get_user_lock(user_id):
        async with database.pool.acquire() as conn:
            async with conn.transaction():
                inserted = await conn.fetchval(
                    """
                    INSERT INTO halloween_candy_awards
                        (source_key, user_id, amount, created_at)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (source_key) DO NOTHING
                    RETURNING amount
                    """,
                    source_key,
                    user_id,
                    amount,
                    time.time(),
                )
                if inserted is None:
                    return 0
                if amount:
                    balance = await conn.fetchval(
                        """
                        INSERT INTO halloween_candy_balances (user_id, balance)
                        VALUES ($1, $2)
                        ON CONFLICT (user_id) DO UPDATE
                        SET balance=halloween_candy_balances.balance + EXCLUDED.balance
                        RETURNING balance
                        """,
                        user_id,
                        amount,
                    )
                    _candy_balances[user_id] = int(balance)
    return amount


async def award_and_notify(channel, user_id: int, source_key: str) -> int:
    """Entrega dulces y publica un aviso separado solo si la tirada ganó."""
    try:
        amount = await award_candy(user_id, source_key)
    except Exception:
        logger.exception("No se pudieron acreditar dulces para %s (%s)", user_id, source_key)
        return 0
    if amount and channel is not None:
        try:
            await channel.send(
                f"<@{user_id}> Has recibido ({amount} {CANDY}) por tu victoria reciente {GHOST}",
                allowed_mentions=discord.AllowedMentions(
                    everyone=False, roles=False, users=True, replied_user=False,
                ),
            )
        except Exception:
            logger.exception("Dulces acreditados, pero falló el aviso de %s", user_id)
    return amount


async def add_craft_item(nombre: str, precio: int, icono: str, descripcion: str) -> bool:
    async with database.pool.acquire() as conn:
        item_id = await conn.fetchval(
            """
            INSERT INTO halloween_craft_items (nombre, precio, icono, descripcion)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (nombre) DO NOTHING
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
            "FROM halloween_craft_items ORDER BY precio ASC, id ASC"
        )
    return [dict(row) for row in rows]


async def purchase_craft_item(user_id: int, item_id: int) -> dict:
    """Canjea un artículo y descuenta dulces en la misma transacción."""
    async with _get_user_lock(user_id):
        async with database.pool.acquire() as conn:
            async with conn.transaction():
                item = await conn.fetchrow(
                    "SELECT id, nombre, precio FROM halloween_craft_items WHERE id=$1",
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
