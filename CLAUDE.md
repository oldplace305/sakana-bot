# lex

## 概要
Discord / Twitter (X) ボットプロジェクト。

## 技術スタック
- Python 3
- discord.py - Discord ボット
- tweepy - Twitter/X API連携
- aiohttp - 非同期HTTP

## 構成
- `bot/` - ボット本体
- `data/` - データファイル
- `scripts/` - 実行スクリプト
- `launchd/` - macOS 自動起動設定

## 実行方法
```bash
pip install -r requirements.txt
python bot/main.py  # または scripts/ 内のスクリプト
```
