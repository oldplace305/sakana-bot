"""Whisper音声文字起こしサービス。
whisper-cli (whisper.cpp) を使ってローカルで高精度な音声文字起こしを行う。
"""
import asyncio
import logging
import os
import tempfile

logger = logging.getLogger(__name__)

# Whisperモデルのデフォルトパス
DEFAULT_MODEL_PATH = os.path.expanduser("~/lex/models/ggml-large-v3-turbo.bin")
WHISPER_CLI = "/opt/homebrew/bin/whisper-cli"


class WhisperTranscriber:
    """whisper.cppによるローカル音声文字起こし。

    M1 Mac miniのGPU (Metal) を使って高速に処理する。
    """

    def __init__(self, model_path: str = None):
        self.model_path = model_path or DEFAULT_MODEL_PATH

    async def transcribe(self, audio_data: bytes, filename: str = "audio.m4a") -> dict:
        """音声データをテキストに変換する。

        Args:
            audio_data: 音声ファイルのバイナリデータ
            filename: 元のファイル名（拡張子から形式を判定）

        Returns:
            dict: {"success": bool, "text": str, "error": str or None}
        """
        # 一時ファイルに保存
        suffix = os.path.splitext(filename)[1] or ".m4a"

        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp_input:
            tmp_input.write(audio_data)
            input_path = tmp_input.name

        # WAVに変換が必要な場合（whisper-cliはWAV 16kHzが必要）
        wav_path = input_path + ".wav"

        try:
            # ffmpegでWAV 16kHz モノラルに変換
            convert_cmd = [
                "ffmpeg", "-y", "-i", input_path,
                "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
                wav_path,
            ]

            process = await asyncio.create_subprocess_exec(
                *convert_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=60)

            if process.returncode != 0:
                error_msg = stderr.decode("utf-8", errors="replace")[:200]
                logger.error(f"ffmpeg変換失敗: {error_msg}")
                return {
                    "success": False,
                    "text": "",
                    "error": f"音声変換失敗: {error_msg}",
                }

            # whisper-cliで文字起こし
            whisper_cmd = [
                WHISPER_CLI,
                "-m", self.model_path,
                "-l", "ja",       # 日本語
                "-nt",            # タイムスタンプなし
                "-f", wav_path,
            ]

            logger.info("Whisper文字起こし開始...")

            process = await asyncio.create_subprocess_exec(
                *whisper_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=300  # 最大5分
            )

            stdout_text = stdout.decode("utf-8", errors="replace").strip()
            stderr_text = stderr.decode("utf-8", errors="replace")

            if not stdout_text:
                logger.error(f"Whisper出力が空: stderr={stderr_text[:300]}")
                return {
                    "success": False,
                    "text": "",
                    "error": "文字起こし結果が空でした",
                }

            # whisper-cliの出力からテキスト部分を抽出
            # -nt フラグで出力されるのは純粋なテキスト
            transcribed = self._clean_output(stdout_text)

            logger.info(f"Whisper文字起こし完了: {len(transcribed)}文字")
            return {
                "success": True,
                "text": transcribed,
                "error": None,
            }

        except asyncio.TimeoutError:
            logger.error("Whisper文字起こしタイムアウト（300秒）")
            return {
                "success": False,
                "text": "",
                "error": "文字起こしタイムアウト",
            }
        except Exception as e:
            logger.error(f"Whisper文字起こしエラー: {e}", exc_info=True)
            return {
                "success": False,
                "text": "",
                "error": str(e),
            }
        finally:
            # 一時ファイルを削除
            for path in [input_path, wav_path]:
                try:
                    os.unlink(path)
                except OSError:
                    pass

    def _clean_output(self, raw_output: str) -> str:
        """whisper-cliの出力からテキストを抽出・整形する。"""
        lines = []
        for line in raw_output.split("\n"):
            line = line.strip()
            # 空行やログ行をスキップ
            if not line:
                continue
            if line.startswith("[") and "]" in line:
                # タイムスタンプ行 [00:00:00.000 --> 00:00:05.000] テキスト
                # → テキスト部分だけ取得
                bracket_end = line.index("]")
                text = line[bracket_end + 1:].strip()
                if text:
                    lines.append(text)
            elif line.startswith("whisper_") or line.startswith("ggml_") or line.startswith("load_"):
                # ライブラリのログ行をスキップ
                continue
            else:
                lines.append(line)

        return " ".join(lines).strip()
