"""全ツール・ディレクトリの絶対パス定義。
PATHに依存せず、どこから起動されても正しいパスを参照できるようにする。
"""
from pathlib import Path

# プロジェクトルート
PROJECT_ROOT = Path("/Users/shuta/sakana-bot")

# 外部ツール（絶対パス指定でPATH問題を回避）
NODE_BIN = "/opt/homebrew/bin/node"
CLAUDE_CLI = "/opt/homebrew/bin/claude"
PYTHON_BIN = str(PROJECT_ROOT / "venv" / "bin" / "python")

# プロジェクトディレクトリ
DATA_DIR = PROJECT_ROOT / "data"
LOGS_DIR = PROJECT_ROOT / "logs"
SCRIPTS_DIR = PROJECT_ROOT / "scripts"

# データファイル
OWNER_PROFILE_FILE = DATA_DIR / "owner_profile.json"
CONVERSATION_LOG_FILE = DATA_DIR / "conversation_log.jsonl"
ERROR_LOG_FILE = DATA_DIR / "error_log.jsonl"
HEALTH_STATE_FILE = DATA_DIR / "health_state.json"
VENTURES_FILE = DATA_DIR / "ventures.json"
RESEARCH_DIR = DATA_DIR / "research"
