import discord
from discord.ext import commands
import asyncio
import time
from settings import TOKEN
from core.database import init_db, load_items_to_cache, load_cargos_to_cache, load_collect_config_to_cache, delete_cargo_temporal, create_game_config_table, load_game_config
from core import cache

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)


async def load_modules():
    await bot.load_extension("modules.economy")
    await bot.load_extension("modules.basic_games")
    await bot.load_extension("modules.staff")
    await bot.load_extension("modules.roulette")
    await bot.load_extension("modules.shop")
    await bot.load_extension("modules.collect")


async def check_cargos_loop():
    """Verifica cargos temporales vencidos cada 12 horas — solo lee de caché."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        await asyncio.sleep(43200)
        now = time.time()
        cargos = cache.get_cargos_cache()
        vencidos = []

        for user_id, lista in list(cargos.items()):
            for cargo in lista:
                if cargo["expira_en"] <= now:
                    vencidos.append((user_id, cargo["guild_id"], cargo["rol_id"]))

        for user_id, guild_id, rol_id in vencidos:
            try:
                guild = bot.get_guild(guild_id)
                if not guild:
                    continue
                member = guild.get_member(user_id)
                if not member:
                    member = await guild.fetch_member(user_id)
                role = guild.get_role(rol_id)
                if role and role in member.roles:
                    await member.remove_roles(role)
            except Exception as e:
                print(f"⚠️ Error removiendo cargo {rol_id} a {user_id}: {e}")
            finally:
                await delete_cargo_temporal(user_id, rol_id)

        if vencidos:
            print(f"✅ Cargos temporales vencidos removidos: {len(vencidos)}")


@bot.event
async def on_ready():
    print(f"✅ Bot conectado como {bot.user}")
    print(f"✅ Caché iniciada | Flush cada 5 minutos")
    print(f"✅ Servidores activos: {len(bot.guilds)}")
    await bot.tree.sync()
    print("✅ Comandos Slash/Staff sincronizados.")
    asyncio.create_task(cache.flush_loop())
    asyncio.create_task(check_cargos_loop())
    print("✅ Task de cargos temporales iniciada | Revisión cada 12 horas")


def run_bot():
    async def main():
        await init_db()
    await create_game_config_table()
    await load_game_config()
        await load_items_to_cache()
        await load_cargos_to_cache()
        await load_collect_config_to_cache()
        await load_modules()
        await bot.start(TOKEN)

    asyncio.run(main())
