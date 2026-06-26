import discord
from discord import app_commands
from discord.ext import commands, tasks
import json
import os
import tempfile
import time
import re
import asyncio
import sqlite3
import random
import aiohttp
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from cryptography.fernet import Fernet, InvalidToken

load_dotenv()

# ══════════════════════════════════════════════════════════════════════════════
#  DATA ENCRYPTION AT REST
#
#  bot_data.db contains case histories, warnings, message stats, etc.
#  It is encrypted on disk using Fernet (AES-128-CBC + HMAC-SHA256).
#  The key lives ONLY in the environment (.env), never in this file or in
#  the data file itself. Losing the key means losing access to the data,
#  so back it up somewhere safe (e.g. a password manager).
# ══════════════════════════════════════════════════════════════════════════════

_DATA_ENCRYPTION_KEY = os.getenv("DATA_ENCRYPTION_KEY")

if not _DATA_ENCRYPTION_KEY:
    raise SystemExit(
        "DATA_ENCRYPTION_KEY is not set. Generate one with:\n"
        "  python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"\n"
        "then add it to your .env file as:\n"
        "  DATA_ENCRYPTION_KEY=your_generated_key_here\n"
        "Keep this key secret and back it up — without it, bot_data.db cannot be decrypted.\n"
    )

try:
    _fernet = Fernet(_DATA_ENCRYPTION_KEY.encode())
except Exception:
    raise SystemExit(
        "DATA_ENCRYPTION_KEY is set but is not a valid Fernet key.\n"
        "Generate a new one with:\n"
        "  python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"\n"
    )

# ══════════════════════════════════════════════════════════════════════════════

#  CONFIGURATION — edit these to match your server exactly

# ══════════════════════════════════════════════════════════════════════════════

TOKEN                = os.getenv("DISCORD_TOKEN")

PREFIX               = "."

MOD_LOG_CHANNEL_NAME  = "🛡️┃staff-logs"

AUTO_LOG_CHANNEL_NAME = "mod-logs"

JAIL_REQUEST_CHANNEL_NAME = "jail-requests"

MOD_ROLE_NAME        = "Moderator"

TRIAL_MOD_ROLE_NAME  = "Trial Moderatorerator"

STAFF_ROLE_KEYWORDS  = ("Staff Team", "admin", "mod", "helper", "support", "management")

JAIL_ROLE_NAME       = "Jailed"

MUTED_ROLE_NAME      = "Muted"

DATA_FILE            = "bot_data.db"   # SQLite database (was bot_data.json)

DEFAULT_MUTE_SECONDS = 14 * 24 * 60 * 60

JAIL_PURGE_MESSAGES  = 100

BOT_OWNER_ID         = 1516072585799139429

AUTOMOD_ENABLED   = False

SPAM_THRESHOLD    = 5

CAPS_PERCENT      = 70

MIN_CAPS_LENGTH   = 8

BLOCKED_WORDS     = ["badword1", "badword2"]

BLOCKED_LINKS     = True

WHITELISTED_LINKS = ["discord.gg", "youtube.com", "youtu.be"]

# ══════════════════════════════════════════════════════════════════════════════

#  DEFAULT CMD_ROLES — used as fallback if no per-guild config saved

# ══════════════════════════════════════════════════════════════════════════════

CMD_ROLES_DEFAULT = {

    "setup":      ["Head Executives"],

}

_guild_cmd_roles: dict[int, dict] = {}

def get_cmd_roles(guild_id: int) -> dict:

    return _guild_cmd_roles.get(guild_id, CMD_ROLES_DEFAULT)

# ══════════════════════════════════════════════════════════════════════════════

intents = discord.Intents.default()

intents.members = True

intents.message_content = True

intents.messages = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)

spam_tracker: dict[int, list] = {}

afk_users: dict[int, dict] = {}

# ══════════════════════════════════════════════════════════════════════════════

#  EMBED HELPERS

# ══════════════════════════════════════════════════════════════════════════════

ACTION_COLORS = {

    "Ban": discord.Color.red(),        "Unban": discord.Color.green(),

    "Kick": discord.Color.orange(),    "Mute": discord.Color.dark_orange(),

    "Unmute": discord.Color.green(),   "Warn": discord.Color.yellow(),

    "Unwarn": discord.Color.teal(),    "Jail": discord.Color.dark_gray(),

    "Unjail": discord.Color.blurple(),

}

ACTION_ICONS = {

    "Ban": "🔨", "Unban": "✅", "Kick": "👢", "Mute": "🔇", "Unmute": "🔊",

    "Warn": "⚠️", "Unwarn": "🗑️", "Jail": "🔒", "Unjail": "🔓", "Automod": "🤖",

    "DWC": "🏷️",

    "Remove DWC": "🏷️",

}

def result_embed(action: str, target, reason: str, case_id: int, extra: str = "") -> discord.Embed:

    embed = discord.Embed(

        title=f"{ACTION_ICONS.get(action, '📋')} {action} | Case #{case_id}",

        color=ACTION_COLORS.get(action, discord.Color.blurple()),

        timestamp=datetime.now(timezone.utc)

    )

    embed.add_field(name="User",   value=f"{target} (`{target.id}`)", inline=False)

    embed.add_field(name="Reason", value=reason, inline=False)

    if extra:

        embed.add_field(name="Note", value=extra, inline=False)

    embed.set_footer(text=f"Case #{case_id}")

    if hasattr(target, "display_avatar"):

        embed.set_thumbnail(url=target.display_avatar.url)

    return embed

def error_embed(description: str) -> discord.Embed:

    return discord.Embed(color=discord.Color.red(), description=f"❌ {description}")

def warn_embed(description: str) -> discord.Embed:

    return discord.Embed(color=discord.Color.yellow(), description=f"⚠️ {description}")

def success_embed(description: str) -> discord.Embed:

    return discord.Embed(color=discord.Color.green(), description=f"✅ {description}")

def info_embed(description: str) -> discord.Embed:

    return discord.Embed(color=discord.Color.blurple(), description=f"ℹ️ {description}")

# ══════════════════════════════════════════════════════════════════════════════

#  SYNTAX HELPER

# ══════════════════════════════════════════════════════════════════════════════

COMMAND_SYNTAX = {

    "ban": {

        "usage":   ".ban @user [reason]",

        "example": ".ban @John breaking server rules",

        "note":    "Permanently bans the user from the server.",

        "perms":   "Moderator / Admin",

    },

    "unban": {

        "usage":   ".unban <user_id>",

        "example": ".unban 123456789012345678",

        "note":    "Unbans a user by their Discord ID. Right-click a user → Copy ID.",

        "perms":   "Moderator / Admin",

    },

    "kick": {

        "usage":   ".kick @user [reason]",

        "example": ".kick @John spamming in general",

        "note":    "Kicks the user from the server. They can rejoin with an invite.",

        "perms":   "Moderator / Admin",

    },

    "mute": {

        "usage":   ".mute @user [duration] [reason]",

        "example": (

            ".mute @John 10m spam\n"

            ".mute @John 2h being disrespectful\n"

            ".mute @John 1d30m repeated violations\n"

            ".mute @John (no duration = 14 days)"

        ),

        "note": (

            "Duration units: `s`, `sec`, `secs`, `second`, `seconds` • "

            "`m`, `min`, `mins`, `minute`, `minutes` • "

            "`h`, `hr`, `hrs`, `hour`, `hours` • "

            "`d`, `day`, `days` • `w`, `week`, `weeks`\n"

            "Combine freely: `1d12h`, `2h30m`, `10sec`, `5mins`, `1day`\n"

            "If no duration is given, defaults to **14 days**."

        ),

        "perms":   "Moderator / Trial Moderator / Admin",

    },

    "unmute": {

        "usage":   ".unmute @user",

        "example": ".unmute @John",

        "note":    "Manually removes the mute before the timer expires.",

        "perms":   "Moderator / Trial Moderator / Admin",

    },

    "warn": {

        "usage":   ".warn @user [reason]",

        "example": ".warn @John repeated rule violations",

        "note":    "Adds a warning to the user's record. The user is DM'd.",

        "perms":   "Moderator / Trial Moderator / Admin",

    },

    "unwarn": {

        "usage":   ".unwarn @user <warn_number>",

        "example": ".unwarn @John 2",

        "note":    "Removes a specific warning by number. Use `.warnings @user` to see warn numbers.",

        "perms":   "Moderator / Trial Moderator / Admin",

    },

    "warnings": {

        "usage":   ".warnings @user",

        "example": ".warnings @John",

        "note":    "Displays all active warnings on a user's record.",

        "perms":   "Moderator / Trial Moderator / Admin",

    },

    "modlogs": {

        "usage":   ".modlogs @user",

        "example": ".modlogs @John",

        "note":    "Shows the full moderation history for a user (last 20 cases).",

        "perms":   "Moderator / Trial Moderator / Admin",

    },

    "case": {

        "usage":   ".case <case_number>",

        "example": ".case 42",

        "note":    "Shows full details for a single moderation case by its case number.",

        "perms":   "Moderator / Trial Moderator / Admin",

    },

    "jail": {

        "usage":   ".jail @user [reason]",

        "example": ".jail @John pending investigation",

        "note":    "Jails the user — hides all channels. Persists across rejoins.",

        "perms":   "Moderator / Admin",

    },

    "unjail": {

        "usage":   ".unjail @user",

        "example": ".unjail @John",

        "note":    "Releases the user from jail and restores their channel access.",

        "perms":   "Moderator / Admin",

    },

    "jreq": {

        "usage":   ".jreq @user [reason]",

        "example": ".jreq @John suspicious activity, needs review",

        "note":    "Submits a jail request for a full mod to review and action. Attach proof or submit directly — proof carries over automatically when a mod jails from the request.",

        "perms":   "Moderator / Trial Moderator / Admin",

    },

    "purge": {

        "usage":   ".purge <amount>",

        "example": ".purge 25",

        "note":    "Bulk-deletes messages in the current channel. Max 100 at a time.",

        "perms":   "Moderator / Admin",

    },

    "purgeuser": {

        "usage":   ".purgeuser @user [amount]",

        "example": ".purgeuser @John 50",

        "note":    "Deletes only that member's messages in the current channel (default 100, max 200). Use this to clean up one person without touching everyone else's messages.",

        "perms":   "Moderator / Admin",

    },

    "addcmd": {

        "usage":   ".addcmd <name> <response>",

        "example": ".addcmd rules Please read the #rules channel!",

        "note":    "Creates a custom command. Staff and members can trigger it with `.name`.",

        "perms":   "Moderator / Admin",

    },

    "delcmd": {

        "usage":   ".delcmd <name>",

        "example": ".delcmd rules",

        "note":    "Permanently deletes a custom command.",

        "perms":   "Moderator / Admin",

    },

    "w": {

        "usage":   ".w [@user|user_id]",

        "example": ".w @John",

        "note":    "Shows detailed information for the tagged user. If no user is given, shows your own info.",

        "perms":   "Any staff role",

    },

    "setup": {

        "usage":   ".setup",

        "example": ".setup",

        "note":    "Creates the mod-logs channel, auto-logs channel, Muted role, and Jailed role if they don't already exist.",

        "perms":   "Server Owner / Bot Owner / roles listed in CMD_ROLES['setup']",

    },

    "rappeal": {

        "usage":   ".rappeal @user [reason]",

        "example": ".rappeal @John appeal rejected after review",

        "note":    "Assigns the configured appeal-rejection role to the member.",

        "perms":   "Moderator / Admin",

    },

    "dwc": {

        "usage":   ".dwc @user [reason]",

        "example": ".dwc @John won the weekly contest",

        "note":    "Assigns the configured DWC special role to the member.",

        "perms":   "Moderator / Admin",

    },

    "rdwc": {

        "usage":   ".rdwc @user [reason]",

        "example": ".rdwc @John appeal accepted",

        "note":    "Removes the configured DWC special role from the member.",

        "perms":   "Moderator / Admin",

    },

    "ping": {

        "usage":   ".ping",

        "example": ".ping",

        "note":    "Shows the bot's current websocket latency.",

        "perms":   "Moderator / Trial Moderator / Admin",

    },

    "📸  Unappeal Roles": {

        "emoji": "📸",

        "description": "Role assigned by appeal rejection commands",

        "commands": ["rappeal"],

    },

}

def syntax_embed(cmd_name: str) -> discord.Embed:

    info = COMMAND_SYNTAX.get(cmd_name)

    if not info:

        return error_embed(f"No syntax info available for `{PREFIX}{cmd_name}`.")

    embed = discord.Embed(

        title=f"📖  `{PREFIX}{cmd_name}` — Command Usage",

        color=discord.Color.blurple(),

        timestamp=datetime.now(timezone.utc)

    )

    embed.add_field(name="Syntax",      value=f"```{info['usage']}```",   inline=False)

    embed.add_field(name="Example",     value=f"```{info['example']}```", inline=False)

    embed.add_field(name="ℹ️ Info",     value=info["note"],               inline=False)

    embed.add_field(name="🔒 Requires", value=info["perms"],              inline=False)

    embed.set_footer(text="[ ] = optional  •  < > = required  •  @user = mention or ID")

    return embed

# ══════════════════════════════════════════════════════════════════════════════

#  DURATION PARSER

# ══════════════════════════════════════════════════════════════════════════════

DURATION_TOKEN_RE = re.compile(

    r"(\d+)\s*"

    r"(seconds?|secs?|sec|minutes?|mins?|min|hours?|hrs?|hr|days?|day|weeks?|week|[smhdw])"

    r"(?!\w)",

    re.IGNORECASE,

)

DURATION_MULTIPLIERS: dict[str, int] = {}

for _aliases, _mult in [

    (["s", "sec", "secs", "second", "seconds"],          1),

    (["m", "min", "mins", "minute", "minutes"],          60),

    (["h", "hr", "hrs", "hour", "hours"],                3600),

    (["d", "day", "days"],                               86400),

    (["w", "week", "weeks"],                             604800),

]:

    for _a in _aliases:

        DURATION_MULTIPLIERS[_a.lower()] = _mult

def _read_duration(text: str):

    pos     = 0

    total   = 0

    matched = False

    while True:

        while pos < len(text) and text[pos].isspace():

            pos += 1

        m = DURATION_TOKEN_RE.match(text, pos)

        if not m:

            break

        matched = True

        unit_key = m.group(2).lower()

        total   += int(m.group(1)) * DURATION_MULTIPLIERS[unit_key]

        pos      = m.end()

    return (total, pos) if (matched and total > 0) else (None, 0)

def parse_duration(text: str) -> int | None:

    text = text.strip()

    if not text:

        return None

    total, pos = _read_duration(text)

    if total is None:

        return None

    return total if text[pos:].strip() == "" else None

def split_duration_and_reason(args: str):

    args  = args.strip()

    total, pos = _read_duration(args)

    if total is None:

        return None, args

    return total, args[pos:].strip()

def fmt_duration(seconds: int) -> str:

    d, rem = divmod(seconds, 86400)

    h, rem = divmod(rem, 3600)

    m, s   = divmod(rem, 60)

    parts = []

    if d: parts.append(f"{d}d")

    if h: parts.append(f"{h}h")

    if m: parts.append(f"{m}m")

    if s: parts.append(f"{s}s")

    return " ".join(parts) or "0s"

# ══════════════════════════════════════════════════════════════════════════════

#  DM EMBED

# ══════════════════════════════════════════════════════════════════════════════

def dm_embed(action: str, guild_name: str, reason: str, case_id: int,

             moderator=None, extra: str = "") -> discord.Embed:

    descriptions = {

        "Ban":    "You have been **banned** from",

        "Kick":   "You have been **kicked** from",

        "Mute":   "You have been **muted** in",

        "Unmute": "You have been **unmuted** in",

        "Warn":   "You have received a **warning** in",

        "Unwarn": "A warning has been **removed** from your record in",

        "Jail":   "You have been **jailed** in",

        "Unjail": "You have been **released from jail** in",

    }

    desc = descriptions.get(action, f"A moderation action (**{action}**) was taken in")

    embed = discord.Embed(

        title=f"{ACTION_ICONS.get(action, '📋')} Moderation Notice",

        description=f"{desc} **{guild_name}**.",

        color=ACTION_COLORS.get(action, discord.Color.blurple()),

        timestamp=datetime.now(timezone.utc)

    )

    embed.add_field(name="Reason", value=reason, inline=False)

    if case_id:

        embed.add_field(name="Case", value=f"#{case_id}", inline=True)

    if moderator:

        embed.add_field(name="Moderator", value=str(moderator), inline=True)

    if extra:

        embed.add_field(name="Note", value=extra, inline=False)

    embed.set_footer(text="If you believe this was a mistake, please contact a staff member.")

    return embed

async def dm_member(member, action: str, guild_name: str, reason: str,

                    case_id: int, moderator=None, extra: str = "") -> bool:

    try:

        await member.send(embed=dm_embed(action, guild_name, reason, case_id,

                                         moderator=moderator, extra=extra))

        return True

    except (discord.Forbidden, discord.HTTPException):

        return False

# ══════════════════════════════════════════════════════════════════════════════

#  DATA LAYER

# ══════════════════════════════════════════════════════════════════════════════

def _default_data() -> dict:

    return {

        "cases": {}, "warns": {}, "custom_commands": {},

        "next_case": 1, "persistent_roles": {}, "mute_timers": {},

        "mod_actions": {}, "afk": {}, "cmd_roles": {}, "giveaways": {},

        "setup_done": {}, "channel_whitelist": {}, "message_stats": {},

        "jail_requests": {}, "jail_request_channel": {}, "next_jreq": 1,

        "appeal_roles": {}, "dwc_roles": {}

    }

def _decrypt_bytes(raw: bytes) -> dict:
    """Decrypts raw bytes pulled from the database into a dict. Transparently
    handles the case where the stored blob is still old, unencrypted plaintext
    JSON (e.g. the very first run after enabling encryption) by detecting
    that and parsing it directly -- it will be re-saved encrypted on the
    next save_data() call."""
    try:
        plaintext = _fernet.decrypt(raw)
        return json.loads(plaintext.decode("utf-8"))
    except InvalidToken:
        # Not Fernet-encrypted -- assume legacy plaintext JSON and migrate.
        data = json.loads(raw.decode("utf-8"))
        print(f"[load_data] NOTE: stored data was unencrypted; it will be encrypted on next save.")
        return data

# ══════════════════════════════════════════════════════════════════════════════
#  SQLITE STORAGE LAYER
#
#  Replaces the old plain bot_data.json file with a SQLite database
#  (bot_data.db). The bot's internal data shape is unchanged -- it is still
#  one big dict (data["cases"], data["warns"], etc.) -- but it is now stored
#  as a single encrypted blob inside a SQLite table instead of a raw JSON
#  file. This gives proper atomic transactions and a real database file
#  while keeping every existing function in this bot (which all read/write
#  via load_data()/save_data()) working unchanged.
#
#  A "legacy_json" row is also kept holding the most recent N versions'
#  worth of history is NOT kept -- SQLite WAL + the OS handle crash safety,
#  and save_data() still does an atomic write via a transaction.
# ══════════════════════════════════════════════════════════════════════════════

_DB_ROW_KEY = "bot_data"

def _get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DATA_FILE)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS kv_store ("
        "  key TEXT PRIMARY KEY,"
        "  value BLOB NOT NULL,"
        "  updated_at TEXT NOT NULL"
        ")"
    )
    conn.commit()
    return conn

def load_data() -> dict:
    if not os.path.exists(DATA_FILE):
        return _default_data()

    try:
        conn = _get_db_connection()
        row  = conn.execute(
            "SELECT value FROM kv_store WHERE key = ?", (_DB_ROW_KEY,)
        ).fetchone()
        conn.close()

        if row is None:
            return _default_data()

        data = _decrypt_bytes(row[0])
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError, sqlite3.Error) as e:
        print(f"[load_data] WARNING: {DATA_FILE} could not be read/parsed ({e}); using defaults for this call.")
        data = _default_data()
        data["_load_failed"] = True
        return data

    for key in ("persistent_roles", "mute_timers", "mod_actions", "afk", "cmd_roles", "giveaways", "setup_done", "channel_whitelist", "message_stats", "jail_requests", "jail_request_channel", "appeal_roles", "dwc_roles"):
        if key not in data:
            data[key] = {}
    if "next_jreq" not in data:
        data["next_jreq"] = 1
    return data

def save_data(data: dict):
    # Safety net: refuse to persist data that came from a failed load_data()
    # read (i.e. defaults), so a transient read glitch can never silently
    # wipe real data in the database. This flag travels WITH the dict that
    # was loaded, instead of living in a global that other coroutines could
    # stomp on between this data's load_data() call and this save_data() call.
    had_existing_row = False
    if data.pop("_load_failed", False):
        try:
            conn = _get_db_connection()
            row  = conn.execute(
                "SELECT length(value) FROM kv_store WHERE key = ?", (_DB_ROW_KEY,)
            ).fetchone()
            conn.close()
            had_existing_row = row is not None and row[0] > 200
        except sqlite3.Error:
            had_existing_row = False
        if had_existing_row:
            print(f"[save_data] REFUSED: this data dict came from a failed read of {DATA_FILE}; refusing to save possibly-default data. Skipping save to avoid data loss.")
            return

    plaintext  = json.dumps(data).encode("utf-8")
    ciphertext = _fernet.encrypt(plaintext)

    # Single transaction = atomic write. SQLite (with WAL mode) guarantees
    # this either fully commits or fully rolls back -- no half-written
    # files, no manual temp-file/os.replace dance, no separate .bak copy
    # needed since the previous committed row is never touched until this
    # transaction commits.
    last_err = None
    for attempt in range(5):
        try:
            conn = _get_db_connection()
            conn.execute(
                "INSERT INTO kv_store (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (_DB_ROW_KEY, ciphertext, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
            conn.close()
            last_err = None
            break
        except sqlite3.OperationalError as e:
            # Database locked by another process/thread -- retry briefly.
            last_err = e
            time.sleep(0.05 * (attempt + 1))
    if last_err is not None:
        raise last_err

def get_appeal_role_id(guild_id: int) -> int | None:

    data = load_data()

    role_id = data.get("appeal_roles", {}).get(str(guild_id))

    return int(role_id) if role_id else None

def set_appeal_role_id(guild_id: int, role_id: int | None):

    data = load_data()

    data.setdefault("appeal_roles", {})

    if role_id is None:

        data["appeal_roles"].pop(str(guild_id), None)

    else:

        data["appeal_roles"][str(guild_id)] = role_id

    save_data(data)

def get_dwc_role_id(guild_id: int) -> int | None:

    data = load_data()

    role_id = data.get("dwc_roles", {}).get(str(guild_id))

    return int(role_id) if role_id else None

def set_dwc_role_id(guild_id: int, role_id: int | None):

    data = load_data()

    data.setdefault("dwc_roles", {})

    if role_id is None:

        data["dwc_roles"].pop(str(guild_id), None)

    else:

        data["dwc_roles"][str(guild_id)] = role_id

    save_data(data)

def next_case_id(data: dict) -> int:

    cid = data.get("next_case", 1)

    data["next_case"] = cid + 1

    return cid

def add_case(data, guild_id, action, moderator, target, reason) -> int:

    cid = next_case_id(data)

    gid = str(guild_id)

    if gid not in data["cases"]:

        data["cases"][gid] = []

    data["cases"][gid].append({

        "case": cid, "action": action,

        "mod_id": moderator.id, "mod_tag": str(moderator),

        "target_id": target.id, "target_tag": str(target),

        "reason": reason, "timestamp": datetime.now(timezone.utc).isoformat()

    })

    save_data(data)

    track_mod_action(data, guild_id, moderator.id, str(moderator),

                     action, str(target), target.id, reason, cid)

    return cid

def track_mod_action(data, guild_id, mod_id, mod_tag, action, target_tag, target_id, reason, case_id):

    key = f"{guild_id}:{mod_id}"

    if key not in data["mod_actions"]:

        data["mod_actions"][key] = {"mod_tag": mod_tag, "actions": []}

    data["mod_actions"][key]["mod_tag"] = mod_tag

    data["mod_actions"][key]["actions"].append({

        "case": case_id, "action": action,

        "target_tag": target_tag, "target_id": target_id,

        "reason": reason, "timestamp": datetime.now(timezone.utc).isoformat()

    })

    save_data(data)

def get_mod_actions(data, guild_id, mod_id):

    key = f"{guild_id}:{mod_id}"

    return data["mod_actions"].get(key, {})

def get_user_cases(data, guild_id, user_id):

    return [c for c in data["cases"].get(str(guild_id), []) if c["target_id"] == user_id]

def get_case_by_id(data, guild_id, case_id: int):

    for c in data["cases"].get(str(guild_id), []):

        if c["case"] == case_id:

            return c

    return None

def add_warn(data, guild_id, user_id, mod, reason) -> int:

    key = f"{guild_id}:{user_id}"

    if key not in data["warns"]:

        data["warns"][key] = []

    n = len(data["warns"][key]) + 1

    data["warns"][key].append({

        "number": n, "mod_id": mod.id, "mod_tag": str(mod),

        "reason": reason, "timestamp": datetime.now(timezone.utc).isoformat()

    })

    save_data(data)

    return n

def get_warns(data, guild_id, user_id):

    return data["warns"].get(f"{guild_id}:{user_id}", [])

def remove_warn(data, guild_id, user_id, index) -> bool:

    key = f"{guild_id}:{user_id}"

    warns = data["warns"].get(key, [])

    if index < 1 or index > len(warns):

        return False

    warns.pop(index - 1)

    for i, w in enumerate(warns):

        w["number"] = i + 1

    data["warns"][key] = warns

    save_data(data)

    return True

def add_persistent_role(data, guild_id, user_id, role_name):

    key = f"{guild_id}:{user_id}"

    if key not in data["persistent_roles"]:

        data["persistent_roles"][key] = []

    if role_name not in data["persistent_roles"][key]:

        data["persistent_roles"][key].append(role_name)

    save_data(data)

def remove_persistent_role(data, guild_id, user_id, role_name):

    key = f"{guild_id}:{user_id}"

    roles = data["persistent_roles"].get(key, [])

    if role_name in roles:

        roles.remove(role_name)

        data["persistent_roles"][key] = roles

        save_data(data)

def get_persistent_roles(data, guild_id, user_id):

    return data["persistent_roles"].get(f"{guild_id}:{user_id}", [])

def set_mute_timer(data, guild_id, user_id, expires_at: datetime):

    key = f"{guild_id}:{user_id}"

    data["mute_timers"][key] = expires_at.isoformat()

    save_data(data)

def clear_mute_timer(data, guild_id, user_id):

    key = f"{guild_id}:{user_id}"

    data["mute_timers"].pop(key, None)

    save_data(data)

def get_mute_expiry(data, guild_id, user_id) -> datetime | None:

    key = f"{guild_id}:{user_id}"

    val = data["mute_timers"].get(key)

    if val:

        return datetime.fromisoformat(val)

    return None

def load_guild_cmd_roles(guild_id: int) -> dict:

    data = load_data()

    saved = data["cmd_roles"].get(str(guild_id))

    if saved:

        return saved

    return dict(CMD_ROLES_DEFAULT)

def save_guild_cmd_roles(guild_id: int, cmd_roles: dict):

    data = load_data()

    data["cmd_roles"][str(guild_id)] = cmd_roles

    save_data(data)


def _message_stat_key(guild_id: int, user_id: int) -> str:
    return f"{guild_id}:{user_id}"


def _ensure_message_stat_record(data: dict, guild_id: int, user_id: int) -> dict:
    stats = data.setdefault("message_stats", {})
    key = _message_stat_key(guild_id, user_id)
    rec = stats.setdefault(key, {
        "today": 0,
        "week": 0,
        "month": 0,
        "all_time": 0,
        "last_day": "",
        "last_week": "",
        "last_month": "",
    })
    return rec


def _message_time_bucket(now: datetime) -> tuple[str, str, str]:
    return (
        now.strftime("%Y-%m-%d"),
        now.strftime("%Y-%W"),
        now.strftime("%Y-%m"),
    )


def increment_message_stat(data: dict, guild_id: int, user_id: int, created_at: datetime | None = None) -> dict:
    now = created_at or datetime.now(timezone.utc)
    day_key, week_key, month_key = _message_time_bucket(now)
    rec = _ensure_message_stat_record(data, guild_id, user_id)

    if rec.get("last_day") != day_key:
        rec["today"] = 0
        rec["last_day"] = day_key
    if rec.get("last_week") != week_key:
        rec["week"] = 0
        rec["last_week"] = week_key
    if rec.get("last_month") != month_key:
        rec["month"] = 0
        rec["last_month"] = month_key

    rec["today"] += 1
    rec["week"] += 1
    rec["month"] += 1
    rec["all_time"] += 1
    return rec


def get_message_stats(data: dict, guild_id: int, user_id: int) -> dict:
    rec = _ensure_message_stat_record(data, guild_id, user_id)
    return {
        "today": rec.get("today", 0),
        "week": rec.get("week", 0),
        "month": rec.get("month", 0),
        "all_time": rec.get("all_time", 0),
    }

    _guild_cmd_roles[guild_id] = cmd_roles

# ══════════════════════════════════════════════════════════════════════════════

#  GIVEAWAY SYSTEM — GiveawayBoat-style UI

# ══════════════════════════════════════════════════════════════════════════════

GIVEAWAY_COLOR         = 0xFF73FA

GIVEAWAY_ENDED_COLOR   = 0x747F8D

GIVEAWAY_ENTER_EMOJI   = "🎉"

GIVEAWAY_CONFETTI      = ["🎊", "🎉", "✨", "🥳", "🎁"]

def get_guild_giveaways(data: dict, guild_id: int) -> dict:

    return data["giveaways"].setdefault(str(guild_id), {})

def get_giveaway(data: dict, guild_id: int, message_id: int) -> dict | None:

    return get_guild_giveaways(data, guild_id).get(str(message_id))

def add_giveaway(data: dict, guild_id: int, message_id: int, giveaway: dict):

    get_guild_giveaways(data, guild_id)[str(message_id)] = giveaway

    save_data(data)

def update_giveaway(data: dict, guild_id: int, message_id: int, **fields):

    g = get_giveaway(data, guild_id, message_id)

    if g is None:

        return

    g.update(fields)

    save_data(data)

def remove_giveaway(data: dict, guild_id: int, message_id: int):

    get_guild_giveaways(data, guild_id).pop(str(message_id), None)

    save_data(data)

def _fmt_bonus_roles(bonus_roles: list) -> str:

    if not bonus_roles:

        return ""

    lines = []

    for br in bonus_roles:

        lines.append(f"<@&{br['role_id']}> — **+{br['entries']}** entries")

    return "\n".join(lines)

def giveaway_embed(g: dict, entries_count: int, ended: bool = False,

                   winners_text: str | None = None,

                   custom_color: int | None = None,

                   custom_image: str | None = None,

                   custom_thumbnail: str | None = None) -> discord.Embed:

    end_ts       = int(datetime.fromisoformat(g["end_time"]).timestamp())

    prize        = g["prize"]

    host_id      = g.get("host_id")

    winners      = g["winners"]

    active_color = custom_color or g.get("color", GIVEAWAY_COLOR)

    ended_color  = g.get("end_color", GIVEAWAY_ENDED_COLOR)

    if ended:

        embed = discord.Embed(

            title=prize,

            color=ended_color,

            timestamp=datetime.now(timezone.utc),

        )

        result_text = winners_text or "No valid entries — no winner could be picked."

        embed.description = (

            f"{result_text}\n\n"

            f"Ended: <t:{end_ts}:R>"

        )

    else:

        embed = discord.Embed(

            title=prize,

            color=active_color,

            timestamp=datetime.now(timezone.utc),

        )

        embed.description = (

            f"Click button to enter!\n"

            f"Winners: {winners}\n"

            f"Duration: {g.get('duration', '')}\n"

            f"Ends: <t:{end_ts}:R> (`Timer`)"

        )

    embed.add_field(name="Requirements", value=_format_giveaway_requirements(g), inline=False)

    embed.set_footer(text=_format_giveaway_footer(end_ts))

    img = custom_image or g.get("image")

    thm = custom_thumbnail or g.get("thumbnail")

    if img:

        embed.set_image(url=img)

    if thm:

        embed.set_thumbnail(url=thm)

    return embed


def _get_participant_count(g: dict) -> int:
    """Number of unique participants (always 1 per person regardless of bonus entries).
    This is what shows on the button label."""
    entries = g.get("entries", {})
    if isinstance(entries, dict):
        return len(entries)
    return len(entries)


def _get_entry_count(g: dict, guild) -> int:
    """Weighted total for display in the embed's Entries field.
    Each person counts as 1 regardless of bonus — consistent with button count."""
    entries = g.get("entries", {})
    if isinstance(entries, dict):
        return len(entries)
    return len(entries)

def _format_giveaway_footer(end_ts: int) -> str:
    dt = datetime.fromtimestamp(end_ts, timezone.utc).astimezone()
    return f"Today at {dt.strftime('%I:%M %p').lstrip('0')}"


def _member_missing_giveaway_roles(g: dict, member: discord.Member) -> list[str]:
    required_role_ids = {g.get("required_role_id")}
    required_role_ids.update(r.get("role_id") for r in g.get("required_roles", []) if isinstance(r, dict))
    required_role_ids.discard(None)

    if not required_role_ids:
        return []

    member_role_ids = {r.id for r in member.roles}
    missing = [] if any(rid in member_role_ids for rid in required_role_ids) else list(required_role_ids)

    return [f"<@&{rid}>" for rid in missing]


def _gather_requirement_blocker(g: dict, member: discord.Member) -> str | None:
    bypass_id = g.get("bypass_role_id")
    if bypass_id and any(r.id == bypass_id for r in member.roles):
        return None

    missing_roles = _member_missing_giveaway_roles(g, member)
    if missing_roles:
        return f"You need one of the required roles to enter this giveaway: {', '.join(missing_roles)}."

    tracked_thresholds = {
        "required_level": g.get("required_level"),
        "req_daily_messages": g.get("req_daily_messages"),
        "req_weekly_messages": g.get("req_weekly_messages"),
        "req_monthly_messages": g.get("req_monthly_messages"),
        "req_total_messages": g.get("req_total_messages"),
    }
    blacklisted = {r.get("role_id") for r in g.get("blacklisted_roles", []) if isinstance(r, dict)}
    if blacklisted and any(r.id in blacklisted for r in member.roles):
        return "You have a blacklisted role and can't enter this giveaway."

    stats = get_message_stats(load_data(), member.guild.id, member.id)
    message_requirements = [
        ("req_daily_messages", "today", "today"),
        ("req_weekly_messages", "this week", "week"),
        ("req_monthly_messages", "this month", "month"),
        ("req_total_messages", "all time", "all_time"),
    ]
    for req_key, label, stat_key in message_requirements:
        required = g.get(req_key)
        if required is not None and stats.get(stat_key, 0) < required:
            return f"You need at least **{required}** messages {label} to enter this giveaway. You currently have **{stats.get(stat_key, 0)}**."

    if g.get("required_level") is not None:
        return "Level requirements are configured for this giveaway, but this bot file does not yet track user levels."

    return None


def _format_giveaway_requirements(g: dict) -> str:
    lines = []
    if g.get("required_role_id"):
        lines.append(f"Required Role: <@&{g['required_role_id']}>")
    required_roles = [r.get("role_id") for r in g.get("required_roles", []) if isinstance(r, dict) and r.get("role_id")]
    if required_roles:
        lines.append("Required Roles: " + ", ".join(f"<@&{rid}>" for rid in required_roles))
    if g.get("required_level") is not None:
        lines.append(f"Required Level: **{g['required_level']}**")
    if g.get("req_daily_messages") is not None:
        lines.append(f"Required Daily Messages: **{g['req_daily_messages']}**")
    if g.get("req_weekly_messages") is not None:
        lines.append(f"Required Weekly Messages: **{g['req_weekly_messages']}**")
    if g.get("req_monthly_messages") is not None:
        lines.append(f"Required Monthly Messages: **{g['req_monthly_messages']}**")
    if g.get("req_total_messages") is not None:
        lines.append(f"Required Total Messages: **{g['req_total_messages']}**")
    if g.get("bypass_role_id"):
        lines.append(f"Bypass Role: <@&{g['bypass_role_id']}>")
    blacklisted_roles = [r.get("role_id") for r in g.get("blacklisted_roles", []) if isinstance(r, dict) and r.get("role_id")]
    if blacklisted_roles:
        lines.append("Blacklisted Roles: " + ", ".join(f"<@&{rid}>" for rid in blacklisted_roles))
    return "\n".join(lines) if lines else "None"


def build_messages_embed(member: discord.Member, stats: dict) -> discord.Embed:
    embed = discord.Embed(
        title=f"📨 Message Stats — {member}",
        description=(
            f"Message counts for **{member.display_name}**.\n"
            f"Tracked across today, this week, this month, and all time."
        ),
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Today", value=str(stats.get("today", 0)), inline=True)
    embed.add_field(name="This Week", value=str(stats.get("week", 0)), inline=True)
    embed.add_field(name="This Month", value=str(stats.get("month", 0)), inline=True)
    embed.add_field(name="All Time", value=str(stats.get("all_time", 0)), inline=True)
    embed.set_footer(text=f"Requested by {member}", icon_url=member.display_avatar.url)
    return embed


async def _url_points_to_image(url: str) -> bool:
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.head(url, allow_redirects=True) as resp:
                content_type = resp.headers.get("Content-Type", "").lower()
                if resp.status == 200 and content_type.startswith("image/"):
                    return True
            async with session.get(url, allow_redirects=True) as resp:
                content_type = resp.headers.get("Content-Type", "").lower()
                return resp.status == 200 and content_type.startswith("image/")
    except Exception:
        return False


def _build_weighted_pool(g: dict, guild) -> list:
    """Build a weighted pool of user IDs for winner selection.
    Users with bonus entries appear multiple times in the pool."""
    entries = g.get("entries", {})
    pool = []
    if isinstance(entries, dict):
        for uid_str, count in entries.items():
            pool.extend([int(uid_str)] * count)
    else:
        pool = list(entries)
    return pool


def _calculate_user_weighted_entries(g: dict, member: discord.Member) -> int:
    """Calculate the WEIGHTED entry count (base 1 + bonus roles).
    Stored in DB for fair draws, but NOT shown as the button count."""
    base = 1
    bonus_roles = g.get("bonus_roles", [])
    extra = 0
    for br in bonus_roles:
        role = member.guild.get_role(br["role_id"])
        if role and role in member.roles:
            extra += br["entries"]
    return base + extra


class GiveawayView(discord.ui.View):

    def __init__(self, message_id: int, entry_count: int = 0):

        super().__init__(timeout=None)

        self.message_id   = message_id

        self._entry_count = entry_count

        self._rebuild_button()

    def _rebuild_button(self):

        self.clear_items()

        btn = discord.ui.Button(

            label=f"{self._entry_count}",

            style=discord.ButtonStyle.blurple,

            emoji=GIVEAWAY_ENTER_EMOJI,

            custom_id=f"giveaway_enter:{self.message_id}",

        )

        btn.callback = self.enter_callback

        self.add_item(btn)

        participants_btn = discord.ui.Button(

            label="Participants",

            style=discord.ButtonStyle.secondary,

            emoji="👥",

            custom_id=f"giveaway_participants:{self.message_id}",

        )

        participants_btn.callback = self.participants_callback

        self.add_item(participants_btn)

    async def participants_callback(self, interaction: discord.Interaction):

        data = load_data()

        g = get_giveaway(data, interaction.guild.id, self.message_id)

        if not g or g.get("ended"):

            return await interaction.response.send_message(

                embed=error_embed("This giveaway has already ended."), ephemeral=True

            )

        entries = g.get("entries", {})
        if not isinstance(entries, dict):
            entries = {str(uid): 1 for uid in entries}

        lines = []
        guild = interaction.guild
        for idx, (uid_str, count) in enumerate(entries.items(), start=1):
            member = guild.get_member(int(uid_str)) if guild else None
            label = member.mention if member else f"<@{uid_str}>"
            suffix = "entry" if count == 1 else "entries"
            lines.append(f"{idx}. {label} ({count} {suffix})")

        embed = discord.Embed(
            title="🎉 Giveaway Participants",
            description=(
                f"These are the members that have participated in the giveaway of **{g['prize']}**:\n\n"
                + ("\n".join(lines) if lines else "*No participants yet.*")
                + f"\n\nTotal Participants: **{len(entries)}**"
            ),
            color=GIVEAWAY_COLOR,
        )

        view = discord.ui.View(timeout=120)
        back_btn = discord.ui.Button(label="<", style=discord.ButtonStyle.secondary, disabled=True)
        page_btn = discord.ui.Button(label="Go To Page", style=discord.ButtonStyle.secondary, disabled=True)
        next_btn = discord.ui.Button(label=">", style=discord.ButtonStyle.secondary, disabled=True)
        tags_btn = discord.ui.Button(label="Show User Tags", style=discord.ButtonStyle.primary)
        remove_btn = discord.ui.Button(label="Remove A Participant", style=discord.ButtonStyle.danger)

        async def _noop(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)

        tags_btn.callback = _noop
        remove_btn.callback = _noop
        view.add_item(back_btn)
        view.add_item(page_btn)
        view.add_item(next_btn)
        view.add_item(tags_btn)
        view.add_item(remove_btn)

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    async def enter_callback(self, interaction: discord.Interaction):

        data = load_data()

        g    = get_giveaway(data, interaction.guild.id, self.message_id)

        if not g or g.get("ended"):

            return await interaction.response.send_message(

                embed=error_embed("This giveaway has already ended."), ephemeral=True

            )

        blocker = _gather_requirement_blocker(g, interaction.user)
        if blocker:
            return await interaction.response.send_message(embed=error_embed(blocker), ephemeral=True)

        # Required role check
        if g.get("required_role_id"):

            role = interaction.guild.get_role(g["required_role_id"])

            if role and role not in interaction.user.roles:

                return await interaction.response.send_message(

                    embed=error_embed(

                        f"You need the {role.mention} role to enter this giveaway."

                    ), ephemeral=True

                )

        # Migrate legacy list entries to dict
        if isinstance(g.get("entries"), list):

            old = g["entries"]

            g["entries"] = {str(uid): 1 for uid in old}

        entries = g.setdefault("entries", {})

        uid_str = str(interaction.user.id)

        if uid_str in entries:

            # Toggle leave
            del entries[uid_str]

            joined = False

            update_giveaway(data, interaction.guild.id, self.message_id, entries=entries)

        else:

            # Calculate weighted entries for DB (used in winner draw)
            member       = interaction.guild.get_member(interaction.user.id) or interaction.user

            weighted     = _calculate_user_weighted_entries(g, member)

            entries[uid_str] = weighted

            joined = True

            update_giveaway(data, interaction.guild.id, self.message_id, entries=entries)

        # Button label = unique participant count (always +1/-1 per person)
        unique_count      = len(entries)
        self._entry_count = unique_count
        self._rebuild_button()

        try:

            await interaction.response.edit_message(view=self)

        except discord.NotFound:

            pass

        if joined:

            user_weighted = entries[uid_str]

            if user_weighted > 1:

                bonus = user_weighted - 1

                msg = (
                    f"You're in! You have **{user_weighted} entries** "
                    f"(+{bonus} from bonus roles). Good luck 🍀"
                )

            else:

                msg = "You're in! Good luck 🍀"

            await interaction.followup.send(

                embed=discord.Embed(

                    description=f"{GIVEAWAY_ENTER_EMOJI} {msg}",

                    color=GIVEAWAY_COLOR,

                ),

                ephemeral=True

            )

        else:

            await interaction.followup.send(

                embed=discord.Embed(

                    description="You've left the giveaway.",

                    color=GIVEAWAY_ENDED_COLOR,

                ),

                ephemeral=True

            )

def pick_winners(g: dict, count: int, guild: discord.Guild,

                 exclude_ids: set = None) -> list:

    exclude_ids = exclude_ids or set()

    pool = _build_weighted_pool(g, guild)

    pool = [uid for uid in pool if uid not in exclude_ids]

    random.shuffle(pool)

    seen    = set()

    winners = []

    for uid in pool:

        if uid in seen:

            continue

        seen.add(uid)

        member = guild.get_member(uid)

        if member:

            winners.append(member)

        if len(winners) >= count:

            break

    return winners

async def end_giveaway(guild: discord.Guild, message_id: int, reroll: bool = False) -> list:

    data = load_data()

    g    = get_giveaway(data, guild.id, message_id)

    if not g:

        return []

    if isinstance(g.get("entries"), list):

        g["entries"] = {str(uid): 1 for uid in g["entries"]}

        update_giveaway(data, guild.id, message_id, entries=g["entries"])

    channel = guild.get_channel(g["channel_id"])

    exclude = set(g.get("winner_ids", [])) if reroll else set()

    winners = pick_winners(g, g["winners"], guild, exclude_ids=exclude)

    if reroll:

        update_giveaway(data, guild.id, message_id, winner_ids=[w.id for w in winners])

    else:

        update_giveaway(data, guild.id, message_id, ended=True, winner_ids=[w.id for w in winners])

    g     = get_giveaway(data, guild.id, message_id)

    total = _get_entry_count(g, guild)

    active_color = g.get("color", GIVEAWAY_COLOR)

    ended_color  = g.get("end_color", GIVEAWAY_ENDED_COLOR)

    if winners:

        mention_str  = ", ".join(w.mention for w in winners)

        winners_text = f"**Winner{'s' if len(winners) > 1 else ''}:** {mention_str}"

    else:

        mention_str  = None

        winners_text = None

    if channel:

        try:

            msg   = await channel.fetch_message(message_id)

            embed = giveaway_embed({**g, "message_id": message_id}, total,

                                   ended=True, winners_text=winners_text)

            done_view = discord.ui.View()

            done_btn  = discord.ui.Button(

                label=f"Giveaway Ended  ·  {total} entries",

                style=discord.ButtonStyle.grey,

                emoji="🎉",

                disabled=True,

            )

            done_view.add_item(done_btn)

            await msg.edit(embed=embed, view=done_view)

        except discord.NotFound:

            pass

        if winners:

            announce_color = active_color if not reroll else 0x5865F2

            confetti       = random.choice(GIVEAWAY_CONFETTI)

            announce_title = f"{'🔁 Giveaway Rerolled' if reroll else f'{confetti} Giveaway Ended'}"

            announce_embed = discord.Embed(

                title=announce_title,

                description=(

                    f"Congratulations {mention_str}!\n"

                    f"You won **{g['prize']}**!\n\n"

                    f"*Hosted by <@{g['host_id']}>*"

                ),

                color=announce_color,

                timestamp=datetime.now(timezone.utc),

            )

            announce_embed.set_footer(text=f"Giveaway ID: {message_id}")

            await channel.send(content=mention_str, embed=announce_embed)

            dm_msg = g.get("winners_dm_message")

            if dm_msg:

                for w in winners:

                    try:

                        await w.send(dm_msg.replace("{prize}", g["prize"]).replace("{server}", guild.name))

                    except (discord.Forbidden, discord.HTTPException):

                        pass

            winners_role_id = g.get("winners_role_id")

            if winners_role_id:

                wr = guild.get_role(winners_role_id)

                if wr:

                    for w in winners:

                        try:

                            await w.add_roles(wr, reason="Giveaway winner role")

                        except (discord.Forbidden, discord.HTTPException):

                            pass

        else:

            announce_embed = discord.Embed(

                title="🎉 Giveaway Ended",

                description=(

                    f"No valid entries — no winner could be determined for **{g['prize']}**.\n\n"

                    f"*Hosted by <@{g['host_id']}>*"

                ),

                color=ended_color,

                timestamp=datetime.now(timezone.utc),

            )

            await channel.send(embed=announce_embed)

    return winners

async def giveaway_watcher():

    await bot.wait_until_ready()

    while True:

        try:

            data = load_data()

            now  = datetime.now(timezone.utc)

            for gid_str, gmap in list(data.get("giveaways", {}).items()):

                guild = bot.get_guild(int(gid_str))

                if not guild:

                    continue

                for mid_str, g in list(gmap.items()):

                    if g.get("ended"):

                        continue

                    if now >= datetime.fromisoformat(g["end_time"]):

                        await end_giveaway(guild, int(mid_str))

        except Exception as e:

            print(f"Giveaway watcher error: {e}")

        await asyncio.sleep(15)

# ══════════════════════════════════════════════════════════════════════════════

#  TIMED UNMUTE

# ══════════════════════════════════════════════════════════════════════════════

async def schedule_unmute(guild_id: int, user_id: int, delay_seconds: float):

    await asyncio.sleep(delay_seconds)

    data   = load_data()

    expiry = get_mute_expiry(data, guild_id, user_id)

    if expiry is None:

        return

    guild = bot.get_guild(guild_id)

    if not guild:

        return

    member = guild.get_member(user_id)

    if not member:

        clear_mute_timer(data, guild_id, user_id)

        remove_persistent_role(data, guild_id, user_id, MUTED_ROLE_NAME)

        return

    role = discord.utils.get(guild.roles, name=MUTED_ROLE_NAME)

    if role and role in member.roles:

        try:

            await member.remove_roles(role, reason="Timed mute expired")

        except discord.Forbidden:

            pass

    clear_mute_timer(data, guild_id, user_id)

    remove_persistent_role(data, guild_id, user_id, MUTED_ROLE_NAME)

    channel = discord.utils.get(guild.text_channels, name=AUTO_LOG_CHANNEL_NAME)

    if channel:

        embed = discord.Embed(

            title="🔊  Auto-Unmute  •  Timer Expired",

            description="─────────────────────────────────",

            color=discord.Color.green(),

            timestamp=datetime.now(timezone.utc)

        )

        embed.add_field(name="👤 User",   value=f"{member}\n`{member.id}`",     inline=True)

        embed.add_field(name="🤖 System", value="Automatic (timer expired)",    inline=True)

        embed.add_field(name="\u200b",    value="\u200b",                        inline=True)

        embed.add_field(name="📄 Reason", value="Mute duration expired",        inline=False)

        embed.set_thumbnail(url=member.display_avatar.url)

        embed.set_footer(text="Auto-Unmute  •  Timer expired")

        await channel.send(embed=embed)

    await dm_member(member, "Unmute", guild.name, "Your mute has expired.", 0)

# ══════════════════════════════════════════════════════════════════════════════

#  PERMISSION HELPERS

# ══════════════════════════════════════════════════════════════════════════════

def has_role(m, name): return discord.utils.get(m.roles, name=name) is not None

def is_admin(m):       return m.guild_permissions.administrator

def has_app_commands_perm(m): return m.guild_permissions.use_application_commands

def is_full_mod(m):    return is_admin(m) or has_role(m, MOD_ROLE_NAME) or has_app_commands_perm(m)

def is_any_mod(m):     return is_full_mod(m) or has_role(m, TRIAL_MOD_ROLE_NAME)

def is_staff(m):

    perms      = m.guild_permissions

    role_names = [role.name.lower() for role in m.roles if role != m.guild.default_role]

    has_staff_role = any(

        keyword in role_name

        for role_name in role_names

        for keyword in STAFF_ROLE_KEYWORDS

    )

    return is_any_mod(m) or has_staff_role or any((

        perms.manage_messages,

        perms.moderate_members,

        perms.kick_members,

        perms.ban_members,

        perms.manage_guild,

    ))

def is_setup_authorized(m):

    if m.id == BOT_OWNER_ID or m.id == m.guild.owner_id or is_admin(m):

        return True

    allowed = get_cmd_roles(m.guild.id).get("setup", [])

    if not allowed:

        return False

    return any(discord.utils.get(m.roles, name=r) is not None for r in allowed)

def hierarchy_ok(actor, target):

    if is_admin(actor): return True

    return actor.top_role > target.top_role

def require_full_mod():

    async def pred(ctx):

        if not is_full_mod(ctx.author): raise commands.CheckFailure()

        return True

    return commands.check(pred)

def require_any_mod():

    async def pred(ctx):

        if not is_any_mod(ctx.author): raise commands.CheckFailure()

        return True

    return commands.check(pred)

def require_staff():

    async def pred(ctx):

        if not is_staff(ctx.author): raise commands.CheckFailure()

        return True

    return commands.check(pred)

def require_setup_auth():

    async def pred(ctx):

        if not is_setup_authorized(ctx.author): raise commands.CheckFailure()

        return True

    return commands.check(pred)

def require_cmd_role(cmd_name: str):

    async def pred(ctx):

        m = ctx.author

        if m.id == BOT_OWNER_ID or is_admin(m):

            return True

        allowed = get_cmd_roles(ctx.guild.id).get(cmd_name, [])

        if not allowed:

            return True

        if any(discord.utils.get(m.roles, name=r) is not None for r in allowed):

            return True

        raise commands.CheckFailure()

    return commands.check(pred)

def slash_cmd_role(cmd_name: str):

    def pred(i: discord.Interaction):

        m = i.user

        if m.id == BOT_OWNER_ID or m.guild_permissions.administrator:

            return True

        allowed = get_cmd_roles(i.guild.id).get(cmd_name, [])

        if not allowed:

            return True

        if any(discord.utils.get(m.roles, name=r) is not None for r in allowed):

            return True

        raise app_commands.CheckFailure()

    return app_commands.check(pred)

def slash_full_mod():

    def pred(i):

        if not is_full_mod(i.user): raise app_commands.CheckFailure()

        return True

    return app_commands.check(pred)

def slash_any_mod():

    def pred(i):

        if not is_any_mod(i.user): raise app_commands.CheckFailure()

        return True

    return app_commands.check(pred)

# ══════════════════════════════════════════════════════════════════════════════

#  MOD LOG POSTER

# ══════════════════════════════════════════════════════════════════════════════

async def post_mod_log(guild, case_id, action, moderator, target, reason, extra="",

                       proof_url: str = None, proof_note: str = None):

    channel = discord.utils.get(guild.text_channels, name=MOD_LOG_CHANNEL_NAME)

    if not channel:

        return

    icon  = ACTION_ICONS.get(action, "📋")

    color = ACTION_COLORS.get(action, discord.Color.blurple())

    embed = discord.Embed(

        title=f"{icon}  {action}  •  Case #{case_id}",

        description="─────────────────────────────────",

        color=color,

        timestamp=datetime.now(timezone.utc)

    )

    embed.add_field(name="👤 User",      value=f"{target}\n`{target.id}`",       inline=True)

    embed.add_field(name="🛡️ Moderator", value=f"{moderator}\n`{moderator.id}`", inline=True)

    embed.add_field(name="\u200b",       value="\u200b",                          inline=True)

    embed.add_field(name="📄 Reason",    value=reason or "No reason provided",   inline=False)

    if extra:

        embed.add_field(name="📌 Details", value=extra, inline=False)

    if proof_note:

        embed.add_field(name="📎 Proof Note", value=proof_note, inline=False)

    if proof_url:

        embed.add_field(name="🔗 Proof", value=proof_url, inline=False)

        if any(proof_url.split("?")[0].lower().endswith(ext)

               for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp")):

            embed.set_image(url=proof_url.split()[0])

    footer_text = f"Case #{case_id}  •  {action}" + ("  •  📎 Proof attached" if proof_url or proof_note else "")

    embed.set_footer(

        text=footer_text,

        icon_url=moderator.display_avatar.url if hasattr(moderator, "display_avatar") else None

    )

    if hasattr(target, "display_avatar"):

        embed.set_thumbnail(url=target.display_avatar.url)

    await channel.send(embed=embed)

async def post_auto_log(guild, embed: discord.Embed):

    channel = discord.utils.get(guild.text_channels, name=AUTO_LOG_CHANNEL_NAME)

    if channel:

        await channel.send(embed=embed)

# ══════════════════════════════════════════════════════════════════════════════

#  PROOF SYSTEM

# ══════════════════════════════════════════════════════════════════════════════

_pending_proof: dict[int, dict] = {}

PER_PAGE = 10

def build_modlogs_embed(member, cases, page: int, total_pages: int) -> discord.Embed:

    start  = page * PER_PAGE

    end    = start + PER_PAGE

    slice_ = cases[start:end]

    embed  = discord.Embed(

        title=f"📋 Mod Logs — {member}",

        color=discord.Color.blurple(),

        timestamp=datetime.now(timezone.utc)

    )

    embed.set_thumbnail(url=member.display_avatar.url)

    if not slice_:

        embed.description = "No moderation history found for this user."

    else:

        for c in slice_:

            icon = ACTION_ICONS.get(c["action"], "📋")

            embed.add_field(

                name=f"Case #{c['case']} — {icon} {c['action']} ({c['timestamp'][:10]})",

                value=f"**Reason:** {c['reason']}\n**Moderator:** {c['mod_tag']}",

                inline=False

            )

    embed.set_footer(text=f"Page {page + 1}/{total_pages}  •  {len(cases)} total case(s)")

    return embed

def build_modcases_embed(guild, data: dict, member=None, requester=None) -> discord.Embed:

    if member is None:

        actions = list(data["cases"].get(str(guild.id), []))

        embed = discord.Embed(

            title="🛡️  Server Mod Cases",

            description="All moderation actions taken in this server.",

            color=discord.Color.blurple(), timestamp=datetime.now(timezone.utc))

        if guild.icon:

            embed.set_thumbnail(url=guild.icon.url)

        no_actions_text = "No moderation actions have been recorded in this server."

        footer_who = "server-wide"

    else:

        records = get_mod_actions(data, guild.id, member.id)

        actions = records.get("actions", [])

        embed = discord.Embed(

            title=f"🛡️  Mod Cases — {member}",

            description=f"Actions taken by **{member.display_name}** as a moderator.",

            color=discord.Color.blurple(), timestamp=datetime.now(timezone.utc))

        embed.set_thumbnail(url=member.display_avatar.url)

        no_actions_text = f"**{member.display_name}** has no recorded mod actions."

        footer_who = str(member)

    if not actions:

        embed.description = no_actions_text

        embed.add_field(name="📊 Total Cases", value="0", inline=False)

    else:

        counts: dict[str, int] = {}

        for a in actions:

            counts[a["action"]] = counts.get(a["action"], 0) + 1

        summary = "  ".join(f"{ACTION_ICONS.get(k, '📋')} **{k}**: {v}" for k, v in sorted(counts.items()))

        embed.add_field(name=f"📊 Total Cases — {len(actions)}", value=summary, inline=False)

        embed.add_field(name="\u200b", value="─────────────────────────────────", inline=False)

        for a in actions[-15:][::-1]:

            ts   = datetime.fromisoformat(a["timestamp"])

            unix = int(ts.timestamp())

            icon = ACTION_ICONS.get(a["action"], "📋")

            value = f"**Target:** {a['target_tag']} (`{a['target_id']}`)\n**Reason:** {a['reason']}"

            if member is None:

                value = f"**Moderator:** {a['mod_tag']}\n" + value

            embed.add_field(

                name=f"{icon} {a['action']} — Case #{a['case']}  •  <t:{unix}:d>",

                value=value,

                inline=False)

        embed.set_footer(text=f"{len(actions)} total case(s) — {footer_who}  •  showing last 15  •  Requested by {requester}")

    return embed


class ModlogsView(discord.ui.View):

    def __init__(self, member, cases, invoker_id: int):

        super().__init__(timeout=180)

        self.member      = member

        self.cases       = list(reversed(cases))

        self.invoker_id  = invoker_id

        self.page        = 0

        self.total_pages = max(1, -(-len(self.cases) // PER_PAGE))

        self._update_buttons()

    def _update_buttons(self):

        self.next_btn.disabled = self.page == 0

        self.prev_btn.disabled = self.page >= self.total_pages - 1

    def current_embed(self) -> discord.Embed:

        return build_modlogs_embed(self.member, self.cases, self.page, self.total_pages)

    async def _check(self, interaction: discord.Interaction) -> bool:

        if interaction.user.id != self.invoker_id:

            await interaction.response.send_message(

                embed=error_embed("Only the person who ran this command can turn pages."),

                ephemeral=True

            )

            return False

        return True

    @discord.ui.button(label="◀ Newer", style=discord.ButtonStyle.secondary)

    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):

        if not await self._check(interaction): return

        self.page -= 1

        self._update_buttons()

        await interaction.response.edit_message(embed=self.current_embed(), view=self)

    @discord.ui.button(label="Older ▶", style=discord.ButtonStyle.secondary)

    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):

        if not await self._check(interaction): return

        self.page += 1

        self._update_buttons()

        await interaction.response.edit_message(embed=self.current_embed(), view=self)

    async def on_timeout(self):

        for child in self.children:

            child.disabled = True

class ProofView(discord.ui.View):

    def __init__(self, guild, case_id, action, moderator, target, reason, extra=""):

        super().__init__(timeout=120)

        self._guild     = guild

        self._case_id   = case_id

        self._action    = action

        self._moderator = moderator

        self._target    = target

        self._reason    = reason

        self._extra     = extra

        self._logged    = False

        self._action_msg_id: int | None = None

    def _disable_all(self):

        for child in self.children:

            child.disabled = True

    @discord.ui.button(label="Log Action", style=discord.ButtonStyle.secondary, emoji="📋")

    async def log_only(self, interaction: discord.Interaction, button: discord.ui.Button):

        if self._logged:

            return await interaction.response.send_message(

                embed=warn_embed("This case has already been logged."), ephemeral=True)

        self._logged = True

        self._disable_all()

        if self._action_msg_id and self._action_msg_id in _pending_proof:

            del _pending_proof[self._action_msg_id]

        await post_mod_log(self._guild, self._case_id, self._action,

                           self._moderator, self._target, self._reason, self._extra)

        await interaction.response.edit_message(view=self)

        await interaction.followup.send(

            embed=success_embed(

                f"Mod log for **Case #{self._case_id}** posted to `#{MOD_LOG_CHANNEL_NAME}`."),

            ephemeral=True)

    @discord.ui.button(label="Add Proof (reply with files)", style=discord.ButtonStyle.primary, emoji="📎")

    async def log_with_proof(self, interaction: discord.Interaction, button: discord.ui.Button):

        if self._logged:

            return await interaction.response.send_message(

                embed=warn_embed("This case has already been logged."), ephemeral=True)

        self._disable_all()

        await interaction.response.edit_message(view=self)

        hint_embed = discord.Embed(

            description=f"📎 **Reply to this message** with your proof attachments. Auto-logs in 2 min.",

            color=discord.Color.blurple(),

        )

        hint_embed.set_footer(text=f"Case #{self._case_id}  •  {self._action}")

        hint_msg = await interaction.followup.send(embed=hint_embed)

        self._action_msg_id = hint_msg.id

        _pending_proof[hint_msg.id] = {

            "guild":     self._guild,

            "channel_id": interaction.channel.id,

            "case_id":   self._case_id,

            "action":    self._action,

            "moderator": self._moderator,

            "target":    self._target,

            "reason":    self._reason,

            "extra":     self._extra,

            "mod_id":    self._moderator.id,

            "view":      self,

        }

    async def on_timeout(self):

        if not self._logged:

            self._logged = True

            if self._action_msg_id and self._action_msg_id in _pending_proof:

                del _pending_proof[self._action_msg_id]

            await post_mod_log(self._guild, self._case_id, self._action,

                               self._moderator, self._target, self._reason, self._extra)

# ══════════════════════════════════════════════════════════════════════════════

#  EDIT CASE COMMANDS

# ══════════════════════════════════════════════════════════════════════════════

class EditCaseView(discord.ui.View):

    def __init__(self, ctx, case_data: dict):

        super().__init__(timeout=120)

        self._ctx            = ctx

        self._case           = case_data

        self._logged         = False

        self._pending_msg_id: int | None = None

    def _disable_all(self):

        for child in self.children:

            child.disabled = True

    def _build_preview_embed(self) -> discord.Embed:

        c      = self._case

        action = c["action"]

        icon   = ACTION_ICONS.get(action, "📋")

        color  = ACTION_COLORS.get(action, discord.Color.blurple())

        ts     = datetime.fromisoformat(c["timestamp"])

        embed  = discord.Embed(

            title=f"{icon}  Edit Case #{c['case']}  •  {action}",

            description="─────────────────────────────────\nChoose what to do with this case:",

            color=color, timestamp=ts,

        )

        embed.add_field(name="👤 User",      value=f"{c['target_tag']}\n`{c['target_id']}`", inline=True)

        embed.add_field(name="🛡️ Moderator", value=f"{c['mod_tag']}\n`{c['mod_id']}`",       inline=True)

        embed.add_field(name="\u200b",        value="\u200b",                                  inline=True)

        embed.add_field(name="📄 Reason",     value=c["reason"] or "No reason provided",       inline=False)

        embed.set_footer(text=f"Case #{c['case']}  •  Editing")

        return embed

    @discord.ui.button(label="Re-log to Modlog", style=discord.ButtonStyle.secondary, emoji="📋")

    async def relog_only(self, interaction: discord.Interaction, button: discord.ui.Button):

        if interaction.user.id != self._ctx.author.id:

            return await interaction.response.send_message(

                embed=error_embed("Only the invoker can use these buttons."), ephemeral=True)

        if self._logged:

            return await interaction.response.send_message(

                embed=warn_embed("Already re-logged this case."), ephemeral=True)

        self._logged = True

        self._disable_all()

        await interaction.response.edit_message(view=self)

        c       = self._case

        guild   = interaction.guild

        channel = discord.utils.get(guild.text_channels, name=MOD_LOG_CHANNEL_NAME)

        if not channel:

            return await interaction.followup.send(

                embed=error_embed(f"Could not find `#{MOD_LOG_CHANNEL_NAME}`."), ephemeral=True)

        action = c["action"]

        icon   = ACTION_ICONS.get(action, "📋")

        color  = ACTION_COLORS.get(action, discord.Color.blurple())

        ts     = datetime.fromisoformat(c["timestamp"])

        embed  = discord.Embed(

            title=f"{icon}  {action}  •  Case #{c['case']}",

            description="─────────────────────────────────",

            color=color, timestamp=ts,

        )

        embed.add_field(name="👤 User",      value=f"{c['target_tag']}\n`{c['target_id']}`", inline=True)

        embed.add_field(name="🛡️ Moderator", value=f"{c['mod_tag']}\n`{c['mod_id']}`",       inline=True)

        embed.add_field(name="\u200b",        value="\u200b",                                  inline=True)

        embed.add_field(name="📄 Reason",     value=c["reason"] or "No reason provided",       inline=False)

        embed.set_footer(text=f"Case #{c['case']}  •  {action}  •  ✏️ Re-logged")

        try:

            target_user = await bot.fetch_user(c["target_id"])

            embed.set_thumbnail(url=target_user.display_avatar.url)

        except Exception:

            pass

        await channel.send(embed=embed)

        await interaction.followup.send(

            embed=success_embed(f"Case **#{c['case']}** re-logged to `#{MOD_LOG_CHANNEL_NAME}`."),

            ephemeral=True)

    @discord.ui.button(label="Add Proof (reply with files)", style=discord.ButtonStyle.primary, emoji="📎")

    async def add_proof(self, interaction: discord.Interaction, button: discord.ui.Button):

        if interaction.user.id != self._ctx.author.id:

            return await interaction.response.send_message(

                embed=error_embed("Only the invoker can use these buttons."), ephemeral=True)

        if self._logged:

            return await interaction.response.send_message(

                embed=warn_embed("Already processed this case."), ephemeral=True)

        self._disable_all()

        await interaction.response.edit_message(view=self)

        c          = self._case

        hint_embed = discord.Embed(

            description="📎 **Reply to this message** with your proof attachments. Auto-logs in 2 min.",

            color=discord.Color.blurple(),

        )

        hint_embed.set_footer(text=f"Case #{c['case']}  •  {c['action']}")

        hint_msg             = await interaction.followup.send(embed=hint_embed)

        self._pending_msg_id = hint_msg.id

        class _FakeUser:

            def __init__(self, id_, tag, avatar):

                self.id             = id_

                self._tag           = tag

                self.display_avatar = avatar

            def __str__(self): return self._tag

        _pending_proof[hint_msg.id] = {

            "guild":     interaction.guild,

            "channel_id": interaction.channel.id,

            "case_id":   c["case"],

            "action":    c["action"],

            "moderator": interaction.user,

            "target":    _FakeUser(c["target_id"], c["target_tag"], interaction.user.display_avatar),

            "reason":    c["reason"],

            "extra":     "",

            "mod_id":    interaction.user.id,

            "view":      self,

        }

    async def on_timeout(self):

        self._disable_all()

        if self._pending_msg_id and self._pending_msg_id in _pending_proof:

            p       = _pending_proof.pop(self._pending_msg_id)

            c       = self._case

            guild   = p["guild"]

            channel = discord.utils.get(guild.text_channels, name=MOD_LOG_CHANNEL_NAME)

            if channel:

                action = c["action"]

                icon   = ACTION_ICONS.get(action, "📋")

                color  = ACTION_COLORS.get(action, discord.Color.blurple())

                ts     = datetime.fromisoformat(c["timestamp"])

                embed  = discord.Embed(

                    title=f"{icon}  {action}  •  Case #{c['case']}",

                    description="─────────────────────────────────",

                    color=color, timestamp=ts,

                )

                embed.add_field(name="👤 User",      value=f"{c['target_tag']}\n`{c['target_id']}`", inline=True)

                embed.add_field(name="🛡️ Moderator", value=f"{c['mod_tag']}\n`{c['mod_id']}`",       inline=True)

                embed.add_field(name="\u200b",        value="\u200b",                                  inline=True)

                embed.add_field(name="📄 Reason",     value=c["reason"] or "No reason provided",       inline=False)

                embed.set_footer(text=f"Case #{c['case']}  •  {action}  •  ✏️ Re-logged (timed out)")

                await channel.send(embed=embed)

@bot.command(name="editcase")

@require_cmd_role("editcase")

async def editcase_cmd(ctx, case_number: int = None):

    if case_number is None:

        return await ctx.send(embed=syntax_embed("editcase") if "editcase" in COMMAND_SYNTAX else discord.Embed(

            title="📖  `.editcase`", description="Usage: `.editcase <case_number>`",

            color=discord.Color.blurple()))

    data = load_data()

    c    = get_case_by_id(data, ctx.guild.id, case_number)

    if not c:

        return await ctx.send(embed=error_embed(f"Case **#{case_number}** not found."))

    view  = EditCaseView(ctx, c)

    embed = view._build_preview_embed()

    await ctx.send(embed=embed, view=view)

@bot.command(name="editc")

@require_cmd_role("editc")

async def editc_cmd(ctx, *, args: str = None):

    if not args or "?r" not in args:

        embed = discord.Embed(

            title="📖  `.editc` — Command Usage", color=discord.Color.blurple(),

            timestamp=datetime.now(timezone.utc))

        embed.add_field(name="Syntax",  value="```\n.editc <case_number>?r <new reason>\n```", inline=False)

        embed.add_field(name="Example", value="```\n.editc 42?r Spamming in general chat\n```", inline=False)

        embed.add_field(name="ℹ️ Info", value="Changes the reason stored in a case and posts an update to the modlog.", inline=False)

        embed.set_footer(text="< > = required  •  ?r separates case number from reason")

        return await ctx.send(embed=embed)

    parts      = args.split("?r", 1)

    case_part  = parts[0].strip()

    new_reason = parts[1].strip() if len(parts) > 1 else ""

    if not case_part.isdigit():

        return await ctx.send(embed=error_embed("Case number must be a number."))

    case_number = int(case_part)

    if not new_reason:

        return await ctx.send(embed=error_embed("Please provide a new reason after `?r`."))

    data = load_data()

    c    = get_case_by_id(data, ctx.guild.id, case_number)

    if not c:

        return await ctx.send(embed=error_embed(f"Case **#{case_number}** not found."))

    old_reason  = c["reason"]

    c["reason"] = new_reason

    save_data(data)

    channel = discord.utils.get(ctx.guild.text_channels, name=MOD_LOG_CHANNEL_NAME)

    if channel:

        action = c["action"]

        icon   = ACTION_ICONS.get(action, "📋")

        color  = ACTION_COLORS.get(action, discord.Color.blurple())

        embed  = discord.Embed(

            title=f"✏️  Reason Edited  •  Case #{case_number}",

            description="─────────────────────────────────",

            color=color, timestamp=datetime.now(timezone.utc))

        embed.add_field(name="👤 User",      value=f"{c['target_tag']}\n`{c['target_id']}`", inline=True)

        embed.add_field(name="🛡️ Moderator", value=f"{c['mod_tag']}\n`{c['mod_id']}`",       inline=True)

        embed.add_field(name="\u200b",        value="\u200b",                                  inline=True)

        embed.add_field(name="📄 Old Reason", value=old_reason or "No reason provided",        inline=False)

        embed.add_field(name="📄 New Reason", value=new_reason,                                inline=False)

        embed.add_field(name="🔧 Edited by",  value=f"{ctx.author} (`{ctx.author.id}`)",      inline=False)

        embed.set_footer(text=f"Case #{case_number}  •  {action}  •  ✏️ Reason edited")

        try:

            target_user = await bot.fetch_user(c["target_id"])

            embed.set_thumbnail(url=target_user.display_avatar.url)

        except Exception:

            pass

        await channel.send(embed=embed)

    await ctx.send(embed=success_embed(

        f"Reason for **Case #{case_number}** updated.\n"

        f"**Old:** {old_reason or 'No reason provided'}\n"

        f"**New:** {new_reason}"))

# ══════════════════════════════════════════════════════════════════════════════

#  ROLE CREATION HELPERS

# ══════════════════════════════════════════════════════════════════════════════

async def get_or_create_role(guild, name, color):

    role = discord.utils.get(guild.roles, name=name)

    if role is None:

        role = await guild.create_role(name=name, color=color, reason=f"Auto-created {name} role")

        for ch in guild.text_channels:

            try:

                await ch.set_permissions(role, send_messages=False, add_reactions=False)

            except discord.Forbidden:

                pass

    return role

# ══════════════════════════════════════════════════════════════════════════════

#  ACTION FUNCTIONS

# ══════════════════════════════════════════════════════════════════════════════

async def action_ban(guild, mod, target, reason, delete_days=0):

    await guild.ban(target, reason=reason, delete_message_days=delete_days)

async def action_unban(guild, user_id):

    user = await bot.fetch_user(user_id)

    await guild.unban(user)

    return user

async def action_kick(target, reason):

    await target.kick(reason=reason)

async def action_mute(guild, target, reason):

    role = await get_or_create_role(guild, MUTED_ROLE_NAME, discord.Color.greyple())

    if role in target.roles: return False

    await target.add_roles(role, reason=reason)

    return True

async def action_unmute(guild, target):

    role = discord.utils.get(guild.roles, name=MUTED_ROLE_NAME)

    if not role or role not in target.roles: return False

    await target.remove_roles(role)

    return True

async def purge_member_messages(guild, target, limit: int):

    if limit <= 0:

        return

    cutoff = datetime.now(timezone.utc) - timedelta(days=14)

    bulk_cutoff = datetime.now(timezone.utc) - timedelta(days=14) + timedelta(minutes=5)

    # Scan every text channel AND its active/archived threads — a per-channel
    # cap (not a global one) so one busy channel can't starve the rest.
    channels: list = []

    for channel in guild.text_channels:

        channels.append(channel)

        try:

            for thread in channel.threads:

                channels.append(thread)

        except (discord.Forbidden, discord.HTTPException):

            pass

        try:

            async for thread in channel.archived_threads(limit=50):

                channels.append(thread)

        except (discord.Forbidden, discord.HTTPException):

            pass

    deleted_total = 0

    for channel in channels:

        if deleted_total >= limit:

            break

        perms = channel.permissions_for(guild.me)

        if not perms.read_message_history or not perms.manage_messages:

            continue

        remaining = limit - deleted_total

        collected: list[discord.Message] = []

        try:

            # No history limit here — we want every matching message within
            # the 14-day window, not just whatever's in the most recent 200.
            async for msg in channel.history(limit=None, after=cutoff):

                if msg.author.id == target.id:

                    collected.append(msg)

                    if len(collected) >= remaining:

                        break

        except (discord.Forbidden, discord.HTTPException):

            continue

        if not collected:

            continue

        # Bulk-delete can only touch messages younger than 14 days, and if a
        # single message in a batch is too old the WHOLE batch raises and is
        # skipped (not just that message). Split into "bulk-eligible" vs
        # "too old for bulk" and handle each safely so one stale message
        # can't take out 99 deletable ones with it.
        bulk_eligible  = [m for m in collected if m.created_at >= bulk_cutoff]

        needs_single   = [m for m in collected if m.created_at < bulk_cutoff]

        for i in range(0, len(bulk_eligible), 100):

            chunk = bulk_eligible[i:i + 100]

            try:

                if len(chunk) == 1:

                    await chunk[0].delete()

                else:

                    await channel.delete_messages(chunk)

                deleted_total += len(chunk)

            except discord.HTTPException:

                # Fall back to one-by-one so a single bad message in the
                # chunk doesn't sink the rest of it.

                for m in chunk:

                    try:

                        await m.delete()

                        deleted_total += 1

                    except (discord.Forbidden, discord.HTTPException):

                        continue

        for m in needs_single:

            try:

                await m.delete()

                deleted_total += 1

            except (discord.Forbidden, discord.HTTPException):

                continue

            if deleted_total >= limit:

                break

async def action_jail(guild, target, reason):

    role = await get_or_create_role(guild, JAIL_ROLE_NAME, discord.Color.dark_gray())

    if role in target.roles: return False

    await target.add_roles(role, reason=reason)

    asyncio.create_task(purge_member_messages(guild, target, JAIL_PURGE_MESSAGES))

    return True

async def action_unjail(guild, target):

    role = discord.utils.get(guild.roles, name=JAIL_ROLE_NAME)

    if not role or role not in target.roles: return False

    await target.remove_roles(role)

    return True

# ══════════════════════════════════════════════════════════════════════════════
#  JAIL REQUEST SYSTEM  (.jreq / /jailreq)
# ══════════════════════════════════════════════════════════════════════════════

def get_jail_request_channel(guild: discord.Guild):
    """Returns the configured jail-request channel for this guild, falling back
    to a channel named JAIL_REQUEST_CHANNEL_NAME if none has been configured."""
    data = load_data()
    cid  = data.get("jail_request_channel", {}).get(str(guild.id))
    if cid:
        ch = guild.get_channel(cid)
        if ch:
            return ch
    return discord.utils.get(guild.text_channels, name=JAIL_REQUEST_CHANNEL_NAME)


def set_jail_request_channel(guild_id: int, channel_id: int):
    data = load_data()
    data.setdefault("jail_request_channel", {})[str(guild_id)] = channel_id
    save_data(data)


def add_jail_request(data, guild_id, requester, target, reason) -> int:
    """Creates a jail-request record, returns its id. Also tracks it against
    the requesting (trial) mod's modlogs via track_mod_action so it shows up
    in their history / counts toward their activity."""
    gid = str(guild_id)
    data.setdefault("jail_requests", {}).setdefault(gid, [])
    rid = data.get("next_jreq", 1)
    data["next_jreq"] = rid + 1
    record = {
        "id": rid,
        "requester_id": requester.id, "requester_tag": str(requester),
        "target_id": target.id, "target_tag": str(target),
        "reason": reason,
        "status": "pending",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "proof_urls": [],
    }
    data["jail_requests"][gid].append(record)
    save_data(data)

    # Count this jail request in the trial mod's modlogs/activity tracking
    track_mod_action(data, guild_id, requester.id, str(requester),
                      "Jail Request", str(target), target.id, reason, rid)
    return rid


def get_jail_request(data, guild_id, rid: int):
    for r in data.get("jail_requests", {}).get(str(guild_id), []):
        if r["id"] == rid:
            return r
    return None


def mark_jail_request_actioned(data, guild_id, rid: int, mod):
    r = get_jail_request(data, guild_id, rid)
    if r:
        r["status"]       = "actioned"
        r["actioned_by"]  = mod.id
        r["actioned_tag"] = str(mod)
        save_data(data)
    return r


_jreq_proof_cache: dict[int, list[str]] = {}   # rid -> [attachment urls]
_pending_jreq_proof: dict[int, dict] = {}      # hint_msg_id -> pending info


def get_jreq_proof(rid: int) -> list[str]:
    return _jreq_proof_cache.get(rid, [])


def build_jail_request_embed(rid: int, requester, target, reason: str, status: str = "pending", proof_urls=None) -> discord.Embed:
    color = discord.Color.orange() if status == "pending" else discord.Color.green()
    icon  = "🟡" if status == "pending" else "✅"
    embed = discord.Embed(
        title=f"🔒 Jail Request #{rid}  •  {icon} {status.title()}",
        description="─────────────────────────────────",
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="👤 Target",       value=f"{target}\n`{target.id}`", inline=True)
    embed.add_field(name="🛡️ Requested by", value=f"{requester}\n`{requester.id}`", inline=True)
    embed.add_field(name="\u200b",          value="\u200b", inline=True)
    embed.add_field(name="📄 Reason",       value=reason or "No reason provided", inline=False)
    if proof_urls:
        embed.add_field(name="📎 Proof", value="\n".join(proof_urls), inline=False)
        first = proof_urls[0].split("?")[0].lower()
        if any(first.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp")):
            embed.set_image(url=proof_urls[0])
    embed.set_footer(text=f"Jail Request #{rid}" + ("  •  📎 Proof attached" if proof_urls else ""))
    if hasattr(target, "display_avatar"):
        embed.set_thumbnail(url=target.display_avatar.url)
    return embed


class JailRequestActionView(discord.ui.View):
    """Buttons mods see on a jail request. Jailing here auto-pulls whatever
    proof the trial mod attached when they ran .jreq — no re-upload needed."""

    def __init__(self, rid: int, guild_id: int):
        super().__init__(timeout=None)   # persists indefinitely until actioned
        self.rid       = rid
        self.guild_id  = guild_id
        self._actioned = False

    def _disable_all(self):
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="Jail User", style=discord.ButtonStyle.danger, emoji="🔒")
    async def jail_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_any_mod(interaction.user):
            return await interaction.response.send_message(
                embed=error_embed("You don't have permission to action jail requests."), ephemeral=True)

        if self._actioned:
            return await interaction.response.send_message(
                embed=warn_embed("This jail request has already been actioned."), ephemeral=True)

        data = load_data()
        req  = get_jail_request(data, self.guild_id, self.rid)
        if not req:
            return await interaction.response.send_message(
                embed=error_embed("Could not find this jail request in storage."), ephemeral=True)

        guild = interaction.guild
        try:
            target = await guild.fetch_member(req["target_id"])
        except discord.NotFound:
            return await interaction.response.send_message(
                embed=error_embed("Target is no longer in the server."), ephemeral=True)

        if not hierarchy_ok(interaction.user, target):
            return await interaction.response.send_message(
                embed=error_embed("You cannot jail someone with an equal or higher role."), ephemeral=True)

        await interaction.response.defer()

        ok = await action_jail(guild, target, req["reason"])
        if not ok:
            self._actioned = True
            self._disable_all()
            await interaction.edit_original_response(view=self)
            return await interaction.followup.send(
                embed=warn_embed(f"**{target.display_name}** is already jailed."), ephemeral=True)

        # Pull proof the trial mod already attached on .jreq — no re-upload needed
        proof_urls = get_jreq_proof(self.rid) or req.get("proof_urls", [])

        cid = add_case(data, guild.id, "Jail", interaction.user, target, req["reason"])
        add_persistent_role(data, guild.id, target.id, JAIL_ROLE_NAME)
        mark_jail_request_actioned(data, guild.id, self.rid, interaction.user)

        await dm_member(target, "Jail", guild.name, req["reason"], cid, moderator=interaction.user)

        await post_mod_log(
            guild, cid, "Jail", interaction.user, target, req["reason"],
            extra=f"Actioned from Jail Request #{self.rid} (requested by {req['requester_tag']})",
            proof_url=proof_urls[0] if len(proof_urls) == 1 else None,
            proof_note="\n".join(proof_urls) if len(proof_urls) > 1 else None,
        )

        self._actioned = True
        self._disable_all()
        await interaction.edit_original_response(view=self)

        await interaction.followup.send(
            embed=success_embed(
                (f"**{target}** jailed and logged as **Case #{cid}**.\n"
                 f"Proof auto-carried from Jail Request #{self.rid}.") if proof_urls else
                f"**{target}** jailed and logged as **Case #{cid}**."),
        )

        try:
            req_embed = build_jail_request_embed(
                self.rid, req["requester_tag"], target, req["reason"],
                status="actioned", proof_urls=proof_urls)
            await interaction.message.edit(embed=req_embed, view=self)
        except Exception:
            pass

    @discord.ui.button(label="Dismiss", style=discord.ButtonStyle.secondary, emoji="🗑️")
    async def dismiss_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_any_mod(interaction.user):
            return await interaction.response.send_message(
                embed=error_embed("You don't have permission to action jail requests."), ephemeral=True)

        if self._actioned:
            return await interaction.response.send_message(
                embed=warn_embed("This jail request has already been actioned."), ephemeral=True)

        data = load_data()
        req  = get_jail_request(data, self.guild_id, self.rid)
        if req:
            req["status"]       = "dismissed"
            req["actioned_by"]  = interaction.user.id
            req["actioned_tag"] = str(interaction.user)
            save_data(data)

        self._actioned = True
        self._disable_all()
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(
            embed=info_embed(f"Jail Request #{self.rid} dismissed by {interaction.user.mention}."))


class JailRequestProofView(discord.ui.View):
    """Mirrors ProofView's UX: trial mod can log the request immediately or
    attach proof first. Either way the request gets posted once logged."""

    def __init__(self, guild, rid, requester, target, reason):
        super().__init__(timeout=120)
        self._guild     = guild
        self._rid       = rid
        self._requester = requester
        self._target    = target
        self._reason    = reason
        self._logged    = False
        self._hint_msg_id: int | None = None

    def _disable_all(self):
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="Submit Request", style=discord.ButtonStyle.secondary, emoji="📋")
    async def log_only(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self._logged:
            return await interaction.response.send_message(
                embed=warn_embed("This request has already been submitted."), ephemeral=True)
        self._logged = True
        self._disable_all()
        await interaction.response.edit_message(view=self)
        await post_jail_request(self._guild, self._rid, self._requester, self._target, self._reason)
        await interaction.followup.send(
            embed=success_embed(f"Jail Request **#{self._rid}** submitted."), ephemeral=True)

    @discord.ui.button(label="Add Proof (reply with files)", style=discord.ButtonStyle.primary, emoji="📎")
    async def log_with_proof(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self._logged:
            return await interaction.response.send_message(
                embed=warn_embed("This request has already been submitted."), ephemeral=True)
        self._disable_all()
        await interaction.response.edit_message(view=self)

        hint_embed = discord.Embed(
            description="📎 **Reply to this message** with your proof attachments. Auto-submits in 2 min.",
            color=discord.Color.blurple(),
        )
        hint_embed.set_footer(text=f"Jail Request #{self._rid}")
        hint_msg = await interaction.followup.send(embed=hint_embed)
        self._hint_msg_id = hint_msg.id

        _pending_jreq_proof[hint_msg.id] = {
            "guild": self._guild, "channel_id": interaction.channel.id,
            "rid": self._rid, "requester": self._requester, "target": self._target,
            "reason": self._reason, "mod_id": self._requester.id, "view": self,
        }

    async def on_timeout(self):
        if not self._logged:
            self._logged = True
            if self._hint_msg_id and self._hint_msg_id in _pending_jreq_proof:
                del _pending_jreq_proof[self._hint_msg_id]
            await post_jail_request(self._guild, self._rid, self._requester, self._target, self._reason)


async def post_jail_request(guild, rid, requester, target, reason, proof_urls=None):
    proof_urls = proof_urls or get_jreq_proof(rid)
    embed = build_jail_request_embed(rid, requester, target, reason, status="pending", proof_urls=proof_urls)

    jreq_channel = get_jail_request_channel(guild)
    thread = None
    if jreq_channel:
        action_view = JailRequestActionView(rid, guild.id)
        jreq_msg    = await jreq_channel.send(embed=embed, view=action_view)
        try:
            thread = await jreq_msg.create_thread(
                name=f"Jail Req #{rid} — {target.display_name}"[:100],
                auto_archive_duration=1440,
            )
            await thread.send(
                embed=info_embed("Mods can discuss and use the buttons above to **Jail** or **Dismiss** this request."))
        except (discord.Forbidden, discord.HTTPException):
            pass

    # Also appears in mod-log channel alongside manual mod actions
    mod_log_channel = discord.utils.get(guild.text_channels, name=MOD_LOG_CHANNEL_NAME)
    if mod_log_channel:
        await mod_log_channel.send(embed=embed)

    return thread


async def handle_jreq_proof_reply(message: discord.Message) -> bool:
    """Returns True if this message was consumed as jail-request proof."""
    if not (message.attachments and _pending_jreq_proof):
        return False

    ref_id   = message.reference.message_id if message.reference else None
    proof_id = None
    pending  = None

    if ref_id and ref_id in _pending_jreq_proof:
        proof_id = ref_id
        pending  = _pending_jreq_proof.get(ref_id)
    else:
        for pid, p in _pending_jreq_proof.items():
            if p.get("channel_id") == message.channel.id and p.get("mod_id") == message.author.id:
                proof_id = pid
                pending  = p
                break

    if not pending or pending["view"]._logged:
        return False

    _pending_jreq_proof.pop(proof_id, None)
    pending["view"]._logged = True

    urls = [a.url for a in message.attachments]
    _jreq_proof_cache[pending["rid"]] = urls

    data = load_data()
    req  = get_jail_request(data, pending["guild"].id, pending["rid"])
    if req:
        req["proof_urls"] = urls
        save_data(data)

    await post_jail_request(pending["guild"], pending["rid"], pending["requester"],
                             pending["target"], pending["reason"], proof_urls=urls)

    await message.add_reaction("✅")
    await message.reply(
        embed=success_embed(f"**{len(message.attachments)}** attachment(s) attached to Jail Request **#{pending['rid']}**."),
        mention_author=False)
    return True


@bot.command(name="jreq")
@require_cmd_role("jreq")
async def jreq_cmd(ctx, member: discord.Member = None, *, reason="No reason provided"):
    if not member:
        return await ctx.send(embed=syntax_embed("jreq"))

    data = load_data()
    rid  = add_jail_request(data, ctx.guild.id, ctx.author, member, reason)

    view  = JailRequestProofView(ctx.guild, rid, ctx.author, member, reason)
    embed = discord.Embed(
        title=f"🔒 Jail Request #{rid} — Draft",
        description=f"Requesting jail on {member.mention} for:\n**{reason}**\n\nAttach proof or submit directly.",
        color=discord.Color.orange(),
    )
    await ctx.send(embed=embed, view=view)


@bot.tree.command(name="jailreq", description="[Trial Mod] Request a jail action on a member")
@app_commands.describe(member="Member to request jail on", reason="Reason")
@slash_cmd_role("jreq")
async def jailreq_slash(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    await interaction.response.defer()

    data = load_data()
    rid  = add_jail_request(data, interaction.guild.id, interaction.user, member, reason)

    view  = JailRequestProofView(interaction.guild, rid, interaction.user, member, reason)
    embed = discord.Embed(
        title=f"🔒 Jail Request #{rid} — Draft",
        description=f"Requesting jail on {member.mention} for:\n**{reason}**\n\nAttach proof or submit directly.",
        color=discord.Color.orange(),
    )
    await interaction.followup.send(embed=embed, view=view)


@jailreq_slash.error
async def jailreq_slash_err(interaction, error):
    if isinstance(error, app_commands.CheckFailure):
        await slash_silent_fail(interaction)


@bot.command(name="setjreqchannel")
@require_setup_auth()
async def set_jreq_channel_cmd(ctx, channel: discord.TextChannel = None):
    if not channel:
        view = JailReqChannelPickerView(ctx.author.id)
        return await ctx.send(
            embed=info_embed("Select the channel jail requests should be posted to:"), view=view)
    set_jail_request_channel(ctx.guild.id, channel.id)
    await ctx.send(embed=success_embed(f"Jail requests will now be posted to {channel.mention}."))


class JailReqChannelPickerView(discord.ui.View):
    def __init__(self, invoker_id: int):
        super().__init__(timeout=120)
        self.invoker_id = invoker_id
        self.add_item(JailReqChannelSelect(invoker_id))


class JailReqChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, invoker_id: int):
        super().__init__(
            placeholder="Choose the jail request channel...",
            min_values=1, max_values=1,
            channel_types=[discord.ChannelType.text],
        )
        self.invoker_id = invoker_id

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.invoker_id:
            return await interaction.response.send_message(
                embed=error_embed("Only the person who ran this command can use this."), ephemeral=True)
        channel = self.values[0]
        set_jail_request_channel(interaction.guild.id, channel.id)
        await interaction.response.edit_message(
            embed=success_embed(f"Jail requests will now be posted to {channel.mention}."), view=None)

# ══════════════════════════════════════════════════════════════════════════════
#  CORE MUTE LOGIC
# ══════════════════════════════════════════════════════════════════════════════

async def do_mute(guild, moderator, member, reason: str, duration_seconds: int):

    ok = await action_mute(guild, member, reason)

    if not ok:

        return None, warn_embed(f"**{member.display_name}** is already muted."), None

    data       = load_data()

    expires_at = datetime.now(timezone.utc) + timedelta(seconds=duration_seconds)

    dur_str    = fmt_duration(duration_seconds)

    extra_note = f"Duration: **{dur_str}** — expires <t:{int(expires_at.timestamp())}:R>"

    cid = add_case(data, guild.id, "Mute", moderator, member, reason)

    add_persistent_role(data, guild.id, member.id, MUTED_ROLE_NAME)

    set_mute_timer(data, guild.id, member.id, expires_at)

    await dm_member(member, "Mute", guild.name, reason, cid, moderator=moderator, extra=extra_note)

    asyncio.create_task(schedule_unmute(guild.id, member.id, duration_seconds))

    view = ProofView(guild, cid, "Mute", moderator, member, reason, extra=extra_note)

    return result_embed("Mute", member, reason, cid, extra=extra_note), None, view

# ══════════════════════════════════════════════════════════════════════════════

#  SETUP PERMS — CATEGORIZED UI

# ══════════════════════════════════════════════════════════════════════════════

PERM_CATEGORIES = {

    "⚔️  Moderation": {

        "emoji": "⚔️",

        "description": "Ban, kick, mute, warn, jail, purge and related commands",

        "commands": ["ban", "unban", "kick", "mute", "unmute", "warn", "unwarn",

                     "warnings", "modlogs", "modcases", "case", "editcase", "editc",

                     "jail", "unjail", "jreq", "purge", "purgeuser", "w"],

    },

    "🎉  Giveaways": {

        "emoji": "🎉",

        "description": "Start, end, reroll and manage giveaways",

        "commands": ["giveaway create", "giveaway end", "giveaway reroll", "giveaway delete", "giveaway list"],

    },

    "🛠️  Misc / Utility": {

        "emoji": "🛠️",

        "description": "Custom commands, role management, AFK and setup",

        "commands": ["role", "rappeal", "dwc", "rdwc", "addcmd", "delcmd", "listcmds", "afk", "setup", "ping"],

    },

    "🚫  Channel Whitelist": {

        "emoji": "🚫",

        "description": "Channels excluded from auto permission overrides",

        "commands": [],

    },

    "🔒  Jail Request Channel": {

        "emoji": "🔒",

        "description": "Where .jreq / /jailreq requests get posted",

        "commands": [],

    },


    "Unappeal Roles": {

        "emoji": "🎯",

        "description": "Set the special role given by appeal rejection commands",

        "commands": [],

    },

    "DWC Roles": {

        "emoji": "🏷️",

        "description": "Set the special role given by .dwc / /dwc",

        "commands": [],

    },

}

COMMAND_EMOJIS = {

    "ban": "🔨", "unban": "✅", "kick": "👢", "mute": "🔇", "unmute": "🔊",

    "warn": "⚠️", "unwarn": "🗑️", "warnings": "📋", "modlogs": "📖",

    "modcases": "🛡️", "case": "🔍", "editcase": "✏️", "editc": "📝",

    "jail": "🔒", "unjail": "🔓", "jreq": "🚨", "purge": "🗑️", "purgeuser": "🧹", "w": "👤", "role": "🎭",

    "addcmd": "➕", "delcmd": "➖", "listcmds": "📃", "afk": "💤", "setup": "⚙️", "rappeal": "🎯", "dwc": "🏷️", "rdwc": "🏷️",

    "giveaway create": "🎉", "giveaway end": "🏁", "giveaway reroll": "🔁", "giveaway delete": "❌", "giveaway list": "📜",

    "ping": "📶",

}

CONFIGURABLE_COMMANDS = [

    cmd for cat in PERM_CATEGORIES.values() for cmd in cat["commands"]

]

def _perms_main_embed(guild: discord.Guild, guild_cmd_roles: dict) -> discord.Embed:

    embed = discord.Embed(

        title="⚙️  Command Permission Manager",

        description=(

            "Use the dropdown below to select a **category** to view or edit.\n\n"

            "**Admins and the bot owner can always use any command.**"

        ),

        color=discord.Color.blurple(),

        timestamp=datetime.now(timezone.utc),

    )

    embed.set_footer(text=f"Server: {guild.name}")

    return embed

class CategorySelectDropdown(discord.ui.Select):

    def __init__(self, guild_cmd_roles: dict, invoker_id: int):

        self.guild_cmd_roles = guild_cmd_roles

        self.invoker_id      = invoker_id

        options = [

            discord.SelectOption(

                label=cat_name,

                value=cat_name,

                emoji=cat_data["emoji"],

                description=cat_data["description"],

            )

            for cat_name, cat_data in PERM_CATEGORIES.items()

        ]

        super().__init__(

            placeholder="Select a category to configure...",

            min_values=1, max_values=1,

            options=options,

        )

    async def callback(self, interaction: discord.Interaction):

        cat_name = self.values[0]

        if cat_name == "🚫  Channel Whitelist":

            view  = ChannelWhitelistView(interaction.guild, self.guild_cmd_roles, self.invoker_id)

            embed = view.build_embed()

            await interaction.response.edit_message(embed=embed, view=view)

            return

        if cat_name == "🔒  Jail Request Channel":

            view  = JailReqChannelConfigView(interaction.guild, self.guild_cmd_roles, self.invoker_id)

            embed = view.build_embed()

            await interaction.response.edit_message(embed=embed, view=view)

            return
        if cat_name == "Unappeal Roles":

            view  = AppealRoleConfigView(interaction.guild, self.guild_cmd_roles, self.invoker_id)

            embed = view.build_embed()

            await interaction.response.edit_message(embed=embed, view=view)

            return

        if cat_name == "DWC Roles":

            view  = DWCRoleConfigView(interaction.guild, self.guild_cmd_roles, self.invoker_id)

            embed = view.build_embed()

            await interaction.response.edit_message(embed=embed, view=view)

            return

        view = CmdSelectView(cat_name, self.guild_cmd_roles, self.invoker_id)

        embed = view.build_embed()

        await interaction.response.edit_message(embed=embed, view=view)

class ChannelWhitelistSelect(discord.ui.ChannelSelect):

    def __init__(self, guild: discord.Guild, guild_cmd_roles: dict, invoker_id: int):

        self.guild           = guild

        self.guild_cmd_roles = guild_cmd_roles

        self.invoker_id      = invoker_id

        super().__init__(

            placeholder="Select channels to toggle on/off whitelist...",

            min_values=1, max_values=10,

            channel_types=[discord.ChannelType.text, discord.ChannelType.voice],

        )

    async def callback(self, interaction: discord.Interaction):

        data      = load_data()

        gid       = str(self.guild.id)

        whitelist = data.setdefault("channel_whitelist", {}).setdefault(gid, [])

        toggled_on  = []

        toggled_off = []

        for ch in self.values:

            if ch.id in whitelist:

                whitelist.remove(ch.id)

                toggled_off.append(ch.mention)

            else:

                whitelist.append(ch.id)

                toggled_on.append(ch.mention)

        save_data(data)

        lines = []

        if toggled_on:

            lines.append(f"✅ Added to whitelist: {', '.join(toggled_on)}")

        if toggled_off:

            lines.append(f"❌ Removed from whitelist: {', '.join(toggled_off)}")

        result_embed_wl = discord.Embed(

            title="🚫 Channel Whitelist Updated",

            description="\n".join(lines),

            color=discord.Color.green(),

            timestamp=datetime.now(timezone.utc),

        )

        current = data["channel_whitelist"].get(gid, [])

        if current:

            ch_lines = []

            for cid in current:

                ch = self.guild.get_channel(cid)

                ch_lines.append(f"🚫 {ch.mention if ch else f'`#{cid}`'}")

            result_embed_wl.add_field(name="Current Whitelist", value="\n".join(ch_lines), inline=False)

        else:

            result_embed_wl.add_field(name="Current Whitelist", value="*Empty — all new channels get permission overrides*", inline=False)

        result_embed_wl.set_footer(text="Changes take effect immediately for future channels.")

        back_view = discord.ui.View(timeout=300)

        back_view.add_item(BackToWhitelistButton(self.guild, self.guild_cmd_roles, self.invoker_id))

        back_view.add_item(BackToCategoryButton(self.guild_cmd_roles, self.invoker_id))

        await interaction.response.edit_message(embed=result_embed_wl, view=back_view)


class BackToWhitelistButton(discord.ui.Button):

    def __init__(self, guild: discord.Guild, guild_cmd_roles: dict, invoker_id: int):

        super().__init__(label="← Back to Whitelist", style=discord.ButtonStyle.secondary)

        self.guild           = guild

        self.guild_cmd_roles = guild_cmd_roles

        self.invoker_id      = invoker_id

    async def callback(self, interaction: discord.Interaction):

        if interaction.user.id != self.invoker_id:

            return await interaction.response.send_message(

                embed=error_embed("Only the original user can navigate."), ephemeral=True)

        view  = ChannelWhitelistView(self.guild, self.guild_cmd_roles, self.invoker_id)

        embed = view.build_embed()

        await interaction.response.edit_message(embed=embed, view=view)


class ChannelWhitelistView(discord.ui.View):

    def __init__(self, guild: discord.Guild, guild_cmd_roles: dict, invoker_id: int):

        super().__init__(timeout=300)

        self.guild           = guild

        self.guild_cmd_roles = guild_cmd_roles

        self.invoker_id      = invoker_id

        self.add_item(ChannelWhitelistSelect(guild, guild_cmd_roles, invoker_id))

        self.add_item(BackToCategoryButton(guild_cmd_roles, invoker_id))

    def build_embed(self) -> discord.Embed:

        data      = load_data()

        whitelist = data.get("channel_whitelist", {}).get(str(self.guild.id), [])

        embed = discord.Embed(

            title="🚫  Channel Whitelist",

            description=(

                "Channels in this list will **not** receive automatic permission overrides\n"

                "when new channels are created (after `.setup` has been run).\n\n"

                "**Select channels below to add or remove them from the whitelist.**\n"

                "Selecting a channel already in the list will remove it."

            ),

            color=discord.Color.blurple(),

            timestamp=datetime.now(timezone.utc),

        )

        if whitelist:

            lines = []

            for cid in whitelist:

                ch = self.guild.get_channel(cid)

                lines.append(f"🚫 {ch.mention if ch else f'`#{cid}`'}")

            embed.add_field(name="Currently Whitelisted", value="\n".join(lines), inline=False)

        else:

            embed.add_field(name="Currently Whitelisted", value="*None — all new channels receive permission overrides*", inline=False)

        embed.set_footer(text="Admins and the bot owner can always use any command.")

        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:

        if interaction.user.id != self.invoker_id:

            await interaction.response.send_message(

                embed=error_embed("Only the original user can use this."), ephemeral=True)

            return False

        return True


class JailReqChannelConfigSelect(discord.ui.ChannelSelect):

    def __init__(self, guild: discord.Guild, guild_cmd_roles: dict, invoker_id: int):

        self.guild           = guild

        self.guild_cmd_roles = guild_cmd_roles

        self.invoker_id      = invoker_id

        super().__init__(

            placeholder="Choose the jail request channel...",

            min_values=1, max_values=1,

            channel_types=[discord.ChannelType.text],

        )

    async def callback(self, interaction: discord.Interaction):

        channel = self.values[0]

        set_jail_request_channel(self.guild.id, channel.id)

        view  = JailReqChannelConfigView(self.guild, self.guild_cmd_roles, self.invoker_id)

        embed = view.build_embed(updated_to=channel)

        await interaction.response.edit_message(embed=embed, view=view)

class JailReqChannelConfigView(discord.ui.View):

    def __init__(self, guild: discord.Guild, guild_cmd_roles: dict, invoker_id: int):

        super().__init__(timeout=300)

        self.guild           = guild

        self.guild_cmd_roles = guild_cmd_roles

        self.invoker_id      = invoker_id

        self.add_item(JailReqChannelConfigSelect(guild, guild_cmd_roles, invoker_id))

        self.add_item(BackToCategoryButton(guild_cmd_roles, invoker_id))

    def build_embed(self, updated_to: discord.TextChannel = None) -> discord.Embed:

        data     = load_data()

        jreq_cid = data.get("jail_request_channel", {}).get(str(self.guild.id))

        embed = discord.Embed(

            title="🔒  Jail Request Channel",

            description=(

                "This is where `.jreq` / `/jailreq` requests get posted, along with\n"

                "a thread so mods can discuss and action them.\n\n"

                "**Select a channel below to set or change it.**"

            ),

            color=discord.Color.blurple(),

            timestamp=datetime.now(timezone.utc),

        )

        if updated_to:

            embed.add_field(name="✅ Updated", value=f"Jail requests will now be posted to {updated_to.mention}.", inline=False)

        elif jreq_cid:

            ch = self.guild.get_channel(jreq_cid)

            embed.add_field(name="Current Channel", value=ch.mention if ch else f"`#{jreq_cid}`", inline=False)

        else:

            embed.add_field(name="Current Channel", value=f"*Not set — defaults to `#{JAIL_REQUEST_CHANNEL_NAME}`*", inline=False)

        embed.set_footer(text="Admins and the bot owner can always use any command.")

        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:

        if interaction.user.id != self.invoker_id:

            await interaction.response.send_message(
                embed=error_embed("Only the original user can use this."), ephemeral=True)

            return False

        return True


class AppealRoleConfigSelect(discord.ui.RoleSelect):

    def __init__(self, guild: discord.Guild, guild_cmd_roles: dict, invoker_id: int):

        self.guild           = guild

        self.guild_cmd_roles = guild_cmd_roles

        self.invoker_id      = invoker_id

        super().__init__(

            placeholder="Choose the special role for appeal rejections...",

            min_values=1, max_values=1,

        )

    async def callback(self, interaction: discord.Interaction):

        role = self.values[0]

        set_appeal_role_id(self.guild.id, role.id)

        view  = AppealRoleConfigView(self.guild, self.guild_cmd_roles, self.invoker_id)

        embed = view.build_embed(updated_to=role)

        await interaction.response.edit_message(embed=embed, view=view)


class AppealRoleConfigView(discord.ui.View):

    def __init__(self, guild: discord.Guild, guild_cmd_roles: dict, invoker_id: int):

        super().__init__(timeout=300)

        self.guild           = guild

        self.guild_cmd_roles = guild_cmd_roles

        self.invoker_id      = invoker_id

        self.add_item(AppealRoleConfigSelect(guild, guild_cmd_roles, invoker_id))

        self.add_item(BackToCategoryButton(guild_cmd_roles, invoker_id))

    def build_embed(self, updated_to: discord.Role = None) -> discord.Embed:

        data = load_data()

        role_id = data.get("appeal_roles", {}).get(str(self.guild.id))

        role = self.guild.get_role(role_id) if role_id else None

        embed = discord.Embed(

            title="🎯  Unappeal Roles",

            description=(

                "Choose the role the bot should give when you run `."
                "rappeal` or `/rejected appeal`.\n\n"
                "**Select a role below to set or change it.**"

            ),

            color=discord.Color.blurple(),

            timestamp=datetime.now(timezone.utc),

        )

        if updated_to:

            embed.add_field(name="✅ Updated", value=f"Appeal rejection role is now {updated_to.mention}.", inline=False)

        elif role:

            embed.add_field(name="Current Role", value=role.mention, inline=False)

        else:

            embed.add_field(name="Current Role", value="*Not set yet*", inline=False)

        embed.set_footer(text="Admins and the bot owner can always use any command.")

        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:

        if interaction.user.id != self.invoker_id:

            await interaction.response.send_message(
                embed=error_embed("Only the original user can use this."), ephemeral=True)

            return False

        return True


class DWCRoleConfigSelect(discord.ui.RoleSelect):

    def __init__(self, guild: discord.Guild, guild_cmd_roles: dict, invoker_id: int):

        self.guild           = guild

        self.guild_cmd_roles = guild_cmd_roles

        self.invoker_id      = invoker_id

        super().__init__(

            placeholder="Choose the special role for .dwc...",

            min_values=1, max_values=1,

        )

    async def callback(self, interaction: discord.Interaction):

        role = self.values[0]

        set_dwc_role_id(self.guild.id, role.id)

        view  = DWCRoleConfigView(self.guild, self.guild_cmd_roles, self.invoker_id)

        embed = view.build_embed(updated_to=role)

        await interaction.response.edit_message(embed=embed, view=view)


class DWCRoleConfigView(discord.ui.View):

    def __init__(self, guild: discord.Guild, guild_cmd_roles: dict, invoker_id: int):

        super().__init__(timeout=300)

        self.guild           = guild

        self.guild_cmd_roles = guild_cmd_roles

        self.invoker_id      = invoker_id

        self.add_item(DWCRoleConfigSelect(guild, guild_cmd_roles, invoker_id))

        self.add_item(BackToCategoryButton(guild_cmd_roles, invoker_id))

    def build_embed(self, updated_to: discord.Role = None) -> discord.Embed:

        data = load_data()

        role_id = data.get("dwc_roles", {}).get(str(self.guild.id))

        role = self.guild.get_role(role_id) if role_id else None

        embed = discord.Embed(

            title="🏷️  DWC Roles",

            description=(

                "Choose the role the bot should give when you run `.dwc` or `/dwc`.\n\n"

                "**Select a role below to set or change it.**"

            ),

            color=discord.Color.blurple(),

            timestamp=datetime.now(timezone.utc),

        )

        if updated_to:

            embed.add_field(name="✅ Updated", value=f"DWC role is now {updated_to.mention}.", inline=False)

        elif role:

            embed.add_field(name="Current Role", value=role.mention, inline=False)

        else:

            embed.add_field(name="Current Role", value="*Not set yet*", inline=False)

        embed.set_footer(text="Admins and the bot owner can always use any command.")

        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:

        if interaction.user.id != self.invoker_id:

            await interaction.response.send_message(
                embed=error_embed("Only the original user can use this."), ephemeral=True)

            return False

        return True

class CategorySelectView(discord.ui.View):

    def __init__(self, guild_cmd_roles: dict, invoker_id: int):

        super().__init__(timeout=300)

        self.invoker_id = invoker_id

        self.add_item(CategorySelectDropdown(guild_cmd_roles, invoker_id))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:

        if interaction.user.id != self.invoker_id:

            await interaction.response.send_message(

                embed=error_embed("Only the person who ran `.setup perms` can use this."),

                ephemeral=True)

            return False

        return True

class CmdSelectDropdown(discord.ui.Select):

    def __init__(self, cat_name: str, guild_cmd_roles: dict, invoker_id: int):

        self.cat_name        = cat_name

        self.guild_cmd_roles = guild_cmd_roles

        self.invoker_id      = invoker_id

        cmds = PERM_CATEGORIES[cat_name]["commands"]

        options = [

            discord.SelectOption(

                label=f".{cmd}",

                value=cmd,

                emoji=COMMAND_EMOJIS.get(cmd, "📌"),

                description=f"Currently: {', '.join(guild_cmd_roles.get(cmd, [])) or 'Everyone'}",

            )

            for cmd in cmds

        ]

        super().__init__(

            placeholder="Select a command to configure...",

            min_values=1, max_values=1,

            options=options,

        )

    async def callback(self, interaction: discord.Interaction):

        cmd  = self.values[0]

        view = RoleEditView(interaction.guild, cmd, self.cat_name, self.guild_cmd_roles, self.invoker_id)

        await interaction.response.edit_message(embed=view.build_embed(), view=view)

class CmdSelectView(discord.ui.View):

    def __init__(self, cat_name: str, guild_cmd_roles: dict, invoker_id: int):

        super().__init__(timeout=300)

        self.cat_name        = cat_name

        self.guild_cmd_roles = guild_cmd_roles

        self.invoker_id      = invoker_id

        self.add_item(CmdSelectDropdown(cat_name, guild_cmd_roles, invoker_id))

        self.add_item(BackToCategoryButton(guild_cmd_roles, invoker_id))

    def build_embed(self) -> discord.Embed:

        cat_data = PERM_CATEGORIES[self.cat_name]

        cmds     = cat_data["commands"]

        lines    = []

        for cmd in cmds:

            roles     = self.guild_cmd_roles.get(cmd, [])

            emoji     = COMMAND_EMOJIS.get(cmd, "📌")

            role_text = ", ".join(f"`{r}`" for r in roles) if roles else "*Everyone*"

            lines.append(f"{emoji} `.{cmd}` → {role_text}")

        embed = discord.Embed(

            title=f"{cat_data['emoji']}  {self.cat_name} — Commands",

            description="Select a command below to change its role requirements.",

            color=discord.Color.blurple(),

            timestamp=datetime.now(timezone.utc),

        )

        embed.add_field(name="Current Permissions", value="\n".join(lines) if lines else "*No commands in this category*", inline=False)

        embed.set_footer(text="Admins and the bot owner can always use any command.")

        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:

        if interaction.user.id != self.invoker_id:

            await interaction.response.send_message(

                embed=error_embed("Only the original user can use this."), ephemeral=True)

            return False

        return True

class BackToCategoryButton(discord.ui.Button):

    def __init__(self, guild_cmd_roles: dict, invoker_id: int):

        super().__init__(label="← Back to categories", style=discord.ButtonStyle.secondary)

        self.guild_cmd_roles = guild_cmd_roles

        self.invoker_id      = invoker_id

    async def callback(self, interaction: discord.Interaction):

        if interaction.user.id != self.invoker_id:

            return await interaction.response.send_message(

                embed=error_embed("Only the original user can navigate."), ephemeral=True)

        embed = _perms_main_embed(interaction.guild, self.guild_cmd_roles)

        view  = CategorySelectView(self.guild_cmd_roles, self.invoker_id)

        await interaction.response.edit_message(embed=embed, view=view)

class RoleSelectDropdown(discord.ui.RoleSelect):

    def __init__(self, cmd: str, cat_name: str, guild_cmd_roles: dict, invoker_id: int):

        self.cmd             = cmd

        self.cat_name        = cat_name

        self.guild_cmd_roles = guild_cmd_roles

        self.invoker_id      = invoker_id

        super().__init__(

            placeholder=f"Select roles that can use .{cmd} (none = everyone)...",

            min_values=0, max_values=10,

        )

    async def callback(self, interaction: discord.Interaction):

        selected_role_names       = [r.name for r in self.values]

        self.guild_cmd_roles[self.cmd] = selected_role_names

        save_guild_cmd_roles(interaction.guild.id, self.guild_cmd_roles)

        role_display = (

            ", ".join(f"`{r}`" for r in selected_role_names)

            if selected_role_names else "**Everyone** (no restriction)"

        )

        embed = discord.Embed(

            title=f"✅ Permissions Updated — `.{self.cmd}`",

            description=f"Allowed roles: {role_display}",

            color=discord.Color.green(),

            timestamp=datetime.now(timezone.utc),

        )

        embed.set_footer(text="Changes are saved and take effect immediately.")

        back_view = discord.ui.View(timeout=300)

        back_view.add_item(BackToCmdListButton(self.cat_name, self.guild_cmd_roles, self.invoker_id))

        back_view.add_item(BackToCategoryButton(self.guild_cmd_roles, self.invoker_id))

        await interaction.response.edit_message(embed=embed, view=back_view)

class BackToCmdListButton(discord.ui.Button):

    def __init__(self, cat_name: str, guild_cmd_roles: dict, invoker_id: int):

        super().__init__(label="← Back to command list", style=discord.ButtonStyle.secondary)

        self.cat_name        = cat_name

        self.guild_cmd_roles = guild_cmd_roles

        self.invoker_id      = invoker_id

    async def callback(self, interaction: discord.Interaction):

        if interaction.user.id != self.invoker_id:

            return await interaction.response.send_message(

                embed=error_embed("Only the original user can navigate."), ephemeral=True)

        view  = CmdSelectView(self.cat_name, self.guild_cmd_roles, self.invoker_id)

        embed = view.build_embed()

        await interaction.response.edit_message(embed=embed, view=view)

class ClearRolesButton(discord.ui.Button):

    def __init__(self, cmd: str, cat_name: str, guild_cmd_roles: dict, invoker_id: int):

        super().__init__(label="Clear (allow everyone)", style=discord.ButtonStyle.danger, emoji="🔓")

        self.cmd             = cmd

        self.cat_name        = cat_name

        self.guild_cmd_roles = guild_cmd_roles

        self.invoker_id      = invoker_id

    async def callback(self, interaction: discord.Interaction):

        if interaction.user.id != self.invoker_id:

            return await interaction.response.send_message(

                embed=error_embed("Only the original user can do this."), ephemeral=True)

        self.guild_cmd_roles[self.cmd] = []

        save_guild_cmd_roles(interaction.guild.id, self.guild_cmd_roles)

        embed = discord.Embed(

            title=f"🔓 Restrictions Cleared — `.{self.cmd}`",

            description=f"`.{self.cmd}` is now usable by **everyone**.",

            color=discord.Color.green(), timestamp=datetime.now(timezone.utc))

        back_view = discord.ui.View(timeout=300)

        back_view.add_item(BackToCmdListButton(self.cat_name, self.guild_cmd_roles, self.invoker_id))

        back_view.add_item(BackToCategoryButton(self.guild_cmd_roles, self.invoker_id))

        await interaction.response.edit_message(embed=embed, view=back_view)

class RoleEditView(discord.ui.View):

    def __init__(self, guild: discord.Guild, cmd: str, cat_name: str,

                 guild_cmd_roles: dict, invoker_id: int):

        super().__init__(timeout=300)

        self.guild           = guild

        self.cmd             = cmd

        self.cat_name        = cat_name

        self.guild_cmd_roles = guild_cmd_roles

        self.invoker_id      = invoker_id

        self.add_item(RoleSelectDropdown(cmd, cat_name, guild_cmd_roles, invoker_id))

        self.add_item(ClearRolesButton(cmd, cat_name, guild_cmd_roles, invoker_id))

        self.add_item(BackToCmdListButton(cat_name, guild_cmd_roles, invoker_id))

        self.add_item(BackToCategoryButton(guild_cmd_roles, invoker_id))

    def build_embed(self) -> discord.Embed:

        current      = self.guild_cmd_roles.get(self.cmd, [])

        role_display = (

            "\n".join(f"• `{r}`" for r in current)

            if current else "**Everyone** (no restriction)"

        )

        emoji = COMMAND_EMOJIS.get(self.cmd, "📌")

        embed = discord.Embed(

            title=f"{emoji}  Configure `.{self.cmd}`",

            description=(

                f"**Current allowed roles:**\n{role_display}\n\n"

                f"Use the dropdown below to pick which roles can run `.{self.cmd}`.\n"

                f"Select **no roles** to allow everyone."

            ),

            color=discord.Color.blurple(), timestamp=datetime.now(timezone.utc))

        embed.set_footer(text="Admins and the bot owner can always use any command.")

        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:

        if interaction.user.id != self.invoker_id:

            await interaction.response.send_message(

                embed=error_embed("Only the original user can use this."), ephemeral=True)

            return False

        return True

# ══════════════════════════════════════════════════════════════════════════════

#  BOT EVENTS

# ══════════════════════════════════════════════════════════════════════════════

@bot.event

async def on_ready():

    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

    try:

        synced = await bot.tree.sync()

        print(f"Synced {len(synced)} slash command(s)")

    except Exception as e:

        print(f"Slash sync failed: {e}")

    data = load_data()

    for gid_str, roles in data.get("cmd_roles", {}).items():

        _guild_cmd_roles[int(gid_str)] = roles

    now = datetime.now(timezone.utc)

    for key, iso in list(data["mute_timers"].items()):

        guild_id, user_id = map(int, key.split(":"))

        expiry    = datetime.fromisoformat(iso)

        remaining = (expiry - now).total_seconds()

        if remaining <= 0:

            asyncio.create_task(schedule_unmute(guild_id, user_id, 0))

        else:

            asyncio.create_task(schedule_unmute(guild_id, user_id, remaining))

    for gid_str, gmap in data.get("giveaways", {}).items():

        for mid_str, g in gmap.items():

            if not g.get("ended"):

                total = _get_entry_count(g, None)

                view  = GiveawayView(int(mid_str), total)

                bot.add_view(view, message_id=int(mid_str))

    asyncio.create_task(giveaway_watcher())

@bot.event

async def on_guild_channel_create(channel: discord.abc.GuildChannel):

    data = load_data()

    gid  = str(channel.guild.id)

    if not data.get("setup_done", {}).get(gid):

        return

    whitelist = data.get("channel_whitelist", {}).get(gid, [])

    if channel.id in whitelist:

        return

    guild = channel.guild

    muted_role  = discord.utils.get(guild.roles, name=MUTED_ROLE_NAME)

    jailed_role = discord.utils.get(guild.roles, name=JAIL_ROLE_NAME)

    if isinstance(channel, discord.TextChannel):

        if muted_role:

            try:

                await channel.set_permissions(

                    muted_role,

                    send_messages=False, add_reactions=False,

                    create_public_threads=False, create_private_threads=False,

                    send_messages_in_threads=False,

                    reason="Auto-setup: Muted role permissions on new channel")

            except discord.Forbidden:

                pass

        if jailed_role:

            try:

                await channel.set_permissions(jailed_role, view_channel=False,

                                               reason="Auto-setup: Jailed role permissions on new channel")

            except discord.Forbidden:

                pass

    elif isinstance(channel, discord.VoiceChannel):

        if jailed_role:

            try:

                await channel.set_permissions(jailed_role, view_channel=False,

                                               reason="Auto-setup: Jailed role permissions on new channel")

            except discord.Forbidden:

                pass

@bot.event

async def on_member_join(member: discord.Member):

    data       = load_data()

    persistent = get_persistent_roles(data, member.guild.id, member.id)

    if not persistent:

        return

    roles_to_add = []

    for role_name in persistent:

        role = discord.utils.get(member.guild.roles, name=role_name)

        if role:

            roles_to_add.append(role)

    if roles_to_add:

        try:

            await member.add_roles(*roles_to_add, reason="Persistent role restored on rejoin")

        except discord.Forbidden:

            pass

        if any(r.name == MUTED_ROLE_NAME for r in roles_to_add):

            expiry = get_mute_expiry(data, member.guild.id, member.id)

            if expiry:

                remaining = (expiry - datetime.now(timezone.utc)).total_seconds()

                asyncio.create_task(schedule_unmute(member.guild.id, member.id, max(remaining, 0)))

# ══════════════════════════════════════════════════════════════════════════════

#  AUTOMOD

# ══════════════════════════════════════════════════════════════════════════════

@bot.event

async def on_message(message: discord.Message):

    if not message.guild or message.author.bot:

        return await bot.process_commands(message)

    data = load_data()
    increment_message_stat(data, message.guild.id, message.author.id, message.created_at)
    save_data(data)

    if message.author.id in afk_users and not message.content.startswith(f"{PREFIX}afk"):

        afk_data = afk_users.pop(message.author.id)

        since    = afk_data["since"]

        elapsed  = datetime.now(timezone.utc) - since

        mins     = int(elapsed.total_seconds() // 60)

        time_str = f"{mins} minute(s)" if mins > 0 else "just now"

        pings    = afk_data.get("pings", [])

        embed    = discord.Embed(

            title="👋 Welcome Back!",

            description=f"I removed your AFK. You were away for **{time_str}**.",

            color=discord.Color.green(), timestamp=datetime.now(timezone.utc))

        if pings:

            ping_lines = []

            for p in pings[-10:]:

                ping_lines.append(f"[{p['author']} — <t:{p['ts']}:R>]({p['jump_url']})")

            embed.add_field(

                name=f"🔔 You were pinged {len(pings)} time(s)",

                value="\n".join(ping_lines), inline=False)

            if len(pings) > 10:

                embed.set_footer(text=f"Showing last 10 of {len(pings)} pings.")

        await message.reply(embed=embed, mention_author=False, delete_after=30)

    if message.mentions:

        for mentioned in message.mentions:

            if mentioned.id in afk_users and mentioned.id != message.author.id:

                afk_info = afk_users[mentioned.id]

                since    = afk_info["since"]

                ts       = int(since.timestamp())

                afk_info.setdefault("pings", []).append({

                    "author":   str(message.author),

                    "ts":       int(message.created_at.timestamp()),

                    "jump_url": message.jump_url

                })

                await message.reply(

                    embed=info_embed(

                        f"**{mentioned.display_name}** is AFK since <t:{ts}:R>\n"

                        f"📝 Reason: {afk_info['reason']}"),

                    mention_author=False, delete_after=15)

    if message.attachments and _pending_proof:

        ref_id   = message.reference.message_id if message.reference else None

        proof_id = None

        proof    = None

        if ref_id and ref_id in _pending_proof:

            proof_id = ref_id

            proof    = _pending_proof.get(ref_id)

        else:

            for pending_id, pending in _pending_proof.items():

                if pending.get("channel_id") == message.channel.id and pending.get("mod_id") == message.author.id:

                    proof_id = pending_id

                    proof    = pending

                    break

        if proof and not proof["view"]._logged:

            _pending_proof.pop(proof_id, None)

            proof["view"]._logged = True

            attachment_urls = "\n".join(a.url for a in message.attachments)

            await post_mod_log(

                proof["guild"], proof["case_id"], proof["action"],

                proof["moderator"], proof["target"], proof["reason"],

                extra=proof["extra"],

                proof_url=attachment_urls if len(message.attachments) == 1 else None,

                proof_note=(

                    "\n".join(a.url for a in message.attachments)

                    if len(message.attachments) > 1 else None

                ),

            )

            await message.add_reaction("✅")

            await message.reply(

                embed=success_embed(

                    f"**{len(message.attachments)}** attachment(s) logged for "

                    f"**Case #{proof['case_id']}** in `#{MOD_LOG_CHANNEL_NAME}`."),

                mention_author=False)

            return await bot.process_commands(message)

    if await handle_jreq_proof_reply(message):

        return await bot.process_commands(message)

    if is_any_mod(message.author):

        return await bot.process_commands(message)

    if AUTOMOD_ENABLED:

        content = message.content

        uid     = message.author.id

        now     = datetime.now(timezone.utc).timestamp()

        lower   = content.lower()

        for word in BLOCKED_WORDS:

            if word.lower() in lower:

                await message.delete()

                await automod_log(message, "Blocked word detected", f"Word: `{word}`")

                return

        if BLOCKED_LINKS and re.search(r"https?://|discord\.gg/", content):

            if not any(w in content for w in WHITELISTED_LINKS):

                await message.delete()

                await automod_log(message, "Unauthorized link", content[:100])

                return

        if len(content) >= MIN_CAPS_LENGTH:

            alpha = [c for c in content if c.isalpha()]

            if alpha and (sum(1 for c in alpha if c.isupper()) / len(alpha)) * 100 >= CAPS_PERCENT:

                await message.delete()

                await automod_log(message, "Excessive caps", content[:100])

                return

        spam_tracker.setdefault(uid, [])

        spam_tracker[uid] = [t for t in spam_tracker[uid] if now - t < 5]

        spam_tracker[uid].append(now)

        if len(spam_tracker[uid]) >= SPAM_THRESHOLD:

            spam_tracker[uid] = []

            try:

                await message.channel.purge(limit=SPAM_THRESHOLD, check=lambda m: m.author.id == uid)

            except discord.Forbidden:

                pass

            await automod_log(message, "Spam detected", f"{SPAM_THRESHOLD} messages in 5 seconds")

            return

    await bot.process_commands(message)

async def automod_log(message, reason, detail):

    channel = discord.utils.get(message.guild.text_channels, name=AUTO_LOG_CHANNEL_NAME)

    if not channel: return

    embed = discord.Embed(

        title="🤖  Automod Action",

        description="─────────────────────────────────",

        color=discord.Color.purple(), timestamp=datetime.now(timezone.utc))

    embed.add_field(name="👤 User",    value=f"{message.author}\n`{message.author.id}`", inline=True)

    embed.add_field(name="📢 Channel", value=message.channel.mention,                    inline=True)

    embed.add_field(name="\u200b",     value="\u200b",                                   inline=True)

    embed.add_field(name="⚠️ Reason",  value=reason,                                     inline=False)

    embed.add_field(name="🔍 Detail",  value=detail or "—",                              inline=False)

    embed.set_thumbnail(url=message.author.display_avatar.url)

    embed.set_footer(text="Automod")

    await channel.send(embed=embed)

# ══════════════════════════════════════════════════════════════════════════════

#  SETUP COMMAND

# ══════════════════════════════════════════════════════════════════════════════

@bot.command(name="setup")

@require_setup_auth()

async def setup_cmd(ctx, subcommand: str = None):

    if subcommand and subcommand.lower() == "perms":

        guild_cmd_roles         = load_guild_cmd_roles(ctx.guild.id)

        _guild_cmd_roles[ctx.guild.id] = guild_cmd_roles

        embed = _perms_main_embed(ctx.guild, guild_cmd_roles)

        view  = CategorySelectView(guild_cmd_roles, ctx.author.id)

        return await ctx.send(embed=embed, view=view)

    guild        = ctx.guild

    status_embed = discord.Embed(

        title="⚙️ Server Setup",

        description="Starting setup… this may take a moment.",

        color=discord.Color.blurple(), timestamp=datetime.now(timezone.utc))

    msg     = await ctx.send(embed=status_embed)

    results = []

    log_channel = discord.utils.get(guild.text_channels, name=MOD_LOG_CHANNEL_NAME)

    if log_channel is None:

        try:

            await guild.create_text_channel(MOD_LOG_CHANNEL_NAME, reason="Setup: created mod-logs channel")

            results.append(f"✅ Created `#{MOD_LOG_CHANNEL_NAME}` (mod actions)")

        except discord.Forbidden:

            results.append(f"❌ Could not create `#{MOD_LOG_CHANNEL_NAME}` — missing permissions")

    else:

        results.append(f"ℹ️ `#{MOD_LOG_CHANNEL_NAME}` already exists")

    auto_channel = discord.utils.get(guild.text_channels, name=AUTO_LOG_CHANNEL_NAME)

    if auto_channel is None:

        try:

            await guild.create_text_channel(AUTO_LOG_CHANNEL_NAME, reason="Setup: created auto-logs channel")

            results.append(f"✅ Created `#{AUTO_LOG_CHANNEL_NAME}` (automated logs)")

        except discord.Forbidden:

            results.append(f"❌ Could not create `#{AUTO_LOG_CHANNEL_NAME}` — missing permissions")

    else:

        results.append(f"ℹ️ `#{AUTO_LOG_CHANNEL_NAME}` already exists")

    jreq_channel = discord.utils.get(guild.text_channels, name=JAIL_REQUEST_CHANNEL_NAME)

    if jreq_channel is None:

        try:

            await guild.create_text_channel(JAIL_REQUEST_CHANNEL_NAME, reason="Setup: created jail-requests channel")

            results.append(f"✅ Created `#{JAIL_REQUEST_CHANNEL_NAME}` (jail requests)")

        except discord.Forbidden:

            results.append(f"❌ Could not create `#{JAIL_REQUEST_CHANNEL_NAME}` — missing permissions")

    else:

        results.append(f"ℹ️ `#{JAIL_REQUEST_CHANNEL_NAME}` already exists")

    muted_role = discord.utils.get(guild.roles, name=MUTED_ROLE_NAME)

    if muted_role is None:

        try:

            muted_role = await guild.create_role(name=MUTED_ROLE_NAME, color=discord.Color.greyple(), reason="Setup: Muted role")

            results.append(f"✅ Created `{MUTED_ROLE_NAME}` role")

        except discord.Forbidden:

            results.append(f"❌ Could not create `{MUTED_ROLE_NAME}` role — missing permissions")

            muted_role = None

    else:

        results.append(f"ℹ️ `{MUTED_ROLE_NAME}` role already exists")

    if muted_role:

        ok, fail = 0, 0

        for channel in guild.text_channels:

            try:

                await channel.set_permissions(

                    muted_role,

                    send_messages=False, add_reactions=False,

                    create_public_threads=False, create_private_threads=False,

                    send_messages_in_threads=False,

                    reason="Setup: Muted role permissions")

                ok += 1

            except discord.Forbidden:

                fail += 1

        results.append(f"✅ Applied Muted perms to **{ok}** channel(s)" + (f" (failed: {fail})" if fail else ""))

    jailed_role = discord.utils.get(guild.roles, name=JAIL_ROLE_NAME)

    if jailed_role is None:

        try:

            jailed_role = await guild.create_role(name=JAIL_ROLE_NAME, color=discord.Color.dark_gray(), reason="Setup: Jailed role")

            results.append(f"✅ Created `{JAIL_ROLE_NAME}` role")

        except discord.Forbidden:

            results.append(f"❌ Could not create `{JAIL_ROLE_NAME}` role — missing permissions")

            jailed_role = None

    else:

        results.append(f"ℹ️ `{JAIL_ROLE_NAME}` role already exists")

    if jailed_role:

        ok, fail = 0, 0

        for channel in list(guild.text_channels) + list(guild.voice_channels):

            try:

                await channel.set_permissions(jailed_role, view_channel=False, reason="Setup: Jailed role permissions")

                ok += 1

            except discord.Forbidden:

                fail += 1

        results.append(f"✅ Applied Jailed perms to **{ok}** channel(s)" + (f" (failed: {fail})" if fail else ""))

    results.append("\n💡 Use `.setup perms` to configure which roles can use each command.")

    results.append("💡 Use `.setup perms` → **Channel Whitelist** to exclude channels from auto-permission overrides.")

    results.append(f"💡 Use `.setjreqchannel #channel` to choose a custom jail-request channel (defaults to `#{JAIL_REQUEST_CHANNEL_NAME}`).")

    _setup_data = load_data()

    _setup_data.setdefault("setup_done", {})[str(guild.id)] = True

    save_data(_setup_data)

    done_embed = discord.Embed(

        title="⚙️ Setup Complete",

        description="\n".join(results),

        color=discord.Color.green(), timestamp=datetime.now(timezone.utc))

    done_embed.set_footer(text=f"Requested by {ctx.author}", icon_url=ctx.author.display_avatar.url)

    await msg.edit(embed=done_embed)

@setup_cmd.error

async def setup_cmd_error(ctx, error):

    if isinstance(error, commands.CheckFailure):

        await ctx.send(embed=error_embed("You don't have permission to run `.setup`."))

# ══════════════════════════════════════════════════════════════════════════════

setup_group = app_commands.Group(name="setup", description="Server setup commands")

@setup_group.command(name="perms", description="[Setup] Configure which roles can use each command")
async def setup_perms_slash(interaction: discord.Interaction):

    if not is_setup_authorized(interaction.user):

        return await interaction.response.send_message(

            embed=error_embed("You don't have permission to run `/setup perms`."), ephemeral=True)

    guild_cmd_roles                        = load_guild_cmd_roles(interaction.guild.id)

    _guild_cmd_roles[interaction.guild.id] = guild_cmd_roles

    embed = _perms_main_embed(interaction.guild, guild_cmd_roles)

    view  = CategorySelectView(guild_cmd_roles, interaction.user.id)

    await interaction.response.send_message(embed=embed, view=view)

bot.tree.add_command(setup_group)

# ══════════════════════════════════════════════════════════════════════════════


async def apply_appeal_role(member: discord.Member, reason: str):

    role_id = get_appeal_role_id(member.guild.id)

    if not role_id:

        return False, "No appeal-rejection role has been configured yet."

    role = member.guild.get_role(role_id)

    if role is None:

        return False, "The configured appeal-rejection role no longer exists."

    if role >= member.guild.me.top_role:

        return False, "I can't manage the configured appeal-rejection role because it is above my highest role."

    try:

        if role not in member.roles:

            await member.add_roles(role, reason=reason)

        return True, role

    except discord.Forbidden:

        return False, "I don't have permission to manage the configured appeal-rejection role."


async def apply_dwc_role(member: discord.Member, reason: str):

    role_id = get_dwc_role_id(member.guild.id)

    if not role_id:

        return False, "No DWC role has been configured yet. Use `.setup perms` → **DWC Roles** to set one."

    role = member.guild.get_role(role_id)

    if role is None:

        return False, "The configured DWC role no longer exists."

    if role >= member.guild.me.top_role:

        return False, "I can't manage the configured DWC role because it is above my highest role."

    try:

        if role not in member.roles:

            await member.add_roles(role, reason=reason)

        return True, role

    except discord.Forbidden:

        return False, "I don't have permission to manage the configured DWC role."


async def remove_dwc_role(member: discord.Member, reason: str):

    role_id = get_dwc_role_id(member.guild.id)

    if not role_id:

        return False, "No DWC role has been configured yet. Use `.setup perms` → **DWC Roles** to set one."

    role = member.guild.get_role(role_id)

    if role is None:

        return False, "The configured DWC role no longer exists."

    if role >= member.guild.me.top_role:

        return False, "I can't manage the configured DWC role because it is above my highest role."

    if role not in member.roles:

        return False, f"{member.mention} doesn't have the configured DWC role."

    try:

        await member.remove_roles(role, reason=reason)

        return True, role

    except discord.Forbidden:

        return False, "I don't have permission to manage the configured DWC role."


@bot.command(name="rappeal")
@require_cmd_role("rappeal")
async def rappeal_cmd(ctx, member: discord.Member = None, *, reason="Appeal rejected"):

    if member is None:

        return await ctx.send(embed=syntax_embed("rappeal"))

    success, result = await apply_appeal_role(member, f"Appeal rejected by {ctx.author}: {reason}")

    if not success:

        return await ctx.send(embed=error_embed(result))

    role = result

    embed = discord.Embed(

        title="🎯 Appeal Rejected",

        description=f"{member.mention} was given {role.mention}.",

        color=discord.Color.orange(), timestamp=datetime.now(timezone.utc))

    embed.add_field(name="Reason", value=reason, inline=False)

    embed.set_footer(text=f"Done by {ctx.author}", icon_url=ctx.author.display_avatar.url)

    await ctx.send(embed=embed)

@rappeal_cmd.error

async def rappeal_cmd_error(ctx, error):

    if isinstance(error, commands.CheckFailure):

        await ctx.send(embed=error_embed("You don't have permission to use `.rappeal`."))


@bot.command(name="dwc")
@require_cmd_role("dwc")
async def dwc_cmd(ctx, member: discord.Member = None, *, reason="No reason provided"):

    if member is None:

        return await ctx.send(embed=syntax_embed("dwc"))

    success, result = await apply_dwc_role(member, f"DWC role given by {ctx.author}: {reason}")

    if not success:

        return await ctx.send(embed=error_embed(result))

    role = result

    data = load_data()

    cid  = add_case(data, ctx.guild.id, "DWC", ctx.author, member, reason)

    embed = discord.Embed(

        title=f"🏷️ DWC Role Given | Case #{cid}",

        description=f"{member.mention} was given {role.mention}.",

        color=discord.Color.blurple(), timestamp=datetime.now(timezone.utc))

    embed.add_field(name="Reason", value=reason, inline=False)

    embed.set_footer(text=f"Done by {ctx.author}", icon_url=ctx.author.display_avatar.url)

    view = ProofView(ctx.guild, cid, "DWC", ctx.author, member, reason)

    await ctx.send(embed=embed, view=view)

@dwc_cmd.error

async def dwc_cmd_error(ctx, error):

    if isinstance(error, commands.CheckFailure):

        await ctx.send(embed=error_embed("You don't have permission to use `.dwc`."))


@bot.command(name="rdwc")
@require_cmd_role("rdwc")
async def rdwc_cmd(ctx, member: discord.Member = None, *, reason="No reason provided"):

    if member is None:

        return await ctx.send(embed=syntax_embed("rdwc"))

    success, result = await remove_dwc_role(member, f"DWC role removed by {ctx.author}: {reason}")

    if not success:

        return await ctx.send(embed=error_embed(result))

    role = result

    data = load_data()

    cid  = add_case(data, ctx.guild.id, "Remove DWC", ctx.author, member, reason)

    embed = discord.Embed(

        title=f"🏷️ DWC Role Removed | Case #{cid}",

        description=f"{role.mention} was removed from {member.mention}.",

        color=discord.Color.green(), timestamp=datetime.now(timezone.utc))

    embed.add_field(name="Reason", value=reason, inline=False)

    embed.set_footer(text=f"Done by {ctx.author}", icon_url=ctx.author.display_avatar.url)

    view = ProofView(ctx.guild, cid, "Remove DWC", ctx.author, member, reason)

    await ctx.send(embed=embed, view=view)

@rdwc_cmd.error

async def rdwc_cmd_error(ctx, error):

    if isinstance(error, commands.CheckFailure):

        await ctx.send(embed=error_embed("You don't have permission to use `.rdwc`."))


#  PREFIX MOD COMMANDS

# ══════════════════════════════════════════════════════════════════════════════

@bot.command(name="ban")

@require_cmd_role("ban")

async def ban_cmd(ctx, member: discord.Member = None, *, reason="No reason provided"):

    if not member:

        return await ctx.send(embed=syntax_embed("ban"))

    if not hierarchy_ok(ctx.author, member):

        return await ctx.send(embed=error_embed("You cannot ban someone with an equal or higher role."))

    try:

        data = load_data()

        cid  = add_case(data, ctx.guild.id, "Ban", ctx.author, member, reason)

        await dm_member(member, "Ban", ctx.guild.name, reason, cid, moderator=ctx.author)

        await action_ban(ctx.guild, ctx.author, member, reason)

        view = ProofView(ctx.guild, cid, "Ban", ctx.author, member, reason)

        await ctx.send(embed=result_embed("Ban", member, reason, cid), view=view)

    except discord.Forbidden:

        await ctx.send(embed=error_embed("I don't have permission to ban this member."))

@bot.command(name="unban")

@require_cmd_role("unban")

async def unban_cmd(ctx, user_id: int = None):

    if not user_id:

        return await ctx.send(embed=syntax_embed("unban"))

    try:

        user = await action_unban(ctx.guild, user_id)

        data = load_data()

        cid  = add_case(data, ctx.guild.id, "Unban", ctx.author, user, "Unbanned")

        view = ProofView(ctx.guild, cid, "Unban", ctx.author, user, "Unbanned")

        await ctx.send(embed=result_embed("Unban", user, "Unbanned", cid), view=view)

    except discord.NotFound:

        await ctx.send(embed=error_embed("No ban found for that user ID."))

    except discord.Forbidden:

        await ctx.send(embed=error_embed("I don't have permission to unban."))

@bot.command(name="kick")

@require_cmd_role("kick")

async def kick_cmd(ctx, member: discord.Member = None, *, reason="No reason provided"):

    if not member:

        return await ctx.send(embed=syntax_embed("kick"))

    if not hierarchy_ok(ctx.author, member):

        return await ctx.send(embed=error_embed("You cannot kick someone with an equal or higher role."))

    try:

        data = load_data()

        cid  = add_case(data, ctx.guild.id, "Kick", ctx.author, member, reason)

        await dm_member(member, "Kick", ctx.guild.name, reason, cid, moderator=ctx.author)

        await action_kick(member, reason)

        view = ProofView(ctx.guild, cid, "Kick", ctx.author, member, reason)

        await ctx.send(embed=result_embed("Kick", member, reason, cid), view=view)

    except discord.Forbidden:

        await ctx.send(embed=error_embed("I don't have permission to kick this member."))

@bot.command(name="mute")

@require_cmd_role("mute")

async def mute_cmd(ctx, member: discord.Member = None, *, args: str = ""):

    if not member:

        return await ctx.send(embed=syntax_embed("mute"))

    if not hierarchy_ok(ctx.author, member):

        return await ctx.send(embed=error_embed("You cannot mute someone with an equal or higher role."))

    secs, rest = split_duration_and_reason(args)

    if secs is not None:

        reason = rest if rest else "No reason provided"

    else:

        secs   = DEFAULT_MUTE_SECONDS

        reason = args if args else "No reason provided"

    embed, err, view = await do_mute(ctx.guild, ctx.author, member, reason, secs)

    if err:

        return await ctx.send(embed=err)

    await ctx.send(embed=embed, view=view)

@bot.command(name="unmute")

@require_cmd_role("unmute")

async def unmute_cmd(ctx, member: discord.Member = None):

    if not member:

        return await ctx.send(embed=syntax_embed("unmute"))

    ok = await action_unmute(ctx.guild, member)

    if not ok:

        return await ctx.send(embed=warn_embed(f"**{member.display_name}** is not muted."))

    data = load_data()

    cid  = add_case(data, ctx.guild.id, "Unmute", ctx.author, member, "Manually unmuted")

    remove_persistent_role(data, ctx.guild.id, member.id, MUTED_ROLE_NAME)

    clear_mute_timer(data, ctx.guild.id, member.id)

    await dm_member(member, "Unmute", ctx.guild.name, "Manually unmuted by a moderator.", cid, moderator=ctx.author)

    view = ProofView(ctx.guild, cid, "Unmute", ctx.author, member, "Manually unmuted")

    await ctx.send(embed=result_embed("Unmute", member, "Manually unmuted", cid), view=view)

@bot.command(name="warn")

@require_cmd_role("warn")

async def warn_cmd(ctx, member: discord.Member = None, *, reason="No reason provided"):

    if not member:

        return await ctx.send(embed=syntax_embed("warn"))

    if not hierarchy_ok(ctx.author, member):

        return await ctx.send(embed=error_embed("You cannot warn someone with an equal or higher role."))

    data     = load_data()

    warn_num = add_warn(data, ctx.guild.id, member.id, ctx.author, reason)

    cid      = add_case(data, ctx.guild.id, "Warn", ctx.author, member, reason)

    await dm_member(member, "Warn", ctx.guild.name, reason, cid,

                    moderator=ctx.author, extra=f"This is warning **#{warn_num}** on your record.")

    view = ProofView(ctx.guild, cid, "Warn", ctx.author, member, reason, extra=f"Warn #{warn_num}")

    await ctx.send(embed=result_embed("Warn", member, reason, cid,

                                      extra=f"This is warn **#{warn_num}** for this user."), view=view)

@bot.command(name="unwarn")

@require_cmd_role("unwarn")

async def unwarn_cmd(ctx, member: discord.Member = None, warn_number: int = None):

    if not member or warn_number is None:

        return await ctx.send(embed=syntax_embed("unwarn"))

    data = load_data()

    ok   = remove_warn(data, ctx.guild.id, member.id, warn_number)

    if not ok:

        return await ctx.send(embed=error_embed(f"Warn #{warn_number} not found for **{member.display_name}**."))

    cid = add_case(data, ctx.guild.id, "Unwarn", ctx.author, member, f"Removed warn #{warn_number}")

    await dm_member(member, "Unwarn", ctx.guild.name,

                    f"Warn #{warn_number} has been removed from your record.", cid, moderator=ctx.author)

    view = ProofView(ctx.guild, cid, "Unwarn", ctx.author, member, f"Removed warn #{warn_number}")

    await ctx.send(embed=result_embed("Unwarn", member, f"Removed warn #{warn_number}", cid), view=view)

@bot.command(name="warnings")

@require_cmd_role("warnings")

async def warnings_cmd(ctx, member: discord.Member = None):

    if not member:

        return await ctx.send(embed=syntax_embed("warnings"))

    data  = load_data()

    warns = get_warns(data, ctx.guild.id, member.id)

    embed = discord.Embed(title=f"⚠️ Warnings for {member}", color=discord.Color.yellow(),

                          timestamp=datetime.now(timezone.utc))

    embed.set_thumbnail(url=member.display_avatar.url)

    if not warns:

        embed.description = "This user has no warnings."

    else:

        for w in warns:

            embed.add_field(

                name=f"Warn #{w['number']} — {w['timestamp'][:10]}",

                value=f"**Reason:** {w['reason']}\n**By:** {w['mod_tag']}", inline=False)

    await ctx.send(embed=embed)

@bot.command(name="modlogs")

@require_cmd_role("modlogs")

async def modlogs_cmd(ctx, member: discord.Member = None):

    if not member:

        return await ctx.send(embed=syntax_embed("modlogs"))

    data  = load_data()

    cases = get_user_cases(data, ctx.guild.id, member.id)

    if not cases:

        embed = discord.Embed(title=f"📋 Mod Logs — {member}", color=discord.Color.blurple(),

                              timestamp=datetime.now(timezone.utc))

        embed.set_thumbnail(url=member.display_avatar.url)

        embed.description = "No moderation history found for this user."

        return await ctx.send(embed=embed)

    view = ModlogsView(member, cases, ctx.author.id)

    await ctx.send(embed=view.current_embed(), view=view)

@bot.command(name="w", aliases=["whois", "userinfo", "memberinfo"])

@require_cmd_role("w")

async def whois_cmd(ctx, *, target: str = None):

    member = None

    if target is None or not target.strip():

        member = ctx.guild.get_member(ctx.author.id) or await ctx.guild.fetch_member(ctx.author.id)

    elif ctx.message.mentions:

        member = ctx.message.mentions[0]

    else:

        raw      = target.strip()

        id_match = re.fullmatch(r"<@!?(\d+)>|\d+", raw)

        if id_match:

            user_id = int(id_match.group(1) or raw)

            member  = ctx.guild.get_member(user_id)

            if member is None:

                try:

                    member = await ctx.guild.fetch_member(user_id)

                except (discord.NotFound, discord.Forbidden, discord.HTTPException):

                    member = None

        if member is None:

            return await ctx.send(embed=error_embed(

                "Could not find that member. Use `.w`, `.w @user`, or `.w user_id`."))

    roles      = [role.mention for role in member.roles if role != ctx.guild.default_role]

    roles_text = ", ".join(reversed(roles)) if roles else "No roles"

    if len(roles_text) > 1024:

        roles_text = roles_text[:1020] + "..."

    created_ts = int(member.created_at.replace(tzinfo=timezone.utc).timestamp())

    joined_ts  = int(member.joined_at.replace(tzinfo=timezone.utc).timestamp()) if member.joined_at else None

    _comm_disabled = getattr(member, 'communication_disabled_until', None)

    timeout_ts = (int(_comm_disabled.replace(tzinfo=timezone.utc).timestamp()) if _comm_disabled else None)

    data         = load_data()

    user_cases   = get_user_cases(data, ctx.guild.id, member.id)

    user_warns   = get_warns(data, ctx.guild.id, member.id)

    total_cases  = len(user_cases)

    total_warns  = len(user_warns)

    action_counts: dict[str, int] = {}

    for c in user_cases:

        action_counts[c["action"]] = action_counts.get(c["action"], 0) + 1

    case_summary = (

        "  ".join(f"{ACTION_ICONS.get(a, '📋')} {a}: **{n}**" for a, n in sorted(action_counts.items()))

        if action_counts else "No moderation history"

    )

    embed = discord.Embed(

        title=f"👤  User Info — {member}",

        color=member.color if member.color.value else discord.Color.blurple(),

        timestamp=datetime.now(timezone.utc))

    embed.set_thumbnail(url=member.display_avatar.url)

    embed.add_field(name="Mention / ID",   value=f"{member.mention}\n`{member.id}`", inline=True)

    embed.add_field(name="Display Name",   value=member.display_name,                inline=True)

    embed.add_field(name="Bot Account",    value="Yes ✅" if member.bot else "No",   inline=True)

    embed.add_field(name="Account Created", value=f"<t:{created_ts}:F>\n<t:{created_ts}:R>", inline=False)

    if joined_ts:

        embed.add_field(name="Joined Server", value=f"<t:{joined_ts}:F>\n<t:{joined_ts}:R>", inline=False)

    embed.add_field(name="Timed Out", value=(f"Until <t:{timeout_ts}:F>" if timeout_ts else "No"), inline=True)

    embed.add_field(name="\u200b", value="─────────────────────────────────", inline=False)

    embed.add_field(name="📋 Total Cases",    value=str(total_cases), inline=True)

    embed.add_field(name="⚠️ Warnings",       value=str(total_warns),  inline=True)

    embed.add_field(name="\u200b",             value="\u200b",          inline=True)

    embed.add_field(name="📊 Case Breakdown",  value=case_summary,      inline=False)

    embed.add_field(name="Roles",              value=roles_text,         inline=False)

    embed.set_footer(text=f"Requested by {ctx.author}", icon_url=ctx.author.display_avatar.url)

    await ctx.send(embed=embed)

@whois_cmd.error

async def whois_cmd_error(ctx, error):

    if isinstance(error, commands.CheckFailure):

        return await ctx.send(embed=error_embed("Only staff can use `.w`."))

    raise error

@bot.command(name="case")

@require_cmd_role("case")

async def case_cmd(ctx, case_number: int = None):

    if case_number is None:

        return await ctx.send(embed=syntax_embed("case"))

    data = load_data()

    c    = get_case_by_id(data, ctx.guild.id, case_number)

    if not c:

        return await ctx.send(embed=error_embed(f"Case **#{case_number}** not found."))

    action  = c["action"]

    icon    = ACTION_ICONS.get(action, "📋")

    color   = ACTION_COLORS.get(action, discord.Color.blurple())

    ts      = datetime.fromisoformat(c["timestamp"])

    unix_ts = int(ts.timestamp())

    embed = discord.Embed(

        title=f"{icon}  Case #{c['case']}  •  {action}",

        description="─────────────────────────────────",

        color=color, timestamp=ts)

    embed.add_field(name="👤 User",      value=f"{c['target_tag']}\n`{c['target_id']}`", inline=True)

    embed.add_field(name="🛡️ Moderator", value=f"{c['mod_tag']}\n`{c['mod_id']}`",       inline=True)

    embed.add_field(name="\u200b",       value="\u200b",                                  inline=True)

    embed.add_field(name="🕐 Date",      value=f"<t:{unix_ts}:F>\n<t:{unix_ts}:R>",      inline=False)

    embed.add_field(name="📄 Reason",    value=c["reason"] or "No reason provided",       inline=False)

    embed.set_footer(text=f"Case #{c['case']}  •  {action}")

    try:

        target_user = await bot.fetch_user(c["target_id"])

        embed.set_thumbnail(url=target_user.display_avatar.url)

    except Exception:

        pass

    await ctx.send(embed=embed)

@bot.command(name="jail")

@require_cmd_role("jail")

async def jail_cmd(ctx, member: discord.Member = None, *, reason="No reason provided"):

    if not member:

        return await ctx.send(embed=syntax_embed("jail"))

    if not hierarchy_ok(ctx.author, member):

        return await ctx.send(embed=error_embed("You cannot jail someone with an equal or higher role."))

    ok = await action_jail(ctx.guild, member, reason)

    if not ok:

        return await ctx.send(embed=warn_embed(f"**{member.display_name}** is already jailed."))

    data = load_data()

    cid  = add_case(data, ctx.guild.id, "Jail", ctx.author, member, reason)

    add_persistent_role(data, ctx.guild.id, member.id, JAIL_ROLE_NAME)

    await dm_member(member, "Jail", ctx.guild.name, reason, cid, moderator=ctx.author)

    view = ProofView(ctx.guild, cid, "Jail", ctx.author, member, reason)

    await ctx.send(embed=result_embed("Jail", member, reason, cid), view=view)

@bot.command(name="unjail")

@require_cmd_role("unjail")

async def unjail_cmd(ctx, member: discord.Member = None):

    if not member:

        return await ctx.send(embed=syntax_embed("unjail"))

    ok = await action_unjail(ctx.guild, member)

    if not ok:

        return await ctx.send(embed=warn_embed(f"**{member.display_name}** is not jailed."))

    data = load_data()

    cid  = add_case(data, ctx.guild.id, "Unjail", ctx.author, member, "Released from jail")

    remove_persistent_role(data, ctx.guild.id, member.id, JAIL_ROLE_NAME)

    await dm_member(member, "Unjail", ctx.guild.name, "You have been released from jail.", cid, moderator=ctx.author)

    view = ProofView(ctx.guild, cid, "Unjail", ctx.author, member, "Released from jail")

    await ctx.send(embed=result_embed("Unjail", member, "Released from jail", cid), view=view)

@bot.command(name="purge", aliases=["clear"])

@require_cmd_role("purge")

async def purge_cmd(ctx, amount: int = None):

    if not amount or amount < 1:

        return await ctx.send(embed=syntax_embed("purge"), delete_after=10)

    amount  = min(amount, 100)

    deleted = await ctx.channel.purge(limit=amount + 1)

    await ctx.send(embed=success_embed(f"Deleted **{len(deleted) - 1}** message(s)."), delete_after=5)

async def purge_user_in_channel(channel, member, amount: int) -> int:
    """Deletes up to `amount` of member's messages in `channel` only, scanning
    beyond Discord's default 100-message window so it actually finds that
    many even in a busy channel. Splits bulk-eligible (<14 days) from
    too-old-for-bulk so one stale message can't sink a whole batch."""

    bulk_cutoff = datetime.now(timezone.utc) - timedelta(days=14) + timedelta(minutes=5)

    collected: list[discord.Message] = []

    async for msg in channel.history(limit=None):

        if msg.author.id == member.id:

            collected.append(msg)

            if len(collected) >= amount:

                break

    if not collected:

        return 0

    bulk_eligible = [m for m in collected if m.created_at >= bulk_cutoff]

    needs_single  = [m for m in collected if m.created_at < bulk_cutoff]

    deleted = 0

    for i in range(0, len(bulk_eligible), 100):

        chunk = bulk_eligible[i:i + 100]

        try:

            if len(chunk) == 1:

                await chunk[0].delete()

            else:

                await channel.delete_messages(chunk)

            deleted += len(chunk)

        except discord.HTTPException:

            for m in chunk:

                try:

                    await m.delete()

                    deleted += 1

                except (discord.Forbidden, discord.HTTPException):

                    continue

    for m in needs_single:

        try:

            await m.delete()

            deleted += 1

        except (discord.Forbidden, discord.HTTPException):

            continue

    return deleted

@bot.command(name="purgeuser")

@require_cmd_role("purgeuser")

async def purgeuser_cmd(ctx, member: discord.Member = None, amount: int = 100):

    if not member:

        return await ctx.send(embed=syntax_embed("purgeuser"), delete_after=10)

    amount  = max(1, min(amount, 200))

    deleted = await purge_user_in_channel(ctx.channel, member, amount)

    await ctx.send(embed=success_embed(f"Deleted **{deleted}** message(s) from **{member.display_name}** in this channel."), delete_after=5)

@bot.command(name="addcmd")

@require_cmd_role("addcmd")

async def addcmd_cmd(ctx, name: str = None, *, response: str = None):

    if not name or not response:

        return await ctx.send(embed=syntax_embed("addcmd"))

    data = load_data()

    data["custom_commands"][name.lower()] = response

    save_data(data)

    await ctx.send(embed=success_embed(f"Custom command `{PREFIX}{name}` added."))

@bot.command(name="delcmd")

@require_cmd_role("delcmd")

async def delcmd_cmd(ctx, name: str = None):

    if not name:

        return await ctx.send(embed=syntax_embed("delcmd"))

    data = load_data()

    if name.lower() not in data["custom_commands"]:

        return await ctx.send(embed=error_embed(f"No command named `{name}` found."))

    del data["custom_commands"][name.lower()]

    save_data(data)

    await ctx.send(embed=success_embed(f"Custom command `{PREFIX}{name}` removed."))

@bot.command(name="listcmds")

async def listcmds_cmd(ctx):

    data = load_data()

    cmds = data.get("custom_commands", {})

    embed = discord.Embed(title="📋 Custom Commands", color=discord.Color.blurple())

    embed.description = (

        "\n".join(f"`{PREFIX}{k}`" for k in sorted(cmds.keys()))

        if cmds else "No custom commands set up yet.")

    await ctx.send(embed=embed)

@bot.command(name="role")

@require_cmd_role("role")

async def role_cmd(ctx, member: discord.Member = None, *, role_name: str = None):

    if not member or not role_name:

        embed = discord.Embed(title="📖  `.role` — Command Usage", color=discord.Color.blurple())

        embed.add_field(name="Syntax",  value="```\n.role @user <role name>\n```",              inline=False)

        embed.add_field(name="Example", value="```\n.role @John Verified\n.role @John Member\n```", inline=False)

        embed.add_field(name="ℹ️ Info", value="If the member already has the role it will be **removed**. Otherwise it will be **added**.", inline=False)

        return await ctx.send(embed=embed)

    role = discord.utils.get(ctx.guild.roles, name=role_name)

    if role is None:

        return await ctx.send(embed=error_embed(f"Role `{role_name}` not found."))

    if role >= ctx.guild.me.top_role:

        return await ctx.send(embed=error_embed("I can't manage that role — it's equal to or higher than my highest role."))

    if role >= ctx.author.top_role and not is_admin(ctx.author):

        return await ctx.send(embed=error_embed("You can't assign a role equal to or higher than your own highest role."))

    try:

        if role in member.roles:

            await member.remove_roles(role, reason=f"Role removed by {ctx.author}")

            await ctx.send(embed=discord.Embed(

                description=f"➖ Removed {role.mention} from {member.mention}.",

                color=discord.Color.orange(), timestamp=datetime.now(timezone.utc)

            ).set_footer(text=f"Done by {ctx.author}", icon_url=ctx.author.display_avatar.url))

        else:

            await member.add_roles(role, reason=f"Role given by {ctx.author}")

            await ctx.send(embed=discord.Embed(

                description=f"➕ Added {role.mention} to {member.mention}.",

                color=discord.Color.green(), timestamp=datetime.now(timezone.utc)

            ).set_footer(text=f"Done by {ctx.author}", icon_url=ctx.author.display_avatar.url))

    except discord.Forbidden:

        await ctx.send(embed=error_embed("I don't have permission to manage that role."))

@role_cmd.error

async def role_cmd_error(ctx, error):

    if isinstance(error, commands.CheckFailure):

        await ctx.send(embed=error_embed("You don't have permission to use `.role`."))

# ══════════════════════════════════════════════════════════════════════════════

#  ERROR HANDLER

# ══════════════════════════════════════════════════════════════════════════════

@bot.event

async def on_command_error(ctx, error):

    if isinstance(error, commands.CommandNotFound):

        cmd_name = ctx.invoked_with.lower()

        data     = load_data()

        if cmd_name in data.get("custom_commands", {}):

            await ctx.send(embed=info_embed(data["custom_commands"][cmd_name]))

        return

    if isinstance(error, commands.CheckFailure):

        return

    if isinstance(error, (commands.MemberNotFound, commands.BadArgument)):

        return

# ══════════════════════════════════════════════════════════════════════════════

#  SLASH COMMANDS — MOD

# ══════════════════════════════════════════════════════════════════════════════

async def slash_silent_fail(interaction: discord.Interaction):

    if not interaction.response.is_done():

        await interaction.response.defer(ephemeral=True)

@bot.tree.command(name="ban", description="[Mod] Ban a member")

@app_commands.describe(member="Member to ban", reason="Reason", delete_days="Days of messages to delete (0-7)")

@slash_cmd_role("ban")

async def ban_slash(interaction: discord.Interaction, member: discord.Member,

                    reason: str = "No reason provided", delete_days: int = 0):

    await interaction.response.defer()

    if not hierarchy_ok(interaction.user, member):

        return await interaction.followup.send(embed=error_embed("You cannot ban someone with an equal or higher role."), ephemeral=True)

    try:

        data = load_data()

        cid  = add_case(data, interaction.guild.id, "Ban", interaction.user, member, reason)

        await dm_member(member, "Ban", interaction.guild.name, reason, cid, moderator=interaction.user)

        await action_ban(interaction.guild, interaction.user, member, reason, delete_days)

        view = ProofView(interaction.guild, cid, "Ban", interaction.user, member, reason)

        await interaction.followup.send(embed=result_embed("Ban", member, reason, cid), view=view)

    except discord.Forbidden:

        await interaction.followup.send(embed=error_embed("I don't have permission to ban this member."), ephemeral=True)

@ban_slash.error

async def ban_slash_err(interaction, error):

    if isinstance(error, app_commands.CheckFailure): await slash_silent_fail(interaction)

@bot.tree.command(name="kick", description="[Mod] Kick a member")

@app_commands.describe(member="Member to kick", reason="Reason")

@slash_cmd_role("kick")

async def kick_slash(interaction: discord.Interaction, member: discord.Member,

                     reason: str = "No reason provided"):

    await interaction.response.defer()

    if not hierarchy_ok(interaction.user, member):

        return await interaction.followup.send(embed=error_embed("You cannot kick someone with an equal or higher role."), ephemeral=True)

    try:

        data = load_data()

        cid  = add_case(data, interaction.guild.id, "Kick", interaction.user, member, reason)

        await dm_member(member, "Kick", interaction.guild.name, reason, cid, moderator=interaction.user)

        await action_kick(member, reason)

        view = ProofView(interaction.guild, cid, "Kick", interaction.user, member, reason)

        await interaction.followup.send(embed=result_embed("Kick", member, reason, cid), view=view)

    except discord.Forbidden:

        await interaction.followup.send(embed=error_embed("I don't have permission to kick this member."), ephemeral=True)

@kick_slash.error

async def kick_slash_err(interaction, error):

    if isinstance(error, app_commands.CheckFailure): await slash_silent_fail(interaction)
    
@bot.tree.command(name="mute", description="[Mod/Trial Moderator] Mute a member")

@app_commands.describe(member="Member to mute",

                       duration="Duration e.g. 10m, 2h, 1d (default: 14 days)", reason="Reason")

@slash_cmd_role("mute")

async def mute_slash(interaction: discord.Interaction, member: discord.Member,

                     duration: str = None, reason: str = "No reason provided"):

    await interaction.response.defer()

    if not hierarchy_ok(interaction.user, member):

        return await interaction.followup.send(embed=error_embed("You cannot mute someone with an equal or higher role."), ephemeral=True)

    if duration is not None:

        secs = parse_duration(duration)

        if secs is None:

            return await interaction.followup.send(

                embed=error_embed("Invalid duration. Examples: `10m`, `2h`, `1day`, `1h30m`."), ephemeral=True)

    else:

        secs = DEFAULT_MUTE_SECONDS

    embed, err, view = await do_mute(interaction.guild, interaction.user, member, reason, secs)

    if err:

        return await interaction.followup.send(embed=err)

    await interaction.followup.send(embed=embed, view=view)

@mute_slash.error

async def mute_slash_err(interaction, error):

    if isinstance(error, app_commands.CheckFailure): await slash_silent_fail(interaction)

@bot.tree.command(name="unmute", description="[Mod/Trial Moderator] Unmute a member")

@app_commands.describe(member="Member to unmute")

@slash_cmd_role("unmute")

async def unmute_slash(interaction: discord.Interaction, member: discord.Member):

    await interaction.response.defer()

    ok = await action_unmute(interaction.guild, member)

    if not ok:

        return await interaction.followup.send(embed=warn_embed(f"**{member.display_name}** is not muted."))

    data = load_data()

    cid  = add_case(data, interaction.guild.id, "Unmute", interaction.user, member, "Manually unmuted")

    remove_persistent_role(data, interaction.guild.id, member.id, MUTED_ROLE_NAME)

    clear_mute_timer(data, interaction.guild.id, member.id)

    await dm_member(member, "Unmute", interaction.guild.name, "Manually unmuted by a moderator.", cid, moderator=interaction.user)

    view = ProofView(interaction.guild, cid, "Unmute", interaction.user, member, "Manually unmuted")

    await interaction.followup.send(embed=result_embed("Unmute", member, "Manually unmuted", cid), view=view)

@unmute_slash.error

async def unmute_slash_err(interaction, error):

    if isinstance(error, app_commands.CheckFailure): await slash_silent_fail(interaction)

@bot.tree.command(name="warn", description="[Mod/Trial Moderator] Warn a member")

@app_commands.describe(member="Member to warn", reason="Reason")

@slash_cmd_role("warn")

async def warn_slash(interaction: discord.Interaction, member: discord.Member,

                     reason: str = "No reason provided"):

    await interaction.response.defer()

    if not hierarchy_ok(interaction.user, member):

        return await interaction.followup.send(embed=error_embed("You cannot warn someone with an equal or higher role."), ephemeral=True)

    data     = load_data()

    warn_num = add_warn(data, interaction.guild.id, member.id, interaction.user, reason)

    cid      = add_case(data, interaction.guild.id, "Warn", interaction.user, member, reason)

    await dm_member(member, "Warn", interaction.guild.name, reason, cid,

                    moderator=interaction.user, extra=f"This is warning **#{warn_num}** on your record.")

    view = ProofView(interaction.guild, cid, "Warn", interaction.user, member, reason, extra=f"Warn #{warn_num}")

    await interaction.followup.send(embed=result_embed("Warn", member, reason, cid,

                                    extra=f"This is warn **#{warn_num}** for this user."), view=view)

@warn_slash.error

async def warn_slash_err(interaction, error):

    if isinstance(error, app_commands.CheckFailure): await slash_silent_fail(interaction)

@bot.tree.command(name="warnings", description="[Mod] View warnings for a member")

@app_commands.describe(member="Member to check")

@slash_cmd_role("warnings")

async def warnings_slash(interaction: discord.Interaction, member: discord.Member):

    await interaction.response.defer()

    data  = load_data()

    warns = get_warns(data, interaction.guild.id, member.id)

    embed = discord.Embed(title=f"⚠️ Warnings for {member}", color=discord.Color.yellow(),

                          timestamp=datetime.now(timezone.utc))

    embed.set_thumbnail(url=member.display_avatar.url)

    if not warns:

        embed.description = "This user has no warnings."

    else:

        for w in warns:

            embed.add_field(

                name=f"Warn #{w['number']} — {w['timestamp'][:10]}",

                value=f"**Reason:** {w['reason']}\n**By:** {w['mod_tag']}", inline=False)

    await interaction.followup.send(embed=embed)

@warnings_slash.error

async def warnings_slash_err(interaction, error):

    if isinstance(error, app_commands.CheckFailure): await slash_silent_fail(interaction)

@bot.tree.command(name="modlogs", description="[Mod] View mod history for a member")

@app_commands.describe(member="Member to check")

@slash_cmd_role("modlogs")

async def modlogs_slash(interaction: discord.Interaction, member: discord.Member):

    await interaction.response.defer()

    data  = load_data()

    cases = get_user_cases(data, interaction.guild.id, member.id)

    if not cases:

        embed = discord.Embed(title=f"📋 Mod Logs — {member}", color=discord.Color.blurple(),

                              timestamp=datetime.now(timezone.utc))

        embed.set_thumbnail(url=member.display_avatar.url)

        embed.description = "No moderation history found for this user."

        return await interaction.followup.send(embed=embed)

    view = ModlogsView(member, cases, interaction.user.id)

    await interaction.followup.send(embed=view.current_embed(), view=view)

@modlogs_slash.error

async def modlogs_slash_err(interaction, error):

    if isinstance(error, app_commands.CheckFailure): await slash_silent_fail(interaction)

@bot.tree.command(name="jail", description="[Mod] Jail a member")

@app_commands.describe(member="Member to jail", reason="Reason")

@slash_cmd_role("jail")

async def jail_slash(interaction: discord.Interaction, member: discord.Member,

                     reason: str = "No reason provided"):

    await interaction.response.defer()

    if not hierarchy_ok(interaction.user, member):

        return await interaction.followup.send(embed=error_embed("You cannot jail someone with an equal or higher role."), ephemeral=True)

    ok = await action_jail(interaction.guild, member, reason)

    if not ok:

        return await interaction.followup.send(embed=warn_embed(f"**{member.display_name}** is already jailed."))

    data = load_data()

    cid  = add_case(data, interaction.guild.id, "Jail", interaction.user, member, reason)

    add_persistent_role(data, interaction.guild.id, member.id, JAIL_ROLE_NAME)

    await dm_member(member, "Jail", interaction.guild.name, reason, cid, moderator=interaction.user)

    view = ProofView(interaction.guild, cid, "Jail", interaction.user, member, reason)

    await interaction.followup.send(embed=result_embed("Jail", member, reason, cid), view=view)

@jail_slash.error

async def jail_slash_err(interaction, error):

    if isinstance(error, app_commands.CheckFailure): await slash_silent_fail(interaction)

@bot.tree.command(name="unjail", description="[Mod] Release a jailed member")

@app_commands.describe(member="Member to unjail")

@slash_cmd_role("unjail")

async def unjail_slash(interaction: discord.Interaction, member: discord.Member):

    await interaction.response.defer()

    ok = await action_unjail(interaction.guild, member)

    if not ok:

        return await interaction.followup.send(embed=warn_embed(f"**{member.display_name}** is not jailed."))

    data = load_data()

    cid  = add_case(data, interaction.guild.id, "Unjail", interaction.user, member, "Released from jail")

    remove_persistent_role(data, interaction.guild.id, member.id, JAIL_ROLE_NAME)

    await dm_member(member, "Unjail", interaction.guild.name, "You have been released from jail.", cid, moderator=interaction.user)

    view = ProofView(interaction.guild, cid, "Unjail", interaction.user, member, "Released from jail")

    await interaction.followup.send(embed=result_embed("Unjail", member, "Released from jail", cid), view=view)

@unjail_slash.error

async def unjail_slash_err(interaction, error):

    if isinstance(error, app_commands.CheckFailure): await slash_silent_fail(interaction)

@bot.tree.command(name="purge", description="[Mod] Bulk delete messages")

@app_commands.describe(amount="Number of messages to delete (max 100)")

@slash_cmd_role("purge")

async def purge_slash(interaction: discord.Interaction, amount: int):

    await interaction.response.defer(ephemeral=True)

    amount  = max(1, min(amount, 100))

    deleted = await interaction.channel.purge(limit=amount)

    await interaction.followup.send(embed=success_embed(f"Deleted **{len(deleted)}** message(s)."), ephemeral=True)

@purge_slash.error

async def purge_slash_err(interaction, error):

    if isinstance(error, app_commands.CheckFailure): await slash_silent_fail(interaction)

@bot.tree.command(name="purgeuser", description="[Mod] Delete only one member's messages in this channel")

@app_commands.describe(member="Member whose messages to delete", amount="Number of their messages to delete (max 200)")

@slash_cmd_role("purgeuser")

async def purgeuser_slash(interaction: discord.Interaction, member: discord.Member, amount: int = 100):

    await interaction.response.defer(ephemeral=True)

    amount  = max(1, min(amount, 200))

    deleted = await purge_user_in_channel(interaction.channel, member, amount)

    await interaction.followup.send(embed=success_embed(f"Deleted **{deleted}** message(s) from **{member.display_name}** in this channel."), ephemeral=True)

@purgeuser_slash.error

async def purgeuser_slash_err(interaction, error):

    if isinstance(error, app_commands.CheckFailure): await slash_silent_fail(interaction)

@bot.tree.command(name="whois", description="[Staff] Show info about a member")

@app_commands.describe(member="Member to look up (leave blank for yourself)")

@slash_cmd_role("w")

async def whois_slash(interaction: discord.Interaction, member: discord.Member = None):

    await interaction.response.defer()

    if member is None:

        member = interaction.guild.get_member(interaction.user.id) or await interaction.guild.fetch_member(interaction.user.id)

    roles      = [role.mention for role in member.roles if role != interaction.guild.default_role]

    roles_text = ", ".join(reversed(roles)) if roles else "No roles"

    if len(roles_text) > 1024:

        roles_text = roles_text[:1020] + "..."

    created_ts     = int(member.created_at.replace(tzinfo=timezone.utc).timestamp())

    joined_ts      = int(member.joined_at.replace(tzinfo=timezone.utc).timestamp()) if member.joined_at else None

    _comm_disabled = getattr(member, 'communication_disabled_until', None)

    timeout_ts     = (int(_comm_disabled.replace(tzinfo=timezone.utc).timestamp()) if _comm_disabled else None)

    data         = load_data()

    user_cases   = get_user_cases(data, interaction.guild.id, member.id)

    user_warns   = get_warns(data, interaction.guild.id, member.id)

    action_counts: dict[str, int] = {}

    for c in user_cases:

        action_counts[c["action"]] = action_counts.get(c["action"], 0) + 1

    case_summary = (

        "  ".join(f"{ACTION_ICONS.get(a, '📋')} {a}: **{n}**" for a, n in sorted(action_counts.items()))

        if action_counts else "No moderation history"

    )

    embed = discord.Embed(

        title=f"👤  User Info — {member}",

        color=member.color if member.color.value else discord.Color.blurple(),

        timestamp=datetime.now(timezone.utc))

    embed.set_thumbnail(url=member.display_avatar.url)

    embed.add_field(name="Mention / ID",   value=f"{member.mention}\n`{member.id}`",    inline=True)

    embed.add_field(name="Display Name",   value=member.display_name,                   inline=True)

    embed.add_field(name="Bot Account",    value="Yes ✅" if member.bot else "No",      inline=True)

    embed.add_field(name="Account Created", value=f"<t:{created_ts}:F>\n<t:{created_ts}:R>", inline=False)

    if joined_ts:

        embed.add_field(name="Joined Server", value=f"<t:{joined_ts}:F>\n<t:{joined_ts}:R>", inline=False)

    embed.add_field(name="Timed Out", value=(f"Until <t:{timeout_ts}:F>" if timeout_ts else "No"), inline=True)

    embed.add_field(name="\u200b", value="─────────────────────────────────", inline=False)

    embed.add_field(name="📋 Total Cases",   value=str(len(user_cases)), inline=True)

    embed.add_field(name="⚠️ Warnings",      value=str(len(user_warns)), inline=True)

    embed.add_field(name="\u200b",            value="\u200b",             inline=True)

    embed.add_field(name="📊 Case Breakdown", value=case_summary,         inline=False)

    embed.add_field(name="Roles",             value=roles_text,            inline=False)

    embed.set_footer(text=f"Requested by {interaction.user}", icon_url=interaction.user.display_avatar.url)

    await interaction.followup.send(embed=embed)

@whois_slash.error

async def whois_slash_err(interaction, error):

    if isinstance(error, app_commands.CheckFailure): await slash_silent_fail(interaction)

@bot.tree.command(name="unban", description="[Mod] Unban a user by ID")

@app_commands.describe(user_id="The user ID to unban")

@slash_cmd_role("unban")

async def unban_slash(interaction: discord.Interaction, user_id: str):

    await interaction.response.defer()

    if not user_id.isdigit():

        return await interaction.followup.send(embed=error_embed("User ID must be a number."), ephemeral=True)

    try:

        user = await action_unban(interaction.guild, int(user_id))

        data = load_data()

        cid  = add_case(data, interaction.guild.id, "Unban", interaction.user, user, "Unbanned")

        view = ProofView(interaction.guild, cid, "Unban", interaction.user, user, "Unbanned")

        await interaction.followup.send(embed=result_embed("Unban", user, "Unbanned", cid), view=view)

    except discord.NotFound:

        await interaction.followup.send(embed=error_embed("No ban found for that user ID."), ephemeral=True)

    except discord.Forbidden:

        await interaction.followup.send(embed=error_embed("I don't have permission to unban."), ephemeral=True)

@unban_slash.error

async def unban_slash_err(interaction, error):

    if isinstance(error, app_commands.CheckFailure): await slash_silent_fail(interaction)

@bot.tree.command(name="unwarn", description="[Mod] Remove a specific warning from a member")

@app_commands.describe(member="Member to remove a warning from", warn_number="The warning number to remove")

@slash_cmd_role("unwarn")

async def unwarn_slash(interaction: discord.Interaction, member: discord.Member, warn_number: int):

    await interaction.response.defer()

    data = load_data()

    ok   = remove_warn(data, interaction.guild.id, member.id, warn_number)

    if not ok:

        return await interaction.followup.send(embed=error_embed(f"Warn #{warn_number} not found for **{member.display_name}**."), ephemeral=True)

    cid = add_case(data, interaction.guild.id, "Unwarn", interaction.user, member, f"Removed warn #{warn_number}")

    await dm_member(member, "Unwarn", interaction.guild.name,
                    f"Warn #{warn_number} has been removed from your record.", cid, moderator=interaction.user)

    view = ProofView(interaction.guild, cid, "Unwarn", interaction.user, member, f"Removed warn #{warn_number}")

    await interaction.followup.send(embed=result_embed("Unwarn", member, f"Removed warn #{warn_number}", cid), view=view)

@unwarn_slash.error

async def unwarn_slash_err(interaction, error):

    if isinstance(error, app_commands.CheckFailure): await slash_silent_fail(interaction)

@bot.tree.command(name="case", description="[Mod] Show full details for a single moderation case")

@app_commands.describe(case_number="The case number to look up")

@slash_cmd_role("case")

async def case_slash(interaction: discord.Interaction, case_number: int):

    await interaction.response.defer()

    data = load_data()

    c    = get_case_by_id(data, interaction.guild.id, case_number)

    if not c:

        return await interaction.followup.send(embed=error_embed(f"Case **#{case_number}** not found."), ephemeral=True)

    action  = c["action"]

    icon    = ACTION_ICONS.get(action, "📋")

    color   = ACTION_COLORS.get(action, discord.Color.blurple())

    ts      = datetime.fromisoformat(c["timestamp"])

    unix_ts = int(ts.timestamp())

    embed = discord.Embed(
        title=f"{icon}  Case #{c['case']}  •  {action}",
        description="─────────────────────────────────",
        color=color, timestamp=ts)

    embed.add_field(name="👤 User",      value=f"{c['target_tag']}\n`{c['target_id']}`", inline=True)

    embed.add_field(name="🛡️ Moderator", value=f"{c['mod_tag']}\n`{c['mod_id']}`",       inline=True)

    embed.add_field(name="\u200b",       value="\u200b",                                  inline=True)

    embed.add_field(name="🕐 Date",      value=f"<t:{unix_ts}:F>\n<t:{unix_ts}:R>",      inline=False)

    embed.add_field(name="📄 Reason",    value=c["reason"] or "No reason provided",       inline=False)

    embed.set_footer(text=f"Case #{c['case']}  •  {action}")

    try:

        target_user = await bot.fetch_user(c["target_id"])

        embed.set_thumbnail(url=target_user.display_avatar.url)

    except Exception:

        pass

    await interaction.followup.send(embed=embed)

@case_slash.error

async def case_slash_err(interaction, error):

    if isinstance(error, app_commands.CheckFailure): await slash_silent_fail(interaction)

@bot.tree.command(name="editcase", description="[Mod] Open the interactive editor for a case")

@app_commands.describe(case_number="The case number to edit")

@slash_cmd_role("editcase")

async def editcase_slash(interaction: discord.Interaction, case_number: int):

    data = load_data()

    c    = get_case_by_id(data, interaction.guild.id, case_number)

    if not c:

        return await interaction.response.send_message(embed=error_embed(f"Case **#{case_number}** not found."), ephemeral=True)

    class _FakeCtx:
        def __init__(self, interaction):
            self.author = interaction.user

    view  = EditCaseView(_FakeCtx(interaction), c)

    embed = view._build_preview_embed()

    await interaction.response.send_message(embed=embed, view=view)

@editcase_slash.error

async def editcase_slash_err(interaction, error):

    if isinstance(error, app_commands.CheckFailure): await slash_silent_fail(interaction)

@bot.tree.command(name="editc", description="[Mod] Quickly change the reason stored on a case")

@app_commands.describe(case_number="The case number to edit", new_reason="The new reason to set")

@slash_cmd_role("editc")

async def editc_slash(interaction: discord.Interaction, case_number: int, new_reason: str):

    await interaction.response.defer()

    data = load_data()

    c    = get_case_by_id(data, interaction.guild.id, case_number)

    if not c:

        return await interaction.followup.send(embed=error_embed(f"Case **#{case_number}** not found."), ephemeral=True)

    old_reason  = c["reason"]

    c["reason"] = new_reason

    save_data(data)

    channel = discord.utils.get(interaction.guild.text_channels, name=MOD_LOG_CHANNEL_NAME)

    if channel:

        action = c["action"]

        icon   = ACTION_ICONS.get(action, "📋")

        color  = ACTION_COLORS.get(action, discord.Color.blurple())

        embed  = discord.Embed(
            title=f"✏️  Reason Edited  •  Case #{case_number}",
            description="─────────────────────────────────",
            color=color, timestamp=datetime.now(timezone.utc))

        embed.add_field(name="👤 User",      value=f"{c['target_tag']}\n`{c['target_id']}`", inline=True)

        embed.add_field(name="🛡️ Moderator", value=f"{c['mod_tag']}\n`{c['mod_id']}`",       inline=True)

        embed.add_field(name="\u200b",        value="\u200b",                                  inline=True)

        embed.add_field(name="📄 Old Reason", value=old_reason or "No reason provided",        inline=False)

        embed.add_field(name="📄 New Reason", value=new_reason,                                inline=False)

        embed.add_field(name="🔧 Edited by",  value=f"{interaction.user} (`{interaction.user.id}`)",      inline=False)

        embed.set_footer(text=f"Case #{case_number}  •  {action}  •  ✏️ Reason edited")

        await channel.send(embed=embed)

    await interaction.followup.send(embed=success_embed(f"Case **#{case_number}** reason updated."))

@editc_slash.error

async def editc_slash_err(interaction, error):

    if isinstance(error, app_commands.CheckFailure): await slash_silent_fail(interaction)

@bot.tree.command(name="addcmd", description="[Mod] Create a custom command")

@app_commands.describe(name="Name of the custom command", response="What the bot should reply with")

@slash_cmd_role("addcmd")

async def addcmd_slash(interaction: discord.Interaction, name: str, response: str):

    data = load_data()

    data["custom_commands"][name.lower()] = response

    save_data(data)

    await interaction.response.send_message(embed=success_embed(f"Custom command `{PREFIX}{name}` added."))

@addcmd_slash.error

async def addcmd_slash_err(interaction, error):

    if isinstance(error, app_commands.CheckFailure): await slash_silent_fail(interaction)

@bot.tree.command(name="delcmd", description="[Mod] Delete a custom command")

@app_commands.describe(name="Name of the custom command to remove")

@slash_cmd_role("delcmd")

async def delcmd_slash(interaction: discord.Interaction, name: str):

    data = load_data()

    if name.lower() not in data["custom_commands"]:

        return await interaction.response.send_message(embed=error_embed(f"No command named `{name}` found."), ephemeral=True)

    del data["custom_commands"][name.lower()]

    save_data(data)

    await interaction.response.send_message(embed=success_embed(f"Custom command `{PREFIX}{name}` removed."))

@delcmd_slash.error

async def delcmd_slash_err(interaction, error):

    if isinstance(error, app_commands.CheckFailure): await slash_silent_fail(interaction)

@bot.tree.command(name="listcmds", description="Show all custom commands")

async def listcmds_slash(interaction: discord.Interaction):

    data = load_data()

    cmds = data.get("custom_commands", {})

    embed = discord.Embed(title="📋 Custom Commands", color=discord.Color.blurple())

    embed.description = (
        "\n".join(f"`{PREFIX}{k}`" for k in sorted(cmds.keys()))
        if cmds else "No custom commands set up yet.")

    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="setjreqchannel", description="[Setup] Choose the channel jail requests get posted to")

@app_commands.describe(channel="The channel to post jail requests to")

async def setjreqchannel_slash(interaction: discord.Interaction, channel: discord.TextChannel):

    if not is_setup_authorized(interaction.user):

        return await slash_silent_fail(interaction)

    set_jail_request_channel(interaction.guild.id, channel.id)

    await interaction.response.send_message(embed=success_embed(f"Jail requests will now be posted to {channel.mention}."))

@bot.command(name="modcases")

@require_cmd_role("modcases")

async def modcases_cmd(ctx, member: discord.Member = None):

    data  = load_data()

    embed = build_modcases_embed(ctx.guild, data, member, requester=ctx.author)

    await ctx.send(embed=embed)

@modcases_cmd.error

async def modcases_cmd_error(ctx, error):

    if isinstance(error, commands.CheckFailure):

        await ctx.send(embed=error_embed("Only staff can use `.modcases`."))

@bot.command(name="afk")

async def afk_cmd(ctx, *, reason: str = "AFK"):

    uid = ctx.author.id

    if uid in afk_users:

        return await ctx.send(embed=warn_embed("You are already AFK. Send any message to remove it."), delete_after=5)

    afk_users[uid] = {"reason": reason, "since": datetime.now(timezone.utc)}

    embed = discord.Embed(

        description=f"💤 **{ctx.author.display_name}** is now AFK\n📝 {reason}",

        color=discord.Color.greyple(), timestamp=datetime.now(timezone.utc))

    embed.set_footer(text="You'll be removed from AFK when you next send a message.")

    await ctx.send(embed=embed)

# ══════════════════════════════════════════════════════════════════════════════

#  GIVEAWAY REVIEW VIEW — Start / Edit / Cancel

# ══════════════════════════════════════════════════════════════════════════════

GIVEAWAY_EDIT_OPTIONS = [
    ("Channel",                     "channel"),
    ("Duration",                    "duration"),
    ("Winners",                     "winners"),
    ("Prize",                       "prize"),
    ("Show Giveaway Entry Captcha", "captcha"),
    ("Giveaway Host",               "host"),
    ("Extra Entries (Bonus Roles)", "extra_entries"),
    ("Stack Entries",               "stack_entries"),
    ("Repeat Duration",             "repeat_duration"),
    ("Repeat Times",                "repeat_times"),
    ("Drop",                        "drop"),
    ("Required Roles",              "required_roles"),
    ("Required Level",              "required_level"),
    ("Required Daily Messages",     "req_daily_messages"),
    ("Required Weekly Messages",    "req_weekly_messages"),
    ("Required Monthly Messages",   "req_monthly_messages"),
    ("Required Total Messages",     "req_total_messages"),
    ("Requirement Bypass Role",     "bypass_role"),
    ("Giveaway Create Message",     "create_message"),
    ("Giveaway Winners DM Message", "winners_dm_message"),
    ("Giveaway Winners Role",       "winners_role"),
    ("Blacklisted Roles",           "blacklisted_roles"),
    ("Image",                       "image"),
    ("Thumbnail",                   "thumbnail"),
]

GIVEAWAY_EDIT_SPECS = {
    "channel":              {"label": "Channel",                     "kind": "channel"},
    "duration":             {"label": "Duration",                    "kind": "duration"},
    "winners":              {"label": "Winners",                     "kind": "int",  "min": 1},
    "prize":                {"label": "Prize",                       "kind": "text"},
    "captcha":              {"label": "Show Giveaway Entry Captcha", "kind": "bool"},
    "host":                 {"label": "Giveaway Host",               "kind": "member"},
    "extra_entries":        {"label": "Extra Entries (Bonus Roles)", "kind": "bonus_roles"},
    "stack_entries":        {"label": "Stack Entries",               "kind": "bool"},
    "repeat_duration":      {"label": "Repeat Duration",             "kind": "duration"},
    "repeat_times":         {"label": "Repeat Times",                "kind": "int",  "min": 1},
    "drop":                 {"label": "Drop",                        "kind": "bool"},
    "required_roles":       {"label": "Required Roles",              "kind": "roles"},
    "required_level":       {"label": "Required Level",              "kind": "int",  "min": 0},
    "req_daily_messages":   {"label": "Required Daily Messages",     "kind": "int",  "min": 0},
    "req_weekly_messages":  {"label": "Required Weekly Messages",    "kind": "int",  "min": 0},
    "req_monthly_messages": {"label": "Required Monthly Messages",   "kind": "int",  "min": 0},
    "req_total_messages":   {"label": "Required Total Messages",     "kind": "int",  "min": 0},
    "bypass_role":          {"label": "Requirement Bypass Role",     "kind": "role"},
    "create_message":       {"label": "Giveaway Create Message",     "kind": "text"},
    "winners_dm_message":   {"label": "Giveaway Winners DM Message", "kind": "text"},
    "winners_role":         {"label": "Giveaway Winners Role",       "kind": "role"},
    "blacklisted_roles":    {"label": "Blacklisted Roles",           "kind": "roles"},
    "image":                {"label": "Image",                       "kind": "url"},
    "thumbnail":            {"label": "Thumbnail",                   "kind": "url"},
}


def _parse_role_ref(guild: discord.Guild, raw: str):
    raw = raw.strip()
    if not raw:
        return None
    if raw.lower() in {"none", "clear", "remove", "reset"}:
        return None
    match = re.search(r"(\d{15,25})", raw)
    if match:
        return guild.get_role(int(match.group(1)))
    return discord.utils.get(guild.roles, name=raw)


def _parse_member_ref(guild: discord.Guild, raw: str):
    raw = raw.strip()
    if not raw:
        return None
    match = re.search(r"(\d{15,25})", raw)
    if match:
        return guild.get_member(int(match.group(1)))
    return None


def _fmt_bonus_roles_edit(bonus_roles: list, guild: discord.Guild) -> str:
    if not bonus_roles:
        return ""
    lines = []
    for item in bonus_roles:
        role = guild.get_role(item.get("role_id"))
        role_name = role.name if role else f"Role ID {item.get('role_id')}"
        lines.append(f"• `{role_name}` → **+{item.get('entries', 0)}** extra entries")
    return "\n".join(lines)


# ── Step 2 of extra_entries flow: modal that asks for the entry count ─────────

class BonusEntryCountModal(discord.ui.Modal, title="Set Bonus Entry Count"):
    def __init__(self, review_view: "GiveawayReviewView", role: discord.Role):
        super().__init__()
        self.review_view = review_view
        self.role        = role
        self.count_input = discord.ui.TextInput(
            label=f"Extra entries for {role.name}",
            placeholder="Enter a number, e.g. 2  (0 = remove this role)",
            required=True,
            max_length=3,
        )
        self.add_item(self.count_input)

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.count_input.value.strip()
        try:
            count = int(raw)
        except ValueError:
            return await interaction.response.send_message(
                embed=error_embed("Please enter a valid whole number."), ephemeral=True)
        if count < 0:
            return await interaction.response.send_message(
                embed=error_embed("Entry count cannot be negative."), ephemeral=True)

        g           = self.review_view.giveaway
        bonus_roles = [br for br in g.get("bonus_roles", []) if br["role_id"] != self.role.id]

        if count > 0:
            bonus_roles.append({"role_id": self.role.id, "entries": count})

        g["bonus_roles"] = bonus_roles

        # Persist to DB if the giveaway is already live
        if self.review_view.msg_id:
            data = load_data()
            update_giveaway(data, interaction.guild.id, self.review_view.msg_id, bonus_roles=bonus_roles)

        # Show updated summary
        current_text = _fmt_bonus_roles_edit(bonus_roles, interaction.guild) or "*None set*"
        if count == 0:
            msg = f"Removed **{self.role.name}** from bonus entry roles."
        else:
            msg = (
                f"Set **{self.role.name}** to **+{count}** extra "
                f"{'entry' if count == 1 else 'entries'}.\n\n"
                f"**Current bonus roles:**\n{current_text}"
            )

        await interaction.response.send_message(
            embed=success_embed(msg), ephemeral=True)


# ── Step 1 of extra_entries flow: pick the role via a RoleSelect ─────────────

class BonusRoleSelectView(discord.ui.View):
    """Shown when the user picks 'Extra Entries' from the edit dropdown."""

    def __init__(self, review_view: "GiveawayReviewView"):
        super().__init__(timeout=120)
        self.review_view = review_view
        self.add_item(BonusRoleSelect(review_view))

    @discord.ui.button(label="← Back", style=discord.ButtonStyle.secondary, row=1)
    async def back_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title=f"✏️ Edit Giveaway — {self.review_view.prize}",
            description="Select a field from the dropdown below, then type the new value in the popup.",
            color=self.review_view.embed_color,
        )
        self.review_view.edit_select.disabled = False
        await interaction.response.edit_message(embed=embed, view=self.review_view)


class BonusRoleSelect(discord.ui.RoleSelect):
    def __init__(self, review_view: "GiveawayReviewView"):
        super().__init__(
            placeholder="Select the role to give bonus entries...",
            min_values=1,
            max_values=1,
        )
        self.review_view = review_view

    async def callback(self, interaction: discord.Interaction):
        role = self.values[0]
        # Open the count modal immediately
        await interaction.response.send_modal(BonusEntryCountModal(self.review_view, role))


# ── Generic text-field modal ──────────────────────────────────────────────────

class GiveawayFieldModal(discord.ui.Modal):
    def __init__(self, review_view: "GiveawayReviewView", field_key: str):
        spec = GIVEAWAY_EDIT_SPECS[field_key]
        super().__init__(title=f"Edit {spec['label']}")
        self.review_view = review_view
        self.field_key   = field_key
        self.input = discord.ui.TextInput(
            label=spec["label"],
            placeholder=(
                "Paste a channel mention or ID here"
                if spec["kind"] == "channel"
                else "Paste an image URL here"
                if field_key in {"image", "thumbnail"}
                else "Type the new value here"
            ),
            required=True,
            max_length=4000,
        )
        self.add_item(self.input)

    @staticmethod
    def _refresh_review_message(review_view: "GiveawayReviewView", interaction: discord.Interaction):
        async def _refresh():
            g = review_view.giveaway
            channel = interaction.guild.get_channel(g["channel_id"])
            if not channel:
                return
            try:
                msg = await channel.fetch_message(review_view.msg_id)
                total = _get_entry_count(g, interaction.guild)
                emb = giveaway_embed(
                    {**g, "message_id": review_view.msg_id},
                    total,
                    custom_color=g.get("color", GIVEAWAY_COLOR),
                    custom_image=g.get("image"),
                    custom_thumbnail=g.get("thumbnail"),
                )
                await msg.edit(embed=emb, view=GiveawayView(review_view.msg_id, total))
            except discord.NotFound:
                pass
        return _refresh()

    async def on_submit(self, interaction: discord.Interaction):
        spec    = GIVEAWAY_EDIT_SPECS[self.field_key]
        raw     = str(self.input.value).strip()
        g       = self.review_view.giveaway
        changes = {}

        if spec["kind"] == "text":
            changes[self.field_key] = raw

        elif spec["kind"] == "url":
            if self.field_key in {"image", "thumbnail"}:
                attachment_url = None
                if interaction.message and getattr(interaction.message, "attachments", None):
                    if interaction.message.attachments:
                        attachment_url = interaction.message.attachments[0].url
                value = None if raw.lower() in {"none", "clear", "remove", "reset"} else (attachment_url or raw)
                if value and not await _url_points_to_image(value):
                    return await interaction.response.send_message(
                        embed=error_embed("That image URL is invalid or does not point to an image."), ephemeral=True)
                changes[self.field_key] = value
            else:
                changes[self.field_key] = (
                    None if raw.lower() in {"none", "clear", "remove", "reset"} else raw
                )

        elif spec["kind"] == "bool":
            lowered = raw.lower()
            if lowered in {"true", "yes", "on", "enable", "enabled", "1"}:
                changes[self.field_key] = True
            elif lowered in {"false", "no", "off", "disable", "disabled", "0"}:
                changes[self.field_key] = False
            else:
                return await interaction.response.send_message(
                    embed=error_embed("Enter `yes` or `no`."), ephemeral=True)

        elif spec["kind"] == "int":
            try:
                value = int(raw)
            except ValueError:
                return await interaction.response.send_message(
                    embed=error_embed("Enter a valid whole number."), ephemeral=True)
            if value < spec.get("min", 0):
                return await interaction.response.send_message(
                    embed=error_embed(f"Value must be at least {spec.get('min', 0)}."), ephemeral=True)
            changes[self.field_key] = value

        elif spec["kind"] == "duration":
            secs = parse_duration(raw)
            if secs is None:
                return await interaction.response.send_message(
                    embed=error_embed("Enter a valid duration like `1h30m`."), ephemeral=True)
            changes[self.field_key] = raw
            if self.field_key == "duration":
                new_end = datetime.now(timezone.utc) + timedelta(seconds=secs)
                g["end_time"]       = new_end.isoformat()
                changes["end_time"] = g["end_time"]

        elif spec["kind"] == "channel":
            # Accept a channel mention or ID
            m = re.search(r"(\d{15,25})", raw)
            if not m:
                return await interaction.response.send_message(
                    embed=error_embed("Paste the channel mention or ID."), ephemeral=True)
            ch = interaction.guild.get_channel(int(m.group(1)))
            if not ch:
                return await interaction.response.send_message(
                    embed=error_embed("Channel not found."), ephemeral=True)
            changes["channel_id"] = ch.id
            self.review_view.channel = ch  # update in-memory reference

        elif spec["kind"] == "role":
            if raw.lower() in {"none", "clear", "remove", "reset"}:
                # Map field_key -> the matching _id key
                id_key = f"{self.field_key}_id"
                changes[id_key] = None
            else:
                role = _parse_role_ref(interaction.guild, raw)
                if not role:
                    return await interaction.response.send_message(
                        embed=error_embed("I couldn't find that role."), ephemeral=True)
                id_key = f"{self.field_key}_id"
                changes[id_key] = role.id

        elif spec["kind"] == "member":
            if raw.lower() in {"none", "clear", "remove", "reset"}:
                changes["host_id"]  = None
                changes["host_tag"] = interaction.user.name
            else:
                member = _parse_member_ref(interaction.guild, raw)
                if not member:
                    return await interaction.response.send_message(
                        embed=error_embed("I couldn't find that member. Use a mention or ID."), ephemeral=True)
                changes["host_id"]  = member.id
                changes["host_tag"] = str(member)

        elif spec["kind"] == "roles":
            # required_roles / blacklisted_roles — still text-based
            if raw.lower() in {"none", "clear", "remove", "reset"}:
                changes[self.field_key] = []
                if self.field_key == "required_roles":
                    changes["required_role_id"] = None
            else:
                parsed_roles = []
                for line in raw.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    role = _parse_role_ref(interaction.guild, line)
                    if not role:
                        return await interaction.response.send_message(
                            embed=error_embed(f"Could not find role `{line}`."), ephemeral=True)
                    parsed_roles.append({"role_id": role.id, "entries": 1})
                changes[self.field_key] = parsed_roles
                if self.field_key == "required_roles":
                    changes["required_role_id"] = parsed_roles[0]["role_id"] if parsed_roles else None

        elif spec["kind"] == "bonus_roles":
            # Should be handled via BonusRoleSelectView, not this modal
            return await interaction.response.send_message(
                embed=warn_embed("Use the role picker for bonus entries."), ephemeral=True)

        else:
            return await interaction.response.send_message(
                embed=warn_embed("That field is not wired yet."), ephemeral=True)

        # Apply changes to in-memory giveaway dict
        g.update(changes)

        # Persist to DB only if the giveaway is already live (msg_id set)
        if self.review_view.msg_id:
            data = load_data()
            update_giveaway(data, interaction.guild.id, self.review_view.msg_id, **changes)
            await GiveawayFieldModal._refresh_review_message(self.review_view, interaction)

        await interaction.response.send_message(
            embed=success_embed(f"Updated **{spec['label']}**."), ephemeral=True)


class GiveawayChannelSelectView(discord.ui.View):
    def __init__(self, review_view: "GiveawayReviewView"):
        super().__init__(timeout=120)
        self.review_view = review_view
        self.add_item(GiveawayChannelSelect(review_view))

    @discord.ui.button(label="← Back to Review", style=discord.ButtonStyle.secondary, row=1)
    async def back_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=self.review_view.build_review_embed(), view=self.review_view)


class GiveawayChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, review_view: "GiveawayReviewView"):
        super().__init__(placeholder="Choose where the giveaway should be posted", min_values=1, max_values=1)
        self.review_view = review_view

    async def callback(self, interaction: discord.Interaction):
        channel = self.values[0]
        self.review_view.giveaway["channel_id"] = channel.id
        self.review_view.channel = channel
        await interaction.response.edit_message(embed=self.review_view.build_review_embed(), view=self.review_view)


class GiveawayMemberSelectView(discord.ui.View):
    def __init__(self, review_view: "GiveawayReviewView", field_key: str):
        super().__init__(timeout=120)
        self.review_view = review_view
        self.field_key = field_key
        self.add_item(GiveawayMemberSelect(review_view, field_key))

    @discord.ui.button(label="← Back to Review", style=discord.ButtonStyle.secondary, row=1)
    async def back_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=self.review_view.build_review_embed(), view=self.review_view)


class GiveawayMemberSelect(discord.ui.UserSelect):
    def __init__(self, review_view: "GiveawayReviewView", field_key: str):
        super().__init__(placeholder="Choose a member", min_values=1, max_values=1)
        self.review_view = review_view
        self.field_key = field_key

    async def callback(self, interaction: discord.Interaction):
        member = self.values[0]
        g = self.review_view.giveaway
        if self.field_key == "host":
            g["host_id"] = member.id
            g["host_tag"] = str(member)
            label = "Giveaway Host"
        else:
            label = self.field_key.replace("_", " ").title()
        await interaction.response.edit_message(embed=self.review_view.build_review_embed(), view=self.review_view)


class GiveawayRoleSelectView(discord.ui.View):
    def __init__(self, review_view: "GiveawayReviewView", field_key: str, multi: bool = False):
        super().__init__(timeout=120)
        self.review_view = review_view
        self.field_key = field_key
        self.multi = multi
        if multi:
            self.add_item(GiveawayMultiRoleSelect(review_view, field_key))
        else:
            self.add_item(GiveawaySingleRoleSelect(review_view, field_key))

    @discord.ui.button(label="← Back to Review", style=discord.ButtonStyle.secondary, row=1)
    async def back_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=self.review_view.build_review_embed(), view=self.review_view)


class GiveawaySingleRoleSelect(discord.ui.RoleSelect):
    def __init__(self, review_view: "GiveawayReviewView", field_key: str):
        super().__init__(placeholder="Choose a role", min_values=1, max_values=1)
        self.review_view = review_view
        self.field_key = field_key

    async def callback(self, interaction: discord.Interaction):
        role = self.values[0]
        g = self.review_view.giveaway
        if self.field_key in {"bypass_role", "winners_role"}:
            g[f"{self.field_key}_id"] = role.id
        elif self.field_key == "required_roles":
            g["required_role_id"] = role.id
            g["required_roles"] = [{"role_id": role.id, "entries": 1}]
        await interaction.response.edit_message(embed=self.review_view.build_review_embed(), view=self.review_view)


class GiveawayMultiRoleSelect(discord.ui.RoleSelect):
    def __init__(self, review_view: "GiveawayReviewView", field_key: str):
        super().__init__(placeholder="Choose one or more roles", min_values=1, max_values=25)
        self.review_view = review_view
        self.field_key = field_key

    async def callback(self, interaction: discord.Interaction):
        roles = self.values
        g = self.review_view.giveaway
        if self.field_key == "required_roles":
            g["required_roles"] = [{"role_id": r.id, "entries": 1} for r in roles]
            g["required_role_id"] = roles[0].id if roles else None
        elif self.field_key == "blacklisted_roles":
            g["blacklisted_roles"] = [{"role_id": r.id, "entries": 1} for r in roles]
        await interaction.response.edit_message(embed=self.review_view.build_review_embed(), view=self.review_view)


# ── Edit dropdown ─────────────────────────────────────────────────────────────

class GiveawayEditSelect(discord.ui.Select):
    def __init__(self, review_view: "GiveawayReviewView"):
        self.review_view = review_view
        options = [
            discord.SelectOption(label=label, value=value)
            for label, value in GIVEAWAY_EDIT_OPTIONS
        ]
        super().__init__(
            placeholder="Make a selection",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        selected = self.values[0]
        spec     = GIVEAWAY_EDIT_SPECS.get(selected)
        if not spec:
            return await interaction.response.send_message(
                embed=warn_embed("That option is not wired yet."), ephemeral=True)

        # Extra Entries → open the 2-step role picker instead of a text modal
        if selected == "extra_entries":
            g            = self.review_view.giveaway
            current_text = _fmt_bonus_roles_edit(g.get("bonus_roles", []), interaction.guild) or "*None set yet*"
            embed = discord.Embed(
                title="⭐ Extra Entries — Bonus Roles",
                description=(
                    "Select a **role** from the dropdown below.\n"
                    "You'll then enter how many **extra entries** that role gets.\n"
                    "Setting **0** removes the role from the bonus list.\n\n"
                    f"**Current bonus roles:**\n{current_text}"
                ),
                color=self.review_view.embed_color,
            )
            view = BonusRoleSelectView(self.review_view)
            return await interaction.response.edit_message(embed=embed, view=view)

        if selected == "channel":
            embed = discord.Embed(
                title="Channel",
                description="Choose the channel where the giveaway should be posted.",
                color=self.review_view.embed_color,
            )
            return await interaction.response.edit_message(embed=embed, view=GiveawayChannelSelectView(self.review_view))

        if selected == "host":
            embed = discord.Embed(
                title="Giveaway Host",
                description="Choose the member who should be shown as the host.",
                color=self.review_view.embed_color,
            )
            return await interaction.response.edit_message(embed=embed, view=GiveawayMemberSelectView(self.review_view, selected))

        if selected in {"bypass_role", "winners_role"}:
            embed = discord.Embed(
                title=spec["label"],
                description="Choose a single role.",
                color=self.review_view.embed_color,
            )
            return await interaction.response.edit_message(embed=embed, view=GiveawayRoleSelectView(self.review_view, selected, multi=False))

        if selected in {"required_roles", "blacklisted_roles"}:
            embed = discord.Embed(
                title=spec["label"],
                description="Choose one or more roles.",
                color=self.review_view.embed_color,
            )
            return await interaction.response.edit_message(embed=embed, view=GiveawayRoleSelectView(self.review_view, selected, multi=True))

        if selected in {"image", "thumbnail"}:
            await interaction.response.send_modal(GiveawayFieldModal(self.review_view, selected))
            return

        # Default text modal for the remaining fields
        await interaction.response.send_modal(GiveawayFieldModal(self.review_view, selected))


class GiveawayEditMenuView(discord.ui.View):
    def __init__(self, review_view: "GiveawayReviewView"):
        super().__init__(timeout=120)
        self.review_view = review_view
        self.edit_select = GiveawayEditSelect(review_view)
        self.add_item(self.edit_select)

    @discord.ui.button(label="← Back to Start", style=discord.ButtonStyle.secondary, row=1)
    async def back_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=self.review_view.build_review_embed(),
            view=GiveawayEditMenuView(self.review_view),
        )


# ── Main review view ──────────────────────────────────────────────────────────

class GiveawayReviewView(discord.ui.View):
    def __init__(self, msg_id: int, guild_id: int, channel, prize: str,
                 winners: int, duration: str, giveaway: dict,
                 embed_color: int, image: str, thumbnail: str):
        super().__init__(timeout=900)
        self.msg_id      = msg_id
        self.guild_id    = guild_id
        self.channel     = channel
        self.prize       = prize
        self.winners     = winners
        self.duration    = duration
        self.giveaway    = giveaway
        self.embed_color = embed_color
        self.image       = image
        self.thumbnail   = thumbnail
        self.started     = False

        self.edit_select          = GiveawayEditSelect(self)
        self.edit_select.disabled = True
        self.add_item(self.edit_select)

    def build_review_embed(self) -> discord.Embed:
        end_ts = int(datetime.fromisoformat(self.giveaway["end_time"]).timestamp())
        review_embed = discord.Embed(
            title=self.prize,
            description=(
                f"Click {GIVEAWAY_ENTER_EMOJI} button to enter!\n"
                f"Winners: {self.winners}\n"
                f"Duration: {self.duration}\n"
                f"Ends at • <t:{end_ts}:t>"
            ),
            color=self.embed_color,
        )
        review_embed.add_field(name="Requirements", value=_format_giveaway_requirements(self.giveaway), inline=False)
        if self.image:
            review_embed.set_image(url=self.image)
        if self.thumbnail:
            review_embed.set_thumbnail(url=self.thumbnail)
        return review_embed

    @discord.ui.button(label="✏️ Edit", style=discord.ButtonStyle.secondary, row=1)
    async def edit_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title=f"✏️ Edit Giveaway — {self.prize}",
            description="Select a field from the dropdown below, then type the new value in the popup.",
            color=self.embed_color,
        )
        await interaction.response.send_message(embed=embed, view=GiveawayEditMenuView(self), ephemeral=True)

    @discord.ui.button(label="▶ Start", style=discord.ButtonStyle.success, emoji="▶", row=1)
    async def start_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.started:
            return await interaction.response.send_message(
                embed=warn_embed("This giveaway has already been started."), ephemeral=True)
        if (interaction.user.id != interaction.guild.owner_id
                and not is_any_mod(interaction.user)
                and interaction.user.id != BOT_OWNER_ID):
            return await interaction.response.send_message(
                embed=error_embed("Only staff can start this giveaway."), ephemeral=True)

        self.started = True
        for child in self.children:
            child.disabled = True

        end_ts = int(datetime.fromisoformat(self.giveaway["end_time"]).timestamp())

        # Create the public giveaway message
        giveaway_msg = await self.channel.send(
            embed=discord.Embed(description="🎉 Setting up giveaway...", color=self.embed_color)
        )
        self.msg_id              = giveaway_msg.id
        self.giveaway["message_id"] = self.msg_id

        data = load_data()
        add_giveaway(data, self.guild_id, self.msg_id, self.giveaway)

        giveaway_total = 0
        await giveaway_msg.edit(
            embed=giveaway_embed({**self.giveaway, "message_id": self.msg_id}, giveaway_total),
            view=GiveawayView(self.msg_id, giveaway_total),
        )

        started_embed = discord.Embed(
            title="✅ Giveaway Started!",
            description=(
                f"🎉 **{self.prize}** giveaway is now live in {self.channel.mention}!\n"
                f"Winners: **{self.winners}** • Ends <t:{end_ts}:R>"
            ),
            color=discord.Color.green(),
        )
        await interaction.response.edit_message(embed=started_embed, view=self)

    @discord.ui.button(label="✗ Cancel", style=discord.ButtonStyle.danger, row=3)
    async def cancel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.msg_id:
            data = load_data()
            g    = get_giveaway(data, self.guild_id, self.msg_id)
            if g:
                try:
                    msg = await self.channel.fetch_message(self.msg_id)
                    await msg.delete()
                except discord.NotFound:
                    pass
                remove_giveaway(data, self.guild_id, self.msg_id)
                save_data(data)

        for child in self.children:
            child.disabled = True

        cancelled_embed = discord.Embed(
            description="❌ Giveaway cancelled and removed.",
            color=discord.Color.red(),
        )
        await interaction.response.edit_message(embed=cancelled_embed, view=self)


# ══════════════════════════════════════════════════════════════════════════════

#  GIVEAWAY SLASH COMMANDS

# ══════════════════════════════════════════════════════════════════════════════

giveaway_group = app_commands.Group(

    name="giveaway",

    description="Giveaway management commands",

)

@giveaway_group.command(name="create", description="Create a giveaway")

@app_commands.describe(

    duration="The duration for this giveaway (e.g. 1h30m, 2d, 45m)",

    winners="The number of winners for this giveaway",

    prize="The prize of this giveaway",

    channel="The channel this giveaway will be created in",

    host="The host of this giveaway",

    required_role="The role required for the giveaway",

    giveaway_winners_role="The role the bot should give to the winners of this giveaway",

    giveaway_winners_dm_message="The message the bot should DM to the winners of this giveaway",

    image="Image this giveaway embed will have (appears at the bottom). Accepts image URL",

    thumbnail="Thumbnail this giveaway embed will have (top right). Accepts image URL",

    required_level="The level required to participate in this giveaway",

    required_daily_messages="Amount of messages required to be sent today to participate",

    required_weekly_messages="Amount of messages required to be sent this week to participate",

    required_monthly_messages="Amount of messages required to be sent this month to participate",

    required_total_messages="Amount of messages required to be sent totally to participate",

    requirement_bypass_role="The role that can bypass all the requirements",

    color="The color the giveaway embed should have. Must be a proper Hex Color Code",

    end_color="The color the giveaway embed should have after the giveaway ends. Hex Color Code",

    giveaway_create_message="The message the bot should send after creating this giveaway",

)

@slash_cmd_role("gstart")

async def giveaway_create(

    interaction: discord.Interaction,

    duration: str,

    winners: int,

    prize: str,

    channel: discord.TextChannel = None,

    host: discord.Member = None,

    required_role: discord.Role = None,

    giveaway_winners_role: discord.Role = None,

    giveaway_winners_dm_message: str = None,

    image: str = None,

    thumbnail: str = None,

    required_level: int = None,

    required_daily_messages: int = None,

    required_weekly_messages: int = None,

    required_monthly_messages: int = None,

    required_total_messages: int = None,

    requirement_bypass_role: discord.Role = None,

    color: str = None,

    end_color: str = None,

    giveaway_create_message: str = None,

):

    await interaction.response.defer(ephemeral=True)

    seconds = parse_duration(duration)

    if seconds is None:

        return await interaction.followup.send(

            embed=error_embed("Couldn't parse that duration. Try `1h30m`, `2d`, or `45m`."), ephemeral=True)

    if winners < 1:

        return await interaction.followup.send(embed=error_embed("Winners must be at least 1."), ephemeral=True)

    embed_color = GIVEAWAY_COLOR

    if color:

        try:

            embed_color = int(color.lstrip("#"), 16)

        except ValueError:

            return await interaction.followup.send(

                embed=error_embed("Invalid hex color. Example: `#FF73FA` or `FF73FA`."), ephemeral=True)

    end_color_val = GIVEAWAY_ENDED_COLOR

    if end_color:

        try:

            end_color_val = int(end_color.lstrip("#"), 16)

        except ValueError:

            return await interaction.followup.send(

                embed=error_embed("Invalid end-color hex. Example: `#747F8D`."), ephemeral=True)

    if image and not await _url_points_to_image(image):
        return await interaction.followup.send(
            embed=error_embed("The giveaway image URL is invalid or does not point to an image."), ephemeral=True)

    if thumbnail and not await _url_points_to_image(thumbnail):
        return await interaction.followup.send(
            embed=error_embed("The giveaway thumbnail URL is invalid or does not point to an image."), ephemeral=True)

    target_channel = channel or interaction.channel

    actual_host    = host or interaction.user

    end_time       = datetime.now(timezone.utc) + timedelta(seconds=seconds)

    giveaway = {

        "channel_id":          target_channel.id,

        "host_id":             actual_host.id,

        "host_tag":            str(actual_host),

        "prize":               prize,

        "winners":             winners,

        "duration":            duration,

        "end_time":            end_time.isoformat(),

        "required_role_id":    required_role.id if required_role else None,

        "winners_role_id":     giveaway_winners_role.id if giveaway_winners_role else None,

        "winners_dm_message":  giveaway_winners_dm_message,

        "bypass_role_id":      requirement_bypass_role.id if requirement_bypass_role else None,

        "image":               image,

        "thumbnail":           thumbnail,

        "required_level":      required_level,

        "req_daily_messages":  required_daily_messages,

        "req_weekly_messages": required_weekly_messages,

        "req_monthly_messages":required_monthly_messages,

        "req_total_messages":  required_total_messages,

        "color":               embed_color,

        "end_color":           end_color_val,

        "bonus_roles":         [],

        "entries":             {},

        "ended":               False,

        "winner_ids":          [],

    }

    end_ts = int(end_time.timestamp())

    review_embed = discord.Embed(

        title=prize,

        description=(

            f"Click {GIVEAWAY_ENTER_EMOJI} button to enter!\n"

            f"Winners: {winners}\n"

            f"Duration: {duration}\n"

            f"Ends at • <t:{end_ts}:t>"

        ),

        color=embed_color,

    )

    if giveaway_create_message:

        review_embed.add_field(name="📢 Create Message", value=giveaway_create_message, inline=False)

    review_embed.set_footer(text='⚠️ Review your giveaway and click "Start" to start this giveaway! This message expires in 15 minutes!')

    view = GiveawayReviewView(

        msg_id=0,

        guild_id=interaction.guild.id,

        channel=target_channel,

        prize=prize,

        winners=winners,

        duration=duration,

        giveaway=giveaway,

        embed_color=embed_color,

        image=image,

        thumbnail=thumbnail,

    )

    await interaction.followup.send(embed=review_embed, view=view)

@giveaway_create.error

async def giveaway_create_err(interaction, error):

    if isinstance(error, app_commands.CheckFailure): await slash_silent_fail(interaction)

@giveaway_group.command(name="creator_roles", description="Set roles with which people can only create or schedule giveaways")

@app_commands.describe(role="The role to allow creating giveaways")

@slash_cmd_role("gstart")

async def giveaway_creator_roles(interaction: discord.Interaction, role: discord.Role = None):

    await interaction.response.defer(ephemeral=True)

    data = load_data()

    gid  = str(interaction.guild.id)

    gcfg = data.setdefault("giveaway_config", {}).setdefault(gid, {})

    creator_roles = gcfg.get("creator_roles", [])

    if role is None:

        if not creator_roles:

            return await interaction.followup.send(embed=info_embed("No giveaway creator roles set."), ephemeral=True)

        role_mentions = ", ".join(f"<@&{rid}>" for rid in creator_roles)

        return await interaction.followup.send(embed=info_embed(f"**Giveaway Creator Roles:** {role_mentions}"), ephemeral=True)

    if role.id in creator_roles:

        creator_roles.remove(role.id)

        msg = f"Removed {role.mention} from giveaway creator roles."

    else:

        creator_roles.append(role.id)

        msg = f"Added {role.mention} as a giveaway creator role."

    gcfg["creator_roles"] = creator_roles

    save_data(data)

    await interaction.followup.send(embed=success_embed(msg), ephemeral=True)

@giveaway_creator_roles.error

async def giveaway_creator_roles_err(interaction, error):

    if isinstance(error, app_commands.CheckFailure): await slash_silent_fail(interaction)

@giveaway_group.command(name="delete", description="Delete a giveaway")

@app_commands.describe(message_id="The giveaway message ID")

@slash_cmd_role("gdelete")

async def giveaway_delete(interaction: discord.Interaction, message_id: str):

    await interaction.response.defer()

    try:

        mid = int(message_id)

    except ValueError:

        return await interaction.followup.send(embed=error_embed("That doesn't look like a valid message ID."), ephemeral=True)

    data = load_data()

    g    = get_giveaway(data, interaction.guild.id, mid)

    if not g:

        return await interaction.followup.send(embed=error_embed("No giveaway found with that message ID."), ephemeral=True)

    channel = interaction.guild.get_channel(g["channel_id"])

    if channel:

        try:

            old_msg = await channel.fetch_message(mid)

            await old_msg.delete()

        except discord.NotFound:

            pass

    remove_giveaway(data, interaction.guild.id, mid)

    await interaction.followup.send(embed=success_embed("Giveaway cancelled and deleted."))

@giveaway_delete.error

async def giveaway_delete_err(interaction, error):

    if isinstance(error, app_commands.CheckFailure): await slash_silent_fail(interaction)

@giveaway_group.command(name="edit", description="Edit a giveaway")

@app_commands.describe(

    message_id="The giveaway message ID",

    duration="New duration to extend giveaway end time by (e.g. 30m, 1h)",

    winners="New number of winners",

    prize="New prize",

    required_role="New required role",

    color="New embed hex color",

)

@slash_cmd_role("gstart")

async def giveaway_edit(

    interaction: discord.Interaction,

    message_id: str,

    duration: str = None,

    winners: int = None,

    prize: str = None,

    required_role: discord.Role = None,

    color: str = None,

):

    await interaction.response.defer()

    try:

        mid = int(message_id)

    except ValueError:

        return await interaction.followup.send(embed=error_embed("Invalid message ID."), ephemeral=True)

    data = load_data()

    g    = get_giveaway(data, interaction.guild.id, mid)

    if not g:

        return await interaction.followup.send(embed=error_embed("No giveaway found with that message ID."), ephemeral=True)

    if g.get("ended"):

        return await interaction.followup.send(embed=error_embed("Cannot edit an ended giveaway."), ephemeral=True)

    changes = {}

    if duration:

        secs = parse_duration(duration)

        if secs is None:

            return await interaction.followup.send(embed=error_embed("Invalid duration."), ephemeral=True)

        new_end = datetime.now(timezone.utc) + timedelta(seconds=secs)

        changes["end_time"] = new_end.isoformat()

    if winners is not None:

        if winners < 1:

            return await interaction.followup.send(embed=error_embed("Winners must be at least 1."), ephemeral=True)

        changes["winners"] = winners

    if prize:

        changes["prize"] = prize

    if required_role is not None:

        changes["required_role_id"] = required_role.id

    if color:

        try:

            changes["color"] = int(color.lstrip("#"), 16)

        except ValueError:

            return await interaction.followup.send(embed=error_embed("Invalid hex color."), ephemeral=True)

    if not changes:

        return await interaction.followup.send(embed=warn_embed("No changes provided."), ephemeral=True)

    update_giveaway(data, interaction.guild.id, mid, **changes)

    g = get_giveaway(data, interaction.guild.id, mid)

    channel = interaction.guild.get_channel(g["channel_id"])

    if channel:

        try:

            msg          = await channel.fetch_message(mid)

            total        = _get_entry_count(g, interaction.guild)

            custom_color = g.get("color", GIVEAWAY_COLOR)

            ge           = giveaway_embed({**g, "message_id": mid}, total, custom_color=custom_color)

            view         = GiveawayView(mid, total)

            await msg.edit(embed=ge, view=view)

        except discord.NotFound:

            pass

    await interaction.followup.send(embed=success_embed("Giveaway updated."))

@giveaway_edit.error

async def giveaway_edit_err(interaction, error):

    if isinstance(error, app_commands.CheckFailure): await slash_silent_fail(interaction)

@giveaway_group.command(name="end", description="End a giveaway with the giveaway message ID")

@app_commands.describe(message_id="The giveaway message ID")

@slash_cmd_role("gend")

async def giveaway_end(interaction: discord.Interaction, message_id: str):

    await interaction.response.defer()

    try:

        mid = int(message_id)

    except ValueError:

        return await interaction.followup.send(embed=error_embed("That doesn't look like a valid message ID."), ephemeral=True)

    data = load_data()

    g    = get_giveaway(data, interaction.guild.id, mid)

    if not g:

        return await interaction.followup.send(embed=error_embed("No giveaway found with that message ID."), ephemeral=True)

    if g.get("ended"):

        return await interaction.followup.send(embed=error_embed("That giveaway has already ended."), ephemeral=True)

    winners = await end_giveaway(interaction.guild, mid)

    await interaction.followup.send(embed=success_embed(f"Giveaway ended. **{len(winners)}** winner(s) picked."))

@giveaway_end.error

async def giveaway_end_err(interaction, error):

    if isinstance(error, app_commands.CheckFailure): await slash_silent_fail(interaction)

@giveaway_group.command(name="fix", description="Fix a giveaway if it fails to end")

@app_commands.describe(message_id="The giveaway message ID")

@slash_cmd_role("gend")

async def giveaway_fix(interaction: discord.Interaction, message_id: str):

    await interaction.response.defer(ephemeral=True)

    try:

        mid = int(message_id)

    except ValueError:

        return await interaction.followup.send(embed=error_embed("Invalid message ID."), ephemeral=True)

    data = load_data()

    g    = get_giveaway(data, interaction.guild.id, mid)

    if not g:

        return await interaction.followup.send(embed=error_embed("No giveaway found with that message ID."), ephemeral=True)

    update_giveaway(data, interaction.guild.id, mid, ended=False)

    winners = await end_giveaway(interaction.guild, mid)

    await interaction.followup.send(

        embed=success_embed(f"Giveaway fixed and ended. **{len(winners)}** winner(s) picked."), ephemeral=True)

@giveaway_fix.error

async def giveaway_fix_err(interaction, error):

    if isinstance(error, app_commands.CheckFailure): await slash_silent_fail(interaction)

@giveaway_group.command(name="list", description="List all active giveaways in this server")

async def giveaway_list(interaction: discord.Interaction):

    await interaction.response.defer()

    data   = load_data()

    gmap   = get_guild_giveaways(data, interaction.guild.id)

    active = {mid: g for mid, g in gmap.items() if not g.get("ended")}

    if not active:

        return await interaction.followup.send(embed=info_embed("There are no active giveaways in this server."))

    embed = discord.Embed(title="🎉 Active Giveaways", color=GIVEAWAY_COLOR,

                          timestamp=datetime.now(timezone.utc))

    for mid, g in active.items():

        end_ts        = int(datetime.fromisoformat(g["end_time"]).timestamp())

        total_entries = _get_entry_count(g, interaction.guild)

        embed.add_field(

            name=f"{g['prize']}  (ID: {mid})",

            value=f"Winners: **{g['winners']}**  •  Entries: **{total_entries}**  •  Ends <t:{end_ts}:R>",

            inline=False)

    await interaction.followup.send(embed=embed)

@giveaway_group.command(name="reroll", description="Reroll the winner(s) of an ended giveaway")

@app_commands.describe(message_id="The giveaway message ID")

@slash_cmd_role("greroll")

async def giveaway_reroll(interaction: discord.Interaction, message_id: str):

    await interaction.response.defer()

    try:

        mid = int(message_id)

    except ValueError:

        return await interaction.followup.send(embed=error_embed("That doesn't look like a valid message ID."), ephemeral=True)

    data = load_data()

    g    = get_giveaway(data, interaction.guild.id, mid)

    if not g:

        return await interaction.followup.send(embed=error_embed("No giveaway found with that message ID."), ephemeral=True)

    if not g.get("ended"):

        return await interaction.followup.send(embed=error_embed("That giveaway hasn't ended yet. Use `/giveaway end` first."), ephemeral=True)

    winners = await end_giveaway(interaction.guild, mid, reroll=True)

    if winners:

        await interaction.followup.send(embed=success_embed(f"Rerolled. **{len(winners)}** new winner(s) picked."))

    else:

        await interaction.followup.send(embed=warn_embed("No valid entries left to reroll from."))

@giveaway_reroll.error

async def giveaway_reroll_err(interaction, error):

    if isinstance(error, app_commands.CheckFailure): await slash_silent_fail(interaction)

bot.tree.add_command(giveaway_group)

# ══════════════════════════════════════════════════════════════════════════════

#  SLASH — MISC

# ══════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="modcases", description="[Staff] Show actions taken by a moderator, or the full server case history")

@app_commands.describe(member="Moderator to look up (leave blank for full server history)")

@slash_cmd_role("modcases")

async def modcases_slash(interaction: discord.Interaction, member: discord.Member = None):

    await interaction.response.defer()

    data  = load_data()

    embed = build_modcases_embed(interaction.guild, data, member, requester=interaction.user)

    await interaction.followup.send(embed=embed)

@modcases_slash.error

async def modcases_slash_err(interaction, error):

    if isinstance(error, app_commands.CheckFailure): await slash_silent_fail(interaction)

@bot.tree.command(name="afk", description="Set yourself as AFK")

@app_commands.describe(reason="Reason for going AFK (optional)")

async def afk_slash(interaction: discord.Interaction, reason: str = "AFK"):

    uid = interaction.user.id

    if uid in afk_users:

        return await interaction.response.send_message(

            embed=warn_embed("You are already AFK. Send any message to remove it."), ephemeral=True)

    afk_users[uid] = {"reason": reason, "since": datetime.now(timezone.utc)}

    embed = discord.Embed(

        description=f"💤 **{interaction.user.display_name}** is now AFK\n📝 {reason}",

        color=discord.Color.greyple(), timestamp=datetime.now(timezone.utc))

    embed.set_footer(text="You'll be removed from AFK when you next send a message.")

    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="role", description="Give or remove a role from a member")

@app_commands.describe(member="Member to give/remove the role", role="Role to add or remove")

@slash_cmd_role("role")

async def role_slash(interaction: discord.Interaction, member: discord.Member, role: discord.Role):

    await interaction.response.defer()

    if role >= interaction.guild.me.top_role:

        return await interaction.followup.send(

            embed=error_embed("I can't manage that role — it's equal to or higher than my highest role."), ephemeral=True)

    if role >= interaction.user.top_role and not interaction.user.guild_permissions.administrator:

        return await interaction.followup.send(

            embed=error_embed("You can't assign a role equal to or higher than your own highest role."), ephemeral=True)

    try:

        if role in member.roles:

            await member.remove_roles(role, reason=f"Role removed by {interaction.user}")

            await interaction.followup.send(embed=discord.Embed(

                description=f"➖ Removed {role.mention} from {member.mention}.",

                color=discord.Color.orange(), timestamp=datetime.now(timezone.utc)

            ).set_footer(text=f"Done by {interaction.user}", icon_url=interaction.user.display_avatar.url))

        else:

            await member.add_roles(role, reason=f"Role given by {interaction.user}")

            await interaction.followup.send(embed=discord.Embed(

                description=f"➕ Added {role.mention} to {member.mention}.",

                color=discord.Color.green(), timestamp=datetime.now(timezone.utc)

            ).set_footer(text=f"Done by {interaction.user}", icon_url=interaction.user.display_avatar.url))

    except discord.Forbidden:

        await interaction.followup.send(embed=error_embed("I don't have permission to manage that role."), ephemeral=True)

@role_slash.error

async def role_slash_err(interaction, error):

    if isinstance(error, app_commands.CheckFailure): await slash_silent_fail(interaction)

# ══════════════════════════════════════════════════════════════════════════════


rejected_group = app_commands.Group(name="rejected", description="Rejected appeal actions")

@rejected_group.command(name="appeal", description="Assign the configured special role after a rejected appeal")
@app_commands.describe(member="Member to give the special role", reason="Reason for rejecting the appeal")
@slash_cmd_role("rappeal")
async def rejected_appeal_slash(interaction: discord.Interaction, member: discord.Member, reason: str = "Appeal rejected"):

    success, result = await apply_appeal_role(member, f"Appeal rejected by {interaction.user}: {reason}")

    if not success:

        return await interaction.response.send_message(embed=error_embed(result), ephemeral=True)

    role = result

    embed = discord.Embed(

        title="🎯 Appeal Rejected",

        description=f"{member.mention} was given {role.mention}.",

        color=discord.Color.orange(), timestamp=datetime.now(timezone.utc))

    embed.add_field(name="Reason", value=reason, inline=False)

    embed.set_footer(text=f"Done by {interaction.user}", icon_url=interaction.user.display_avatar.url)

    await interaction.response.send_message(embed=embed)

@rejected_appeal_slash.error

async def rejected_appeal_slash_err(interaction, error):

    if isinstance(error, app_commands.CheckFailure): await slash_silent_fail(interaction)

bot.tree.add_command(rejected_group)


@bot.tree.command(name="dwc", description="[Mod] Give the configured DWC role to a member")
@app_commands.describe(member="Member to give the DWC role", reason="Reason")
@slash_cmd_role("dwc")
async def dwc_slash(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):

    success, result = await apply_dwc_role(member, f"DWC role given by {interaction.user}: {reason}")

    if not success:

        return await interaction.response.send_message(embed=error_embed(result), ephemeral=True)

    role = result

    data = load_data()

    cid  = add_case(data, interaction.guild.id, "DWC", interaction.user, member, reason)

    embed = discord.Embed(

        title=f"🏷️ DWC Role Given | Case #{cid}",

        description=f"{member.mention} was given {role.mention}.",

        color=discord.Color.blurple(), timestamp=datetime.now(timezone.utc))

    embed.add_field(name="Reason", value=reason, inline=False)

    embed.set_footer(text=f"Done by {interaction.user}", icon_url=interaction.user.display_avatar.url)

    view = ProofView(interaction.guild, cid, "DWC", interaction.user, member, reason)

    await interaction.response.send_message(embed=embed, view=view)

@dwc_slash.error

async def dwc_slash_err(interaction, error):

    if isinstance(error, app_commands.CheckFailure): await slash_silent_fail(interaction)


@bot.tree.command(name="rdwc", description="[Mod] Remove the configured DWC role from a member")
@app_commands.describe(member="Member to remove the DWC role from", reason="Reason")
@slash_cmd_role("rdwc")
async def rdwc_slash(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):

    success, result = await remove_dwc_role(member, f"DWC role removed by {interaction.user}: {reason}")

    if not success:

        return await interaction.response.send_message(embed=error_embed(result), ephemeral=True)

    role = result

    data = load_data()

    cid  = add_case(data, interaction.guild.id, "Remove DWC", interaction.user, member, reason)

    embed = discord.Embed(

        title=f"🏷️ DWC Role Removed | Case #{cid}",

        description=f"{role.mention} was removed from {member.mention}.",

        color=discord.Color.green(), timestamp=datetime.now(timezone.utc))

    embed.add_field(name="Reason", value=reason, inline=False)

    embed.set_footer(text=f"Done by {interaction.user}", icon_url=interaction.user.display_avatar.url)

    view = ProofView(interaction.guild, cid, "Remove DWC", interaction.user, member, reason)

    await interaction.response.send_message(embed=embed, view=view)

@rdwc_slash.error

async def rdwc_slash_err(interaction, error):

    if isinstance(error, app_commands.CheckFailure): await slash_silent_fail(interaction)

#  PING COMMAND

# ══════════════════════════════════════════════════════════════════════════════

@bot.command(name="ping")

@require_cmd_role("ping")

async def ping_cmd(ctx):

    latency_ms = round(bot.latency * 1000)

    embed = discord.Embed(

        description=f"🏓 Pong! Latency: **{latency_ms}ms**",

        color=discord.Color.green())

    await ctx.send(embed=embed)

@ping_cmd.error

async def ping_cmd_error(ctx, error):

    if isinstance(error, commands.CheckFailure):

        return

@bot.tree.command(name="ping", description="[Mod] Show the bot's latency")

@slash_cmd_role("ping")

async def ping_slash(interaction: discord.Interaction):

    latency_ms = round(bot.latency * 1000)

    embed = discord.Embed(

        description=f"🏓 Pong! Latency: **{latency_ms}ms**",

        color=discord.Color.green())

    await interaction.response.send_message(embed=embed)

@ping_slash.error

async def ping_slash_err(interaction, error):

    if isinstance(error, app_commands.CheckFailure): await slash_silent_fail(interaction)

# ══════════════════════════════════════════════════════════════════════════════

# ???????????????????????????????????????????????????????????????????????????????
#
#  MESSAGE STATS
#
# ???????????????????????????????????????????????????????????????????????????????

@bot.command(name="messages")
async def messages_cmd(ctx, member: discord.Member = None):
    member = member or ctx.author
    data = load_data()
    stats = get_message_stats(data, ctx.guild.id, member.id)
    await ctx.send(embed=build_messages_embed(member, stats))


@bot.tree.command(name="messages", description="Show a member's message statistics")
@app_commands.describe(member="Member to view (leave blank for yourself)")
async def messages_slash(interaction: discord.Interaction, member: discord.Member = None):
    member = member or interaction.user
    data = load_data()
    stats = get_message_stats(data, interaction.guild.id, member.id)
    await interaction.response.send_message(embed=build_messages_embed(member, stats))

#  HELP COMMAND

# ══════════════════════════════════════════════════════════════════════════════

@bot.command(name="help")

async def help_cmd(ctx):

    embed = discord.Embed(

        title="📖  Bot Command Reference",

        description="All available commands. `< >` = required  •  `[ ]` = optional\nSlash commands shown as `/cmd` — prefix commands as `.cmd`",

        color=discord.Color.blurple(), timestamp=datetime.now(timezone.utc))

    embed.add_field(name="⚔️  Moderation — Full Mod / Admin", value=(

        "`ban` `.ban @user [reason]` — Permanently ban a member\n"

        "`unban` `.unban <user_id>` — Unban by ID\n"

        "`kick` `.kick @user [reason]` — Kick a member\n"

        "`jail` `.jail @user [reason]` — Jail a member (hides all channels)\n"

        "`unjail` `.unjail @user` — Release from jail\n"

        "`purge` `.purge <amount>` — Bulk delete messages (max 100)\n"

        "`purgeuser` `.purgeuser @user [amount]` — Delete one member's messages (default 100, max 200)"), inline=False)

    embed.add_field(name="🔇  Muting — Mod / Trial Moderator / Admin", value=(

        "`mute` `.mute @user [duration] [reason]` — Mute a member\n"

        "　　Duration: `10m` `2h` `1d` `1h30m` *(default: 14 days)*\n"

        "`unmute` `.unmute @user` — Remove mute early"), inline=False)

    embed.add_field(name="⚠️  Warnings — Mod / Trial Moderator / Admin", value=(

        "`warn` `.warn @user [reason]` — Add a warning\n"

        "`unwarn` `.unwarn @user <#>` — Remove a specific warning\n"

        "`warnings` `.warnings @user` — View all warnings"), inline=False)

    embed.add_field(name="📋  Records — Mod / Trial Moderator / Admin", value=(

        "`modlogs` `.modlogs @user` — Full mod history\n"

        "`case` `.case <number>` — View a single case\n"

        "`editcase` `.editcase <number>` — Interactively edit a case\n"

        "`editc` `.editc <number> <new reason>` — Quickly update a case reason\n"

        "`w` `.w [@user|id]` — Detailed user info\n"

        "`modcases` `.modcases [@mod]` — Actions by a mod, or full server case history if blank\n"

        "`messages` `.messages [@user]` — View a member's message statistics"), inline=False)

    embed.add_field(name="🔒  Jail Requests — Mod / Trial Moderator", value=(

        "`jreq` `.jreq @user [reason]` — Submit a jail request for a full mod to review\n"

        "`/jailreq` `/jailreq @user [reason]` — Same as above (slash version)"), inline=False)

    embed.add_field(name="🎉  Giveaways — Mod / Admin", value=(

        "`/giveaway create` — Start a giveaway\n"

        "`/giveaway edit` — Edit an active giveaway\n"

        "`/giveaway end` — End a giveaway early\n"

        "`/giveaway fix` — Fix a giveaway that failed to end\n"

        "`/giveaway reroll` — Reroll winner(s)\n"

        "`/giveaway delete` — Cancel a giveaway\n"

        "`/giveaway list` — List active giveaways\n"

        "`/giveaway creator_roles` — Set roles that can create/schedule giveaways"), inline=False)

    embed.add_field(name="🛠️  Utility", value=(

        "`role` `.role @user <role name>` — Add/remove a role\n"

        "`rappeal` `.rappeal @user [reason]` — Give the configured unappeal role\n"

        "`/rejected appeal` `/rejected appeal @user [reason]` — Same as above (slash version)\n"

        "`dwc` `.dwc @user [reason]` — Give the configured DWC role\n"

        "`/dwc` `/dwc @user [reason]` — Same as above (slash version)\n"

        "`rdwc` `.rdwc @user [reason]` — Remove the configured DWC role\n"

        "`/rdwc` `/rdwc @user [reason]` — Same as above (slash version)\n"

        "`addcmd` `.addcmd <name> <response>` — Create a custom command\n"

        "`delcmd` `.delcmd <name>` — Delete a custom command\n"

        "`listcmds` `.listcmds` — Show all custom commands\n"

        "`afk` `.afk [reason]` — Set AFK status\n"

        "`messages` `.messages [@user]` — View message statistics\n"

        "`ping` `.ping` — Bot latency"), inline=False)

    embed.add_field(name="⚙️  Setup — Head Executives / Admin", value=(

        "`setup` `.setup` — Create log channels and roles\n"

        "`setup perms` `.setup perms` — Categorized permission manager (interactive)\n"

        "`/setup perms` `/setup perms` — Same as above (slash version)\n"

        "`setjreqchannel` `.setjreqchannel #channel` — Set the jail requests channel\n"

        "`/setjreqchannel` `/setjreqchannel #channel` — Same as above (slash version)"), inline=False)

    embed.set_footer(text=f"Requested by {ctx.author}  •  Use .setup to get started",

                     icon_url=ctx.author.display_avatar.url)

    await ctx.send(embed=embed)

# ══════════════════════════════════════════════════════════════════════════════

import psutil

@bot.command(name="sysinfo")
@require_full_mod()
async def sysinfo_cmd(ctx):
    process = psutil.Process(os.getpid())
    mem_mb  = process.memory_info().rss / (1024 * 1024)
    cpu_pct = process.cpu_percent(interval=0.5)

    embed = discord.Embed(
        title="📊 Bot Resource Usage",
        color=discord.Color.blurple())
    embed.add_field(name="Memory (RSS)", value=f"{mem_mb:.1f} MB", inline=True)
    embed.add_field(name="CPU", value=f"{cpu_pct:.1f}%", inline=True)
    embed.add_field(name="Guilds", value=str(len(bot.guilds)), inline=True)
    embed.add_field(name="Cached Members", value=str(sum(g.member_count for g in bot.guilds)), inline=True)
    await ctx.send(embed=embed)

# ══════════════════════════════════════════════════════════════════════════════

if not TOKEN:

    raise SystemExit(

        "DISCORD_TOKEN is not set. Create a .env file with:\n"

        "DISCORD_TOKEN=your_token_here\n")

bot.run(TOKEN)