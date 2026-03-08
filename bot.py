import os
import json # ⭐ 외부 하청(Subprocess) 결과를 읽기 위해 추가됨!
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
        await self.tree.sync()
        print("모든 명령어 동기화 완료!")

bot = MusicBot()

# --- 데이터 저장소 ---
server_data = {} 
current_song = {}    
status_messages = {} 
is_paused = {}       

# (이제 yt-dlp를 외부에서 실행하므로 이 설정은 파이썬 내부에서 쓰이지 않지만, 혹시 모를 호환성을 위해 남겨둡니다)
yt_dl_opts = {
    'format': 'bestaudio/best', 
    'noplaylist': True,
    'extract_flat': 'in_playlist',
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',
}
ytdl = yt_dlp.YoutubeDL(yt_dl_opts)

# FFmpeg 옵션 (노트북용 버퍼 10MB 뻥튀기 세팅)
ffmpeg_options = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -multiple_requests 1',
    'options': '-vn -bufsize 10000k'
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

class MusicControlView(discord.ui.View):
    def __init__(self, guild_id):
        super().__init__(timeout=None)
        self.guild_id = guild_id

    @discord.ui.button(label="노래 추가", style=discord.ButtonStyle.green, emoji="➕")
    async def add_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddSongModal())

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

    current_vol = int(server_data.get(guild_id, {}).get('volume', 0.2) * 100)
    embed = discord.Embed(title=f"🎧 Music Player (🔊 {current_vol}%)", color=0x9900ff)
    
    if guild_id in current_song and current_song[guild_id]:
        song = current_song[guild_id]
        status = "⏸️ 일시정지" if is_paused.get(guild_id, False) else "▶️ 재생 중"
        embed.add_field(name=status, value=f"[{song['title']}]({song['web_url']})\n🎤 신청자: **{song['requester']}**", inline=False)
        if song.get('thumbnail'):
            embed.set_thumbnail(url=song['thumbnail'])
    else:
        embed.add_field(name="💤 상태", value="대기 중...", inline=False)

    display_queue = get_display_queue(guild_id)
    if display_queue:
        queue_text = ""
        for i, song in enumerate(display_queue):
            title = song['title']
            if len(title) > 35: title = title[:35] + "..."
            queue_text += f"`{i+1}.` {title} - {song['requester']}\n"
        if len(queue_text) > 1000:
            queue_text = queue_text[:950] + "\n...(너무 길어서 생략됨)"
        embed.add_field(name=f"📜 대기열 ({len(display_queue)}곡)", value=queue_text, inline=False)
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
        await interaction.followup.send("먼저 음성 채널에 들어가주세요!")
        return
    if interaction.guild.voice_client is None:
        try: await user.voice.channel.connect()
        except: pass

    if guild_id not in server_data:
        server_data[guild_id] = {'user_order': [], 'user_songs': {}, 'volume': 0.2}

    target_url = query
    if not ("youtube.com" in query or "youtu.be" in query):
        target_url = f"ytsearch1:{query}"

    try:
        # ⭐ 파이썬의 뇌(GIL)를 멈추지 않도록 윈도우에 외부 하청(Subprocess)을 맡기는 부분!
        cmd = [
            'yt-dlp', 
            '--dump-json', 
            '--no-playlist', 
            '--no-warnings', 
            '--ignore-errors',
            target_url
        ]
        
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        stdout, stderr = await process.communicate()
        
        if not stdout:
            raise Exception("유튜브에서 데이터를 가져오지 못했습니다. 다시 시도해 주세요.")
            
        # 결과물(문자열)을 JSON으로 해석합니다
        raw_text = stdout.decode('utf-8').strip().split('\n')[0]
        data = json.loads(raw_text)
        
        if 'entries' in data: 
            data = data['entries'][0]

        # -------------------------------------------------------------

        final_url = data['url']
        web_url = data.get('webpage_url', final_url)
        if "&list=" in web_url: web_url = web_url.split("&list=")[0]

        song_info = {
            'url': final_url, 'web_url': web_url, 'title': data['title'],
            'thumbnail': data.get('thumbnail'), 'requester': user.display_name, 'user_id': user.id
        }

        if user.id not in server_data[guild_id]['user_songs']:
            server_data[guild_id]['user_songs'][user.id] = []
        server_data[guild_id]['user_songs'][user.id].append(song_info)
        if user.id not in server_data[guild_id]['user_order']:
            server_data[guild_id]['user_order'].append(user.id)

        sent_msg = await interaction.followup.send(f"✅ **{data['title']}** 추가 완료!", wait=True)
        await sent_msg.delete(delay=5)

        if not guild.voice_client.is_playing() and not is_paused.get(guild_id, False):
            await play_next(guild)
        else:
            await update_status_message(guild)

    except Exception as e:
        await send_alert(interaction, f"❌ 오류 발생: {e}", delay=10)
        print(f"에러 상세: {e}")
        

# --- 재생 및 종료 로직 ---
async def play_next(guild):
    guild_id = guild.id
    if guild_id not in server_data or not server_data[guild_id]['user_order']:
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
        
        current_volume = server_data.get(guild_id, {}).get('volume', 0.2)
        volume_player = discord.PCMVolumeTransformer(player, volume=current_volume)
        
        voice_client.play(volume_player, after=lambda e: bot.loop.create_task(play_next(guild)))
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
    if not interaction.response.is_done():
        await interaction.response.defer()
    if interaction.guild.id not in status_messages:
        msg = await interaction.followup.send("loading...", wait=True)
        status_messages[interaction.guild.id] = msg
    if not interaction.user.voice:
        await interaction.followup.send("먼저 음성 채널에 들어가주세요! 🎤", ephemeral=True)
        return
    if not interaction.guild.voice_client:
        try: await interaction.user.voice.channel.connect()
        except Exception as e:
            await interaction.followup.send(f"음성 채널 접속 실패: {e}")
            return
    await add_song_logic(interaction, query)

@bot.tree.command(name="remove", description="대기열에서 노래를 삭제합니다.")
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

@bot.tree.command(name="volume", description="봇의 볼륨을 조절합니다 (0~100).")
@app_commands.describe(vol="볼륨 크기 (0~100)")
async def volume(interaction: discord.Interaction, vol: int):
    if vol < 0 or vol > 100:
        await send_alert(interaction, "❌ 볼륨은 0에서 100 사이로 입력해주세요.")
        return

    guild_id = interaction.guild.id
    if guild_id not in server_data:
        server_data[guild_id] = {'user_order': [], 'user_songs': {}, 'volume': 0.2}

    new_volume = vol / 100.0
    server_data[guild_id]['volume'] = new_volume

    voice_client = interaction.guild.voice_client
    if voice_client and voice_client.source and isinstance(voice_client.source, discord.PCMVolumeTransformer):
        voice_client.source.volume = new_volume

    await send_alert(interaction, f"🔊 볼륨이 **{vol}%**로 설정되었습니다.")
    await update_status_message(interaction.guild)

@bot.tree.command(name="stop", description="노래를 끄고 봇을 내보냅니다.")
async def stop(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await stop_logic(interaction.guild)
    await interaction.followup.send("👋 봇이 퇴장했습니다.", ephemeral=True)

@bot.tree.command(name="help", description="봇 사용법을 알려줍니다.")
async def help(interaction: discord.Interaction):
    embed = discord.Embed(title="도움말", description="아래 명령어를 슬래시(/)와 함께 사용하세요.", color=0x00ff00)
    embed.add_field(name="/play [검색어/URL]", value="노래를 재생하거나 대기열에 추가합니다.", inline=False)
    embed.add_field(name="/volume [0~100]", value="봇의 재생 볼륨을 조절합니다.", inline=False)
    embed.add_field(name="/skip", value="현재 노래를 건너뜁니다.", inline=False)
    embed.add_field(name="/remove [번호]", value="대기열에서 특정 번호의 노래를 지웁니다.", inline=False)
    embed.add_field(name="/swap [번호1] [번호2]", value="두 노래의 순서를 바꿉니다.", inline=False)
    embed.add_field(name="/pause & /resume", value="일시정지 및 다시 재생", inline=False)
    embed.add_field(name="/stop", value="음악을 끄고 봇을 내보냅니다.", inline=False)
    embed.set_footer(text="상태창 아래의 버튼을 눌러도 작동합니다!")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.event
async def on_ready():
    print(f'{bot.user} 로그인 성공!')
    await bot.tree.sync()

bot.run(TOKEN)