"""定期報告Cog v2 - Lex Venturesリサーチ統合版。
1日3回（朝9:00、昼12:00、夕18:00）、リサーチ結果とVenture状況を報告。

v2変更点:
- 朝: リサーチレポート + Venture候補提案（✅/❌承認フロー）
- 昼: Venture進捗 + システム状況
- 夕: 1日の総括 + 月間サマリー
"""
import discord
from discord.ext import commands, tasks
from discord import app_commands
import logging
from datetime import datetime, timezone, timedelta, time
from bot.services.claude_cli import ClaudeCLIBridge
from bot.services.owner_profile import OwnerProfile
from bot.services.conversation import ConversationManager
from bot.config import OWNER_ID, REPORT_CHANNEL_ID

logger = logging.getLogger(__name__)

# 日本時間
JST = timezone(timedelta(hours=9))

# レポート時刻（JST）
AM_REPORT_HOUR = 9    # 午前9時
NOON_REPORT_HOUR = 12 # 昼12時
PM_REPORT_HOUR = 18   # 午後6時

# --- v2 プロンプト ---

AM_REPORT_PROMPT = """
あなたはLex。Lex Venturesの朝の定期報告を行ってください。

## 本日のリサーチデータ
{research_data}

## Venture状況
{venture_summary}

## 出力構成（自然な日本語で、合計500文字程度）
```
━━━ Lex Daily Report — AM ━━━

📊 今日のリサーチ (N件)
[リサーチデータから注目トレンド3件を日本語で簡潔に紹介]

💡 Venture候補
[リサーチから最も有望な1件を提案。ただしresearch_dataにventure_candidateがあればそれを使う]
  元ネタ: [ソース]
  想定: [収益化方法]
  → ✅ 承認 / ❌ スキップ

📋 システム状況
  Lex稼働: 正常 | エラー: 0
```

※ Venture候補セクションはリアクション承認で使うので、フォーマットを守ること。
※ JSONやメタデータは出力しない。読みやすいテキストのみ。
""".strip()

NOON_REPORT_PROMPT = """
あなたはLex。Lex Venturesの昼の定期報告を行ってください。

## Venture状況
{venture_summary}

## X投稿状況
{x_stats}

## 出力構成（自然な日本語で、合計300文字程度）
```
━━━ Lex Daily Report — Noon ━━━

🔨 Venture進捗
[承認済み・構築中のVentureがあれば進捗を報告。なければ「待機中」]

📱 X投稿状況
[X投稿統計データを反映]

📈 数字
  稼働中Venture: N | 構築中: N | 累計PV: N
```

※ 自然な日本語で。JSONやメタデータは不要。
""".strip()

PM_REPORT_PROMPT = """
あなたはLex。Lex Venturesの夕方の定期報告を行ってください。

## 今日のリサーチ結果
{research_data}

## Venture状況
{venture_summary}

## 出力構成（自然な日本語で、合計400文字程度）
```
━━━ Lex Daily Report — PM ━━━

✅ 今日の成果
  リサーチ: N記事分析、N件を報告
  [Venture進捗があれば記載]

📊 Venture一覧
  [稼働中Ventureがあれば一覧]

🔍 明日のリサーチ
  テーマ: [明日の注目分野を1つ提案]

💰 月間サマリー
  Venture収益: ¥N (今月)
  実験数: N/3 (目標: 月3個)
```

※ 自然な日本語で。JSONやメタデータは不要。
""".strip()


class DailyReport(commands.Cog):
    """定期報告を自動送信するCog（v2: Ventures統合）。"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        health = getattr(bot, 'health_monitor', None)
        self.claude = ClaudeCLIBridge(health_monitor=health)
        self.profile = OwnerProfile()
        self.conversation = ConversationManager()
        self._report_enabled = True

    async def cog_load(self):
        """Cog読み込み時にスケジューラーを開始。"""
        self.daily_report_loop.start()
        logger.info("⚡ 定期報告スケジューラー開始 (v2)")

    async def cog_unload(self):
        """Cog解除時にスケジューラーを停止。"""
        self.daily_report_loop.cancel()
        logger.info("⚡ 定期報告スケジューラー停止")

    def _get_report_channel(self) -> discord.TextChannel:
        """レポート送信先チャンネルを取得。"""
        if REPORT_CHANNEL_ID:
            channel = self.bot.get_channel(REPORT_CHANNEL_ID)
            if channel:
                return channel
            logger.warning(
                f"REPORT_CHANNEL_ID ({REPORT_CHANNEL_ID}) が見つかりません。"
                "DMにフォールバックします。"
            )
        return None

    async def _send_to_owner(self, content: str):
        """オーナーにメッセージを送信（チャンネル or DM）。"""
        channel = self._get_report_channel()
        if channel:
            await channel.send(content)
            return

        try:
            owner = await self.bot.fetch_user(OWNER_ID)
            if owner:
                await owner.send(content)
        except Exception as e:
            logger.error(f"オーナーへのDM送信失敗: {e}")

    def _get_research_data(self) -> str:
        """リサーチCogから最新データを取得。"""
        try:
            research_cog = self.bot.get_cog("Research")
            if research_cog:
                return research_cog.format_for_report()
        except Exception as e:
            logger.warning(f"リサーチデータ取得エラー: {e}")
        return "リサーチデータなし（Research Cogが未ロード）"

    def _get_venture_summary(self) -> str:
        """VenturesCogからサマリーを取得。"""
        try:
            ventures_cog = self.bot.get_cog("Ventures")
            if ventures_cog:
                return ventures_cog.manager.format_summary()
        except Exception as e:
            logger.warning(f"Ventureサマリー取得エラー: {e}")
        return "Ventureデータなし（Ventures Cogが未ロード）"

    def _get_x_stats(self) -> str:
        """XPosterCogから投稿統計を取得。"""
        try:
            x_cog = self.bot.get_cog("XPoster")
            if x_cog:
                return x_cog.format_stats_for_report()
        except Exception as e:
            logger.warning(f"X統計取得エラー: {e}")
        return "X投稿データなし（XPoster Cogが未ロード）"

    async def _generate_report(self, report_type: str) -> str:
        """Claude CLIを使ってレポートを生成。

        Args:
            report_type: "am", "noon", or "pm"

        Returns:
            str: レポートテキスト
        """
        # リサーチデータ、Ventureサマリー、X統計を取得
        research_data = self._get_research_data()
        venture_summary = self._get_venture_summary()
        x_stats = self._get_x_stats()

        # プロンプトにデータを埋め込む
        prompts = {
            "am": AM_REPORT_PROMPT,
            "noon": NOON_REPORT_PROMPT,
            "pm": PM_REPORT_PROMPT,
        }
        prompt_template = prompts.get(report_type, AM_REPORT_PROMPT)
        prompt = prompt_template.replace(
            "{research_data}", research_data
        ).replace(
            "{venture_summary}", venture_summary
        ).replace(
            "{x_stats}", x_stats
        )

        system_prompt = self.profile.get_system_context()

        # 会話コンテキストも含める
        conversation_context = self.conversation.get_context(max_turns=5)
        if conversation_context:
            system_prompt += "\n\n" + conversation_context

        result = await self.claude.ask(
            prompt,
            system_prompt=system_prompt,
            max_turns=3,
        )

        if result["success"]:
            self.conversation.add_bot_response(
                content=result["text"],
                risk_level="LOW",
                cost_usd=result.get("cost_usd", 0),
            )
            return result["text"]
        else:
            logger.error(f"定期報告生成エラー: {result['error']}")
            return f"⚠️ 定期報告の生成に失敗しました: {result['error']}"

    async def _handle_am_report(self):
        """朝の日報: リサーチ実行 → 日報 → Venture提案。"""
        # まずリサーチを実行
        research_cog = self.bot.get_cog("Research")
        if research_cog:
            logger.info("⚡ 朝日報前のリサーチ実行")
            analysis = await research_cog.run_research()
        else:
            analysis = None

        # 日報生成
        report = await self._generate_report("am")
        await self._send_to_owner(report)

        # Venture候補があれば承認フロー付きで送信
        if analysis and analysis.get("venture_candidate"):
            channel = self._get_report_channel()
            if channel:
                ventures_cog = self.bot.get_cog("Ventures")
                if ventures_cog:
                    await ventures_cog.propose_venture(channel, analysis)

    @tasks.loop(minutes=1)
    async def daily_report_loop(self):
        """毎分チェックし、レポート時刻になったら報告を送信。"""
        if not self._report_enabled:
            return

        now = datetime.now(JST)
        current_hour = now.hour
        current_minute = now.minute

        # 毎時0分にのみチェック
        if current_minute != 0:
            return

        if current_hour == AM_REPORT_HOUR:
            logger.info("⚡ 朝定期報告を生成開始 (v2)")
            await self._handle_am_report()
            logger.info("⚡ 朝定期報告送信完了")

        elif current_hour == NOON_REPORT_HOUR:
            logger.info("⚡ 昼定期報告を生成開始")
            report = await self._generate_report("noon")
            await self._send_to_owner(report)
            logger.info("⚡ 昼定期報告送信完了")

        elif current_hour == PM_REPORT_HOUR:
            logger.info("⚡ 夕定期報告を生成開始")
            report = await self._generate_report("pm")
            await self._send_to_owner(report)
            logger.info("⚡ 夕定期報告送信完了")

    @daily_report_loop.before_loop
    async def before_daily_report(self):
        """Bot起動完了を待つ。"""
        await self.bot.wait_until_ready()
        logger.info("⚡ 定期報告: Bot準備完了。スケジュール監視開始。")

    # --- スラッシュコマンド ---

    @app_commands.command(
        name="report", description="定期報告を今すぐ生成する（午前/午後）"
    )
    @app_commands.describe(
        report_type="レポートの種類（am=朝, noon=昼, pm=夕）"
    )
    @app_commands.choices(
        report_type=[
            app_commands.Choice(name="🌅 朝報告", value="am"),
            app_commands.Choice(name="☀️ 昼報告", value="noon"),
            app_commands.Choice(name="🌆 夕報告", value="pm"),
        ]
    )
    async def report_now(
        self, interaction: discord.Interaction, report_type: str = "am"
    ):
        """手動で定期報告を生成。"""
        if not interaction.user.id == OWNER_ID:
            await interaction.response.send_message(
                "⚡ このコマンドはオーナー専用です。", ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True)
        logger.info(f"/report コマンド受信: {report_type}")

        if report_type == "am":
            # 朝報告はリサーチも実行
            research_cog = self.bot.get_cog("Research")
            if research_cog:
                await research_cog.run_research()

        report = await self._generate_report(report_type)
        await interaction.followup.send(report)

    @app_commands.command(
        name="report_toggle", description="定期報告のON/OFF切り替え"
    )
    async def report_toggle(self, interaction: discord.Interaction):
        """定期報告の有効/無効を切り替え。"""
        if not interaction.user.id == OWNER_ID:
            await interaction.response.send_message(
                "⚡ このコマンドはオーナー専用です。", ephemeral=True
            )
            return

        self._report_enabled = not self._report_enabled
        status = "✅ 有効" if self._report_enabled else "❌ 無効"
        await interaction.response.send_message(
            f"⚡ 定期報告: {status}\n"
            f"朝 {AM_REPORT_HOUR}:00 / 昼 {NOON_REPORT_HOUR}:00 / 夕 {PM_REPORT_HOUR}:00（JST）"
        )
        logger.info(f"定期報告切り替え: {status}")

    @app_commands.command(
        name="report_status", description="定期報告の設定状況を表示"
    )
    async def report_status(self, interaction: discord.Interaction):
        """定期報告の現在の設定を表示。"""
        if not interaction.user.id == OWNER_ID:
            await interaction.response.send_message(
                "⚡ このコマンドはオーナー専用です。", ephemeral=True
            )
            return

        channel = self._get_report_channel()
        channel_info = f"#{channel.name}" if channel else "DM（オーナー宛）"

        embed = discord.Embed(
            title="⚡ 定期報告 設定状況 (v2)",
            color=discord.Color.blue(),
        )
        embed.add_field(
            name="ステータス",
            value="✅ 有効" if self._report_enabled else "❌ 無効",
            inline=True,
        )
        embed.add_field(
            name="朝報告",
            value=f"{AM_REPORT_HOUR}:00 JST\n(リサーチ+Venture提案)",
            inline=True,
        )
        embed.add_field(
            name="昼報告",
            value=f"{NOON_REPORT_HOUR}:00 JST\n(Venture進捗)",
            inline=True,
        )
        embed.add_field(
            name="夕報告",
            value=f"{PM_REPORT_HOUR}:00 JST\n(総括+月間サマリー)",
            inline=True,
        )
        embed.add_field(
            name="送信先",
            value=channel_info,
            inline=False,
        )
        embed.add_field(
            name="リサーチ自動実行",
            value="毎朝 8:30 JST（日報30分前）",
            inline=False,
        )
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    """Cogを登録。"""
    await bot.add_cog(DailyReport(bot))
