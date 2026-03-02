"""トレンド収集モジュール。
英語圏のHackerNews, Reddit, RSS等から無料APIでトレンドを収集する。
全ソースは無料・認証不要。
"""
import asyncio
import json
import logging
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import aiohttp

from bot.utils.paths import DATA_DIR

logger = logging.getLogger(__name__)

# リサーチデータ保存先
RESEARCH_DIR = DATA_DIR / "research"

# 収集ソース
SOURCES = {
    "hackernews": {
        "url": "https://hacker-news.firebaseio.com/v0/topstories.json",
        "type": "api",
    },
    "reddit_technology": {
        "url": "https://www.reddit.com/r/technology/top.json?t=day&limit=15",
        "type": "reddit",
    },
    "reddit_programming": {
        "url": "https://www.reddit.com/r/programming/top.json?t=day&limit=15",
        "type": "reddit",
    },
    "techcrunch": {
        "url": "https://techcrunch.com/feed/",
        "type": "rss",
    },
    "the_verge": {
        "url": "https://www.theverge.com/rss/index.xml",
        "type": "rss",
    },
}

# ユーザーエージェント（Reddit API要件）
USER_AGENT = "LexVentures/1.0 (Discord Bot; trend research)"

# リクエストタイムアウト
REQUEST_TIMEOUT = 15


class TrendCollector:
    """英語圏トレンドを収集するクラス。"""

    def __init__(self):
        RESEARCH_DIR.mkdir(parents=True, exist_ok=True)

    async def collect_all(self) -> dict:
        """全ソースからトレンドを収集。

        Returns:
            dict: {
                "collected_at": ISO形式の日時,
                "sources": {ソース名: [記事リスト]},
                "total_items": 合計記事数,
            }
        """
        results = {}
        async with aiohttp.ClientSession(
            headers={"User-Agent": USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
        ) as session:
            # 各ソースを並行取得
            tasks = {
                name: self._fetch_source(session, name, config)
                for name, config in SOURCES.items()
            }
            gathered = await asyncio.gather(
                *tasks.values(), return_exceptions=True
            )
            for name, result in zip(tasks.keys(), gathered):
                if isinstance(result, Exception):
                    logger.warning(f"ソース取得エラー [{name}]: {result}")
                    results[name] = []
                else:
                    results[name] = result

        total = sum(len(items) for items in results.values())
        data = {
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "sources": results,
            "total_items": total,
        }

        # 日次ファイルに保存
        self._save_daily(data)
        logger.info(f"トレンド収集完了: {total}件（{len(results)}ソース）")
        return data

    async def _fetch_source(
        self, session: aiohttp.ClientSession, name: str, config: dict
    ) -> list:
        """個別ソースからデータを取得。"""
        source_type = config["type"]
        url = config["url"]

        try:
            if source_type == "api" and "hackernews" in name:
                return await self._fetch_hackernews(session, url)
            elif source_type == "reddit":
                return await self._fetch_reddit(session, url)
            elif source_type == "rss":
                return await self._fetch_rss(session, url)
            else:
                logger.warning(f"未知のソースタイプ: {source_type}")
                return []
        except Exception as e:
            logger.warning(f"ソース取得失敗 [{name}]: {e}")
            return []

    async def _fetch_hackernews(
        self, session: aiohttp.ClientSession, url: str
    ) -> list:
        """HackerNews Top Storiesを取得。上位15件の詳細を取得。"""
        async with session.get(url) as resp:
            if resp.status != 200:
                return []
            story_ids = await resp.json()

        # 上位15件の詳細を並行取得
        items = []
        top_ids = story_ids[:15]
        detail_tasks = [
            self._fetch_hn_item(session, sid) for sid in top_ids
        ]
        details = await asyncio.gather(*detail_tasks, return_exceptions=True)

        for detail in details:
            if isinstance(detail, Exception) or detail is None:
                continue
            items.append({
                "title": detail.get("title", ""),
                "url": detail.get("url", ""),
                "score": detail.get("score", 0),
                "comments": detail.get("descendants", 0),
                "source": "hackernews",
                "hn_id": detail.get("id"),
            })

        # スコア順でソート
        items.sort(key=lambda x: x["score"], reverse=True)
        return items

    async def _fetch_hn_item(
        self, session: aiohttp.ClientSession, item_id: int
    ) -> dict | None:
        """HackerNews個別アイテムを取得。"""
        url = f"https://hacker-news.firebaseio.com/v0/item/{item_id}.json"
        try:
            async with session.get(url) as resp:
                if resp.status == 200:
                    return await resp.json()
        except Exception:
            pass
        return None

    async def _fetch_reddit(
        self, session: aiohttp.ClientSession, url: str
    ) -> list:
        """Reddit JSON APIからトップ記事を取得。"""
        async with session.get(url) as resp:
            if resp.status != 200:
                logger.warning(f"Reddit API応答: {resp.status}")
                return []
            data = await resp.json()

        items = []
        posts = data.get("data", {}).get("children", [])
        for post in posts:
            post_data = post.get("data", {})
            items.append({
                "title": post_data.get("title", ""),
                "url": post_data.get("url", ""),
                "score": post_data.get("score", 0),
                "comments": post_data.get("num_comments", 0),
                "subreddit": post_data.get("subreddit", ""),
                "source": "reddit",
            })
        return items

    async def _fetch_rss(
        self, session: aiohttp.ClientSession, url: str
    ) -> list:
        """RSSフィードからエントリを取得（簡易XMLパース）。"""
        async with session.get(url) as resp:
            if resp.status != 200:
                return []
            text = await resp.text()

        # 簡易XMLパース（feedparserなしで動作）
        items = []
        entries = self._simple_rss_parse(text)
        for entry in entries[:15]:
            items.append({
                "title": entry.get("title", ""),
                "url": entry.get("link", ""),
                "source": "rss",
            })
        return items

    def _simple_rss_parse(self, xml_text: str) -> list:
        """RSSフィードを簡易パース。外部依存なし。

        <item>または<entry>タグから title と link を抽出。
        """
        import re

        entries = []

        # RSS 2.0: <item> ... </item>
        item_pattern = re.compile(
            r"<item[^>]*>(.*?)</item>", re.DOTALL | re.IGNORECASE
        )
        # Atom: <entry> ... </entry>
        entry_pattern = re.compile(
            r"<entry[^>]*>(.*?)</entry>", re.DOTALL | re.IGNORECASE
        )

        blocks = item_pattern.findall(xml_text) or entry_pattern.findall(xml_text)

        for block in blocks:
            title_match = re.search(
                r"<title[^>]*>(.*?)</title>", block, re.DOTALL
            )
            # RSS link
            link_match = re.search(
                r"<link[^>]*>(.*?)</link>", block, re.DOTALL
            )
            # Atom link (href属性)
            if not link_match or not link_match.group(1).strip():
                link_match = re.search(
                    r'<link[^>]*href=["\']([^"\']+)["\']', block
                )

            title = ""
            link = ""
            if title_match:
                title = title_match.group(1).strip()
                # CDATA除去
                title = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", title)
            if link_match:
                link = link_match.group(1).strip()

            if title:
                entries.append({"title": title, "link": link})

        return entries

    def _save_daily(self, data: dict):
        """日次リサーチ結果をJSONファイルに保存。"""
        today = datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d")
        filepath = RESEARCH_DIR / f"{today}.json"
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info(f"リサーチデータ保存: {filepath}")

    def get_latest_data(self) -> dict | None:
        """最新のリサーチデータを読み込む。"""
        today = datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d")
        filepath = RESEARCH_DIR / f"{today}.json"
        if filepath.exists():
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)

        # 今日のデータがなければ最新のファイルを探す
        files = sorted(RESEARCH_DIR.glob("*.json"), reverse=True)
        if files:
            with open(files[0], "r", encoding="utf-8") as f:
                return json.load(f)
        return None

    def format_for_analysis(self, data: dict) -> str:
        """収集データをClaude分析用のテキストに変換。"""
        if not data:
            return "リサーチデータがありません。"

        lines = [f"収集日時: {data.get('collected_at', '不明')}"]
        lines.append(f"合計記事数: {data.get('total_items', 0)}")
        lines.append("")

        for source_name, items in data.get("sources", {}).items():
            if not items:
                continue
            lines.append(f"=== {source_name} ({len(items)}件) ===")
            for item in items:
                title = item.get("title", "タイトルなし")
                url = item.get("url", "")
                score = item.get("score", "")
                score_str = f" [score: {score}]" if score else ""
                comments = item.get("comments", "")
                comments_str = f" [comments: {comments}]" if comments else ""
                lines.append(f"- {title}{score_str}{comments_str}")
                if url:
                    lines.append(f"  URL: {url}")
            lines.append("")

        return "\n".join(lines)
