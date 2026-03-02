"""リサーチCog - 英語圏トレンドを自動収集・分析。
1日3回（8:30, 11:30, 17:30）にデータ収集→Claude CLIで分析→日報に統合される。
"""
import discord
from discord.ext import commands, tasks
from discord import app_commands
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from bot.services.trend_collector import TrendCollector
from bot.services.claude_cli import ClaudeCLIBridge
from bot.config import OWNER_ID

logger = logging.getLogger(__name__)

# 日本時間
JST = timezone(timedelta(hours=9))

# リサーチ実行時刻（各日報の30分前に収集完了させる）
# 朝日報9:00 → 8:30, 昼日報12:00 → 11:30, 夕日報18:00 → 17:30
RESEARCH_TIMES = [
    (8, 30),   # 朝リサーチ
    (11, 30),  # 昼リサーチ
    (17, 30),  # 夕リサーチ
]

# 分析プロンプト
ANALYSIS_PROMPT = """
あなたはLex Ventures のリサーチアナリスト。
以下の英語圏テクノロジートレンドデータを分析してください。

## 分析タスク
1. **日本で未注目のトレンド**: 日本語圏でまだ話題になっていない、早期キャッチすべきネタを3件選定
2. **Venture候補**: 上記から、日本語版ツール・サービスとして構築可能なものを1件提案
3. **X投稿ネタ**: 日本語ツイートにできそうな速報ネタを3件ピックアップ

## 出力フォーマット（JSON）
```json
{
    "trends": [
        {
            "title": "トレンド名",
            "source": "ソース名",
            "why_notable": "なぜ注目すべきか（日本語1-2文）",
            "score": スコア数値
        }
    ],
    "venture_candidate": {
        "name": "提案名（日本語）",
        "description": "概要（日本語2-3文）",
        "source_trend": "元ネタのトレンド名",
        "monetization": "収益化方法",
        "difficulty": "easy/medium/hard",
        "estimated_build_time": "見積もり時間"
    },
    "x_posts": [
        {
            "topic": "投稿テーマ",
            "hook": "ツイートの冒頭（日本語、注目を引くフレーズ）"
        }
    ]
}
```

## トレンドデータ
{trend_data}
""".strip()


class Research(commands.Cog):
    """英語圏トレンドリサーチを自動実行するCog。"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.collector = TrendCollector()
        health = getattr(bot, "health_monitor", None)
        self.claude = ClaudeCLIBridge(health_monitor=health)
        self._last_analysis = None  # 最新の分析結果キャッシュ

    async def cog_load(self):
        """Cog読み込み時にスケジューラー開始。"""
        self.research_loop.start()
        logger.info("🔍 リサーチスケジューラー開始")

    async def cog_unload(self):
        """Cog解除時にスケジューラー停止。"""
        self.research_loop.cancel()
        logger.info("🔍 リサーチスケジューラー停止")

    @tasks.loop(minutes=1)
    async def research_loop(self):
        """毎分チェックし、リサーチ時刻になったら実行。"""
        now = datetime.now(JST)
        for hour, minute in RESEARCH_TIMES:
            if now.hour == hour and now.minute == minute:
                label = {8: "朝", 11: "昼", 17: "夕"}[hour]
                logger.info(f"🔍 {label}リサーチ開始")
                await self.run_research()
                break

    @research_loop.before_loop
    async def before_research(self):
        """Bot準備完了を待つ。"""
        await self.bot.wait_until_ready()
        logger.info("🔍 リサーチ: Bot準備完了。スケジュール監視開始。")

    async def run_research(self) -> Optional[dict]:
        """トレンド収集→Claude分析を実行。

        Returns:
            dict: 分析結果（JSON）またはNone
        """
        try:
            # Step 1: トレンド収集
            logger.info("🔍 Step 1: トレンドデータ収集中...")
            raw_data = await self.collector.collect_all()

            if raw_data.get("total_items", 0) == 0:
                logger.warning("🔍 トレンドデータが0件。分析スキップ。")
                return None

            # Step 2: Claude CLIで分析
            logger.info("🔍 Step 2: Claude分析開始...")
            trend_text = self.collector.format_for_analysis(raw_data)
            prompt = ANALYSIS_PROMPT.replace("{trend_data}", trend_text)

            result = await self.claude.ask(
                prompt,
                profile="complex",
                max_turns=3,
            )

            if not result["success"]:
                logger.error(f"🔍 Claude分析失敗: {result['error']}")
                return None

            # JSON部分を抽出
            analysis = self._extract_json(result["text"])
            self._last_analysis = analysis

            logger.info(
                f"🔍 リサーチ完了: "
                f"トレンド{len(analysis.get('trends', []))}件, "
                f"Venture候補あり={bool(analysis.get('venture_candidate'))}"
            )
            return analysis

        except Exception as e:
            logger.error(f"🔍 リサーチ実行エラー: {e}", exc_info=True)
            return None

    def _extract_json(self, text: str) -> dict:
        """Claude応答からJSONブロックを抽出。"""
        import json
        import re

        # ```json ... ``` パターン
        json_match = re.search(
            r"```json\s*(.*?)\s*```", text, re.DOTALL
        )
        if json_match:
            try:
                return json.loads(json_match.group(1))
            except json.JSONDecodeError:
                pass

        # { ... } 直接パターン
        brace_match = re.search(r"\{.*\}", text, re.DOTALL)
        if brace_match:
            try:
                return json.loads(brace_match.group(0))
            except json.JSONDecodeError:
                pass

        # パースできない場合はテキストとして返す
        logger.warning("🔍 JSON抽出失敗。テキスト応答として保持。")
        return {"raw_text": text, "trends": [], "venture_candidate": None, "x_posts": []}

    def get_latest_analysis(self) -> Optional[dict]:
        """最新の分析結果を取得（日報用）。"""
        return self._last_analysis

    def format_for_report(self, analysis: Optional[dict] = None) -> str:
        """分析結果を日報フォーマットに変換。"""
        if analysis is None:
            analysis = self._last_analysis
        if not analysis:
            return "📊 リサーチデータなし（次回の収集をお待ちください）"

        lines = []

        # トレンド
        trends = analysis.get("trends", [])
        if trends:
            lines.append(f"📊 今日のリサーチ ({len(trends)}件)")
            for i, trend in enumerate(trends, 1):
                source = trend.get("source", "?")
                title = trend.get("title", "?")
                why = trend.get("why_notable", "")
                score = trend.get("score", "")
                score_str = f" ({score}pt)" if score else ""
                lines.append(f"{i}. [{source}] {title}{score_str}")
                if why:
                    lines.append(f"   → {why}")
        else:
            lines.append("📊 リサーチ: 注目トレンドなし")

        # Venture候補
        venture = analysis.get("venture_candidate")
        if venture:
            lines.append("")
            lines.append("💡 Venture候補")
            lines.append(f"「{venture.get('name', '?')}」")
            desc = venture.get("description", "")
            if desc:
                lines.append(f"  {desc}")
            source = venture.get("source_trend", "")
            if source:
                lines.append(f"  元ネタ: {source}")
            monetization = venture.get("monetization", "")
            if monetization:
                lines.append(f"  収益化: {monetization}")
            difficulty = venture.get("difficulty", "")
            build_time = venture.get("estimated_build_time", "")
            if difficulty or build_time:
                lines.append(f"  難易度: {difficulty} / 見積もり: {build_time}")
            lines.append("  → ✅ 承認 / ❌ スキップ")

        return "\n".join(lines)

    # --- スラッシュコマンド ---

    @app_commands.command(
        name="research",
        description="英語圏トレンドリサーチを今すぐ実行",
    )
    async def research_now(self, interaction: discord.Interaction):
        """手動でリサーチを実行。"""
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message(
                "⚡ このコマンドはオーナー専用です。", ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True)
        logger.info("/research コマンド受信")

        analysis = await self.run_research()
        if analysis:
            report = self.format_for_report(analysis)
            await interaction.followup.send(
                f"🔍 **Lex Ventures リサーチレポート**\n\n{report}"
            )
        else:
            await interaction.followup.send(
                "⚠️ リサーチの実行に失敗しました。ログを確認してください。"
            )

    @app_commands.command(
        name="trends",
        description="最新のトレンドデータ（生データ）を表示",
    )
    async def show_trends(self, interaction: discord.Interaction):
        """最新のトレンド生データを表示。"""
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message(
                "⚡ このコマンドはオーナー専用です。", ephemeral=True
            )
            return

        data = self.collector.get_latest_data()
        if not data:
            await interaction.response.send_message(
                "📊 リサーチデータがありません。`/research` で収集してください。"
            )
            return

        text = self.collector.format_for_analysis(data)
        # Discordメッセージの2000文字制限
        if len(text) > 1900:
            text = text[:1900] + "\n\n...（省略）"

        await interaction.response.send_message(f"```\n{text}\n```")


async def setup(bot: commands.Bot):
    """Cogを登録。"""
    await bot.add_cog(Research(bot))
