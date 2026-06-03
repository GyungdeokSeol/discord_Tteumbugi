import os
import re
import random
from dotenv import load_dotenv
import discord
from discord import app_commands
from discord.ext import commands
import yt_dlp
import asyncio

# --- 설정값 ---
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')

intents = discord.Intents.default()
intents.message_content = True

class MusicBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix='!', intents=intents, help_command=None)

    async def setup_hook(self):
        await self.tree.sync()
        print("모든 명령어 동기화 완료!")

bot = MusicBot()

# --- 데이터 저장소 ---
server_data = {} 
current_song = {}    
status_messages = {} 
is_paused = {}       
played_history = {}  # ⭐ A-B-A-B 굴레 방지를 위한 최근 재생 기록 {guild_id: [video_id1, ...]}

# --- 유튜브/FFmpeg 옵션 ---
yt_dl_opts = {
    'format': 'bestaudio/best',
    'noplaylist': True,
    'quiet': True,
    'cookiefile': 'cookies.txt',
}
ytdl = yt_dlp.YoutubeDL(yt_dl_opts)

yt_dl_opts_playlist = {
    'extract_flat': 'in_playlist',
    'playlistend': 20, 
    'quiet': True,
    'cookiefile': 'cookies.txt',
}
ytdl_playlist = yt_dlp.YoutubeDL(yt_dl_opts_playlist)

ffmpeg_options = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'
}

# --- Helper 함수들 ---
async def delete_later(msg, delay):
    await asyncio.sleep(delay)
    try: await msg.delete()
    except: pass

async def send_alert(interaction, text, delay=10):
    try:
        msg = None
        if not interaction.response.is_done():
            await interaction.response.send_message(text, ephemeral=False)
            msg = await interaction.original_response()
        else:
            msg = await interaction.followup.send(text, ephemeral=False)
        if msg: asyncio.create_task(delete_later(msg, delay))
    except Exception as e:
        print(f"메시지 전송 오류: {e}")

# ⭐ 완벽하게 복구 및 수정된 라운드 로빈(사용자 순번 교대) 디스플레이 로직
def get_display_queue(guild_id):
    if guild_id not in server_data: return []
    data = server_data[guild_id]
    temp_order = list(data['user_order']) 
    temp_songs = {uid: list(songs) for uid, songs in data['user_songs'].items()}
    
    display_list = []
    while temp_order:
        user_id = temp_order.pop(0)
        if temp_songs[user_id]:
            song = temp_songs[user_id].pop(0)
            display_list.append(song)
            # 유저에게 남은 곡이 있다면 순번을 맨 뒤로 돌려보냄 (라운드 로빈 유지)
            if temp_songs[user_id]: 
                temp_order.append(user_id)
    return display_list

# --- UI Components ---
class AddSongModal(discord.ui.Modal, title='노래 추가하기'):
    query = discord.ui.TextInput(label='유튜브 URL 또는 검색어', placeholder='듣고 싶은 노래를 입력하세요.')
    async def on_submit(self, interaction: discord.Interaction):
        await send_alert(interaction, f"🔍 **{self.query.value}** 검색 중...", 5)
        await add_song_logic(interaction, self.query.value)

class AddPlaylistModal(discord.ui.Modal, title='재생목록 추가하기'):
    url = discord.ui.TextInput(label='유튜브 재생목록 URL', placeholder='https://www.youtube.com/playlist?list=...', required=True)
    async def on_submit(self, interaction: discord.Interaction):
        await send_alert(interaction, "📂 재생목록을 분석하고 곡을 담는 중입니다...", 5)
        await add_playlist_logic(interaction, self.url.value)

class MusicControlView(discord.ui.View):
    def __init__(self, guild_id):
        super().__init__(timeout=None)
        self.guild_id = guild_id

    @discord.ui.button(label="노래 추가", style=discord.ButtonStyle.green, emoji="➕")
    async def add_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddSongModal())

    @discord.ui.button(label="재생목록", style=discord.ButtonStyle.success, emoji="📂")
    async def playlist_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddPlaylistModal())

    # ⭐ 즉시 자동 추천 버튼 신규 추가
    @discord.ui.button(label="자동 추천 시작", style=discord.ButtonStyle.secondary, emoji="✨")
    async def autoplay_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await start_autoplay_logic(interaction)

    @discord.ui.button(label="일시정지/재생", style=discord.ButtonStyle.primary, emoji="⏯️")
    async def pause_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        voice_client = interaction.guild.voice_client
        if not voice_client or not voice_client.is_playing():
            if not is_paused.get(self.guild_id, False):
                await send_alert(interaction, "재생 중인 노래가 없습니다.")
                return
        if voice_client.is_paused():
            voice_client.resume()
            is_paused[self.guild_id] = False
            await send_alert(interaction, "▶️ 다시 재생합니다.")
        else:
            voice_client.pause()
            is_paused[self.guild_id] = True
            await send_alert(interaction, "⏸️ 일시정지했습니다.")
        await update_status_message(interaction.guild)

    @discord.ui.button(label="스킵", style=discord.ButtonStyle.secondary, emoji="⏭️")
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        voice_client = interaction.guild.voice_client
        if voice_client and (voice_client.is_playing() or voice_client.is_paused()):
            voice_client.stop()
            await send_alert(interaction, "⏭️ 스킵됨.")
        else:
            await send_alert(interaction, "스킵할 노래가 없습니다.")

    @discord.ui.button(label="정지", style=discord.ButtonStyle.danger, emoji="⏹️")
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await stop_logic(interaction.guild)

# --- 상태창 업데이트 ---
# --- 상태창 업데이트 함수 ---
async def update_status_message(guild):
    guild_id = guild.id
    channel = status_messages.get(guild_id)
    if not channel: return
    try: channel = channel.channel
    except: return

    embed = discord.Embed(title="🎧 Music Player", color=0x9900ff)
    
    # ⭐ 1. 현재 곡 정보 (여기도 1024자 초과 방어막 추가!)
    if guild_id in current_song and current_song[guild_id]:
        song = current_song[guild_id]
        status = "⏸️ 일시정지" if is_paused.get(guild_id, False) else "▶️ 재생 중"
        
        # 제목이 비정상적으로 길면 50자에서 컷!
        title = song['title']
        if len(title) > 50: title = title[:47] + "..."
        
        value_text = f"[{title}]({song['web_url']})\n🎤 신청자: **{song['requester']}**"
        
        # 만약 URL 자체에 꼬리표가 너무 많이 붙어서 1000자가 넘어가면 강제 컷!
        if len(value_text) > 1000:
            value_text = value_text[:950] + "...\n*(링크가 너무 길어 생략됨)*"
            
        embed.add_field(name=status, value=value_text, inline=False)
    else:
        embed.add_field(name="💤 상태", value="대기 중...", inline=False)

    # ⭐ 2. 전체 대기열 표시 (기존 방어막 유지)
    display_queue = get_display_queue(guild_id)
    if display_queue:
        queue_text = ""
        max_display = min(10, len(display_queue)) 
        for i in range(max_display):
            song = display_queue[i]
            title = song['title'] if len(song['title']) <= 35 else song['title'][:35] + "..."
            queue_text += f"`{i+1}.` {title} - {song['requester']}\n"
        if len(display_queue) > max_display:
            queue_text += f"\n*...그리고 **{len(display_queue) - max_display}곡**이 더 대기 중입니다! 🎶*"
        
        if len(queue_text) > 1000:
            queue_text = queue_text[:950] + "\n...(글자 수 제한으로 생략됨)"
            
        embed.add_field(name=f"📜 대기열 (총 {len(display_queue)}곡)", value=queue_text, inline=False)
    else:
        embed.add_field(name="📜 대기열", value="텅 비어있음! 노래를 추가해주세요.", inline=False)

    view = MusicControlView(guild_id)

    if guild_id in status_messages and status_messages[guild_id]:
        try:
            await status_messages[guild_id].edit(embed=embed, view=view)
            return
        except: pass 
    new_msg = await channel.send(embed=embed, view=view)
    status_messages[guild_id] = new_msg

# --- 핵심 로직들 ---
async def add_song_logic(interaction, query):
    guild = interaction.guild
    guild_id = guild.id
    user = interaction.user
    
    if not user.voice:
        await send_alert(interaction, "먼저 음성 채널에 들어가주세요!")
        return
    if not interaction.guild.voice_client:
        await user.voice.channel.connect()

    if guild_id not in server_data:
        server_data[guild_id] = {'user_order': [], 'user_songs': {}}

    target_url = query if ("youtube.com" in query or "youtu.be" in query) else f"ytsearch1:{query}"

    try:
        if not interaction.response.is_done(): await interaction.response.defer()
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, lambda: ytdl.extract_info(target_url, download=False))
        if 'entries' in data: data = data['entries'][0]

        final_url = data['url']
        web_url = data.get('webpage_url', final_url)
        if "&list=" in web_url: web_url = web_url.split("&list=")[0]

        song_info = {
            'url': final_url,
            'web_url': web_url,
            'title': data['title'],
            'thumbnail': data.get('thumbnail'),
            'requester': user.display_name,
            'user_id': user.id
        }

        if user.id not in server_data[guild_id]['user_songs']:
            server_data[guild_id]['user_songs'][user.id] = []
        server_data[guild_id]['user_songs'][user.id].append(song_info)

        if user.id not in server_data[guild_id]['user_order']:
            server_data[guild_id]['user_order'].append(user.id)

        await send_alert(interaction, f"✅ **{data['title']}** 추가 완료!")
        if not guild.voice_client.is_playing() and not is_paused.get(guild_id, False):
            await play_next(guild)
        else:
            await update_status_message(guild)
    except Exception as e:
        await send_alert(interaction, f"오류 발생: {e}")

async def add_playlist_logic(interaction, url):
    guild_id = interaction.guild.id
    user = interaction.user
    if not user.voice:
        await send_alert(interaction, "먼저 음성 채널에 들어가주세요! 🎤")
        return
    if not interaction.response.is_done(): await interaction.response.defer()
    if not interaction.guild.voice_client:
        try: await user.voice.channel.connect()
        except Exception as e:
            await send_alert(interaction, f"음성 채널 접속 실패: {e}")
            return

    try:
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, lambda: ytdl_playlist.extract_info(url, download=False))
        if 'entries' not in data:
            await send_alert(interaction, "❌ 재생목록을 찾을 수 없습니다.")
            return
            
        entries = data['entries']
        added_count = 0
        if guild_id not in server_data: server_data[guild_id] = {'user_order': [], 'user_songs': {}}

        for entry in entries:
            if not entry: continue
            target_url = entry.get('url') or f"https://www.youtube.com/watch?v={entry.get('id')}"
            try:
                detail_data = await loop.run_in_executor(None, lambda: ytdl.extract_info(target_url, download=False))
                song_info = {
                    'url': detail_data['url'],
                    'web_url': detail_data.get('webpage_url', target_url),
                    'title': detail_data['title'],
                    'thumbnail': detail_data.get('thumbnail'),
                    'requester': user.display_name,
                    'user_id': user.id
                }
                if user.id not in server_data[guild_id]['user_songs']: server_data[guild_id]['user_songs'][user.id] = []
                server_data[guild_id]['user_songs'][user.id].append(song_info)
                if user.id not in server_data[guild_id]['user_order']: server_data[guild_id]['user_order'].append(user.id)
                added_count += 1
            except: continue

        await send_alert(interaction, f"✅ 재생목록에서 **{added_count}곡**을 성공적으로 대기열에 담았습니다!")
        if not interaction.guild.voice_client.is_playing() and not is_paused.get(guild_id, False):
            await play_next(interaction.guild)
        else:
            await update_status_message(interaction.guild)
    except Exception as e:
        await send_alert(interaction, f"❌ 재생목록 처리 중 오류가 발생했습니다: {e}")

# ⭐ A-B-A-B 굴레 완벽 차단용 자동 추천 로직
async def auto_play_related(guild, last_song):
    guild_id = guild.id
    if guild_id not in played_history: played_history[guild_id] = []

    target_url = None
    bot_user_id = bot.user.id

    if last_song and 'web_url' in last_song:
        match = re.search(r"(?:v=|\/)([0-9A-Za-z_-]{11}).*", last_song['web_url'])
        if match:
            video_id = match.group(1)
            mix_url = f"https://www.youtube.com/watch?v={video_id}&list=RD{video_id}"
            try:
                loop = asyncio.get_event_loop()
                data = await loop.run_in_executor(None, lambda: ytdl_playlist.extract_info(mix_url, download=False))
                if 'entries' in data:
                    for entry in data['entries']:
                        if not entry: continue
                        vid = entry.get('id')
                        # 핵심: 기록에 없는 노래면, 무조건 깨끗한 유튜브 주소로 직접 조립합니다!
                        if vid and vid not in played_history[guild_id]:
                            target_url = f"https://www.youtube.com/watch?v={vid}"
                            played_history[guild_id].append(vid)
                            break
            except: pass

    # 기록이 비었거나 추천곡을 못 찾았다면 즉시 분위기에 맞는 곡 장전
    if not target_url:
        fallback_queries = ["ytsearch1:마비노기 BGM", "ytsearch1:프로젝트 세카이", "ytsearch1:jpop"]
        query = random.choice(fallback_queries)
        try:
            loop = asyncio.get_event_loop()
            data = await loop.run_in_executor(None, lambda: ytdl.extract_info(query, download=False))
            if 'entries' in data and data['entries']:
                vid = data['entries'][0].get('id')
                # 대체 검색 시에도 이상한 원본 파일 대신 깨끗한 주소로 조립합니다.
                target_url = f"https://www.youtube.com/watch?v={vid}"
                played_history[guild_id].append(vid)
        except: return

    # 기록 데이터가 무거워지지 않게 최근 30곡까지만 기억력 유지
    if len(played_history[guild_id]) > 30:
        played_history[guild_id].pop(0)

    try:
        loop = asyncio.get_event_loop()
        detail_data = await loop.run_in_executor(None, lambda: ytdl.extract_info(target_url, download=False))
        song_info = {
            'url': detail_data['url'],
            'web_url': detail_data.get('webpage_url', target_url),
            'title': detail_data['title'],
            'thumbnail': detail_data.get('thumbnail'),
            'requester': "🤖 뜸부기 추천 (Autoplay)", 
            'user_id': bot_user_id
        }
        if guild_id not in server_data: server_data[guild_id] = {'user_order': [], 'user_songs': {}}
        if bot_user_id not in server_data[guild_id]['user_songs']: server_data[guild_id]['user_songs'][bot_user_id] = []
        
        server_data[guild_id]['user_songs'][bot_user_id].append(song_info)
        if bot_user_id not in server_data[guild_id]['user_order']:
            server_data[guild_id]['user_order'].append(bot_user_id)
        await play_next(guild)
    except Exception as e:
        print(f"자동 재생 실패: {e}")
        current_song[guild.id] = None
        await update_status_message(guild)

# ⭐ 즉시 추천 시작 로직
async def start_autoplay_logic(interaction):
    guild = interaction.guild
    guild_id = guild.id
    if not interaction.user.voice:
        await send_alert(interaction, "먼저 음성 채널에 들어가주세요! 🎤")
        return
    if not guild.voice_client:
        try: await interaction.user.voice.channel.connect()
        except Exception as e:
            await send_alert(interaction, f"음성 채널 접속 실패: {e}")
            return

    if guild.voice_client.is_playing() or is_paused.get(guild_id, False):
        await send_alert(interaction, "이미 노래가 재생 중입니다! 대기열이 비면 알아서 추천이 시작됩니다.")
        return

    if not interaction.response.is_done(): await interaction.response.defer()
    await send_alert(interaction, "✨ **즉시 자동 추천 모드를 가동합니다!**")
    
    last = current_song.get(guild_id)
    bot.loop.create_task(auto_play_related(guild, last))

async def play_next(guild):
    guild_id = guild.id
    if guild_id not in server_data or not server_data[guild_id]['user_order']:
        if current_song.get(guild_id):
            last_song = current_song[guild_id]
            bot.loop.create_task(auto_play_related(guild, last_song))
        else:
            current_song[guild_id] = None
            is_paused[guild_id] = False
            await update_status_message(guild)
        return

    data = server_data[guild_id]
    current_user_id = data['user_order'].pop(0)
    
    if current_user_id in data['user_songs'] and data['user_songs'][current_user_id]:
        song_info = data['user_songs'][current_user_id].pop(0)
        current_song[guild_id] = song_info
        is_paused[guild_id] = False
        
        # ⭐ 라운드 로빈 실제 동작부: 뺀 유저에게 남은 노래가 있다면 줄의 맨 뒤로 다시 보냅니다.
        if data['user_songs'][current_user_id]:
            data['user_order'].append(current_user_id)
        
        await update_status_message(guild)

        voice_client = guild.voice_client
        if not voice_client: return

        audio_source = discord.FFmpegPCMAudio(song_info['url'], **ffmpeg_options)
        # 기본 볼륨 20% 강제 고정 유지
        player = discord.PCMVolumeTransformer(audio_source, volume=0.2)
        
        # 노래가 끝날 때마다 방금 재생한 곡을 기록장에 적어둡니다
        if 'web_url' in song_info:
            match = re.search(r"(?:v=|\/)([0-9A-Za-z_-]{11}).*", song_info['web_url'])
            if match:
                if guild_id not in played_history: played_history[guild_id] = []
                vid = match.group(1)
                if vid not in played_history[guild_id]: played_history[guild_id].append(vid)

        voice_client.play(player, after=lambda e: bot.loop.create_task(play_next(guild)))
    else:
        await play_next(guild)

async def stop_logic(guild):
    guild_id = guild.id
    if guild_id in server_data: del server_data[guild_id]
    current_song[guild_id] = None
    is_paused[guild_id] = False
    
    if guild_id in status_messages and status_messages[guild_id]:
        try: await status_messages[guild_id].delete()
        except: pass
    if guild.voice_client: await guild.voice_client.disconnect()

# --- Slash Commands ---
@bot.tree.command(name="play", description="노래를 재생하거나 대기열에 추가합니다.")
@app_commands.describe(query="유튜브 링크 또는 검색어")
async def play(interaction: discord.Interaction, query: str):
    if interaction.guild.id not in status_messages:
        await interaction.response.defer() 
        status_messages[interaction.guild.id] = await interaction.followup.send("loading...")
    else:
        if not interaction.response.is_done(): await interaction.response.defer()
    await add_song_logic(interaction, query)

@bot.tree.command(name="playlist", description="유튜브 재생목록 링크를 입력해 최대 20곡을 추가합니다.")
@app_commands.describe(url="재생목록 링크")
async def playlist(interaction: discord.Interaction, url: str):
    if interaction.guild.id not in status_messages:
        await interaction.response.defer() 
        status_messages[interaction.guild.id] = await interaction.followup.send("loading...")
    else:
        if not interaction.response.is_done(): await interaction.response.defer()
    await add_playlist_logic(interaction, url)

# ⭐ 즉시 추천 시작 커맨드 추가
@bot.tree.command(name="autoplay", description="대기열이 비어있을 때 즉시 자동 추천 재생을 시작합니다.")
async def autoplay_cmd(interaction: discord.Interaction):
    await start_autoplay_logic(interaction)

@bot.tree.command(name="remove", description="대기열에서 노래를 삭제합니다.")
@app_commands.describe(index="삭제할 노래의 번호")
async def remove(interaction: discord.Interaction, index: int):
    guild_id = interaction.guild.id
    display_queue = get_display_queue(guild_id)
    if index < 1 or index > len(display_queue):
        await send_alert(interaction, "❌ 올바른 번호를 입력해주세요.")
        return
    target_song = display_queue[index - 1]
    owner_id = target_song['user_id']
    user_songs = server_data[guild_id]['user_songs'][owner_id]
    for i, song in enumerate(user_songs):
        if song == target_song:
            del user_songs[i]
            break
    await send_alert(interaction, f"🗑️ **{target_song['title']}** 삭제 완료.")
    await update_status_message(interaction.guild)

@bot.tree.command(name="swap", description="대기열의 두 노래 순서를 바꿉니다.")
@app_commands.describe(index1="첫 번째 노래 번호", index2="두 번째 노래 번호")
async def swap(interaction: discord.Interaction, index1: int, index2: int):
    guild_id = interaction.guild.id
    display_queue = get_display_queue(guild_id)
    if index1 < 1 or index2 < 1 or index1 > len(display_queue) or index2 > len(display_queue):
        await send_alert(interaction, "❌ 올바른 번호를 입력해주세요.")
        return
    song1 = display_queue[index1 - 1]
    song2 = display_queue[index2 - 1]
    for key in ['url', 'web_url', 'title', 'thumbnail', 'requester']:
        song1[key], song2[key] = song2[key], song1[key]
    await send_alert(interaction, f"🔀 **{index1}번**과 **{index2}번** 노래를 바꿨습니다.")
    await update_status_message(interaction.guild)

@bot.tree.command(name="skip", description="현재 노래를 건너뜁니다.")
async def skip(interaction: discord.Interaction):
    if interaction.guild.voice_client and interaction.guild.voice_client.is_playing():
        interaction.guild.voice_client.stop()
        await send_alert(interaction, "⏭️ 스킵!")
    else: await send_alert(interaction, "재생 중인 노래가 없습니다.")

@bot.tree.command(name="pause", description="노래를 일시정지합니다.")
async def pause(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client
    if voice_client and voice_client.is_playing():
        voice_client.pause()
        is_paused[interaction.guild.id] = True
        await send_alert(interaction, "⏸️ 일시정지")
        await update_status_message(interaction.guild)
    else: await send_alert(interaction, "재생 중이 아닙니다.")

@bot.tree.command(name="resume", description="노래를 다시 재생합니다.")
async def resume(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client
    if voice_client and voice_client.is_paused():
        voice_client.resume()
        is_paused[interaction.guild.id] = False
        await send_alert(interaction, "▶️ 다시 재생")
        await update_status_message(interaction.guild)
    else: await send_alert(interaction, "일시정지 상태가 아닙니다.")

@bot.tree.command(name="stop", description="노래를 끄고 봇을 내보냅니다.")
async def stop(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await stop_logic(interaction.guild)
    await interaction.followup.send("👋 봇이 퇴장했습니다.", ephemeral=True)

@bot.tree.command(name="refresh", description="봇이 적은 이전 메시지들을 싹 청소하고 플레이어를 새로 띄웁니다.")
async def refresh(interaction: discord.Interaction):
    # 청소 작업은 시간이 조금 걸릴 수 있으므로 '생각 중' 상태로 대기합니다. (유저 본인에게만 보임)
    await interaction.response.defer(ephemeral=True)
    
    # 1. 현재 채널에서 '봇이 작성한 메시지'만 골라서 삭제 (최근 100개 기준)
    try:
        deleted = await interaction.channel.purge(limit=100, check=lambda m: m.author == bot.user)
    except Exception as e:
        await interaction.followup.send(f"청소 중 오류가 발생했습니다: {e}", ephemeral=True)
        return
        
    # 2. 플레이어 창을 현재 채널에 새로 띄우기 위한 '가짜 메시지(Dummy)' 꼼수
    class DummyMsg:
        def __init__(self, ch):
            self.channel = ch
            
    # 기존 상태창 데이터를 가짜 데이터로 덮어씌워서, 봇이 '수정' 대신 '새로 작성'하도록 유도합니다.
    status_messages[interaction.guild.id] = DummyMsg(interaction.channel)
    
    # 3. 상태창 새로고침 로직 실행
    await update_status_message(interaction.guild)
    
    # 완료 안내 메시지
    await interaction.followup.send(f"🧹 {len(deleted)}개의 메시지를 청소하고 플레이어를 아래로 불러왔습니다!", ephemeral=True)

@bot.event
async def on_ready():
    print(f'{bot.user} 로그인 성공!')
    await bot.tree.sync()

bot.run(TOKEN)