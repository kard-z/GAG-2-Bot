import discord
from discord import app_commands
from discord.ext import commands, tasks
import json
import os
import tempfile
import asyncio
import aiohttp
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

# ══════════════════════════════════════════════════════════════════════════════
#  CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

TOKEN  = os.getenv("GAG2_DISCORD_TOKEN")
PREFIX = "."

# ──────────────────────────────────────────────────────────────────────────────
#  ⚠️ PLACEHOLDER API — REPLACE BEFORE RUNNING ⚠️
#
#  No working public API for Grow a Garden 2 stock has been verified yet.
#  Swap GAG2_API_URL for a real, confirmed-working endpoint once you have one,
#  and adjust _parse_gag2_response() below to match its actual JSON shape.
# ──────────────────────────────────────────────────────────────────────────────
GAG2_API_URL = "https://REPLACE-WITH-REAL-GAG2-API.example.com/stock"

DATA_FILE          = "gag2_stock_data.json"
POLL_INTERVAL_SECS = 30

# Roles allowed to configure the bot (setchannel / start / stop / force).
# Adjust to match your server's actual role names.
SETUP_ROLE_NAMES = ("Admin", "Administrator", "Owner", "Head Executives")

CATEGORIES = ["seed", "gear", "crate", "seedpack", "weather"]

CATEGORY_DISPLAY = {
    "seed":     ("🌱", "Seed Shop",      discord.Color.green()),
    "gear":     ("🛠️", "Gear Shop",      discord.Color.blue()),
    "crate":    ("📦", "Crates",         discord.Color.gold()),
    "seedpack": ("🎁", "Seed Packs",     discord.Color.purple()),
    "weather":  ("🌦️", "Weather",        discord.Color.teal()),
}

# ══════════════════════════════════════════════════════════════════════════════

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)

_last_error: str | None = None
_last_poll_at: datetime | None = None


# ══════════════════════════════════════════════════════════════════════════════
#  EMBED HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def error_embed(description: str) -> discord.Embed:
    return discord.Embed(color=discord.Color.red(), description=f"❌ {description}")

def success_embed(description: str) -> discord.Embed:
    return discord.Embed(color=discord.Color.green(), description=f"✅ {description}")

def info_embed(description: str) -> discord.Embed:
    return discord.Embed(color=discord.Color.blurple(), description=f"ℹ️ {description}")


# ══════════════════════════════════════════════════════════════════════════════
#  PERMISSION CHECK
# ══════════════════════════════════════════════════════════════════════════════

def is_setup_authorized(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    member_role_names = {r.name for r in member.roles}
    return bool(member_role_names.intersection(SETUP_ROLE_NAMES))

def require_setup_auth():
    async def predicate(ctx):
        if not is_setup_authorized(ctx.author):
            raise commands.CheckFailure("Not authorized.")
        return True
    return commands.check(predicate)


# ══════════════════════════════════════════════════════════════════════════════
#  DATA PERSISTENCE
# ══════════════════════════════════════════════════════════════════════════════

def _default_data() -> dict:
    return {"channels": {}, "running": False, "last_snapshot": {}}

def load_data() -> dict:
    if not os.path.exists(DATA_FILE):
        return _default_data()
    try:
        with open(DATA_FILE, "r") as f:
            data = json.load(f)
        data.setdefault("channels", {})
        data.setdefault("running", False)
        data.setdefault("last_snapshot", {})
        return data
    except (json.JSONDecodeError, OSError):
        return _default_data()

def save_data(data: dict):
    dir_name = os.path.dirname(os.path.abspath(DATA_FILE)) or "."
    fd, tmp_path = tempfile.mkstemp(dir=dir_name)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, DATA_FILE)
    except OSError:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

def set_channel_id(data: dict, guild_id: int, category: str, channel_id: int):
    data["channels"].setdefault(str(guild_id), {})[category] = channel_id
    save_data(data)


# ══════════════════════════════════════════════════════════════════════════════
#  API FETCH + PARSING
#
#  _parse_gag2_response() is the only thing you should need to edit once you
#  have a real endpoint — make it return a dict shaped like:
#  {
#      "seed":     {"items": [{"name": "Carrot", "quantity": 5, "emoji": "🥕"}, ...]},
#      "gear":     {"items": [...]},
#      "crate":    {"items": [...]},
#      "seedpack": {"items": [...]},
#      "weather":  {"current": "Rain", "countdown": "00h 04m 12s"},
#  }
# ══════════════════════════════════════════════════════════════════════════════

async def fetch_gag2_stock() -> dict | None:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(GAG2_API_URL, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    print(f"[gag2_stock] API returned status {resp.status}")
                    return None
                raw = await resp.json()
    except Exception as e:
        print(f"[gag2_stock] fetch error: {e}")
        return None

    return _parse_gag2_response(raw)


def _parse_gag2_response(raw: dict) -> dict:
    """
    PLACEHOLDER PARSER.
    Adjust this to match the real API's actual response shape once known.
    Currently assumes the API already roughly matches the target shape
    described above and just normalizes missing keys.
    """
    parsed = {}
    for cat in CATEGORIES:
        cat_obj = raw.get(cat, {}) or {}
        if cat == "weather":
            parsed[cat] = {
                "current":   cat_obj.get("current", "Unknown"),
                "countdown": cat_obj.get("countdown"),
            }
        else:
            items = cat_obj.get("items", []) or []
            parsed[cat] = {"items": items}
    return parsed


def snapshot_signature(data: dict) -> dict:
    sig = {}
    for cat in CATEGORIES:
        cat_obj = data.get(cat, {})
        if cat == "weather":
            sig[cat] = cat_obj.get("current")
        else:
            items = cat_obj.get("items", [])
            sig[cat] = sorted((i.get("name"), i.get("quantity")) for i in items)
    return sig


# ══════════════════════════════════════════════════════════════════════════════
#  EMBED BUILDERS
# ══════════════════════════════════════════════════════════════════════════════

def build_category_embed(category: str, data: dict) -> discord.Embed:
    emoji, title, color = CATEGORY_DISPLAY.get(category, ("📦", category.title(), discord.Color.blurple()))
    cat_obj = data.get(category, {})

    if category == "weather":
        current   = cat_obj.get("current", "Unknown")
        countdown = cat_obj.get("countdown")
        desc = f"**Current:** {current}"
        if countdown:
            desc += f"\n**Changes in:** {countdown}"
        embed = discord.Embed(title=f"{emoji} {title}", description=desc, color=color,
                               timestamp=datetime.now(timezone.utc))
        return embed

    items = cat_obj.get("items", [])
    if not items:
        desc = "*No items currently in stock.*"
    else:
        lines = []
        for item in items:
            name = item.get("name", "Unknown")
            qty  = item.get("quantity", "?")
            icon = item.get("emoji", "•")
            lines.append(f"{icon} **{name}** x{qty}")
        desc = "\n".join(lines)

    embed = discord.Embed(title=f"{emoji} {title}", description=desc, color=color,
                           timestamp=datetime.now(timezone.utc))
    embed.set_footer(text="Grow a Garden 2 Stock")
    return embed


def status_embed(guild: discord.Guild) -> discord.Embed:
    data = load_data()
    guild_channels = data["channels"].get(str(guild.id), {})

    embed = discord.Embed(title="📊 Grow a Garden 2 Stock — Status", color=discord.Color.blurple())
    embed.add_field(name="Polling", value="🟢 Running" if data.get("running") else "🔴 Stopped", inline=True)
    embed.add_field(
        name="Last Poll",
        value=_last_poll_at.strftime("%H:%M:%S UTC") if _last_poll_at else "Never",
        inline=True,
    )
    if _last_error:
        embed.add_field(name="Last Error", value=_last_error, inline=False)

    if guild_channels:
        lines = []
        for cat in CATEGORIES:
            cid = guild_channels.get(cat)
            emoji, title, _c = CATEGORY_DISPLAY.get(cat, ("📦", cat.title(), None))
            lines.append(f"{emoji} **{title}** → " + (f"<#{cid}>" if cid else "*not set*"))
        embed.add_field(name="Channels", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="Channels", value="*None configured yet.*", inline=False)

    return embed


def _categories_help() -> str:
    return ", ".join(f"`{c}`" for c in CATEGORIES)


# ══════════════════════════════════════════════════════════════════════════════
#  POLL LOOP
# ══════════════════════════════════════════════════════════════════════════════

@tasks.loop(seconds=POLL_INTERVAL_SECS)
async def poll_loop():
    global _last_error, _last_poll_at

    data = load_data()
    if not data.get("running"):
        return

    raw = await fetch_gag2_stock()
    _last_poll_at = datetime.now(timezone.utc)

    if raw is None:
        _last_error = "Failed to fetch from API (no response or bad status)."
        return
    _last_error = None

    new_sig = snapshot_signature(raw)
    old_sig = data.get("last_snapshot", {})
    changed_categories = [cat for cat in CATEGORIES if new_sig.get(cat) != old_sig.get(cat)]

    if not changed_categories:
        return

    for guild in bot.guilds:
        guild_channels = data["channels"].get(str(guild.id), {})
        for cat in changed_categories:
            channel_id = guild_channels.get(cat)
            if not channel_id:
                continue
            channel = guild.get_channel(channel_id)
            if not channel:
                continue
            try:
                await channel.send(embed=build_category_embed(cat, raw))
            except discord.Forbidden:
                print(f"[gag2_stock] Missing permission to post in channel {channel_id} (guild {guild.id})")
            except discord.HTTPException as e:
                print(f"[gag2_stock] Failed to send embed for {cat}: {e}")

    data["last_snapshot"] = new_sig
    save_data(data)


@poll_loop.before_loop
async def before_poll():
    await bot.wait_until_ready()


# ══════════════════════════════════════════════════════════════════════════════
#  SHARED ACTIONS
# ══════════════════════════════════════════════════════════════════════════════

async def do_setchannel(guild: discord.Guild, category: str, channel: discord.TextChannel):
    data = load_data()
    set_channel_id(data, guild.id, category, channel.id)

async def do_start(guild: discord.Guild) -> bool:
    data = load_data()
    guild_channels = data["channels"].get(str(guild.id), {})
    if not guild_channels:
        return False
    data["running"] = True
    save_data(data)
    if not poll_loop.is_running():
        poll_loop.start()
    return True

async def do_stop():
    data = load_data()
    data["running"] = False
    save_data(data)

async def do_force(guild: discord.Guild):
    raw = await fetch_gag2_stock()
    if raw is None:
        return None, 0

    data           = load_data()
    guild_channels = data["channels"].get(str(guild.id), {})
    posted = 0
    for cat, cid in guild_channels.items():
        channel = guild.get_channel(cid)
        if not channel:
            continue
        try:
            await channel.send(embed=build_category_embed(cat, raw))
            posted += 1
        except (discord.Forbidden, discord.HTTPException):
            continue

    data["last_snapshot"] = snapshot_signature(raw)
    save_data(data)
    return raw, posted


# ══════════════════════════════════════════════════════════════════════════════
#  PREFIX COMMANDS
# ══════════════════════════════════════════════════════════════════════════════

@bot.command(name="gag2setchannel")
@require_setup_auth()
async def gag2setchannel_cmd(ctx, category: str = None, channel: discord.TextChannel = None):
    if not category or not channel:
        return await ctx.send(embed=error_embed(
            f"Usage: `.gag2setchannel <category> #channel`\nValid categories: {_categories_help()}"))
    category = category.lower()
    if category not in CATEGORIES:
        return await ctx.send(embed=error_embed(f"Unknown category `{category}`. Valid: {_categories_help()}"))
    await do_setchannel(ctx.guild, category, channel)
    emoji, title, _c = CATEGORY_DISPLAY.get(category, ("📦", category.title(), None))
    await ctx.send(embed=success_embed(f"{emoji} **{title}** updates will now post to {channel.mention}."))

@gag2setchannel_cmd.error
async def gag2setchannel_cmd_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        await ctx.send(embed=error_embed("You don't have permission to use `.gag2setchannel`."))


@bot.command(name="gag2start")
@require_setup_auth()
async def gag2start_cmd(ctx):
    ok = await do_start(ctx.guild)
    if not ok:
        return await ctx.send(embed=error_embed("No channels configured yet. Use `.gag2setchannel <category> #channel` first."))
    await ctx.send(embed=success_embed(f"Started polling Grow a Garden 2 stock every **{POLL_INTERVAL_SECS}s**."))

@gag2start_cmd.error
async def gag2start_cmd_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        await ctx.send(embed=error_embed("You don't have permission to use `.gag2start`."))


@bot.command(name="gag2stop")
@require_setup_auth()
async def gag2stop_cmd(ctx):
    await do_stop()
    await ctx.send(embed=info_embed("⏹️ Stopped polling Grow a Garden 2 stock."))

@gag2stop_cmd.error
async def gag2stop_cmd_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        await ctx.send(embed=error_embed("You don't have permission to use `.gag2stop`."))


@bot.command(name="gag2status")
async def gag2status_cmd(ctx):
    await ctx.send(embed=status_embed(ctx.guild))


@bot.command(name="gag2force")
@require_setup_auth()
async def gag2force_cmd(ctx):
    raw, posted = await do_force(ctx.guild)
    if raw is None:
        return await ctx.send(embed=error_embed("Failed to fetch stock from the API."))
    if posted == 0:
        return await ctx.send(embed=error_embed("No channels configured for this server."))
    await ctx.send(embed=success_embed(f"Force-posted {posted} categor{'y' if posted == 1 else 'ies'}."))

@gag2force_cmd.error
async def gag2force_cmd_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        await ctx.send(embed=error_embed("You don't have permission to use `.gag2force`."))


# ══════════════════════════════════════════════════════════════════════════════
#  SLASH COMMANDS — setup group
# ══════════════════════════════════════════════════════════════════════════════

gag2_group = app_commands.Group(name="gag2", description="Grow a Garden 2 live stock notifier")


@gag2_group.command(name="setchannel", description="[Setup] Set which channel a stock category posts to")
@app_commands.describe(category="seed, gear, crate, seedpack, weather", channel="The channel to post updates to")
async def gag2_setchannel_slash(interaction: discord.Interaction, category: str, channel: discord.TextChannel):
    if not is_setup_authorized(interaction.user):
        return await interaction.response.send_message(embed=error_embed("You don't have permission to use this."), ephemeral=True)
    category = category.lower()
    if category not in CATEGORIES:
        return await interaction.response.send_message(
            embed=error_embed(f"Unknown category `{category}`. Valid: {_categories_help()}"), ephemeral=True)
    await do_setchannel(interaction.guild, category, channel)
    emoji, title, _c = CATEGORY_DISPLAY.get(category, ("📦", category.title(), None))
    await interaction.response.send_message(embed=success_embed(f"{emoji} **{title}** updates will now post to {channel.mention}."))


@gag2_setchannel_slash.autocomplete("category")
async def gag2_setchannel_autocomplete(interaction: discord.Interaction, current: str):
    return [
        app_commands.Choice(name=c, value=c)
        for c in CATEGORIES if current.lower() in c.lower()
    ][:25]


@gag2_group.command(name="start", description="[Setup] Start polling Grow a Garden 2 stock")
async def gag2_start_slash(interaction: discord.Interaction):
    if not is_setup_authorized(interaction.user):
        return await interaction.response.send_message(embed=error_embed("You don't have permission to use this."), ephemeral=True)
    ok = await do_start(interaction.guild)
    if not ok:
        return await interaction.response.send_message(
            embed=error_embed("No channels configured yet. Use `/gag2 setchannel` first."), ephemeral=True)
    await interaction.response.send_message(embed=success_embed(f"Started polling Grow a Garden 2 stock every **{POLL_INTERVAL_SECS}s**."))


@gag2_group.command(name="stop", description="[Setup] Stop polling Grow a Garden 2 stock")
async def gag2_stop_slash(interaction: discord.Interaction):
    if not is_setup_authorized(interaction.user):
        return await interaction.response.send_message(embed=error_embed("You don't have permission to use this."), ephemeral=True)
    await do_stop()
    await interaction.response.send_message(embed=info_embed("⏹️ Stopped polling Grow a Garden 2 stock."))


@gag2_group.command(name="status", description="Show Grow a Garden 2 stock notifier status")
async def gag2_status_slash(interaction: discord.Interaction):
    await interaction.response.send_message(embed=status_embed(interaction.guild))


@gag2_group.command(name="force", description="[Setup] Force-post current Grow a Garden 2 stock now")
async def gag2_force_slash(interaction: discord.Interaction):
    if not is_setup_authorized(interaction.user):
        return await interaction.response.send_message(embed=error_embed("You don't have permission to use this."), ephemeral=True)
    await interaction.response.defer()
    raw, posted = await do_force(interaction.guild)
    if raw is None:
        return await interaction.followup.send(embed=error_embed("Failed to fetch stock from the API."))
    if posted == 0:
        return await interaction.followup.send(embed=error_embed("No channels configured for this server."))
    await interaction.followup.send(embed=success_embed(f"Force-posted {posted} categor{'y' if posted == 1 else 'ies'}."))


bot.tree.add_command(gag2_group)


# ══════════════════════════════════════════════════════════════════════════════
#  SLASH COMMANDS — one quick-view command per category (for everyone)
# ══════════════════════════════════════════════════════════════════════════════

def _make_category_slash(category: str):
    emoji, title, _c = CATEGORY_DISPLAY.get(category, ("📦", category.title(), None))

    @app_commands.command(name=category, description=f"Show the current Grow a Garden 2 {title} stock")
    async def _cmd(interaction: discord.Interaction):
        await interaction.response.defer()
        raw = await fetch_gag2_stock()
        if raw is None:
            return await interaction.followup.send(embed=error_embed("Failed to fetch stock from the API."))
        await interaction.followup.send(embed=build_category_embed(category, raw))

    return _cmd


for _cat in CATEGORIES:
    bot.tree.add_command(_make_category_slash(_cat))


# ══════════════════════════════════════════════════════════════════════════════
#  EVENTS
# ══════════════════════════════════════════════════════════════════════════════

@bot.event
async def on_ready():
    try:
        await bot.tree.sync()
    except discord.HTTPException as e:
        print(f"[gag2_stock] Slash command sync failed: {e}")

    data = load_data()
    if data.get("running") and not poll_loop.is_running():
        poll_loop.start()

    print(f"[gag2_stock] Logged in as {bot.user} — ready.")


# ══════════════════════════════════════════════════════════════════════════════

if not TOKEN:
    raise SystemExit(
        "GAG2_DISCORD_TOKEN is not set. Create a .env file with:\n"
        "GAG2_DISCORD_TOKEN=your_token_here\n")

bot.run(TOKEN)