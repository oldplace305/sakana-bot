"""Lex Bot - メインBotクラス。
discord.py の commands.Bot を拡張し、Cog自動読み込みとステータス設定を行う。
Lex = Nosuke Labの一員。しゅうたの右腕として事業を推進する。

Phase 2: HealthMonitor 統合
Phase 3: 自己修復 Cog 追加
Phase 4: 再起動後の修復検証
"""
import discord
from discord.ext import commands
import logging
from bot.config import OWNER_ID, BOT_PREFIX
from bot.services.health_monitor import HealthMonitor

logger = logging.getLogger(__name__)


class LexBot(commands.Bot):
    """Lex Botのメインクラス。"""

    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True  # メッセージ内容の読み取り
        intents.members = True          # メンバー情報の取得

        super().__init__(
            command_prefix=BOT_PREFIX,
            intents=intents,
            owner_id=OWNER_ID,
        )

        # ヘルスモニター（全Cogで共有）
        self.health_monitor = HealthMonitor()

    async def setup_hook(self):
        """Bot起動時にCogを読み込み、スラッシュコマンドを同期。"""
        cog_list = [
            "bot.cogs.general",
            "bot.cogs.claude_bridge",
            "bot.cogs.owner",
            "bot.cogs.script_ops",
            "bot.cogs.research",
            "bot.cogs.ventures",
            "bot.cogs.x_poster",
            "bot.cogs.daily_report",
            "bot.cogs.business",
            "bot.cogs.backup",
            "bot.cogs.health",
            "bot.cogs.self_repair",
        ]

        for cog in cog_list:
            try:
                await self.load_extension(cog)
                logger.info(f"Cog読み込み成功: {cog}")
            except Exception as e:
                logger.error(f"Cog読み込み失敗 {cog}: {e}", exc_info=True)

        # スラッシュコマンドをDiscordに同期
        try:
            synced = await self.tree.sync()
            logger.info(f"スラッシュコマンド同期完了: {len(synced)}個")
        except Exception as e:
            logger.error(f"スラッシュコマンド同期失敗: {e}", exc_info=True)

    async def on_ready(self):
        """Bot起動完了時のイベント。"""
        logger.info(f"⚡ Lex オンライン: {self.user} (ID: {self.user.id})")
        logger.info(f"接続サーバー数: {len(self.guilds)}")

        # ステータスメッセージを設定
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="Nosuke Lab を推進中 ⚡",
            )
        )

        # Phase 4: 再起動後の修復検証
        await self._check_post_repair()

    async def _check_post_repair(self):
        """再起動後に修復状態を検証する。"""
        repair_state = self.health_monitor.get_repair_state()
        if not repair_state:
            return

        logger.info(
            f"修復状態を検出: {repair_state.get('description', '?')}"
        )

        try:
            owner = await self.fetch_user(OWNER_ID)
            if not owner:
                return

            # 起動できている = 少なくとも致命的エラーはない
            report = self.health_monitor.get_health_report()

            if report["status_healthy"]:
                await owner.send(
                    f"✅ **自己修復成功**\n\n"
                    f"修復内容: {repair_state.get('description', '?')}\n"
                    f"ブランチ: {repair_state.get('branch', '?')}\n\n"
                    f"正常に動作しています。"
                )
                self._merge_repair_branch(repair_state.get("branch", ""))
                logger.info("修復検証成功: ブランチをマージしました")
            else:
                await owner.send(
                    f"⚠️ **修復後の検証**\n\n"
                    f"修復内容: {repair_state.get('description', '?')}\n"
                    f"起動は成功しましたが、ヘルスチェックに問題があります。\n"
                    f"注意点: {report.get('attention_reason', 'なし')}\n\n"
                    f"`/health` で詳細を確認してください。"
                )
        except Exception as e:
            logger.error(f"修復後検証エラー: {e}")
        finally:
            self.health_monitor.clear_repair_state()

    def _merge_repair_branch(self, branch_name: str):
        """修復ブランチをmainにマージ。"""
        if not branch_name:
            return
        try:
            import subprocess
            from bot.utils.paths import PROJECT_ROOT
            subprocess.run(
                ["git", "checkout", "main"],
                cwd=str(PROJECT_ROOT), capture_output=True, timeout=10,
            )
            subprocess.run(
                ["git", "merge", branch_name],
                cwd=str(PROJECT_ROOT), capture_output=True, timeout=10,
            )
            subprocess.run(
                ["git", "branch", "-d", branch_name],
                cwd=str(PROJECT_ROOT), capture_output=True, timeout=10,
            )
            logger.info(f"修復ブランチマージ完了: {branch_name}")
        except Exception as e:
            logger.warning(f"修復ブランチマージエラー: {e}")

    async def on_command_error(self, ctx, error):
        """コマンドエラーのハンドリング。"""
        if isinstance(error, commands.CommandNotFound):
            return  # 不明なコマンドは無視
        logger.error(f"コマンドエラー: {error}", exc_info=True)
