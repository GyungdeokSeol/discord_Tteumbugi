import os
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
yt_dl_opts = {
        'format': 'bestaudio/best',
        'noplaylist': True,
        'cookiefile': 'cookies.txt', 
        'nocheckcertificate': True,
        'ignoreerrors': False,
        'logtostderr': False,
        'quiet': True,
        'no_warnings': True,
        'default_search': 'auto',
        # 'source_address': '0.0.0.0',  <-- 주석 처리 유지 (IPv6 사용 허용)
        'extractor_args': {
            'youtube': {
                # 아이폰(ios)은 포맷 에러가 나므로 안드로이드(android)로 변경
                'player_client': ['android'], 
            }
        }
    }
ytdl = yt_dlp.YoutubeDL(yt_dl_opts)

ffmpeg_options = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn -headers "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"'
}

# --- [Helper] 메시지 자동 삭제 도우미 함수 ---

# 1. 실제로 삭제를 수행하는 백그라운드 함수
async def delete_later(msg, delay):
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except:
        pass

# 2. 메시지를 보내고 삭제를 예약하는 통합 함수 (이걸 사용하세요)
async def send_alert(interaction, text, delay=10):
    try:
        msg = None
        if not interaction.response.is_done():
            # 아직 응답 안 했으면 (일반적인 경우)
            await interaction.response.send_message(text, ephemeral=False)
            msg = await interaction.original_response()
        else:
            # 이미 응답 했으면 (defer 등을 쓴 경우)
            msg = await interaction.followup.send(text, ephemeral=False)
        
        # 삭제 작업 예약
        if msg:
            asyncio.create_task(delete_later(msg, delay))
            
    except Exception as e:
        print(f"메시지 전송 오류: {e}")

# --- [Logic] 헬퍼 함수들 ---

# 라운드 로빈 순서대로 정렬된 리스트 반환
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

# 1. 노래 추가 모달
class AddSongModal(discord.ui.Modal, title='노래 추가하기'):
    query = discord.ui.TextInput(label='유튜브 URL 또는 검색어', placeholder='듣고 싶은 노래를 입력하세요.')

    async def on_submit(self, interaction: discord.Interaction):
        await send_alert(interaction, f"🔍 **{self.query.value}** 검색 중...", 5)
        # play 함수 로직 재사용
        await add_song_logic(interaction, self.query.value)

# 2. 플레이어 제어 버튼
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

    # 2. 전체 대기열 표시
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
    
    # 1. (중복 방지) play 함수에서 이미 체크했지만 안전장치로 둠
    if not user.voice:
        await interaction.followup.send("먼저 음성 채널에 들어가주세요!")
        return

    # 2. 봇 연결 확인 (play에서 했지만 비상용)
    if interaction.guild.voice_client is None:
        try:
            await user.voice.channel.connect()
        except:
            pass # play 함수에서 이미 연결했을 테니 패스

    # 데이터 초기화
    if guild_id not in server_data:
        server_data[guild_id] = {'user_order': [], 'user_songs': {}}

    # URL 처리
    target_url = query
    if not ("youtube.com" in query or "youtu.be" in query):
        target_url = f"ytsearch1:{query}"

    try:
        # ▼▼▼ [수정 핵심] defer() 삭제함 (play 함수가 이미 함) ▼▼▼

        # 노래 정보 추출
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

        # 서버 데이터에 저장
        if user.id not in server_data[guild_id]['user_songs']:
            server_data[guild_id]['user_songs'][user.id] = []
        server_data[guild_id]['user_songs'][user.id].append(song_info)

        if user.id not in server_data[guild_id]['user_order']:
            server_data[guild_id]['user_order'].append(user.id)

        # ▼▼▼ [수정] wait=True로 메시지 객체를 받고, delay 옵션으로 삭제합니다 ▼▼▼
        sent_msg = await interaction.followup.send(f"✅ **{data['title']}** 추가 완료!", wait=True)
        await sent_msg.delete(delay=5)

        # 재생 로직
        if not guild.voice_client.is_playing() and not is_paused.get(guild_id, False):
            await play_next(guild)
        else:
            await update_status_message(guild)

    except Exception as e:
        # 에러 메시지도 followup으로
        await interaction.followup.send(f"오류 발생: {e}")
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
    # 1. 안전한 Defer (이미 응답했는지 확인하고 시간 벌기)
    if not interaction.response.is_done():
        await interaction.response.defer()

    # 2. 로딩 메시지 (Followup 사용)
    # 기존에 status_messages 로직이 있다면 유지하되, send 대신 followup.send를 씁니다.
    if interaction.guild.id not in status_messages:
        msg = await interaction.followup.send("loading...", wait=True)
        status_messages[interaction.guild.id] = msg
    
    # 3. 사용자 음성 채널 확인
    if not interaction.user.voice:
        await interaction.followup.send("먼저 음성 채널에 들어가주세요! 🎤", ephemeral=True)
        return

    # 4. 봇 자동 입장 로직
    if not interaction.guild.voice_client:
        try:
            channel = interaction.user.voice.channel
            await channel.connect()
        except Exception as e:
            await interaction.followup.send(f"음성 채널 접속 실패: {e}")
            return

    # 5. 노래 추가 로직 실행
    # (주의: add_song_logic 안에는 interaction.response.defer()가 없어야 합니다!)
    await add_song_logic(interaction, query)

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