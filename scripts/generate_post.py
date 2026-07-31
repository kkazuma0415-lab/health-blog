"""
毎日の健康ブログ記事を自動生成し、_posts フォルダに保存するスクリプト。
GitHub Actions から実行される想定。
環境変数 ANTHROPIC_API_KEY が必要。
"""

import os
import re
import json
import datetime
import urllib.request

API_KEY = os.environ["ANTHROPIC_API_KEY"]
API_URL = "https://api.anthropic.com/v1/messages"

TOPICS = [
    "睡眠の質を上げる工夫",
    "毎日の食事でできる健康習慣",
    "運動不足を解消する簡単な方法",
    "メンタルヘルスを整えるヒント",
    "疲労回復のための生活習慣",
    "姿勢と体の使い方",
    "水分補給と体調管理",
    "季節の変わり目の体調管理",
]

# 日付に応じてテーマを一つ選ぶ(単純に日数で割り当てローテーション)
today = datetime.date.today()
topic = TOPICS[today.toordinal() % len(TOPICS)]

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

# 記事本文からタイトル行(# ...)を抜き出す
title_match = re.search(r"^#\s+(.+)$", article_md, re.MULTILINE)
title = title_match.group(1).strip() if title_match else topic

# ファイル名用にタイトルをスラッグ化(日本語はそのまま使い、記号だけ除去)
slug = re.sub(r"[^\w\u3040-\u30ff\u4e00-\u9fff]+", "-", title).strip("-")
date_str = today.strftime("%Y-%m-%d")
filename = f"_posts/{date_str}-{slug or 'health-post'}.md"

front_matter = f"""---
layout: post
title: "{title}"
date: {date_str} 07:00:00 +0900
categories: [health]
---

"""

with open(filename, "w", encoding="utf-8") as f:
    f.write(front_matter + article_md + "\n")

print(f"Created: {filename}")
