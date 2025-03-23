import discord
from discord.ext import commands, tasks
import asyncio
import os
import logging
import yt_dlp as youtube_dl
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor
import random

import os.path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# --- 1. Configuration and Secrets Management ---
load_dotenv(dotenv_path="tokes.env")
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
if not DISCORD_TOKEN:
    print("CRITICAL: Discord token not found in environment variables!")
    exit(1)

# --- 2. Logging Setup ---
if not os.path.exists("logs"):
    os.makedirs("logs")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("logs/bot_log.log", encoding='utf-8'),  # Specify encoding
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)

# --- 3. Bot Setup ---
intents = discord.Intents.all()
bot = commands.Bot(command_prefix='!', intents=intents)


# --- 4. Ensure Downloads Directory Exists ---
DOWNLOAD_DIR = os.path.abspath("downloads")
if not os.path.exists(DOWNLOAD_DIR):
    os.makedirs(DOWNLOAD_DIR)

# --- 5. Thread Pool for Parallel Downloads ---
executor = ThreadPoolExecutor(max_workers=4)

# --- 6. yt-dlp Options ---
ytdl_format_options = {
    'format': 'bestaudio/best',
    'outtmpl': f'{DOWNLOAD_DIR}/%(title)s.%(ext)s',
    'quiet': True,  # Changed to True to reduce console output
    'default_search': 'ytsearch',
    'noplaylist': True,
    'restrictfilenames': True,
    'postprocessors': [{
        'key': 'FFmpegExtractAudio',
        'preferredcodec': 'm4a',
        'preferredquality': '192',
    }],
}

ffmpeg_options = {
    'options': '-vn -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -loglevel error'
}

ytdl = youtube_dl.YoutubeDL(ytdl_format_options)

# --- 7. Queue System ---
queues = {}  # {guild_id: [song1, song2, ...]}


def get_queue(guild_id):
    if guild_id not in queues:
        queues[guild_id] = []  # Use a list for simplicity
    return queues[guild_id]


class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, file_path, volume=0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get('title')
        self.url = data.get('webpage_url')
        self.file_path = file_path

    @classmethod
    async def from_url(cls, url, *, loop=None, download=True):
        loop = loop or asyncio.get_event_loop()

        try:
            data = await loop.run_in_executor(executor, lambda: ytdl.extract_info(url, download=download))
        except Exception as e:
            logger.error(f"yt-dlp extract_info error: {e}")
            return None

        if 'entries' in data:
            songs = []
            for entry in data['entries']:
                if not entry:
                    continue

                file_path = ytdl.prepare_filename(entry)
                file_path = f"{os.path.splitext(file_path)[0]}.m4a"

                if os.path.exists(file_path):
                    try:
                        song = cls(discord.FFmpegPCMAudio(file_path, **ffmpeg_options), data=entry, file_path=file_path)
                        songs.append(song)
                    except Exception as e:
                        logger.error(f"FFmpegPCMAudio error: {e}, file: {file_path}")
                        continue # Skip adding the song
                else:
                    logger.warning(f"File does not exist: {file_path}")
            return songs if songs else None
        else:
            file_path = ytdl.prepare_filename(data)
            file_path = f"{os.path.splitext(file_path)[0]}.m4a"

            if os.path.exists(file_path):
                try:
                    return cls(discord.FFmpegPCMAudio(file_path, **ffmpeg_options), data=data, file_path=file_path)
                except Exception as e:
                    logger.error(f"FFmpegPCMAudio error: {e}, file: {file_path}")
                    return None
            else:
                logger.warning(f"File does not exist: {file_path}")
                return None


async def play_next(ctx):
    """Plays the next song in the queue."""
    guild_id = ctx.guild.id
    queue = get_queue(guild_id)
    voice_client = ctx.voice_client

    if not queue:
        await ctx.send("🎵 Queue is empty! Add more songs with `!play`.")
        return

    next_song = queue.pop(0)  # Get the first song from the queue

    def after_playing(error):
        if error:
            logger.error(f"Stream error: {error}")
            asyncio.run_coroutine_threadsafe(ctx.send(f"Error playing: {error}"), bot.loop)  #Inform the user.
        else:
            if os.path.exists(next_song.file_path):
                try:
                    os.remove(next_song.file_path)  # Clean up the downloaded file
                except Exception as e:
                    logger.error(f"Error deleting file: {next_song.file_path}, {e}")
            asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop)

    try:
        voice_client.play(next_song, after=after_playing)
        await ctx.send(f"🎶 Now playing: **{next_song.title}**")
    except Exception as e:
        logger.error(f"Error during playback: {e}")
        await ctx.send(f"Error playing the song: {e}")
        asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop) # Attempt to play the next song


@bot.command(name='play', help='Plays a song or playlist')
async def play(ctx, *, query):
    """Plays a song or playlist."""
    voice_client = ctx.voice_client

    if not voice_client:
        await ctx.send("Join a voice channel first using `!join`.")
        return

    async with ctx.typing():
        try:
            songs = await YTDLSource.from_url(query, loop=bot.loop)
            if not songs:
                await ctx.send("⚠️ Could not retrieve the song.")
                return

            queue = get_queue(ctx.guild.id)
            if isinstance(songs, list):
                playlist_title = songs[0].data.get('playlist_title', 'Unknown Playlist')
                duration = sum(song.data.get('duration', 0) for song in songs)
                playlist_length = len(songs)
                queue.extend(songs)

                await ctx.send(
                    f"📜 **Added Playlist**\n**Playlist:** {playlist_title}\n"
                    f"**Playlist Length:** {str(duration // 3600).zfill(2)}:"
                    f"{str((duration % 3600) // 60).zfill(2)}:"
                    f"{str(duration % 60).zfill(2)} | **Tracks:** {playlist_length}"
                )
            else:
                queue.append(songs)
                await ctx.send(f"🎵 **Added to queue:** {songs.title}")

            if not voice_client.is_playing():
                await play_next(ctx)

        except Exception as e:
            logger.error(f"yt-dlp Error: {e}", exc_info=True)
            await ctx.send(f"⚠️ Failed to retrieve the song: {e}")


@bot.command(name='queue', help='Shows the current queue')
async def queue(ctx):
    """Shows the current queue."""
    queue = get_queue(ctx.guild.id)

    if not queue:
        await ctx.send("🎵 The queue is currently empty!")
        return

    # Create pages for the queue
    songs_per_page = 10
    pages = [queue[i:i + songs_per_page] for i in range(0, len(queue), songs_per_page)]
    current_page = 0

    async def update_embed(page_num):
        start_index = page_num * songs_per_page
        end_index = min(len(queue), (page_num + 1) * songs_per_page)
        upcoming_songs = queue[start_index:end_index]

        queue_list = "\n".join(f"{start_index + i + 1}. {song.title}" for i, song in enumerate(upcoming_songs))
        embed = discord.Embed(title=f"📜 **Upcoming Songs (Page {page_num + 1}/{len(pages)})**",
                              description=f"``````",
                              color=discord.Color.blue())
        return embed

    embed = await update_embed(current_page)
    message = await ctx.send(embed=embed)

    # Add navigation buttons if there are multiple pages
    if len(pages) > 1:
        await message.add_reaction("⬅️")
        await message.add_reaction("➡️")

        def check(reaction, user):
            return user == ctx.author and reaction.message.id == message.id and str(reaction.emoji) in ["⬅️", "➡️"]

        while True:
            try:
                reaction, user = await bot.wait_for("reaction_add", timeout=60.0, check=check)

                if str(reaction.emoji) == "➡️" and current_page < len(pages) - 1:
                    current_page += 1
                    embed = await update_embed(current_page)
                    await message.edit(embed=embed)
                    await message.remove_reaction(reaction, user)
                elif str(reaction.emoji) == "⬅️" and current_page > 0:
                    current_page -= 1
                    embed = await update_embed(current_page)
                    await message.edit(embed=embed)
                    await message.remove_reaction(reaction, user)
                else:
                    await message.remove_reaction(reaction, user)

            except asyncio.TimeoutError:
                # Remove reactions after timeout
                try:
                    await message.clear_reactions()
                except discord.errors.Forbidden:
                    logger.warning("Missing permissions to clear reactions.")
                break


@bot.command(name='skip', help='Skips the current song')
async def skip(ctx):
    """Skips the currently playing song."""
    voice_client = ctx.voice_client
    if voice_client and voice_client.is_playing():
        await ctx.send("⏭️ **Skipping current song...**")
        voice_client.stop()  # This will trigger `play_next(ctx)`
    else:
        await ctx.send("⚠️ No song is currently playing.")


@bot.command(name='join', help='Bot joins the voice channel')
async def join(ctx):
    """Joins the voice channel."""
    if ctx.author.voice:
        channel = ctx.author.voice.channel
        try:
            await channel.connect()
        except discord.errors.ClientException:
            await ctx.send("I am already in a voice channel.")
    else:
        await ctx.send("Join a voice channel first.")


@bot.command(name='leave', help='Bot leaves the voice channel')
async def leave(ctx):
    """Leaves the voice channel."""
    voice_client = ctx.voice_client
    if voice_client:
        await voice_client.disconnect()
        queue = get_queue(ctx.guild.id)
        queue.clear()  # Clear the queue when leaving
        await ctx.send("👋 Left the voice channel and cleared the queue.")
    else:
        await ctx.send("I'm not in a voice channel.")


@bot.command(name='clear', help='Clears the song queue')
async def clear(ctx):
    """Clears the song queue."""
    queue = get_queue(ctx.guild.id)
    queue.clear()
    await ctx.send("🗑️ Queue cleared!")


@bot.command(name='remove', help='Removes a song from the queue by its position')
async def remove(ctx, position: int):
    """Removes a song from the queue."""
    queue = get_queue(ctx.guild.id)
    if 1 <= position <= len(queue):
        removed_song = queue.pop(position - 1)
        await ctx.send(f"❌ Removed **{removed_song.title}** from the queue.")
    else:
        await ctx.send("Invalid position in the queue.")

@bot.command(name='shuffle', help='Shuffles the song queue')
async def shuffle(ctx):
    """Shuffles the song queue."""
    queue = get_queue(ctx.guild.id)
    if len(queue) > 1:
        random.shuffle(queue)
        await ctx.send("🔀 Queue shuffled!")
    else:
        await ctx.send("Not enough songs in the queue to shuffle.")

@bot.command(name='loop', help='Loops the current song or queue')
async def loop(ctx, mode: str = "song"):
    """Loops the current song or the entire queue."""
    # Implement looping logic here
    await ctx.send("Looping is not yet implemented.")

@bot.command(name='siata', help='Pokazuje infromacje o siatkówce')
async def siatka(ctx):
    """
    Fetches volleyball data from a Google Sheet and displays it in a formatted Discord message.
    """

    SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    SAMPLE_SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")  #From .env file
    SAMPLE_RANGE_NAME = "Arkusz1!A1:J16"
    CREDENTIALS_FILE = "credentials.json"
    TOKEN_FILE = "token.json"

    if not SAMPLE_SPREADSHEET_ID:
        await ctx.send("Error: Spreadsheet ID not set in environment variables.")
        return

    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:
                await ctx.send(f"Error refreshing credentials: {e}")
                return
        else:
            if not os.path.exists(CREDENTIALS_FILE):
                await ctx.send(f"Error: Credentials file '{CREDENTIALS_FILE}' not found.")
                return

            try:
                flow = InstalledAppFlow.from_client_secrets_file(
                    CREDENTIALS_FILE, SCOPES
                )
                creds = flow.run_local_server(port=0)
            except Exception as e:
                await ctx.send(f"Error during authentication: {e}")
                return

        with open(TOKEN_FILE, "w") as token:
            token.write(creds.to_json())

    try:
        service = build("sheets", "v4", credentials=creds)
        sheet = service.spreadsheets()
        result = sheet.values().get(spreadsheetId=SAMPLE_SPREADSHEET_ID, range=SAMPLE_RANGE_NAME).execute()
        values = result.get("values", [])

        if not values:
            await ctx.send("No data found in the spreadsheet.")
            return

        siatka = []
        for row in values:
            cleaned_row = [str(item).strip() for item in row if str(item).strip()]
            if cleaned_row:
                siatka.append(cleaned_row)

        embed = discord.Embed(
            title="Informacje o Siatkówce",
            color=discord.Color.blue()
        )

        for i, row in enumerate(siatka):
            if len(row) > 1:
                row_string = "".join(row[1:])  # Concatenate from the second element
            else:
                row_string = "".join(row)

            if len(row_string) > 1024:
                row_string = row_string[:1021] + "..."

            embed.add_field(name=f"{row[0]}", value=row_string, inline=False)

        if len(embed) > 6000:
            await ctx.send("Error: The data is too large to display.")
            return

        await ctx.send(embed=embed)

    except HttpError as err:
        await ctx.send(f"An HTTP error occurred: {err}")
        logger.exception(f"Google Sheets API error: {err}")
    except Exception as e:
        await ctx.send(f"An unexpected error occurred: {e}")
        logger.exception(f"An unexpected error occurred: {e}")

# --- 8. Event Listeners ---
@bot.event
async def on_ready():
    """Prints bot information to the console when the bot is ready."""
    print(f'Logged in as {bot.user.name} ({bot.user.id})')
    print('------')
    await bot.change_presence(activity=discord.Game(name="Listening to !help"))

# --- 9. Error Handling ---
@bot.event
async def on_command_error(ctx, error):
    """Handles command errors."""
    if isinstance(error, commands.errors.CommandNotFound):
        await ctx.send("Invalid command. Use !help to see available commands.")
    elif isinstance(error, commands.errors.MissingRequiredArgument):
        await ctx.send("You are missing a required argument.  Check !help for command usage.")
    elif isinstance(error, commands.errors.BadArgument):
        await ctx.send("Invalid argument. Check !help for command usage.")
    else:
        logger.error(f"Command error: {error}", exc_info=True)
        await ctx.send(f"An error occurred: {error}")


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
