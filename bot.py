import discord
from discord.ext import commands
import asyncio
import time
import discord
print(f"discord.py version: {discord.__version__}")
from settings import TOKEN
from core.database import (
    init_db, load_items_to_cache, load_cargos_to_cache,
    load_collect_config_to_cache, delete_cargo_temporal,
    create_game_config_table, load_game_config, load_dados_config
)
from core import cache

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


async def load_modules():
    await bot.load_extension("modules.economy")
    await bot.load_extension("modules.basic_games")
    await bot.load_extension("modules.staff")
    await bot.load_extension("modules.roulette")
    await bot.load_extension("modules.russian_roulette")
    await bot.load_extension("modules.rob")
    await bot.load_extension("modules.shop")
    await bot.load_extension("modules.collect")
    await bot.load_extension("modules.dados")
    await bot.load_extension("modules.duels")
    await bot.load_extension("modules.golpear")
    await bot.load_extension("modules.carrera")
    await bot.load_extension("modules.registro_ruleta")
    await bot.load_extension("modules.memo")


async def check_cargos_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        await asyncio.sleep(60)
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


AYUDA_CHANNEL_ID = 1488006594976415786

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        await ctx.send(
            f"❌ Este comando no es válido, consulta **/ayuda_nave** en el canal <#{AYUDA_CHANNEL_ID}>",
            delete_after=10
        )
    elif isinstance(error, commands.CommandOnCooldown):
        return  # deja que el handler del módulo lo maneje
    else:
        raise error

async def shutdown():
    print("⚠️ Apagando bot — flusheando caché a DB...")
    await cache.flush_to_db()
    print("✅ Caché flusheada correctamente.")
    await bot.close()

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
    print("\n⫷ 𝙋𝙐𝙍𝙋𝙇𝙀𝙅𝘼𝘾𝙆 𝙀𝙉 𝙇𝙄𝙉𝙀𝘼 ⫸\n")


def run_bot():
    async def main():
        await init_db()
        await create_game_config_table()
        await load_game_config()
        await load_dados_config()
        await load_items_to_cache()
        await load_cargos_to_cache()
        await load_collect_config_to_cache()
        await load_modules()
        try:
            await bot.start(TOKEN)
        finally:
            await cache.flush_to_db()
            print("✅ Flush final completado al cerrar.")

    asyncio.run(main())


