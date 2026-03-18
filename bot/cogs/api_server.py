"""ローカルAPIサーバーCog — 音声入力ワークフロー Phase 2 + 音声自動処理。
aiohttp.web でHTTPサーバーを起動し、外部からのリクエストを受け付ける。

エンドポイント:
  GET  /health   → ヘルスチェック
  POST /memo     → Appleメモに追記 + Discord通知
  POST /notify   → Discordに通知送信
  POST /research → リサーチをキック → 結果をDiscordに送信
  POST /voice    → 音声テキストを自動処理（意図判定 + リライト + 保存）
"""
import json
import logging
from aiohttp import web
from discord.ext import commands
from bot.config import OWNER_ID, REPORT_CHANNEL_ID, API_HOST, API_PORT, API_TOKEN
from bot.services.apple_notes import AppleNotesService
from bot.services.claude_cli import ClaudeCLIBridge
from bot.services.voice_processor import VoiceProcessor
from bot.services.whisper_transcriber import WhisperTranscriber

logger = logging.getLogger(__name__)


class ApiServer(commands.Cog):
    """ローカルHTTPサーバーを提供するCog。"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.notes_service = AppleNotesService()
        # 音声自動処理用のClaude CLIブリッジとプロセッサ
        health = getattr(bot, 'health_monitor', None)
        self.claude_bridge = ClaudeCLIBridge(health_monitor=health)
        self.voice_processor = VoiceProcessor(
            claude_bridge=self.claude_bridge,
            notes_service=self.notes_service,
            notify_func=self._send_to_owner,
        )
        self.whisper = WhisperTranscriber()
        self.runner = None
        self._setup_app()

    def _setup_app(self):
        """aiohttpアプリケーションとルートを設定。"""
        self.app = web.Application(middlewares=[self._auth_middleware])
        self.app.router.add_get("/health", self.handle_health)
        self.app.router.add_post("/memo", self.handle_memo)
        self.app.router.add_post("/notify", self.handle_notify)
        self.app.router.add_post("/research", self.handle_research)
        self.app.router.add_post("/voice", self.handle_voice)

    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler):
        """Bearer トークン認証ミドルウェア。
        API_TOKENが空なら認証スキップ。/healthは常にスキップ。
        """
        if request.path == "/health" or not API_TOKEN:
            return await handler(request)

        auth_header = request.headers.get("Authorization", "")
        if auth_header == f"Bearer {API_TOKEN}":
            return await handler(request)

        return self._json_response(
            {"status": "error", "error": "認証エラー: 無効なトークン"}, 401
        )

    async def cog_load(self):
        """Cog読み込み時にHTTPサーバーを起動。"""
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, API_HOST, API_PORT)
        await site.start()
        auth_status = "Bearer認証あり" if API_TOKEN else "認証なし"
        logger.info(f"⚡ APIサーバー起動: http://{API_HOST}:{API_PORT} ({auth_status})")

    async def cog_unload(self):
        """Cog解除時にHTTPサーバーを停止。"""
        if self.runner:
            await self.runner.cleanup()
            logger.info("⚡ APIサーバー停止")

    # --- ヘルパー ---

    def _get_report_channel(self):
        """レポート送信先チャンネルを取得。"""
        if REPORT_CHANNEL_ID:
            channel = self.bot.get_channel(REPORT_CHANNEL_ID)
            if channel:
                return channel
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

    def _json_response(self, data: dict, status: int = 200) -> web.Response:
        """JSON レスポンスを返す。"""
        return web.Response(
            text=json.dumps(data, ensure_ascii=False),
            status=status,
            content_type="application/json",
        )

    # --- エンドポイント ---

    async def handle_health(self, request: web.Request) -> web.Response:
        """GET /health — ヘルスチェック。"""
        return self._json_response({
            "status": "ok",
            "bot_ready": self.bot.is_ready(),
            "latency_ms": round(self.bot.latency * 1000, 1),
        })

    async def handle_memo(self, request: web.Request) -> web.Response:
        """POST /memo — Appleメモに追記 + Discord通知。

        body: {
            "note_name": "X投稿案",
            "raw_text": "原文テキスト",
            "rewritten_text": "リライトテキスト"
        }
        """
        try:
            body = await request.json()
        except Exception:
            return self._json_response(
                {"status": "error", "error": "JSONパースエラー"}, 400
            )

        note_name = body.get("note_name", "")
        raw_text = body.get("raw_text", "")
        rewritten_text = body.get("rewritten_text", "")

        if not note_name or not raw_text:
            return self._json_response(
                {"status": "error", "error": "note_name と raw_text は必須です"}, 400
            )

        # Appleメモに追記
        result = await self.notes_service.append_to_note(
            note_name, raw_text, rewritten_text
        )

        if result["success"]:
            # 成功したらDiscordに自動通知
            await self._send_to_owner(
                f"📝 **{note_name}** に記録しました\n"
                f"> {raw_text[:100]}{'...' if len(raw_text) > 100 else ''}"
            )
            return self._json_response({"status": "ok", "note_name": note_name})
        else:
            return self._json_response(
                {"status": "error", "error": result["error"]}, 500
            )

    async def handle_notify(self, request: web.Request) -> web.Response:
        """POST /notify — Discordに通知を送信。

        body: { "message": "通知テキスト" }
        """
        try:
            body = await request.json()
        except Exception:
            return self._json_response(
                {"status": "error", "error": "JSONパースエラー"}, 400
            )

        message = body.get("message", "")
        if not message:
            return self._json_response(
                {"status": "error", "error": "message は必須です"}, 400
            )

        await self._send_to_owner(message)
        return self._json_response({"status": "ok"})

    async def handle_research(self, request: web.Request) -> web.Response:
        """POST /research — リサーチをキックしてDiscordに結果を送信。

        body: { "query": "検索クエリ" }
        """
        try:
            body = await request.json()
        except Exception:
            return self._json_response(
                {"status": "error", "error": "JSONパースエラー"}, 400
            )

        query = body.get("query", "")
        if not query:
            return self._json_response(
                {"status": "error", "error": "query は必須です"}, 400
            )

        # Research Cogにリサーチを依頼
        research_cog = self.bot.get_cog("Research")
        if not research_cog:
            # Research Cogがない場合はクエリをそのままDiscordに転送
            await self._send_to_owner(
                f"🔍 **リサーチリクエスト**\n> {query}\n\n"
                f"⚠️ Research Cogが未ロードのため、手動で確認してください。"
            )
            return self._json_response({
                "status": "ok",
                "message": "クエリをDiscordに転送しました（Research Cog未ロード）",
            })

        # 非同期でリサーチを実行（レスポンスは即返す）
        async def _run_research():
            try:
                await self._send_to_owner(f"🔍 **リサーチ開始**: {query}")
                result = await research_cog.run_research()
                if result:
                    await self._send_to_owner(
                        f"✅ **リサーチ完了**: {query}\n\n"
                        f"結果は定期報告で確認してください。"
                    )
            except Exception as e:
                logger.error(f"リサーチエラー: {e}")
                await self._send_to_owner(f"⚠️ リサーチエラー: {e}")

        # バックグラウンドタスクとして実行
        self.bot.loop.create_task(_run_research())

        return self._json_response({
            "status": "ok",
            "message": "リサーチを開始しました",
        })


    async def handle_voice(self, request: web.Request) -> web.Response:
        """POST /voice — 音声テキストまたは音声ファイルを自動処理。

        2つのモードに対応:
        1. JSON: { "text": "ポスト ..." } → テキストを直接処理
        2. multipart/form-data: audio=<音声ファイル> → Whisperで文字起こし → 処理

        処理はバックグラウンドで実行し、即座にレスポンスを返す。
        結果はDiscord通知で報告。
        """
        content_type = request.content_type or ""

        # --- モード1: JSON（テキスト直接送信） ---
        if "json" in content_type:
            try:
                body = await request.json()
            except Exception:
                return self._json_response(
                    {"status": "error", "error": "JSONパースエラー"}, 400
                )
            text = body.get("text", "").strip()
            if not text:
                return self._json_response(
                    {"status": "error", "error": "text は必須です"}, 400
                )
            logger.info(f"🎤 /voice テキスト受信: {text[:80]}...")
            self.bot.loop.create_task(self._process_voice_text(text))
            return self._json_response({
                "status": "ok",
                "message": "音声処理を開始しました。結果はDiscordで通知します。",
            })

        # --- モード2: multipart（音声ファイル送信） ---
        if "multipart" in content_type:
            try:
                reader = await request.multipart()
                audio_data = None
                filename = "audio.m4a"

                async for part in reader:
                    if part.name == "audio":
                        filename = part.filename or filename
                        audio_data = await part.read()
                        break

                if not audio_data:
                    return self._json_response(
                        {"status": "error", "error": "audio フィールドが必要です"}, 400
                    )

                size_mb = len(audio_data) / (1024 * 1024)
                logger.info(f"🎤 /voice 音声ファイル受信: {filename} ({size_mb:.1f}MB)")

                self.bot.loop.create_task(
                    self._process_voice_audio(audio_data, filename)
                )
                return self._json_response({
                    "status": "ok",
                    "message": "音声ファイルを受信しました。文字起こし→処理を開始します。",
                })

            except Exception as e:
                logger.error(f"multipart解析エラー: {e}", exc_info=True)
                return self._json_response(
                    {"status": "error", "error": f"ファイル受信エラー: {e}"}, 400
                )

        return self._json_response(
            {"status": "error", "error": "Content-Type は application/json または multipart/form-data にしてください"}, 400
        )

    async def _process_voice_text(self, text: str):
        """テキストモードの音声処理（バックグラウンド）。"""
        try:
            await self._send_to_owner(
                f"🎤 音声入力を受信しました\n> {text[:150]}"
                f"{'...' if len(text) > 150 else ''}\n"
                f"⏳ 処理中..."
            )
            await self.voice_processor.process(text)
        except Exception as e:
            logger.error(f"音声処理エラー: {e}", exc_info=True)
            await self._send_to_owner(f"⚠️ 音声処理エラー: {e}")

    async def _process_voice_audio(self, audio_data: bytes, filename: str):
        """音声ファイルモードの処理（バックグラウンド）。"""
        try:
            await self._send_to_owner("🎤 音声ファイルを受信しました\n⏳ 文字起こし中...")

            # Whisperで文字起こし
            transcribe_result = await self.whisper.transcribe(audio_data, filename)

            if not transcribe_result["success"]:
                await self._send_to_owner(
                    f"⚠️ 文字起こし失敗: {transcribe_result['error']}"
                )
                return

            text = transcribe_result["text"]
            logger.info(f"Whisper文字起こし結果: {text[:100]}...")

            await self._send_to_owner(
                f"📝 文字起こし完了\n> {text[:200]}"
                f"{'...' if len(text) > 200 else ''}\n"
                f"⏳ リライト処理中..."
            )

            # Claude CLIで処理
            await self.voice_processor.process(text)

        except Exception as e:
            logger.error(f"音声ファイル処理エラー: {e}", exc_info=True)
            await self._send_to_owner(f"⚠️ 音声処理エラー: {e}")


async def setup(bot: commands.Bot):
    """Cogを登録。"""
    await bot.add_cog(ApiServer(bot))
