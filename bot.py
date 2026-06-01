import os
import re
from dotenv import load_dotenv
import discord
from discord import app_commands
from discord.ext import commands
import yt_dlp
import asyncio

# --- 설정값 ---
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')

# 봇 권한 설정
intents = discord.Intents.default()
intents.message_content = True

# Slash Command를 위한 Bot 클래스 정의
class MusicBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix='!', intents=intents, help_command=None)

    async def setup_hook(self):
        # 봇 시작 시 명령어 동기화 (시간이 좀 걸릴 수 있음)
        await self.tree.sync()
        print("모든 명령어 동기화 완료!")

bot = MusicBot()

# --- 데이터 저장소 ---
# server_data[guild_id] = {'user_order': [], 'user_songs': {}}
server_data = {} 
current_song = {}    # {guild_id: song_info}
status_messages = {} # {guild_id: message_object}
is_paused = {}       # {guild_id: bool}

# --- 유튜브/FFmpeg 옵션 ---
# 1. 단일 곡 검색용 옵션
yt_dl_opts = {
    'format': 'bestaudio/best',
    'noplaylist': True,
    'quiet': True,
    'cookiefile': 'cookies.txt',
}
ytdl = yt_dlp.YoutubeDL(yt_dl_opts)

# 2. 재생목록(Playlist) 전용 옵션 (최대 20곡 제한)
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

# --- [Helper] 메시지 자동 삭제 도우미 함수 ---

async def delete_later(msg, delay):
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except:
        pass

async def send_alert(interaction, text, delay=10):
    try:
        msg = None
        if not interaction.response.is_done():
            await interaction.response.send_message(text, ephemeral=False)
            msg = await interaction.original_response()
        else:
            msg = await interaction.followup.send(text, ephemeral=False)
        
        if msg:
            asyncio.create_task(delete_later(msg, delay))
            
    except Exception as e:
        print(f"메시지 전송 오류: {e}")

# --- [Logic] 헬퍼 함수들 ---

def get_display_queue(guild_id):
    if guild_id not in server_data:
        return []

    data = server_data[guild_id]
    temp_order = list(data['user_order']) 
    temp_songs = {uid: list(songs) for uid, songs in data['user_songs'].items()}
    
    display_list = []
    
    while temp_order:
        user_id = temp_order.pop(0)
        if temp_songs[user_id]:
            song = temp_songs[user_id].pop(0)
            display_list.append(song)
            temp_order.append(user_id)
            
    return display_list

# --- [UI Components] 버튼 & 모달 ---

class AddSongModal(discord.ui.Modal, title='노래 추가하기'):
    query = discord.ui.TextInput(label='유튜브 URL 또는 검색어', placeholder='듣고 싶은 노래를 입력하세요.')

    async def on_submit(self, interaction: discord.Interaction):
        await send_alert(interaction, f"🔍 **{self.query.value}** 검색 중...", 5)
        await add_song_logic(interaction, self.query.value)

class AddPlaylistModal(discord.ui.Modal, title='재생목록 추가하기'):
    url = discord.ui.TextInput(
        label='유튜브 재생목록 URL', 
        placeholder='https://www.youtube.com/playlist?list=...',
        required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        await send_alert(interaction, "📂 재생목록을 분석하고 곡을 담는 중입니다...", 5)
        # 아래 3번에서 만들 통합 처리 함수를 호출합니다.
        await add_playlist_logic(interaction, self.url.value)

class MusicControlView(discord.ui.View):
    def __init__(self, guild_id):
        super().__init__(timeout=None)
        self.guild_id = guild_id

    @discord.ui.button(label="노래 추가", style=discord.ButtonStyle.green, emoji="➕")
    async def add_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddSongModal())

    @discord.ui.button(label="재생목록 추가", style=discord.ButtonStyle.success, emoji="📂")
    async def playlist_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddPlaylistModal())

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

# --- 상태창 업데이트 함수 ---
async def update_status_message(guild):
    guild_id = guild.id
    channel = None
    
    if guild_id in status_messages:
        channel = status_messages[guild_id].channel

    if not channel: return

    embed = discord.Embed(title="🎧 Music Player", color=0x9900ff)
    
    # 1. 현재 곡 정보
    if guild_id in current_song and current_song[guild_id]:
        song = current_song[guild_id]
        status = "⏸️ 일시정지" if is_paused.get(guild_id, False) else "▶️ 재생 중"
        
        embed.add_field(
            name=status, 
            value=f"[{song['title']}]({song['web_url']})\n🎤 신청자: **{song['requester']}**", 
            inline=False
        )
        if song.get('thumbnail'):
            embed.set_thumbnail(url=song['thumbnail'])
    else:
        embed.add_field(name="💤 상태", value="대기 중...", inline=False)

    # 2. 전체 대기열 표시 (최대 10곡 제한으로 봇 튕김 방지)
    display_queue = get_display_queue(guild_id)
    if display_queue:
        queue_text = ""
        max_display = min(10, len(display_queue)) 
        
        for i in range(max_display):
            song = display_queue[i]
            title = song['title']
            if len(title) > 35: title = title[:35] + "..."
            queue_text += f"`{i+1}.` {title} - {song['requester']}\n"
        
        if len(display_queue) > max_display:
            queue_text += f"\n*...그리고 **{len(display_queue) - max_display}곡**이 더 대기 중입니다! 🎶*"
            
        embed.add_field(name=f"📜 대기열 (총 {len(display_queue)}곡)", value=queue_text, inline=False)
    else:
        embed.add_field(name="📜 대기열", value="텅 비어있음! 노래를 추가해주세요.", inline=False)

    view = MusicControlView(guild_id)

    if guild_id in status_messages and status_messages[guild_id]:
        try:
            await status_messages[guild_id].edit(embed=embed, view=view)
            return
        except:
            pass 

    new_msg = await channel.send(embed=embed, view=view)
    status_messages[guild_id] = new_msg

# --- 노래 추가 핵심 로직 ---
async def add_song_logic(interaction, query):
    guild = interaction.guild
    guild_id = guild.id
    user = interaction.user
    
    if not user.voice:
        await send_alert(interaction, "먼저 음성 채널에 들어가주세요!")
        return

    if interaction.guild.voice_client is None:
        await user.voice.channel.connect()

    if guild_id not in server_data:
        server_data[guild_id] = {'user_order': [], 'user_songs': {}}

    target_url = query
    if not ("youtube.com" in query or "youtu.be" in query):
        target_url = f"ytsearch1:{query}"

    try:
        if not interaction.response.is_done():
            await interaction.response.defer()

        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, lambda: ytdl.extract_info(target_url, download=False))
        
        if 'entries' in data:
            data = data['entries'][0]

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
        print(e)

# --- 재생목록 추가 핵심 로직 ---
async def add_playlist_logic(interaction, url):
    guild_id = interaction.guild.id
    user = interaction.user

    if not user.voice:
        await send_alert(interaction, "먼저 음성 채널에 들어가주세요! 🎤")
        return

    if not interaction.response.is_done():
        await interaction.response.defer()

    if not interaction.guild.voice_client:
        try: await user.voice.channel.connect()
        except Exception as e:
            await send_alert(interaction, f"음성 채널 접속 실패: {e}")
            return

    try:
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, lambda: ytdl_playlist.extract_info(url, download=False))
        
        if 'entries' not in data:
            await send_alert(interaction, "❌ 재생목록을 찾을 수 없습니다. 비공개이거나 잘못된 링크입니다.")
            return
            
        entries = data['entries']
        added_count = 0
        
        if guild_id not in server_data:
            server_data[guild_id] = {'user_order': [], 'user_songs': {}}

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
                
                if user.id not in server_data[guild_id]['user_songs']:
                    server_data[guild_id]['user_songs'][user.id] = []
                    
                server_data[guild_id]['user_songs'][user.id].append(song_info)
                server_data[guild_id]['user_order'].append(user.id)
                added_count += 1
                
            except Exception as e:
                print(f"재생목록 곡 추출 실패: {e}")
                continue

        await send_alert(interaction, f"✅ 재생목록에서 **{added_count}곡**을 성공적으로 대기열에 담았습니다!")
        
        if not interaction.guild.voice_client.is_playing() and not is_paused.get(guild_id, False):
            await play_next(interaction.guild)
        else:
            await update_status_message(interaction.guild)
            
    except Exception as e:
        await send_alert(interaction, f"❌ 재생목록 처리 중 오류가 발생했습니다: {e}")

# --- 자동 재생(Autoplay) 핵심 로직 ---
async def auto_play_related(guild, last_song):
    match = re.search(r"(?:v=|\/)([0-9A-Za-z_-]{11}).*", last_song['web_url'])
    if not match: return
    
    video_id = match.group(1)
    mix_url = f"https://www.youtube.com/watch?v={video_id}&list=RD{video_id}"
    
    try:
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, lambda: ytdl_playlist.extract_info(mix_url, download=False))
        
        if 'entries' in data and len(data['entries']) > 1:
            next_entry = data['entries'][1]
            target_url = next_entry.get('url') or f"https://www.youtube.com/watch?v={next_entry.get('id')}"
            
            detail_data = await loop.run_in_executor(None, lambda: ytdl.extract_info(target_url, download=False))
            
            bot_user_id = bot.user.id
            song_info = {
                'url': detail_data['url'],
                'web_url': detail_data.get('webpage_url', target_url),
                'title': detail_data['title'],
                'thumbnail': detail_data.get('thumbnail'),
                'requester': "🤖 뜸부기 추천 (Autoplay)", 
                'user_id': bot_user_id
            }
            
            guild_id = guild.id
            if guild_id not in server_data:
                server_data[guild_id] = {'user_order': [], 'user_songs': {}}
            if bot_user_id not in server_data[guild_id]['user_songs']:
                server_data[guild_id]['user_songs'][bot_user_id] = []
                
            server_data[guild_id]['user_songs'][bot_user_id].append(song_info)
            server_data[guild_id]['user_order'].append(bot_user_id)
            
            await play_next(guild)
            
    except Exception as e:
        print(f"자동 재생 실패: {e}")
        current_song[guild.id] = None
        await update_status_message(guild)

# --- 재생 및 종료 로직 ---
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
        
        if data['user_songs'][current_user_id]:
            data['user_order'].append(current_user_id)
        
        await update_status_message(guild)

        voice_client = guild.voice_client
        if not voice_client: return

        player = discord.FFmpegPCMAudio(song_info['url'], **ffmpeg_options)
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
    
    if guild.voice_client:
        await guild.voice_client.disconnect()

# --- [Slash Commands] ---

@bot.tree.command(name="play", description="노래를 재생하거나 대기열에 추가합니다.")
@app_commands.describe(query="유튜브 링크 또는 검색어")
async def play(interaction: discord.Interaction, query: str):
    if interaction.guild.id not in status_messages:
        await interaction.response.defer() 
        msg = await interaction.followup.send("loading...")
        status_messages[interaction.guild.id] = await interaction.original_response()
    else:
        if not interaction.response.is_done():
            await interaction.response.defer()

    if not interaction.user.voice:
        await interaction.followup.send("먼저 음성 채널에 들어가주세요! 🎤", ephemeral=True)
        return

    if not interaction.guild.voice_client:
        try:
            channel = interaction.user.voice.channel
            await channel.connect()
        except Exception as e:
            await interaction.followup.send(f"음성 채널 접속 실패: {e}")
            return

    await add_song_logic(interaction, query)

@bot.tree.command(name="playlist", description="유튜브 재생목록 링크를 입력해 최대 20곡을 추가합니다.")
@app_commands.describe(url="재생목록 링크")
async def playlist(interaction: discord.Interaction, url: str):
    await add_playlist_logic(interaction, url)
    
@bot.tree.command(name="remove", description="대기열에서 노래를 삭제합니다.")
@app_commands.describe(index="삭제할 노래의 번호 (대기열에 보이는 숫자)")
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
        
    if index1 == index2:
        await send_alert(interaction, "같은 번호입니다.")
        return

    song1 = display_queue[index1 - 1]
    song2 = display_queue[index2 - 1]
    
    temp_data = song1.copy()
    keys_to_swap = ['url', 'web_url', 'title', 'thumbnail', 'requester']
    
    for key in keys_to_swap:
        val1 = song1[key]
        val2 = song2[key]
        song1[key] = val2
        song2[key] = val1
        
    await send_alert(interaction, f"🔀 **{index1}번**과 **{index2}번** 노래를 바꿨습니다.")
    await update_status_message(interaction.guild)

@bot.tree.command(name="skip", description="현재 노래를 건너뜁니다.")
async def skip(interaction: discord.Interaction):
    if interaction.guild.voice_client and interaction.guild.voice_client.is_playing():
        interaction.guild.voice_client.stop()
        await send_alert(interaction, "⏭️ 스킵!")
    else:
        await send_alert(interaction, "재생 중인 노래가 없습니다.")

@bot.tree.command(name="pause", description="노래를 일시정지합니다.")
async def pause(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client
    if voice_client and voice_client.is_playing():
        voice_client.pause()
        is_paused[interaction.guild.id] = True
        await send_alert(interaction, "⏸️ 일시정지")
        await update_status_message(interaction.guild)
    else:
        await send_alert(interaction, "재생 중이 아닙니다.")

@bot.tree.command(name="resume", description="노래를 다시 재생합니다.")
async def resume(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client
    if voice_client and voice_client.is_paused():
        voice_client.resume()
        is_paused[interaction.guild.id] = False
        await send_alert(interaction, "▶️ 다시 재생")
        await update_status_message(interaction.guild)
    else:
        await send_alert(interaction, "일시정지 상태가 아닙니다.")

@bot.tree.command(name="stop", description="노래를 끄고 봇을 내보냅니다.")
async def stop(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await stop_logic(interaction.guild)
    await interaction.followup.send("👋 봇이 퇴장했습니다.", ephemeral=True)

@bot.tree.command(name="help", description="봇 사용법을 알려줍니다.")
async def help(interaction: discord.Interaction):
    embed = discord.Embed(title="도움말", description="아래 명령어를 슬래시(/)와 함께 사용하세요.", color=0x00ff00)
    embed.add_field(name="/play [검색어/URL]", value="노래를 재생하거나 대기열에 추가합니다.", inline=False)
    embed.add_field(name="/playlist [재생목록 URL]", value="재생목록의 노래를 최대 20곡까지 대기열에 담습니다.", inline=False)
    embed.add_field(name="/skip", value="현재 노래를 건너뜁니다.", inline=False)
    embed.add_field(name="/remove [번호]", value="대기열에서 특정 번호의 노래를 지웁니다.", inline=False)
    embed.add_field(name="/swap [번호1] [번호2]", value="두 노래의 순서를 바꿉니다.", inline=False)
    embed.add_field(name="/pause & /resume", value="일시정지 및 다시 재생", inline=False)
    embed.add_field(name="/stop", value="음악을 끄고 봇을 내보냅니다.", inline=False)
    embed.set_footer(text="대기열이 비면 뜸부기가 찰떡같은 노래를 알아서 추천해 줍니다!")
    
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.event
async def on_ready():
    print(f'{bot.user} 로그인 성공!')
    await bot.tree.sync()

bot.run(TOKEN)