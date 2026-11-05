import os

TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
GUILD_ID = os.getenv("GUILD_ID", "980073134411644939")
TEST_MODE = False

if not TOKEN or not DATABASE_URL or not GUILD_ID:
    raise RuntimeError(
        "PurpleJack requiere DISCORD_TOKEN y DATABASE_URL en Square Cloud."
    )
