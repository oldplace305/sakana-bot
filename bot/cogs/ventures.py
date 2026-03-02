"""Ventures Cog - Ventureの提案・承認・管理。
Discordリアクション（✅/❌）で承認フローを実現する。
"""
import discord
from discord.ext import commands
from discord import app_commands
import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

from bot.config import OWNER_ID, REPORT_CHANNEL_ID
from bot.utils.paths import DATA_DIR

logger = logging.getLogger(__name__)

# 日本時間
JST = timezone(timedelta(hours=9))

# Ventureデータファイル
VENTURES_FILE = DATA_DIR / "ventures.json"

# Ventureのライフサイクル状態
STATES = {
    "proposed": "💭 提案中",
    "approved": "✅ 承認済み",
    "building": "🔨 構築中",
    "deployed": "🚀 デプロイ済み",
    "active": "🟢 稼働中",
    "retired": "⬛ 終了",
}

# リアクション絵文字
APPROVE_EMOJI = "✅"
REJECT_EMOJI = "❌"


class VentureManager:
    """Ventureデータの永続化と管理。"""

    def __init__(self):
        self._ensure_file()

    def _ensure_file(self):
        """ventures.jsonが存在しなければ初期化。"""
        if not VENTURES_FILE.exists():
            self._save({
                "ventures": {},
                "next_id": 1,
                "created_at": datetime.now(JST).strftime("%Y-%m-%d"),
            })

    def _load(self) -> dict:
        """ventures.jsonを読み込む。"""
        with open(VENTURES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)

    def _save(self, data: dict):
        """ventures.jsonに保存。"""
        with open(VENTURES_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def propose(self, name: str, description: str, source: str = "",
                monetization: str = "", difficulty: str = "medium") -> str:
        """新しいVentureを提案。

        Returns:
            str: Venture ID (例: "V001")
        """
        data = self._load()
        vid = f"V{data['next_id']:03d}"
        data["ventures"][vid] = {
            "name": name,
            "description": description,
            "state": "proposed",
            "source": source,
            "monetization": monetization,
            "difficulty": difficulty,
            "proposed_date": datetime.now(JST).strftime("%Y-%m-%d"),
            "approved_date": None,
            "deployed_date": None,
            "url": None,
            "monthly_pv": 0,
            "monthly_revenue": 0,
            "discord_message_id": None,
        }
        data["next_id"] += 1
        self._save(data)
        logger.info(f"Venture提案: {vid} - {name}")
        return vid

    def approve(self, vid: str) -> bool:
        """Ventureを承認。"""
        data = self._load()
        venture = data["ventures"].get(vid)
        if not venture or venture["state"] != "proposed":
            return False
        venture["state"] = "approved"
        venture["approved_date"] = datetime.now(JST).strftime("%Y-%m-%d")
        self._save(data)
        logger.info(f"Venture承認: {vid}")
        return True

    def reject(self, vid: str) -> bool:
        """Ventureをスキップ（提案取り消し）。"""
        data = self._load()
        venture = data["ventures"].get(vid)
        if not venture or venture["state"] != "proposed":
            return False
        venture["state"] = "retired"
        self._save(data)
        logger.info(f"Ventureスキップ: {vid}")
        return True

    def update_state(self, vid: str, new_state: str, **kwargs) -> bool:
        """Ventureの状態を更新。"""
        if new_state not in STATES:
            return False
        data = self._load()
        venture = data["ventures"].get(vid)
        if not venture:
            return False
        venture["state"] = new_state
        for key, value in kwargs.items():
            if key in venture:
                venture[key] = value
        self._save(data)
        logger.info(f"Venture更新: {vid} → {new_state}")
        return True

    def set_message_id(self, vid: str, message_id: int):
        """VentureにDiscordメッセージIDを紐付け。"""
        data = self._load()
        venture = data["ventures"].get(vid)
        if venture:
            venture["discord_message_id"] = message_id
            self._save(data)

    def find_by_message_id(self, message_id: int) -> tuple[str, dict] | None:
        """メッセージIDからVentureを検索。"""
        data = self._load()
        for vid, venture in data["ventures"].items():
            if venture.get("discord_message_id") == message_id:
                return vid, venture
        return None

    def get_all(self) -> dict:
        """全Ventureを取得。"""
        return self._load().get("ventures", {})

    def get_active(self) -> dict:
        """稼働中のVentureのみ取得。"""
        return {
            vid: v for vid, v in self.get_all().items()
            if v["state"] in ("approved", "building", "deployed", "active")
        }

    def get_stats(self) -> dict:
        """Venture統計を取得。"""
        ventures = self.get_all()
        stats = {state: 0 for state in STATES}
        total_pv = 0
        total_revenue = 0

        for v in ventures.values():
            state = v.get("state", "proposed")
            if state in stats:
                stats[state] += 1
            total_pv += v.get("monthly_pv", 0)
            total_revenue += v.get("monthly_revenue", 0)

        return {
            "total": len(ventures),
            "by_state": stats,
            "total_monthly_pv": total_pv,
            "total_monthly_revenue": total_revenue,
        }

    def format_summary(self) -> str:
        """Venture一覧サマリーを生成。"""
        ventures = self.get_all()
        if not ventures:
            return "📋 Venture: なし"

        lines = ["📊 Venture一覧"]
        for vid, v in ventures.items():
            state_emoji = STATES.get(v["state"], "❓")
            name = v["name"]
            url = v.get("url", "")
            pv = v.get("monthly_pv", 0)

            line = f"  {vid} {name} [{state_emoji}]"
            if url:
                line += f" {url}"
            if pv > 0:
                line += f" PV: {pv}"
            lines.append(line)

        stats = self.get_stats()
        lines.append("")
        lines.append(
            f"💰 月間: PV {stats['total_monthly_pv']} / "
            f"収益 ¥{stats['total_monthly_revenue']:,}"
        )

        return "\n".join(lines)


class Ventures(commands.Cog):
    """Venture管理Cog。提案・承認・追跡。"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.manager = VentureManager()

    async def propose_venture(
        self, channel: discord.TextChannel, analysis: dict
    ) -> str | None:
        """リサーチ分析結果からVentureを提案し、承認リアクションを付ける。

        Args:
            channel: 送信先チャンネル
            analysis: research.pyの分析結果

        Returns:
            str: Venture ID or None
        """
        candidate = analysis.get("venture_candidate")
        if not candidate:
            return None

        name = candidate.get("name", "無題のVenture")
        description = candidate.get("description", "")
        source = candidate.get("source_trend", "")
        monetization = candidate.get("monetization", "")
        difficulty = candidate.get("difficulty", "medium")

        # Venture登録
        vid = self.manager.propose(
            name=name,
            description=description,
            source=source,
            monetization=monetization,
            difficulty=difficulty,
        )

        # Discord Embed送信
        embed = discord.Embed(
            title=f"💡 Venture候補 {vid}",
            description=f"**{name}**",
            color=discord.Color.gold(),
        )
        if description:
            embed.add_field(name="概要", value=description, inline=False)
        if source:
            embed.add_field(name="元ネタ", value=source, inline=True)
        if monetization:
            embed.add_field(name="収益化", value=monetization, inline=True)
        if difficulty:
            embed.add_field(name="難易度", value=difficulty, inline=True)

        embed.set_footer(text="✅ = 承認して構築開始 / ❌ = スキップ")

        msg = await channel.send(embed=embed)
        await msg.add_reaction(APPROVE_EMOJI)
        await msg.add_reaction(REJECT_EMOJI)

        # メッセージIDを紐付け
        self.manager.set_message_id(vid, msg.id)
        logger.info(f"Venture提案送信: {vid} (msg_id: {msg.id})")
        return vid

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """リアクション追加を監視し、Venture承認/却下を処理。"""
        # Bot自身のリアクションは無視
        if payload.user_id == self.bot.user.id:
            return

        # オーナーのリアクションのみ処理
        if payload.user_id != OWNER_ID:
            return

        emoji = str(payload.emoji)
        if emoji not in (APPROVE_EMOJI, REJECT_EMOJI):
            return

        # メッセージIDからVentureを検索
        result = self.manager.find_by_message_id(payload.message_id)
        if result is None:
            return

        vid, venture = result

        # 既にproposed以外の状態なら無視
        if venture["state"] != "proposed":
            return

        channel = self.bot.get_channel(payload.channel_id)
        if not channel:
            return

        if emoji == APPROVE_EMOJI:
            self.manager.approve(vid)
            await channel.send(
                f"✅ **{vid} 承認！** — 「{venture['name']}」の構築を開始します。"
            )
            logger.info(f"Venture承認（リアクション）: {vid}")
            # TODO: Phase 3でここから自動構築パイプラインを起動

        elif emoji == REJECT_EMOJI:
            self.manager.reject(vid)
            await channel.send(
                f"❌ **{vid} スキップ** — 「{venture['name']}」は見送ります。"
            )
            logger.info(f"Ventureスキップ（リアクション）: {vid}")

    # --- スラッシュコマンド ---

    @app_commands.command(
        name="ventures",
        description="Venture一覧を表示",
    )
    async def list_ventures(self, interaction: discord.Interaction):
        """Venture一覧を表示。"""
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message(
                "⚡ このコマンドはオーナー専用です。", ephemeral=True
            )
            return

        summary = self.manager.format_summary()
        await interaction.response.send_message(summary)

    @app_commands.command(
        name="venture_status",
        description="Venture統計サマリーを表示",
    )
    async def venture_stats(self, interaction: discord.Interaction):
        """Venture統計を表示。"""
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message(
                "⚡ このコマンドはオーナー専用です。", ephemeral=True
            )
            return

        stats = self.manager.get_stats()
        embed = discord.Embed(
            title="📊 Lex Ventures 統計",
            color=discord.Color.blue(),
        )
        embed.add_field(name="合計", value=str(stats["total"]), inline=True)

        for state, label in STATES.items():
            count = stats["by_state"].get(state, 0)
            if count > 0:
                embed.add_field(name=label, value=str(count), inline=True)

        embed.add_field(
            name="月間PV",
            value=str(stats["total_monthly_pv"]),
            inline=True,
        )
        embed.add_field(
            name="月間収益",
            value=f"¥{stats['total_monthly_revenue']:,}",
            inline=True,
        )

        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    """Cogを登録。"""
    await bot.add_cog(Ventures(bot))
