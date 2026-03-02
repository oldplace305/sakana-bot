"""X(Twitter)投稿Cog - Lex Venturesの別アカウントで英語圏トレンド速報を投稿。
Free tier: 月500投稿（書き込みのみ）。

投稿フロー:
1. リサーチ結果 → Claude CLIで日本語ツイート生成
2. Discord承認（✅/❌リアクション） or 自動投稿
3. X API v2 POST /2/tweets

最初は全投稿を承認制。品質安定後、カテゴリ別に自動投稿に切り替え。
"""
import discord
from discord.ext import commands, tasks
from discord import app_commands
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, List

from bot.services.claude_cli import ClaudeCLIBridge
from bot.config import (
    OWNER_ID, REPORT_CHANNEL_ID,
    X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_SECRET,
)
from bot.utils.paths import DATA_DIR

logger = logging.getLogger(__name__)

# 日本時間
JST = timezone(timedelta(hours=9))

# X投稿承認リアクション
POST_APPROVE_EMOJI = "📤"
POST_REJECT_EMOJI = "🚫"

# 投稿キューファイル
X_QUEUE_FILE = DATA_DIR / "x_post_queue.json"

# Free tier 月間上限
MONTHLY_POST_LIMIT = 500

# ツイート生成プロンプト
TWEET_PROMPT = """
あなたはLex Ventures（@{account_name}）のX運用担当。
以下のトレンドデータから、日本語で3件のツイートを生成してください。

## ルール
- 各ツイート280文字以内（日本語は140文字目安）
- 「英語圏で話題」「日本ではまだ知られていない」という切り口
- ハッシュタグは1-2個まで
- 煽りすぎない、事実ベースで
- 冒頭にフック（🔥、⚡、📈 など）

## トレンドデータ
{trend_data}

## 出力フォーマット（JSON配列）
```json
[
    {{
        "text": "ツイート本文",
        "topic": "元ネタのトピック名",
        "category": "tech/ai/startup/tool"
    }}
]
```
""".strip()


class XPostQueue:
    """X投稿キューの管理。承認待ち投稿を永続化。"""

    def __init__(self):
        self._ensure_file()

    def _ensure_file(self):
        """キューファイルが存在しなければ初期化。"""
        if not X_QUEUE_FILE.exists():
            self._save({
                "pending": [],
                "posted": [],
                "rejected": [],
                "monthly_count": 0,
                "month": datetime.now(JST).strftime("%Y-%m"),
            })

    def _load(self) -> dict:
        with open(X_QUEUE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)

    def _save(self, data: dict):
        with open(X_QUEUE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _reset_monthly_if_needed(self, data: dict) -> dict:
        """月が変わったらカウントリセット。"""
        current_month = datetime.now(JST).strftime("%Y-%m")
        if data.get("month") != current_month:
            data["monthly_count"] = 0
            data["month"] = current_month
        return data

    def add_pending(self, text: str, topic: str, category: str,
                    discord_message_id: Optional[int] = None) -> int:
        """承認待ちキューに投稿を追加。返値はキュー内インデックス。"""
        data = self._load()
        data = self._reset_monthly_if_needed(data)
        entry = {
            "text": text,
            "topic": topic,
            "category": category,
            "created_at": datetime.now(JST).isoformat(),
            "discord_message_id": discord_message_id,
            "status": "pending",
        }
        data["pending"].append(entry)
        self._save(data)
        return len(data["pending"]) - 1

    def approve(self, index: int) -> Optional[dict]:
        """承認待ちからポップして返す。"""
        data = self._load()
        data = self._reset_monthly_if_needed(data)
        if index < 0 or index >= len(data["pending"]):
            return None
        entry = data["pending"].pop(index)
        entry["status"] = "approved"
        entry["approved_at"] = datetime.now(JST).isoformat()
        self._save(data)
        return entry

    def reject(self, index: int) -> bool:
        """承認待ちを却下。"""
        data = self._load()
        if index < 0 or index >= len(data["pending"]):
            return False
        entry = data["pending"].pop(index)
        entry["status"] = "rejected"
        data["rejected"].append(entry)
        self._save(data)
        return True

    def record_posted(self, entry: dict, tweet_id: str = ""):
        """投稿完了を記録。"""
        data = self._load()
        data = self._reset_monthly_if_needed(data)
        entry["posted_at"] = datetime.now(JST).isoformat()
        entry["tweet_id"] = tweet_id
        data["posted"].append(entry)
        data["monthly_count"] = data.get("monthly_count", 0) + 1
        self._save(data)

    def find_pending_by_message_id(self, message_id: int) -> Optional[int]:
        """メッセージIDから承認待ちインデックスを検索。"""
        data = self._load()
        for i, entry in enumerate(data["pending"]):
            if entry.get("discord_message_id") == message_id:
                return i
        return None

    def get_stats(self) -> dict:
        """投稿統計を取得。"""
        data = self._load()
        data = self._reset_monthly_if_needed(data)
        return {
            "pending": len(data["pending"]),
            "posted_total": len(data["posted"]),
            "rejected_total": len(data["rejected"]),
            "monthly_count": data.get("monthly_count", 0),
            "monthly_remaining": MONTHLY_POST_LIMIT - data.get("monthly_count", 0),
        }


class XPoster(commands.Cog):
    """X投稿管理Cog。ツイート生成・承認・投稿。"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.queue = XPostQueue()
        health = getattr(bot, "health_monitor", None)
        self.claude = ClaudeCLIBridge(health_monitor=health)
        self._x_configured = bool(X_API_KEY and X_ACCESS_TOKEN)

        if self._x_configured:
            logger.info("📱 X API設定済み。投稿機能有効。")
        else:
            logger.warning(
                "📱 X API未設定。ツイート生成は可能ですが投稿はスキップされます。"
                "  .envにX_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_SECRETを設定してください。"
            )

    async def generate_tweets(self, trend_data: str, account_name: str = "lex_ventures") -> List[dict]:
        """Claude CLIでツイートを生成。

        Args:
            trend_data: リサーチのフォーマット済みテキスト
            account_name: Xアカウント名

        Returns:
            list: [{"text": "...", "topic": "...", "category": "..."}]
        """
        prompt = TWEET_PROMPT.replace("{trend_data}", trend_data).replace(
            "{account_name}", account_name
        )

        result = await self.claude.ask(prompt, profile="normal", max_turns=3)

        if not result["success"]:
            logger.error(f"📱 ツイート生成失敗: {result['error']}")
            return []

        return self._extract_tweets(result["text"])

    def _extract_tweets(self, text: str) -> List[dict]:
        """Claude応答からツイート配列を抽出。"""
        import re

        # ```json ... ``` パターン
        json_match = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
        if json_match:
            try:
                tweets = json.loads(json_match.group(1))
                if isinstance(tweets, list):
                    return tweets
            except json.JSONDecodeError:
                pass

        # [ ... ] 直接パターン
        bracket_match = re.search(r"\[.*\]", text, re.DOTALL)
        if bracket_match:
            try:
                tweets = json.loads(bracket_match.group(0))
                if isinstance(tweets, list):
                    return tweets
            except json.JSONDecodeError:
                pass

        logger.warning("📱 ツイートJSON抽出失敗")
        return []

    async def _post_to_x(self, text: str) -> Optional[str]:
        """X API v2でツイートを投稿。

        Returns:
            str: tweet_id or None
        """
        if not self._x_configured:
            logger.warning("📱 X API未設定のため投稿スキップ")
            return None

        # 月間上限チェック
        stats = self.queue.get_stats()
        if stats["monthly_remaining"] <= 0:
            logger.warning("📱 月間投稿上限(500)に達しました")
            return None

        try:
            import tweepy

            client = tweepy.Client(
                consumer_key=X_API_KEY,
                consumer_secret=X_API_SECRET,
                access_token=X_ACCESS_TOKEN,
                access_token_secret=X_ACCESS_SECRET,
            )

            response = client.create_tweet(text=text)
            tweet_id = str(response.data["id"])
            logger.info(f"📱 ツイート投稿成功: {tweet_id}")
            return tweet_id

        except Exception as e:
            logger.error(f"📱 ツイート投稿エラー: {e}")
            return None

    async def send_for_approval(self, channel: discord.TextChannel,
                                tweets: List[dict]):
        """ツイート案をDiscordに送信し、承認リアクションを付ける。"""
        for tweet in tweets:
            text = tweet.get("text", "")
            topic = tweet.get("topic", "")
            category = tweet.get("category", "")

            embed = discord.Embed(
                title=f"📱 X投稿案 [{category}]",
                description=text,
                color=discord.Color.blue(),
            )
            if topic:
                embed.set_footer(text=f"元ネタ: {topic}")

            msg = await channel.send(embed=embed)
            await msg.add_reaction(POST_APPROVE_EMOJI)
            await msg.add_reaction(POST_REJECT_EMOJI)

            # キューに追加
            self.queue.add_pending(
                text=text,
                topic=topic,
                category=category,
                discord_message_id=msg.id,
            )

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """リアクションでツイート承認/却下を処理。"""
        # Bot自身のリアクション無視
        if payload.user_id == self.bot.user.id:
            return
        # オーナーのみ
        if payload.user_id != OWNER_ID:
            return

        emoji = str(payload.emoji)
        if emoji not in (POST_APPROVE_EMOJI, POST_REJECT_EMOJI):
            return

        # メッセージIDからキューを検索
        index = self.queue.find_pending_by_message_id(payload.message_id)
        if index is None:
            return

        channel = self.bot.get_channel(payload.channel_id)
        if not channel:
            return

        if emoji == POST_APPROVE_EMOJI:
            entry = self.queue.approve(index)
            if entry:
                tweet_id = await self._post_to_x(entry["text"])
                self.queue.record_posted(entry, tweet_id or "")

                if tweet_id:
                    await channel.send(
                        f"📤 **投稿完了！** tweet_id: {tweet_id}"
                    )
                else:
                    await channel.send(
                        "📤 **承認済み** （X API未設定のため実際の投稿はスキップ）"
                    )

        elif emoji == POST_REJECT_EMOJI:
            self.queue.reject(index)
            await channel.send("🚫 投稿却下")

    def format_stats_for_report(self) -> str:
        """日報用の投稿統計フォーマット。"""
        stats = self.queue.get_stats()
        return (
            f"📱 X アカウント\n"
            f"  承認待ち: {stats['pending']}件 | "
            f"今月投稿: {stats['monthly_count']}/{MONTHLY_POST_LIMIT}\n"
            f"  累計投稿: {stats['posted_total']}件"
        )

    # --- スラッシュコマンド ---

    @app_commands.command(
        name="x_generate",
        description="リサーチデータからツイート案を生成して承認フローに送る",
    )
    async def x_generate(self, interaction: discord.Interaction):
        """ツイート生成→承認フロー。"""
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message(
                "⚡ このコマンドはオーナー専用です。", ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True)

        # リサーチデータ取得
        research_cog = self.bot.get_cog("Research")
        if not research_cog:
            await interaction.followup.send("⚠️ Research Cogが読み込まれていません。")
            return

        from bot.services.trend_collector import TrendCollector
        collector = TrendCollector()
        data = collector.get_latest_data()
        if not data:
            await interaction.followup.send(
                "📊 リサーチデータがありません。先に `/research` を実行してください。"
            )
            return

        trend_text = collector.format_for_analysis(data)

        # ツイート生成
        tweets = await self.generate_tweets(trend_text)
        if not tweets:
            await interaction.followup.send("⚠️ ツイート生成に失敗しました。")
            return

        await interaction.followup.send(
            f"📱 {len(tweets)}件のツイート案を生成しました。承認してください："
        )

        # 承認フローに送信
        await self.send_for_approval(interaction.channel, tweets)

    @app_commands.command(
        name="x_stats",
        description="X投稿の統計を表示",
    )
    async def x_stats(self, interaction: discord.Interaction):
        """X投稿統計。"""
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message(
                "⚡ このコマンドはオーナー専用です。", ephemeral=True
            )
            return

        stats = self.queue.get_stats()
        embed = discord.Embed(
            title="📱 X投稿 統計",
            color=discord.Color.blue(),
        )
        embed.add_field(name="承認待ち", value=str(stats["pending"]), inline=True)
        embed.add_field(
            name="今月の投稿",
            value=f"{stats['monthly_count']}/{MONTHLY_POST_LIMIT}",
            inline=True,
        )
        embed.add_field(
            name="残り枠",
            value=str(stats["monthly_remaining"]),
            inline=True,
        )
        embed.add_field(name="累計投稿", value=str(stats["posted_total"]), inline=True)
        embed.add_field(name="累計却下", value=str(stats["rejected_total"]), inline=True)
        embed.add_field(
            name="X API",
            value="✅ 設定済み" if self._x_configured else "❌ 未設定",
            inline=True,
        )

        await interaction.response.send_message(embed=embed)

    @app_commands.command(
        name="x_post",
        description="テキストを直接Xに投稿（承認フローをスキップ）",
    )
    @app_commands.describe(text="投稿するテキスト")
    async def x_post_direct(self, interaction: discord.Interaction, text: str):
        """直接投稿。"""
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message(
                "⚡ このコマンドはオーナー専用です。", ephemeral=True
            )
            return

        if not self._x_configured:
            await interaction.response.send_message(
                "⚠️ X APIが設定されていません。`.env`にキーを追加してください。"
            )
            return

        await interaction.response.defer(thinking=True)

        tweet_id = await self._post_to_x(text)
        if tweet_id:
            entry = {
                "text": text,
                "topic": "direct",
                "category": "manual",
                "status": "approved",
            }
            self.queue.record_posted(entry, tweet_id)
            await interaction.followup.send(f"📤 **投稿完了！** tweet_id: {tweet_id}")
        else:
            await interaction.followup.send("⚠️ 投稿に失敗しました。ログを確認してください。")


async def setup(bot: commands.Bot):
    """Cogを登録。"""
    await bot.add_cog(XPoster(bot))
