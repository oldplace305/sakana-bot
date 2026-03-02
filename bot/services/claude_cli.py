"""Claude Code CLI連携モジュール。
subprocessでClaude Code CLIを非同期呼び出しし、JSON形式で結果を取得する。

Phase 1改善:
- プロファイル制タイムアウト（simple/normal/complex/repair）
- 自動複雑度判定
- max-turns到達時の改善されたメッセージ
- ヘルスモニター統合準備
"""
import asyncio
import json
import logging
import os
import re
from bot.utils.paths import NODE_BIN, CLAUDE_CLI, PROJECT_ROOT
from bot.config import CLAUDE_OAUTH_TOKEN

logger = logging.getLogger(__name__)

# --- タイムアウトプロファイル ---
TIMEOUT_PROFILES = {
    "simple":  {"timeout": 30,  "max_turns": 1},
    "normal":  {"timeout": 120, "max_turns": 5},
    "complex": {"timeout": 300, "max_turns": 15},
    "repair":  {"timeout": 600, "max_turns": 25},
    "venture": {"timeout": 900, "max_turns": 30},
}

# --- 複雑度自動判定パターン ---
SIMPLE_PATTERNS = [
    "こんにちは", "おはよう", "こんばんは", "ありがとう",
    "お疲れ", "よろしく", "おやすみ", "元気", "調子",
    "hello", "hi", "thanks", "good morning",
    "何してる", "今日は", "お願い",
]

COMPLEX_PATTERNS = [
    "コード", "スクリプト", "実装", "分析", "調査",
    "プログラム", "デバッグ", "設計", "アーキテクチャ",
    "Playwright", "ブラウザ", "スクレイピング",
    "戦略", "計画", "まとめ", "レポート作成",
    "x_trend", "投稿案", "note商品", "収益モデル",
]

REPAIR_PATTERNS = [
    "エラーを解消", "エラーを直", "エラー修正",
    "バグ直", "バグ修正", "自分を修正", "自己修復",
    "fix yourself", "heal yourself", "self-repair",
    "自分のコードを", "診断して", "diagnose",
]


class ClaudeCLIBridge:
    """Claude Code CLIとの通信を管理するクラス。

    - asyncio.create_subprocess_execで非同期実行
    - --output-format json でJSON出力を取得
    - -p フラグで非インタラクティブモード
    - asyncio.Lockで同時実行を防止
    - プロファイル制タイムアウト
    """

    def __init__(self, health_monitor=None):
        self._lock = asyncio.Lock()
        self._health = health_monitor  # Phase 2で統合

    async def ask(self, prompt: str, system_prompt: str = None,
                 allowed_tools: list = None, max_turns: int = None,
                 profile: str = None) -> dict:
        """Claude Code CLIにプロンプトを送信し、結果を返す。

        Args:
            prompt: ユーザーからの質問・指示
            system_prompt: システムプロンプト（オーナープロフィール等）
            allowed_tools: 許可するツールのリスト（リスクレベルに応じた制御）
            max_turns: 最大ターン数（明示指定時はプロファイルより優先）
            profile: タイムアウトプロファイル（None=自動判定）

        Returns:
            dict: {"success": bool, "text": str, "error": str or None,
                   "cost_usd": float, "profile_used": str}
        """
        async with self._lock:
            return await self._execute(
                prompt, system_prompt, allowed_tools, max_turns, profile
            )

    @staticmethod
    def classify_complexity(prompt: str) -> str:
        """プロンプトの複雑度を自動判定する。

        Returns:
            "simple" / "normal" / "complex" / "repair"
        """
        prompt_lower = prompt.lower()

        # 修復パターンを最優先でチェック
        for pattern in REPAIR_PATTERNS:
            if pattern.lower() in prompt_lower:
                return "repair"

        # 複雑パターン
        for pattern in COMPLEX_PATTERNS:
            if pattern.lower() in prompt_lower:
                return "complex"

        # 短いメッセージ（50文字以下）+ 挨拶パターン → simple
        if len(prompt) <= 50:
            for pattern in SIMPLE_PATTERNS:
                if pattern.lower() in prompt_lower:
                    return "simple"

        # 長いメッセージ（300文字以上）は complex 寄り
        if len(prompt) >= 300:
            return "complex"

        return "normal"

    async def _execute(self, prompt: str, system_prompt: str = None,
                       allowed_tools: list = None, max_turns: int = None,
                       profile: str = None) -> dict:
        """実際のCLI実行処理。"""
        # プロファイル決定
        if profile is None:
            profile = self.classify_complexity(prompt)

        profile_config = TIMEOUT_PROFILES.get(profile, TIMEOUT_PROFILES["normal"])
        timeout = profile_config["timeout"]

        # max_turnsは明示指定 > プロファイルデフォルト
        if max_turns is None:
            max_turns = profile_config["max_turns"]
        turns_str = str(max_turns)

        # コマンド構築
        cmd = [
            NODE_BIN, CLAUDE_CLI,
            "-p", prompt,                    # 非インタラクティブ
            "--output-format", "json",       # JSON出力
            "--max-turns", turns_str,        # 自律実行の最大ターン数
        ]

        # 許可ツール制御（スマート承認システム連携）
        if allowed_tools:
            for tool in allowed_tools:
                cmd.extend(["--allowedTools", tool])

        # システムプロンプト追加
        if system_prompt:
            cmd.extend(["--system-prompt", system_prompt])

        logger.info(
            f"Claude CLI呼び出し開始"
            f"（プロファイル: {profile}, タイムアウト: {timeout}秒, "
            f"max-turns: {turns_str}）"
        )
        logger.debug(f"プロンプト: {prompt[:100]}...")

        try:
            # 環境変数を明示的に設定
            env = os.environ.copy()
            env.pop("CLAUDECODE", None)
            env["HOME"] = "/Users/shuta"
            env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

            # Claude CLI認証トークンを設定（launchd環境で必要）
            if CLAUDE_OAUTH_TOKEN:
                env["CLAUDE_CODE_OAUTH_TOKEN"] = CLAUDE_OAUTH_TOKEN

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=str(PROJECT_ROOT),
            )

            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout,
            )

            stdout_text = stdout.decode("utf-8", errors="replace")
            stderr_text = stderr.decode("utf-8", errors="replace")

            if process.returncode != 0:
                logger.warning(
                    f"Claude CLI 非ゼロ終了 (code={process.returncode}): "
                    f"stderr={stderr_text[:200]}"
                )
                logger.warning(f"stdout内容: {stdout_text[:300]}")

                # "Not logged in" チェック
                if ("not logged in" in stdout_text.lower()
                        or "please run /login" in stdout_text.lower()):
                    logger.error("Claude CLIが未ログイン状態です")
                    result = {
                        "success": False,
                        "text": "",
                        "error": ("Claude CLIが未ログイン状態です。"
                                  "Mac miniのターミナルで `claude login` を"
                                  "実行してください。"),
                        "profile_used": profile,
                    }
                    self._record_failure("auth_error", result["error"])
                    return result

                # stdoutに応答がある場合は成功として扱う
                if stdout_text.strip():
                    logger.info("stdoutに応答あり。成功として処理します")
                else:
                    logger.error("stdoutが空。エラーとして処理します")
                    result = {
                        "success": False,
                        "text": "",
                        "error": (f"CLI error: "
                                  f"{stderr_text[:500] if stderr_text.strip() else '応答がありませんでした'}"),
                        "profile_used": profile,
                    }
                    self._record_failure("cli_error", result["error"])
                    return result

            # JSON出力をパース
            response_text = self._extract_text(stdout_text)
            cost_usd = self._extract_cost(stdout_text)
            is_max_turns = self._is_max_turns(stdout_text)

            logger.info(
                f"Claude CLI応答取得成功（{len(response_text)}文字, "
                f"コスト: ${cost_usd:.4f}, プロファイル: {profile}）"
            )

            # ヘルスモニター記録
            self._record_success(cost_usd)

            return {
                "success": True,
                "text": response_text,
                "error": None,
                "cost_usd": cost_usd,
                "profile_used": profile,
                "max_turns_reached": is_max_turns,
            }

        except asyncio.TimeoutError:
            logger.error(f"Claude CLI タイムアウト（{timeout}秒, プロファイル: {profile}）")
            try:
                process.kill()
                await process.wait()
            except Exception:
                pass

            error_msg = (
                f"タイムアウト（{timeout}秒を超過しました）\n"
                f"💡 ヒント: 質問をより具体的にするか、"
                f"タスクを小さく分割してみてください。"
            )
            self._record_failure("timeout", error_msg)

            return {
                "success": False,
                "text": "",
                "error": error_msg,
                "profile_used": profile,
            }

        except Exception as e:
            logger.error(f"Claude CLI 予期しないエラー: {e}", exc_info=True)
            error_msg = f"予期しないエラー: {str(e)}"
            self._record_failure("unexpected", error_msg)

            return {
                "success": False,
                "text": "",
                "error": error_msg,
                "profile_used": profile,
            }

    def _record_success(self, cost_usd: float):
        """ヘルスモニターに成功を記録（Phase 2で有効化）。"""
        if self._health:
            try:
                self._health.record_cli_success(cost_usd)
            except Exception as e:
                logger.debug(f"ヘルス記録エラー（無視）: {e}")

    def _record_failure(self, error_type: str, error_msg: str, cost_usd: float = 0):
        """ヘルスモニターに失敗を記録（Phase 2で有効化）。"""
        if self._health:
            try:
                self._health.record_cli_failure(error_type, error_msg, cost_usd)
            except Exception as e:
                logger.debug(f"ヘルス記録エラー（無視）: {e}")

    def _is_max_turns(self, raw_output: str) -> bool:
        """出力がmax-turns到達かどうかを判定。"""
        try:
            data = json.loads(raw_output)
            if isinstance(data, dict):
                return data.get("subtype") == "error_max_turns"
        except (json.JSONDecodeError, TypeError):
            pass
        return False

    def _extract_text(self, raw_output: str) -> str:
        """Claude CLI のJSON出力からテキストを抽出する。

        --output-format json の出力形式:
        {
            "type": "result",
            "subtype": "success" | "error_max_turns" | ...,
            "result": "テキスト応答" | null,
            "total_cost_usd": 0.03,
            ...
        }
        """
        try:
            data = json.loads(raw_output)
        except json.JSONDecodeError:
            logger.warning("JSON解析失敗。生テキストを返します")
            return raw_output.strip()

        if not isinstance(data, dict):
            if isinstance(data, list):
                return self._extract_from_blocks(data)
            logger.warning("未知のJSON構造。文字列化して返します")
            return str(data)

        # --- メインパターン: Claude CLI --output-format json ---
        subtype = data.get("subtype", "")
        result = data.get("result")

        # error_max_turns: ターン上限に達した場合
        if subtype == "error_max_turns":
            logger.warning("Claude CLIがmax-turnsに到達しました")
            if result and isinstance(result, str) and result.strip():
                # 途中結果があれば表示 + 注釈
                return (
                    f"{result}\n\n"
                    f"---\n"
                    f"⚡ ターン上限に達しましたが、途中までの応答を表示しています。\n"
                    f"続きが必要な場合は、もう少し具体的に指示してください。"
                )
            # result が null の場合
            return (
                "⚠️ 処理のステップ数が上限に達しました。\n\n"
                "以下を試してみてください：\n"
                "1. 質問をより具体的にする\n"
                "2. 大きなタスクを小さく分割する\n"
                "3. 「〜について教えて」のように情報取得に絞る"
            )

        # result フィールドがある場合
        if result is not None:
            if isinstance(result, str) and result.strip():
                return result
            if isinstance(result, list):
                return self._extract_from_blocks(result)

        # result が None/空 でも subtype が success の場合
        if subtype == "success" and (result is None or result == ""):
            logger.warning("success だが result が空")
            return "（応答が空でした。もう一度お試しください。）"

        # パターン2: {"content": "テキスト"} 形式（古い形式の互換）
        if "content" in data:
            content = data["content"]
            if isinstance(content, str) and content.strip():
                return content

        # is_error フラグチェック
        if data.get("is_error"):
            error_msg = result or data.get("error", "不明なエラー")
            return f"⚠️ Claude CLIエラー: {error_msg}"

        # フォールバック
        logger.warning(
            f"未知のレスポンス構造: subtype={subtype}, "
            f"result_type={type(result).__name__}"
        )
        return "⚠️ 予期しない応答形式です。もう一度お試しください。"

    def _extract_cost(self, raw_output: str) -> float:
        """Claude CLI のJSON出力からコスト情報を抽出。"""
        try:
            data = json.loads(raw_output)
            if isinstance(data, dict):
                cost = data.get("total_cost_usd", 0)
                if cost:
                    return float(cost)
                usage = data.get("usage", {})
                if isinstance(usage, dict):
                    return 0
                model_usage = data.get("modelUsage", {})
                if isinstance(model_usage, dict):
                    for model_info in model_usage.values():
                        if isinstance(model_info, dict):
                            cost = model_info.get("costUSD", 0)
                            if cost:
                                return float(cost)
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
        return 0

    def _extract_from_blocks(self, blocks: list) -> str:
        """コンテントブロックのリストからテキストを抽出。"""
        texts = []
        for block in blocks:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    texts.append(block.get("text", ""))
                elif "content" in block:
                    texts.append(str(block["content"]))
                elif "text" in block:
                    texts.append(str(block["text"]))
            elif isinstance(block, str):
                texts.append(block)
        return "\n".join(texts) if texts else str(blocks)
