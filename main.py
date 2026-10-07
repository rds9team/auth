import os
import sys
import time
import random
import sqlite3
from datetime import datetime

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import discord
from discord import app_commands
from discord.ext import commands, tasks

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CLIENT_ID = os.getenv("CLIENT_ID")
ROLE_ID = int(os.getenv("ROLE_ID")) if os.getenv("ROLE_ID") and os.getenv("ROLE_ID").isdigit() else None
UNVERIFIED_ROLE_ID = int(os.getenv("UNVERIFIED_ROLE_ID")) if os.getenv("UNVERIFIED_ROLE_ID") and os.getenv("UNVERIFIED_ROLE_ID").isdigit() else None
BLACKLIST_ROLE_ID = int(os.getenv("BLACKLIST_ROLE_ID")) if os.getenv("BLACKLIST_ROLE_ID") and os.getenv("BLACKLIST_ROLE_ID").isdigit() else None
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID")) if os.getenv("LOG_CHANNEL_ID") and os.getenv("LOG_CHANNEL_ID").isdigit() else None

if not DISCORD_TOKEN:
    print("エラー: 環境変数 DISCORD_TOKEN が設定されていません。")
    sys.exit(1)

# データベース設定
DB_PATH = os.path.join(os.path.dirname(__file__), "auth_bot.db")

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER PRIMARY KEY,
                log_channel_id INTEGER,
                updated_at TEXT
            )
        """)
        conn.commit()

init_db()

def get_guild_log_channel_id(guild_id: int) -> int | None:
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute("SELECT log_channel_id FROM guild_settings WHERE guild_id = ?", (guild_id,))
            row = cur.fetchone()
            if row and row[0]:
                return row[0]
    except Exception as e:
        print(f"DB読み込みエラー: {e}")
    return LOG_CHANNEL_ID

def set_guild_log_channel_id(guild_id: int, channel_id: int | None):
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.cursor()
            now_str = datetime.now().isoformat()
            cur.execute("""
                INSERT INTO guild_settings (guild_id, log_channel_id, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    log_channel_id = excluded.log_channel_id,
                    updated_at = excluded.updated_at
            """, (guild_id, channel_id, now_str))
            conn.commit()
    except Exception as e:
        print(f"DB保存エラー: {e}")

async def get_log_channel(guild: discord.Guild) -> discord.TextChannel | None:
    ch_id = get_guild_log_channel_id(guild.id)
    if not ch_id:
        return None
    channel = guild.get_channel(ch_id)
    if not channel:
        try:
            channel = await guild.fetch_channel(ch_id)
        except Exception:
            return None
    if isinstance(channel, discord.TextChannel):
        return channel
    return None

# クールダウン管理 (連打・DoS防止)
cooldowns = {}
COOLDOWN_TIME = 3.0
start_time = time.time()
status_index = 0

def check_cooldown(user_id: int) -> float | None:
    now = time.time()
    last_time = cooldowns.get(user_id)
    if last_time and (now - last_time) < COOLDOWN_TIME:
        return round(COOLDOWN_TIME - (now - last_time), 1)
    cooldowns[user_id] = now
    return None

def generate_captcha(length: int = 4) -> str:
    chars = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(random.choices(chars, k=length))

def format_uptime(seconds: float) -> str:
    seconds = int(seconds)
    d = seconds // 86400
    h = (seconds % 86400) // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    parts = []
    if d > 0:
        parts.append(f"{d}日")
    if h > 0:
        parts.append(f"{h}時間")
    if m > 0:
        parts.append(f"{m}分")
    parts.append(f"{s}秒")
    return " ".join(parts)

# 認証ログ送信ヘルパー
async def send_auth_log(guild: discord.Guild, user: discord.User | discord.Member, role: discord.Role | None, removed_role: discord.Role | None = None):
    channel = await get_log_channel(guild)
    if not channel:
        return
    try:
        embed = discord.Embed(
            title="認証ログ",
            description="ユーザーが認証を完了しました。",
            color=discord.Color.green(),
            timestamp=datetime.now()
        )
        embed.add_field(name="ユーザー", value=f"{user} ({user.mention})", inline=True)
        embed.add_field(name="ユーザーID", value=str(user.id), inline=True)
        embed.add_field(name="付与ロール", value=role.mention if role else "未設定", inline=True)
        if removed_role:
            embed.add_field(name="剥奪ロール", value=removed_role.mention, inline=True)
        if user.avatar:
            embed.set_thumbnail(url=user.avatar.url)
        await channel.send(embed=embed)
    except Exception as e:
        print(f"ログ送信エラー: {e}")

# ブラックリストブロック時のログ送信ヘルパー
async def send_blacklist_log(guild: discord.Guild, user: discord.User | discord.Member, blacklist_role: discord.Role):
    channel = await get_log_channel(guild)
    if not channel:
        return
    try:
        embed = discord.Embed(
            title="認証ブロックログ",
            description="ブラックリストロールを持つユーザーの認証を拒否しました。",
            color=discord.Color.red(),
            timestamp=datetime.now()
        )
        embed.add_field(name="ユーザー", value=f"{user} ({user.mention})", inline=True)
        embed.add_field(name="ユーザーID", value=str(user.id), inline=True)
        embed.add_field(name="所持制限ロール", value=blacklist_role.mention, inline=True)
        if user.avatar:
            embed.set_thumbnail(url=user.avatar.url)
        await channel.send(embed=embed)
    except Exception as e:
        print(f"ブラックリストログ送信エラー: {e}")

# 認証失敗時のログ送信ヘルパー
async def send_auth_fail_log(guild: discord.Guild, user: discord.User | discord.Member, reason: str):
    channel = await get_log_channel(guild)
    if not channel:
        return
    try:
        embed = discord.Embed(
            title="認証失敗ログ",
            description=f"認証処理に失敗しました: {reason}",
            color=discord.Color.gold(),
            timestamp=datetime.now()
        )
        embed.add_field(name="ユーザー", value=f"{user} ({user.mention})", inline=True)
        embed.add_field(name="ユーザーID", value=str(user.id), inline=True)
        if user.avatar:
            embed.set_thumbnail(url=user.avatar.url)
        await channel.send(embed=embed)
    except Exception as e:
        print(f"失敗ログ送信エラー: {e}")

# ウェルカムDM送信ヘルパー
async def send_welcome_dm(member: discord.Member):
    try:
        embed = discord.Embed(
            title=f"🎉 {member.guild.name} へようこそ！",
            description="認証が完了し、チャンネルが利用可能になりました！\nサーバーのルールをご確認の上、お楽しみください。",
            color=discord.Color.green(),
            timestamp=datetime.now()
        )
        await member.send(embed=embed)
    except Exception:
        pass

# メンバーのブラックリストチェック関数
async def check_blacklist(guild: discord.Guild, member: discord.Member, blacklist_role_id: int | None) -> discord.Role | None:
    b_id = blacklist_role_id or BLACKLIST_ROLE_ID
    if not b_id:
        return None

    b_role = guild.get_role(b_id)
    if not b_role:
        try:
            b_role = await guild.fetch_role(b_id)
        except Exception:
            b_role = None

    if b_role and b_role in member.roles:
        return b_role
    return None

# 共通ロール付与 & 剥奪処理
async def handle_role_assignment(interaction: discord.Interaction, target_role_id: int | None, remove_role_id: int | None, blacklist_role_id: int | None = None):
    guild = interaction.guild
    if not guild:
        return await interaction.response.send_message("この操作はサーバー内でのみ有効です。", ephemeral=True)

    member = interaction.user
    if not isinstance(member, discord.Member):
        try:
            member = await guild.fetch_member(interaction.user.id)
        except Exception:
            return await interaction.response.send_message("エラー: ユーザー情報の取得に失敗しました。", ephemeral=True)

    # ブラックリストロールチェック
    b_role = await check_blacklist(guild, member, blacklist_role_id)
    if b_role:
        await send_blacklist_log(guild, member, b_role)
        return await interaction.response.send_message(
            f"❌ あなたには認証が制限されたロール（**{b_role.name}**）が付与されているため、認証を行うことができません。\n心当たりがない場合はサーバー管理者にお問い合わせください。",
            ephemeral=True
        )

    role_id = target_role_id or ROLE_ID
    if not role_id:
        return await interaction.response.send_message("エラー: 付与するロールが設定されていません。管理者に連絡してください。", ephemeral=True)

    role = guild.get_role(role_id)
    if not role:
        try:
            role = await guild.fetch_role(role_id)
        except Exception:
            role = None

    if not role:
        return await interaction.response.send_message("エラー: 付与するロールが見つかりません。管理者に連絡してください。", ephemeral=True)

    if role in member.roles:
        return await interaction.response.send_message("すでに認証されています！", ephemeral=True)

    try:
        await member.add_roles(role)

        r_id = remove_role_id or UNVERIFIED_ROLE_ID
        removed_role = None
        if r_id:
            unverified_role = guild.get_role(r_id)
            if not unverified_role:
                try:
                    unverified_role = await guild.fetch_role(r_id)
                except Exception:
                    unverified_role = None
            if unverified_role and unverified_role in member.roles:
                try:
                    await member.remove_roles(unverified_role)
                    removed_role = unverified_role
                except Exception as err:
                    print(f"ロール剥奪エラー: {err}")

        await interaction.response.send_message("認証が完了しました！ロールを付与しました🎉", ephemeral=True)

        await send_auth_log(guild, member, role, removed_role)
        await send_welcome_dm(member)
    except discord.Forbidden:
        await interaction.response.send_message("エラーが発生しました。Botのロール位置が、付与・剥奪したいロールより上にあるか確認してください！", ephemeral=True)
    except Exception as e:
        print(f"ロール操作エラー: {e}")
        await interaction.response.send_message("予期せぬエラーが発生しました。管理者に連絡してください。", ephemeral=True)

# モーダル定義
class CaptchaModal(discord.ui.Modal, title="サーバー認証 (CAPTCHA)"):
    def __init__(self, target_role_id: int | None, remove_role_id: int | None, blacklist_role_id: int | None, expected_captcha: str):
        super().__init__()
        self.target_role_id = target_role_id
        self.remove_role_id = remove_role_id
        self.blacklist_role_id = blacklist_role_id
        self.expected_captcha = expected_captcha

        self.captcha_input = discord.ui.TextInput(
            label=f"確認コード「{expected_captcha}」を入力",
            placeholder=expected_captcha,
            min_length=4,
            max_length=4,
            required=True
        )
        self.add_item(self.captcha_input)

    async def on_submit(self, interaction: discord.Interaction):
        if self.captcha_input.value.strip().upper() != self.expected_captcha.upper():
            if interaction.guild:
                await send_auth_fail_log(interaction.guild, interaction.user, f"CAPTCHA不一致 (入力: {self.captcha_input.value.strip()})")
            return await interaction.response.send_message("❌ 認証コードが一致しません。もう一度やり直してください。", ephemeral=True)
        await handle_role_assignment(interaction, self.target_role_id, self.remove_role_id, self.blacklist_role_id)

class MathModal(discord.ui.Modal, title="サーバー認証 (計算問題)"):
    def __init__(self, target_role_id: int | None, remove_role_id: int | None, blacklist_role_id: int | None, answer: int, expr_str: str):
        super().__init__()
        self.target_role_id = target_role_id
        self.remove_role_id = remove_role_id
        self.blacklist_role_id = blacklist_role_id
        self.answer = answer

        self.answer_input = discord.ui.TextInput(
            label=f"{expr_str} の答えを入力してください",
            placeholder="半角数字で入力",
            min_length=1,
            max_length=5,
            required=True
        )
        self.add_item(self.answer_input)

    async def on_submit(self, interaction: discord.Interaction):
        val = self.answer_input.value.strip()
        if not val.isdigit() or int(val) != self.answer:
            if interaction.guild:
                await send_auth_fail_log(interaction.guild, interaction.user, f"計算間違い (入力: {val})")
            return await interaction.response.send_message("❌ 計算の答えが一致しません。もう一度お試しください。", ephemeral=True)
        await handle_role_assignment(interaction, self.target_role_id, self.remove_role_id, self.blacklist_role_id)

class PassphraseModal(discord.ui.Modal, title="サーバー認証 (合言葉)"):
    def __init__(self, target_role_id: int | None, remove_role_id: int | None, blacklist_role_id: int | None, expected_passphrase: str):
        super().__init__()
        self.target_role_id = target_role_id
        self.remove_role_id = remove_role_id
        self.blacklist_role_id = blacklist_role_id
        self.expected_passphrase = expected_passphrase

        self.pass_input = discord.ui.TextInput(
            label="合言葉を入力してください",
            placeholder="ルール等に書かれた合言葉",
            required=True
        )
        self.add_item(self.pass_input)

    async def on_submit(self, interaction: discord.Interaction):
        if self.pass_input.value.strip() != self.expected_passphrase.strip():
            if interaction.guild:
                await send_auth_fail_log(interaction.guild, interaction.user, "合言葉不一致")
            return await interaction.response.send_message("❌ 合言葉が間違っています。もう一度お試しください。", ephemeral=True)
        await handle_role_assignment(interaction, self.target_role_id, self.remove_role_id, self.blacklist_role_id)

class TermsModal(discord.ui.Modal, title="サーバー認証 (利用規約同意)"):
    def __init__(self, target_role_id: int | None, remove_role_id: int | None, blacklist_role_id: int | None):
        super().__init__()
        self.target_role_id = target_role_id
        self.remove_role_id = remove_role_id
        self.blacklist_role_id = blacklist_role_id

        self.terms_input = discord.ui.TextInput(
            label="「同意する」と入力してください",
            placeholder="同意する",
            required=True
        )
        self.add_item(self.terms_input)

    async def on_submit(self, interaction: discord.Interaction):
        val = self.terms_input.value.strip()
        if val not in ("同意する", "同意") and val.lower() != "agree":
            if interaction.guild:
                await send_auth_fail_log(interaction.guild, interaction.user, f"規約同意文字列不一致 (入力: {val})")
            return await interaction.response.send_message("❌ 「同意する」と正確に入力してください。", ephemeral=True)
        await handle_role_assignment(interaction, self.target_role_id, self.remove_role_id, self.blacklist_role_id)

# Botクラス
class AuthBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        super().__init__(command_prefix="!", intents=intents, help_command=None)

    async def setup_hook(self):
        try:
            synced = await self.tree.sync()
            print(f"コマンド同期成功: {len(synced)} 件")
        except Exception as e:
            print(f"コマンド同期エラー: {e}")

bot = AuthBot()

# ステータスローテーション (10秒ごと)
@tasks.loop(seconds=10)
async def rotate_status():
    global status_index
    if not bot.is_ready() or not bot.user:
        return

    total_members = sum(g.member_count or 0 for g in bot.guilds)
    total_guilds = len(bot.guilds)
    ping = max(0, round(bot.latency * 1000))

    statuses = [
        f"{total_members}人 | {total_guilds}鯖",
        f"ping {ping}ms",
        "Powered by rds9"
    ]

    text = statuses[status_index % len(statuses)]
    status_index += 1

    activity = discord.CustomActivity(name=text)
    await bot.change_presence(activity=activity, status=discord.Status.online)

@bot.event
async def on_ready():
    print(f"認証Bot起動完了！ ({bot.user})")
    if not rotate_status.is_running():
        rotate_status.start()

# ボタンクリック検知リスナー
@bot.listen("on_interaction")
async def on_button_click(interaction: discord.Interaction):
    if interaction.type != discord.InteractionType.component:
        return

    custom_id = interaction.data.get("custom_id", "")

    # 連打防止
    rem = check_cooldown(interaction.user.id)
    if rem is not None:
        return await interaction.response.send_message(f"⚠️ 操作が早すぎます。あと **{rem}秒** 待ってから再度お試しください。", ephemeral=True)

    # 旧バージョン互換
    if custom_id == "auth_button":
        return await handle_role_assignment(interaction, None, None, None)

    if custom_id.startswith("auth_btn:"):
        parts = custom_id.split(":")
        role_id = int(parts[1]) if len(parts) > 1 and parts[1] != "default" and parts[1].isdigit() else None
        remove_role_id = int(parts[2]) if len(parts) > 2 and parts[2] != "none" and parts[2].isdigit() else None
        blacklist_role_id = int(parts[3]) if len(parts) > 3 and parts[3] != "none" and parts[3].isdigit() else None
        auth_type = parts[4] if len(parts) > 4 else "direct"

        # ボタン押下時に即座にブラックリスト判定
        if interaction.guild and isinstance(interaction.user, discord.Member):
            b_role = await check_blacklist(interaction.guild, interaction.user, blacklist_role_id)
            if b_role:
                await send_blacklist_log(interaction.guild, interaction.user, b_role)
                return await interaction.response.send_message(
                    f"❌ あなたには認証が制限されたロール（**{b_role.name}**）が付与されているため、認証を行うことができません。\n心当たりがない場合はサーバー管理者にお問い合わせください。",
                    ephemeral=True
                )

        if auth_type == "captcha":
            captcha = generate_captcha(4)
            return await interaction.response.send_modal(CaptchaModal(role_id, remove_role_id, blacklist_role_id, captcha))

        if auth_type == "math":
            n1 = random.randint(1, 19)
            n2 = random.randint(1, 19)
            ans = n1 + n2
            return await interaction.response.send_modal(MathModal(role_id, remove_role_id, blacklist_role_id, ans, f"{n1} + {n2}"))

        if auth_type == "pass":
            passphrase = ":".join(parts[5:]) if len(parts) > 5 else ""
            return await interaction.response.send_modal(PassphraseModal(role_id, remove_role_id, blacklist_role_id, passphrase))

        if auth_type == "terms":
            return await interaction.response.send_modal(TermsModal(role_id, remove_role_id, blacklist_role_id))

        # direct
        return await handle_role_assignment(interaction, role_id, remove_role_id, blacklist_role_id)

# === スラッシュコマンド ===

# === セットアップUIコンポーネント ===

class SetupDetailModal(discord.ui.Modal, title="認証パネル 詳細設定"):
    def __init__(self, setup_view: "SetupView"):
        super().__init__()
        self.setup_view = setup_view

        self.title_input = discord.ui.TextInput(
            label="パネルのタイトル",
            default=setup_view.panel_title,
            max_length=100,
            required=True
        )
        self.desc_input = discord.ui.TextInput(
            label="パネルの説明文",
            style=discord.TextStyle.paragraph,
            default=setup_view.panel_desc,
            max_length=1000,
            required=True
        )
        self.btn_label_input = discord.ui.TextInput(
            label="ボタンのテキスト",
            default=setup_view.button_label,
            max_length=50,
            required=True
        )
        self.extra_input = discord.ui.TextInput(
            label="合言葉 / 規約文（方式に合わせて使用）",
            default=setup_view.extra_text,
            max_length=500,
            required=False
        )

        self.add_item(self.title_input)
        self.add_item(self.desc_input)
        self.add_item(self.btn_label_input)
        self.add_item(self.extra_input)

    async def on_submit(self, interaction: discord.Interaction):
        self.setup_view.panel_title = self.title_input.value.strip() or self.setup_view.panel_title
        self.setup_view.panel_desc = self.desc_input.value.strip() or self.setup_view.panel_desc
        self.setup_view.button_label = self.btn_label_input.value.strip() or self.setup_view.button_label
        if self.extra_input.value.strip():
            self.setup_view.extra_text = self.extra_input.value.strip()

        embed = self.setup_view.build_embed()
        await interaction.response.edit_message(embed=embed, view=self.setup_view)

class SetupView(discord.ui.View):
    def __init__(self, author_id: int, current_log_channel: discord.TextChannel | None = None):
        super().__init__(timeout=300)
        self.author_id = author_id
        self.target_role: discord.Role | None = None
        self.remove_role: discord.Role | None = None
        self.blacklist_role: discord.Role | None = None
        self.log_channel: discord.TextChannel | None = current_log_channel
        self.auth_type: str = "direct"
        self.panel_title: str = "サーバー認証"
        self.panel_desc: str = "下のボタンを押して認証を完了してください。"
        self.button_label: str = "認証する"
        self.extra_text: str = ""

        self.build_components()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("この設定メニューは操作できません。", ephemeral=True)
            return False
        return True

    def build_components(self):
        self.clear_items()

        # 1. 付与ロール選択
        role_select = discord.ui.RoleSelect(
            placeholder="付与するロールを選択",
            min_values=0,
            max_values=1,
            row=0
        )
        role_select.callback = self.on_role_select
        self.add_item(role_select)

        # 2. ログチャンネル選択
        chan_select = discord.ui.ChannelSelect(
            channel_types=[discord.ChannelType.text],
            placeholder="ログ送信先チャンネルを選択",
            min_values=0,
            max_values=1,
            row=1
        )
        chan_select.callback = self.on_channel_select
        self.add_item(chan_select)

        # 3. 認証方式選択
        type_options = [
            discord.SelectOption(label="ワンクリック認証", value="direct", description="ボタンを押すだけで即時完了", default=(self.auth_type == "direct")),
            discord.SelectOption(label="計算問題認証", value="math", description="ランダムな足し算の答えを入力", default=(self.auth_type == "math")),
            discord.SelectOption(label="CAPTCHA認証", value="captcha", description="4桁の英数字コードを入力", default=(self.auth_type == "captcha")),
            discord.SelectOption(label="合言葉認証", value="passphrase", description="指定の合言葉を入力", default=(self.auth_type == "passphrase")),
            discord.SelectOption(label="利用規約同意", value="terms", description="「同意する」と入力", default=(self.auth_type == "terms")),
        ]
        type_select = discord.ui.Select(
            placeholder="認証方式を選択",
            options=type_options,
            row=2
        )
        type_select.callback = self.on_type_select
        self.add_item(type_select)

        # 4. 操作ボタン
        detail_btn = discord.ui.Button(label="詳細設定", style=discord.ButtonStyle.secondary, row=3)
        detail_btn.callback = self.on_detail_btn
        self.add_item(detail_btn)

        submit_btn = discord.ui.Button(label="パネルを設置", style=discord.ButtonStyle.primary, row=3)
        submit_btn.callback = self.on_submit_btn
        self.add_item(submit_btn)

        cancel_btn = discord.ui.Button(label="閉じる", style=discord.ButtonStyle.danger, row=3)
        cancel_btn.callback = self.on_cancel_btn
        self.add_item(cancel_btn)

    def build_embed(self) -> discord.Embed:
        type_names = {
            "direct": "ワンクリック認証",
            "math": "計算問題認証",
            "captcha": "CAPTCHA認証",
            "passphrase": "合言葉認証",
            "terms": "利用規約同意"
        }
        embed = discord.Embed(
            title="認証パネル設定",
            description="各項目を選択して設定を調整し、「パネルを設置」を押してください。",
            color=discord.Color.dark_gray()
        )
        role_val = self.target_role.mention if self.target_role else "未選択（.env設定を使用）"
        log_val = self.log_channel.mention if self.log_channel else "未選択（.env設定を使用）"
        remove_val = self.remove_role.mention if self.remove_role else "なし"
        black_val = self.blacklist_role.mention if self.blacklist_role else "なし"

        embed.add_field(name="付与ロール", value=role_val, inline=True)
        embed.add_field(name="ログ送信先", value=log_val, inline=True)
        embed.add_field(name="認証方式", value=type_names.get(self.auth_type, self.auth_type), inline=True)
        embed.add_field(name="剥奪ロール", value=remove_val, inline=True)
        embed.add_field(name="制限ロール", value=black_val, inline=True)
        embed.add_field(name="ボタン文字", value=self.button_label, inline=True)
        embed.add_field(name="見出し", value=self.panel_title, inline=False)
        preview_desc = (self.panel_desc[:120] + "...") if len(self.panel_desc) > 120 else self.panel_desc
        embed.add_field(name="説明文", value=preview_desc, inline=False)
        if self.extra_text:
            embed.add_field(name="合言葉 / 規約文", value=self.extra_text, inline=False)

        return embed

    async def on_role_select(self, interaction: discord.Interaction):
        values = interaction.data.get("values", [])
        if values:
            role_id = int(values[0])
            self.target_role = interaction.guild.get_role(role_id) if interaction.guild else None
        else:
            self.target_role = None
        self.build_components()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def on_channel_select(self, interaction: discord.Interaction):
        values = interaction.data.get("values", [])
        if values:
            channel_id = int(values[0])
            self.log_channel = interaction.guild.get_channel(channel_id) if interaction.guild else None
        else:
            self.log_channel = None
        self.build_components()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def on_type_select(self, interaction: discord.Interaction):
        values = interaction.data.get("values", [])
        if values:
            self.auth_type = values[0]
            if self.auth_type == "passphrase" and not self.extra_text:
                self.extra_text = "pass1234"
            elif self.auth_type == "terms" and not self.extra_text:
                self.extra_text = "本サーバーの利用規約に同意します。"
        self.build_components()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def on_detail_btn(self, interaction: discord.Interaction):
        await interaction.response.send_modal(SetupDetailModal(self))

    async def on_cancel_btn(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content="設定メニューを終了しました。", embed=None, view=None)

    async def on_submit_btn(self, interaction: discord.Interaction):
        guild = interaction.guild
        channel = interaction.channel
        if not guild or not channel:
            return await interaction.response.send_message("サーバー内のチャンネルで実行してください。", ephemeral=True)

        if not self.target_role and not ROLE_ID:
            return await interaction.response.send_message("付与するロールを選択してください。", ephemeral=True)

        if self.auth_type == "passphrase" and not self.extra_text:
            return await interaction.response.send_message("合言葉認証にはキーワードの設定が必要です。「詳細設定」から入力してください。", ephemeral=True)

        # ログチャンネル設定があればDBに永続化
        if self.log_channel:
            set_guild_log_channel_id(guild.id, self.log_channel.id)

        role_id_str = str(self.target_role.id) if self.target_role else "default"
        remove_role_id_str = str(self.remove_role.id) if self.remove_role else "none"
        blacklist_role_id_str = str(self.blacklist_role.id) if self.blacklist_role else "none"

        prefix = f"auth_btn:{role_id_str}:{remove_role_id_str}:{blacklist_role_id_str}"
        if self.auth_type == "passphrase":
            custom_id = f"{prefix}:pass:{self.extra_text}"
        elif self.auth_type == "terms":
            custom_id = f"{prefix}:terms"
        else:
            custom_id = f"{prefix}:{self.auth_type}"

        # 設置用パネル作成
        panel_embed = discord.Embed(
            title=self.panel_title,
            description=self.panel_desc,
            color=discord.Color.green()
        )
        if self.auth_type == "terms" and self.extra_text:
            panel_embed.add_field(name="利用規約", value=self.extra_text, inline=False)

        panel_view = discord.ui.View(timeout=None)
        button = discord.ui.Button(
            label=self.button_label,
            style=discord.ButtonStyle.success,
            custom_id=custom_id
        )
        panel_view.add_item(button)

        await channel.send(embed=panel_embed, view=panel_view)

        done_embed = discord.Embed(
            title="設置完了",
            description=f"認証パネルを {channel.mention} に設置しました。",
            color=discord.Color.green()
        )
        await interaction.response.edit_message(embed=done_embed, view=None)

# === スラッシュコマンド ===

@bot.tree.command(name="setup", description="認証パネルの簡単作成・設定メニューを開きます（管理者限定）")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    remove_role="認証時に剥奪するロール（未認証ロール等・任意）",
    blacklist_role="認証を禁止するブラックリストロール（任意）"
)
async def setup_command(
    interaction: discord.Interaction,
    remove_role: discord.Role | None = None,
    blacklist_role: discord.Role | None = None
):
    if not interaction.guild:
        return await interaction.response.send_message("このコマンドはサーバー内でのみ使用できます。", ephemeral=True)

    current_log_id = get_guild_log_channel_id(interaction.guild.id)
    current_log_ch = interaction.guild.get_channel(current_log_id) if current_log_id else None
    if not current_log_ch and current_log_id:
        try:
            current_log_ch = await interaction.guild.fetch_channel(current_log_id)
        except Exception:
            current_log_ch = None

    view = SetupView(interaction.user.id, current_log_channel=current_log_ch)
    if remove_role:
        view.remove_role = remove_role
    if blacklist_role:
        view.blacklist_role = blacklist_role

    embed = view.build_embed()
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

@bot.tree.command(name="set-log-channel", description="サーバーの認証ログ送信先チャンネルを設定します（管理者限定）")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(channel="ログを送信するテキストチャンネル（指定なしでリセット）")
async def set_log_channel_cmd(interaction: discord.Interaction, channel: discord.TextChannel | None = None):
    if not interaction.guild:
        return await interaction.response.send_message("このコマンドはサーバー内でのみ使用できます。", ephemeral=True)

    if channel:
        set_guild_log_channel_id(interaction.guild.id, channel.id)
        await interaction.response.send_message(f"ログ送信先チャンネルを {channel.mention} に設定しました。", ephemeral=True)
    else:
        set_guild_log_channel_id(interaction.guild.id, None)
        await interaction.response.send_message("サーバー個別のログ設定を解除しました（デフォルト設定を使用します）。", ephemeral=True)

@bot.tree.command(name="set-panel", description="認証パネルをこのチャンネルに設置します（管理者限定）")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    role="認証時に付与するロール（未指定時はデフォルトロール）",
    remove_role="認証時に剥奪するロール（未認証ロール等）",
    blacklist_role="認証を禁止するブラックリストロール（要注意ロール等）",
    title="パネルのタイトル",
    description="パネルの説明文",
    button_label="ボタンのテキスト（デフォルト: 認証する）",
    auth_type="認証の方式（デフォルト: ワンクリック）",
    passphrase="合言葉認証を選択した場合の正解キーワード",
    terms_text="規約同意認証で表示する規約メッセージ"
)
@app_commands.choices(auth_type=[
    app_commands.Choice(name="ワンクリック認証", value="direct"),
    app_commands.Choice(name="計算問題認証", value="math"),
    app_commands.Choice(name="CAPTCHA認証（ランダム文字列）", value="captcha"),
    app_commands.Choice(name="合言葉認証（キーワード入力）", value="passphrase"),
    app_commands.Choice(name="規約同意認証（同意入力）", value="terms"),
])
async def set_panel(
    interaction: discord.Interaction,
    role: discord.Role | None = None,
    remove_role: discord.Role | None = None,
    blacklist_role: discord.Role | None = None,
    title: str = "サーバー認証",
    description: str = "下のボタンを押して認証を完了してください。",
    button_label: str = "認証する",
    auth_type: app_commands.Choice[str] | None = None,
    passphrase: str | None = None,
    terms_text: str = "本サーバーのルールを守り、他のメンバーへ迷惑となる行為を行わないことに同意します。"
):
    selected_auth = auth_type.value if auth_type else "direct"
    if selected_auth == "passphrase" and not passphrase:
        return await interaction.response.send_message("エラー: 合言葉認証を選択した場合は「passphrase」オプションで合言葉を指定してください。", ephemeral=True)

    role_id_str = str(role.id) if role else "default"
    remove_role_id_str = str(remove_role.id) if remove_role else "none"
    blacklist_role_id_str = str(blacklist_role.id) if blacklist_role else "none"

    prefix = f"auth_btn:{role_id_str}:{remove_role_id_str}:{blacklist_role_id_str}"
    if selected_auth == "passphrase":
        custom_id = f"{prefix}:pass:{passphrase}"
    elif selected_auth == "terms":
        custom_id = f"{prefix}:terms"
    else:
        custom_id = f"{prefix}:{selected_auth}"

    embed = discord.Embed(
        title=title,
        description=description,
        color=discord.Color.green()
    )

    if selected_auth == "terms":
        embed.add_field(name="利用規約", value=terms_text, inline=False)

    view = discord.ui.View(timeout=None)
    button = discord.ui.Button(
        label=button_label,
        style=discord.ButtonStyle.success,
        custom_id=custom_id
    )
    view.add_item(button)

    await interaction.response.send_message("認証パネルを設置しました。", ephemeral=True)
    if interaction.channel:
        await interaction.channel.send(embed=embed, view=view)

@bot.tree.command(name="ping", description="Botの応答速度（Ping）を測定します")
async def ping(interaction: discord.Interaction):
    ws_ping = max(0, round(bot.latency * 1000))
    status_text = "良好" if ws_ping < 150 else ("普通" if ws_ping < 300 else "遅延")
    color = discord.Color.green() if ws_ping < 150 else (discord.Color.gold() if ws_ping < 300 else discord.Color.red())

    embed = discord.Embed(title="Pong", color=color, timestamp=datetime.now())
    embed.add_field(name="WebSocket Ping", value=f"{ws_ping} ms", inline=True)
    embed.add_field(name="ステータス", value=status_text, inline=True)
    embed.set_footer(text="Powered by rds9")

    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="stats", description="サーバーの認証統計やBotの稼働状態を表示します")
async def stats(interaction: discord.Interaction):
    guild = interaction.guild
    if not guild:
        return await interaction.response.send_message("このコマンドはサーバー内でのみ使用できます。", ephemeral=True)

    await interaction.response.defer(ephemeral=True)

    total_members = guild.member_count or len(guild.members)
    verified_count = 0
    if ROLE_ID:
        target_role = guild.get_role(ROLE_ID)
        if target_role:
            verified_count = sum(1 for m in guild.members if target_role in m.roles and not m.bot)

    unverified_count = max(0, total_members - verified_count)
    verified_rate = f"{(verified_count / total_members * 100):.1f}%" if total_members > 0 else "0%"
    uptime_str = format_uptime(time.time() - start_time)
    ws_ping = max(0, round(bot.latency * 1000))

    embed = discord.Embed(
        title=f"{guild.name} 認証統計",
        color=discord.Color.blue(),
        timestamp=datetime.now()
    )
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)

    embed.add_field(name="総メンバー数", value=f"{total_members} 人", inline=True)
    embed.add_field(name="認証済みメンバー", value=f"{verified_count} 人 ({verified_rate})", inline=True)
    embed.add_field(name="未認証メンバー", value=f"{unverified_count} 人", inline=True)
    embed.add_field(name="Bot稼働時間", value=uptime_str, inline=True)
    embed.add_field(name="WebSocket Ping", value=f"{ws_ping} ms", inline=True)
    embed.add_field(name="参加サーバー総数", value=f"{len(bot.guilds)} 鯖", inline=True)
    embed.set_footer(text="Powered by rds9")

    await interaction.followup.send(embed=embed)

@bot.tree.command(name="help", description="認証Botのヘルプと使い方を表示します")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="認証Bot ヘルプ & コマンド一覧",
        description="サーバーを保護するための認証Botです。",
        color=discord.Color.blue(),
        timestamp=datetime.now()
    )
    embed.add_field(
        name="管理者コマンド",
        value=(
            "`/setup`\nインタラクティブな設定メニュー（ロール選択・ログ先・方式選択）から簡単に認証パネルを作成・設置します。\n\n"
            "`/set-log-channel [channel]`\n認証ログの送信先チャンネルを個別に設定・解除します。\n\n"
            "`/set-panel`\nコマンド引数から直接認証パネルを設置します。"
        ),
        inline=False
    )
    embed.add_field(
        name="情報コマンド",
        value="`/stats` - サーバーの認証人数や稼働状況を表示\n`/ping` - 応答速度（Ping）を測定\n`/help` - このヘルプを表示",
        inline=False
    )
    embed.add_field(
        name="認証方式の一覧",
        value=(
            "• **ワンクリック認証**: ボタンを押すだけで即時認証\n"
            "• **計算問題認証**: 簡単な足し算の答えを入力（Bot対策）\n"
            "• **CAPTCHA認証**: ランダムな4文字の確認コード入力\n"
            "• **合言葉認証**: 設定されたキーワードを入力\n"
            "• **規約同意認証**: 規約を確認し「同意する」と入力"
        ),
        inline=False
    )
    embed.add_field(
        name="ブラックリスト機能",
        value="要注意ロールや制限ロールを持つユーザーの認証を自動でブロックし、ログに記録します。",
        inline=False
    )
    embed.set_footer(text="Powered by rds9")

    await interaction.response.send_message(embed=embed, ephemeral=True)

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
