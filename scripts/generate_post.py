"""
毎日の健康ブログ記事を自動生成し、_posts フォルダに保存するスクリプト。
GitHub Actions から実行される想定。
環境変数 ANTHROPIC_API_KEY, PEXELS_API_KEY が必要。
"""

import os
import re
import json
import datetime
import urllib.request
import urllib.parse

API_KEY = os.environ["ANTHROPIC_API_KEY"]
API_URL = "https://api.anthropic.com/v1/messages"
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "")
PEXELS_SEARCH_URL = "https://api.pexels.com/v1/search"

# テーマ(日本語)と、画像検索用の英語キーワードのペア
TOPICS = [
    ("睡眠の質を上げる工夫", "peaceful sleep bedroom"),
    ("毎日の食事でできる健康習慣", "healthy meal vegetables"),
    ("運動不足を解消する簡単な方法", "stretching exercise home"),
    ("メンタルヘルスを整えるヒント", "calm relaxation mindfulness"),
    ("疲労回復のための生活習慣", "rest relaxing tea"),
    ("姿勢と体の使い方", "good posture stretching"),
    ("水分補給と体調管理", "drinking water glass"),
    ("季節の変わり目の体調管理", "seasonal change health"),
]

# 日付に応じてテーマを一つ選ぶ(単純に日数で割り当てローテーション)
today = datetime.date.today()
topic, image_query = TOPICS[today.toordinal() % len(TOPICS)]

prompt = f"""あなたは健康分野の専門ライターです。
以下の条件で日本語のブログ記事を1本書いてください。

テーマ:{topic}
文字数:800〜1200字程度
構成:タイトル(# 見出し)、導入、見出し付きの本文(##)、まとめ
文体:丁寧で分かりやすく、具体的で今日から実践できる内容にすること
出力形式:Markdownの本文のみ。前置きや説明文は一切つけないこと。
"""

body = {
    "model": "claude-sonnet-4-6",
    "max_tokens": 2000,
    "messages": [{"role": "user", "content": prompt}],
}

req = urllib.request.Request(
    API_URL,
    data=json.dumps(body).encode("utf-8"),
    headers={
        "Content-Type": "application/json",
        "x-api-key": API_KEY,
        "anthropic-version": "2023-06-01",
    },
    method="POST",
)

with urllib.request.urlopen(req) as res:
    data = json.loads(res.read().decode("utf-8"))

article_md = "".join(
    block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
).strip()


def fetch_featured_image(query: str) -> str:
    """Pexelsからテーマに合った画像のURLを1枚取得する。取得できなければ空文字を返す。"""
    if not PEXELS_API_KEY:
        return ""
    try:
        url = f"{PEXELS_SEARCH_URL}?query={urllib.parse.quote(query)}&per_page=1&orientation=landscape"
        req = urllib.request.Request(url, headers={"Authorization": PEXELS_API_KEY})
        with urllib.request.urlopen(req, timeout=10) as res:
            result = json.loads(res.read().decode("utf-8"))
        photos = result.get("photos", [])
        if photos:
            return photos[0]["src"]["large"]
    except Exception as e:
        print(f"画像取得に失敗しました: {e}")
    return ""


image_url = fetch_featured_image(image_query)

# 記事本文からタイトル行(# ...)を抜き出す
title_match = re.search(r"^#\s+(.+)$", article_md, re.MULTILINE)
title = title_match.group(1).strip() if title_match else topic

# ファイル名用にタイトルをスラッグ化(日本語はそのまま使い、記号だけ除去)
slug = re.sub(r"[^\w぀-ヿ一-鿿]+", "-", title).strip("-")
date_str = today.strftime("%Y-%m-%d")
filename = f"_posts/{date_str}-{slug or 'health-post'}.md"

image_line = f"image: \"{image_url}\"\n" if image_url else ""

front_matter = f"""---
layout: post
title: "{title}"
date: {date_str} 07:00:00 +0900
categories: [health]
{image_line}---

"""

with open(filename, "w", encoding="utf-8") as f:
    f.write(front_matter + article_md + "\n")

print(f"Created: {filename}")
