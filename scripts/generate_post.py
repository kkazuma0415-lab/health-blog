"""
毎日の健康ブログ記事を自動生成し、_posts フォルダに保存するスクリプト。
GitHub Actions から実行される想定。

曜日によって2つのモードを自動切り替えする:
- 商品紹介モード (月・水・金・土): products.json から商品を1つ選び、
  購入につながるレビュー記事をClaude APIで生成する。
  「使ってみた」記事(tried: true)と「気になる商品紹介」記事(tried: false)を
  投稿ごとに必ず交互に出す(次項参照)。
- 健康情報モード (火・木・日): これまで通りテーマに沿った健康情報記事を生成し、
  Pexelsからアイキャッチ画像を取得する。

環境変数 ANTHROPIC_API_KEY, PEXELS_API_KEY(健康情報モードのみ必須) が必要。
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

# 曜日番号: Monday=0 ... Sunday=6
PRODUCT_DAYS = {0, 2, 4, 5}  # 月・水・金・土
HEALTH_DAYS = {1, 3, 6}      # 火・木・日

# GitHub Actionsのサーバー時刻はUTCだが、曜日判定・日付はJST基準で行う
# (UTC基準のままだと、日本時間の曜日と1日ズレて商品/健康の判定が狂うため)
JST = datetime.timezone(datetime.timedelta(hours=9))
today = datetime.datetime.now(JST).date()
weekday = today.weekday()
date_str = today.strftime("%Y-%m-%d")

# 健康情報モード用テーマ(日本語)と、画像検索用の英語キーワードのペア
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

PRODUCTS_FILE = os.path.join(os.path.dirname(__file__), "..", "products.json")
ROTATION_FILE = os.path.join(os.path.dirname(__file__), "..", ".product_rotation")

# サイトのbaseurl(_config.ymlの設定に合わせる)。
# products.json内の画像パスはサイトルート相対(/assets/...)で書かれているため、
# 実際のURLにするにはこのbaseurlを前に付ける必要がある。
BASEURL = "/health-blog"


def site_path(path: str) -> str:
    """サイトルート相対パスにbaseurlを付与する。外部URL(http/https)はそのまま返す。"""
    if not path or path.startswith("http"):
        return path
    return BASEURL + path


def call_claude(prompt: str, max_tokens: int = 2000) -> str:
    body = {
        "model": "claude-sonnet-4-6",
        "max_tokens": max_tokens,
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
    return "".join(
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


def _load_rotation_state() -> dict:
    """商品ローテーションの状態(各プールでの位置、次に使うスタイル)を読み込む。"""
    default_state = {"tried_idx": 0, "untried_idx": 0, "next_style": "tried"}
    if not os.path.exists(ROTATION_FILE):
        return default_state
    try:
        with open(ROTATION_FILE, encoding="utf-8") as f:
            content = f.read().strip()
        if not content:
            return default_state
        if content.lstrip("-").isdigit():
            # 旧形式(単なる整数のインデックス)からの移行。位置だけ引き継ぐ。
            default_state["tried_idx"] = int(content)
            return default_state
        state = json.loads(content)
        for key, value in default_state.items():
            state.setdefault(key, value)
        return state
    except (ValueError, OSError, json.JSONDecodeError):
        return default_state


def _save_rotation_state(state: dict):
    with open(ROTATION_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


def _product_already_reviewed(product: dict) -> bool:
    """この商品(affiliate_urlで判定)について、記事が既に_postsに存在するか確認する。

    タイトルはAIが毎回変えて生成するため、商品名やタイトルの一致では
    重複を検出できない。affiliate_url(product_link)は記事のfront matterに
    必ず埋め込まれるため、これが一致する記事の有無で「この商品は既にレビュー
    済みかどうか」を判定する。これにより、一度レビューした商品を自動生成が
    新しい創作エピソードで際限なく再レビューしてしまうのを防ぐ。
    """
    target_url = product.get("affiliate_url", "")
    if not target_url:
        return False
    posts_dir = os.path.join(os.path.dirname(__file__), "..", "_posts")
    if not os.path.isdir(posts_dir):
        return False
    for name in os.listdir(posts_dir):
        if not name.endswith(".md"):
            continue
        try:
            with open(os.path.join(posts_dir, name), encoding="utf-8") as f:
                content = f.read()
        except OSError:
            continue
        if target_url in content:
            return True
    return False


def next_product() -> tuple:
    """products.jsonから、まだ記事になっていない商品を1つ選ぶ。

    既に_postsに記事がある商品(affiliate_urlの一致で判定)は選択対象から除外する
    (同じ商品を使い回しの創作エピソードで何度も再レビューしないため)。

    残った未レビューの商品の中から、「使ってみた」記事(tried: true)と
    「気になる商品紹介」記事(tried: false)を、商品紹介の投稿ごとに
    必ず交互に出すようローテーションする。どちらか一方のプールが空
    (該当する未レビュー商品がまだ無い)場合は、商品が揃うまで存在する
    方のスタイルを使い続ける。

    未レビューの商品が1つも無い場合は (None, None) を返す
    (=新しい商品が追加されるまで、商品紹介モードの日は記事を生成しない)。

    戻り値: (商品dict, スタイル文字列 "tried" または "untried") または (None, None)
    """
    with open(PRODUCTS_FILE, encoding="utf-8") as f:
        products = json.load(f)
    if not products:
        raise RuntimeError("products.json に商品が登録されていません。")

    eligible = [p for p in products if not _product_already_reviewed(p)]
    if not eligible:
        return None, None

    # tried未設定の商品は、後方互換として「使ってみた」扱いにする
    tried_pool = [p for p in eligible if p.get("tried", True)]
    untried_pool = [p for p in eligible if not p.get("tried", True)]

    state = _load_rotation_state()
    desired_style = state.get("next_style", "tried")

    if desired_style == "untried" and not untried_pool:
        style = "tried"
    elif desired_style == "tried" and not tried_pool:
        style = "untried"
    else:
        style = desired_style

    pool = tried_pool if style == "tried" else untried_pool
    if not pool:
        return None, None

    idx_key = "tried_idx" if style == "tried" else "untried_idx"
    idx = state.get(idx_key, 0) % len(pool)
    product = pool[idx]

    state[idx_key] = (idx + 1) % len(pool)
    # 両方のプールに商品がある時だけ、次回は逆のスタイルにする(厳密な交互ローテーション)。
    # 片方が空のうちは、商品が揃うまで同じ希望スタイルを維持する。
    if tried_pool and untried_pool:
        state["next_style"] = "untried" if style == "tried" else "tried"
    else:
        state["next_style"] = desired_style
    _save_rotation_state(state)

    return product, style


def youtube_embed_id(url: str) -> str:
    """youtu.be/xxx や youtube.com/watch?v=xxx から動画IDを取り出す。"""
    match = re.search(r"(?:youtu\.be/|youtube\.com/watch\?v=)([\w-]+)", url)
    return match.group(1) if match else ""


def build_slug(title: str) -> str:
    slug = re.sub(r"[^\w぀-ヿ一-鿿]+", "-", title).strip("-")
    return slug or "post"


def extract_description(article_md: str, max_len: int = 120) -> str:
    """記事本文からmeta description用の要約テキストを作る。

    front matterのdescriptionには、商品記事の場合ここでは「※本記事は
    アフィリエイトリンクを含みます...」という免責の定型文ではなく、
    実際の記事内容の書き出しを使いたい。そのため見出し・引用(免責文)・
    強調やリンクなどのMarkdown記法を取り除いた上で、先頭からmax_len
    文字程度に丸める。
    """
    text = article_md
    text = re.sub(r"^#+\s+.*$", "", text, flags=re.MULTILINE)      # 見出し行を除去
    text = re.sub(r"^>\s?.*$", "", text, flags=re.MULTILINE)        # 引用行(免責文など)を除去
    text = re.sub(r"^-{3,}$", "", text, flags=re.MULTILINE)         # 区切り線を除去
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)                    # 強調記法を除去
    text = re.sub(r"\[(.+?)\]\(.+?\)", r"\1", text)                 # リンク記法を除去
    text = re.sub(r"^[-*]\s+", "", text, flags=re.MULTILINE)        # 箇条書き記号を除去
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_len:
        text = text[:max_len].rstrip() + "…"
    # front matterはダブルクォートで囲むため、本文中の"は"に置き換えて壊れないようにする
    return text.replace('"', "'")


def build_image_gallery(images: list) -> str:
    """複数画像を横スクロールで見られるギャラリーのHTMLを組み立てる。"""
    if not images:
        return ""
    items = "".join(
        f'<img src="{site_path(src)}" alt="商品画像" '
        f'style="height:220px; width:auto; border-radius:8px; flex-shrink:0; scroll-snap-align:start;">\n'
        for src in images
    )
    return (
        '<div style="display:flex; gap:0.75em; overflow-x:auto; padding:0.5em 0; '
        'margin-bottom:1.5em; scroll-snap-type:x mandatory;">\n'
        f"{items}"
        "</div>\n\n"
    )


def write_post(filename: str, front_matter: dict, body: str):
    lines = ["---"]
    for key, value in front_matter.items():
        if value is None:
            continue
        lines.append(f'{key}: "{value}"' if isinstance(value, str) else f"{key}: {value}")
    lines.append("---")
    lines.append("")
    content = "\n".join(lines) + body + "\n"
    os.makedirs(os.path.dirname(filename), exist_ok=True)
    with open(filename, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"Created: {filename}")


def generate_product_post():
    product, style = next_product()
    if product is None:
        print("紹介できる新しい商品(未レビューの商品)が products.json にありません。今日は商品紹介記事の生成をスキップします。")
        return

    if style == "tried":
        prompt = f"""あなたは健康・美容ジャンルのレビューライターです。
以下の商品について、実際に使ってみたレビュー記事を書いてください。

商品名: {product['name']}
特徴・メモ: {product['notes']}
関連キーワード: {product['keywords']}

条件:
- 文字数: 900〜1300字程度
- 構成: タイトル(# 見出し)、悩み提起の導入、商品の説明、
  「使ってみた感想」(具体的な体感を交えて、リアルな一人称の文章で)、
  良かった点・気になった点(正直に)、こんな人におすすめ、まとめ
- 「特徴・メモ」の情報は、箇条書きの羅列にせず、自然な文章の中に溶け込ませること
- 商品説明や体験談の中に、それとなく購入への興味を持たせる流れを作ること
- 誇大な効果を断定せず、個人の感想であることが伝わる書き方にすること
- 1段落は2〜4文程度に収め、段落ごとに空行を入れて区切ること(スマホで読んだときに文字が詰まって見えないようにするため)
- 出力形式: Markdownの本文のみ。前置きや説明文は一切つけないこと。
"""
        disclaimer = "> ※本記事はアフィリエイトリンクを含みます。紹介する商品は実際に使用した上での個人的な感想です。\n\n"
    else:
        prompt = f"""あなたは健康・美容ジャンルの情報ライターです。
以下の商品について、公式サイトの情報をもとにした「気になる商品紹介」記事を書いてください。
この商品はまだ実際に使用したことがない前提で書きます。

商品名: {product['name']}
特徴・メモ: {product['notes']}
関連キーワード: {product['keywords']}

条件:
- 文字数: 900〜1300字程度
- 構成: タイトル(# 見出し)、話題になっていて気になった、という導入、
  商品や成分の説明、この商品の特徴(具体的に)、こんな人には気になる商品かも、
  まだ実際に試したことはない旨を正直に伝える一文
- 「実際に使ってみた」「使用感」「体感」など、あたかも自分で使用したかのような
  一人称の体験談は絶対に書かないこと
- 「特徴・メモ」の情報は、箇条書きの羅列にせず、自然な文章の中に溶け込ませること
- 誇大な効果を断定せず、公式情報をもとにした紹介であることが伝わる書き方にすること
- 1段落は2〜4文程度に収め、段落ごとに空行を入れて区切ること(スマホで読んだときに文字が詰まって見えないようにするため)
- 出力形式: Markdownの本文のみ。前置きや説明文は一切つけないこと。
"""
        disclaimer = "> ※本記事はアフィリエイトリンクを含みます。まだ実際に試したことのない商品のため、公式サイトの情報をもとにした紹介記事です。\n\n"

    article_md = call_claude(prompt)

    title_match = re.search(r"^#\s+(.+)$", article_md, re.MULTILINE)
    title = title_match.group(1).strip() if title_match else product["name"]
    # 本文からタイトル行を除去(front matterのtitleと重複させないため)
    article_md = re.sub(r"^#\s+.+\n?", "", article_md, count=1, flags=re.MULTILINE)

    slug = build_slug(title)
    filename = f"_posts/{date_str}-{slug}.md"

    front_matter = {
        "layout": "post",
        "title": title,
        "date": f"{date_str} 07:00:00 +0900",
        "category": "review",
        "affiliate": True,
        "product_link": product["affiliate_url"],
        "image": (product.get("images") or [None])[0],
        "description": extract_description(article_md),
    }

    body = "\n"
    body += disclaimer
    body += build_image_gallery(product.get("images", []))
    body += article_md.strip() + "\n\n"

    video_id = youtube_embed_id(product.get("video", ""))
    if video_id:
        body += "## 商品紹介動画\n\n"
        body += (
            f'<div style="position:relative; padding-bottom:56.25%; height:0; overflow:hidden; '
            f'border-radius:8px; margin-bottom:1.5em;">\n'
            f'  <iframe src="https://www.youtube.com/embed/{video_id}" '
            f'style="position:absolute; top:0; left:0; width:100%; height:100%; border:0;" '
            f'allowfullscreen loading="lazy"></iframe>\n'
            f"</div>\n\n"
        )

    body += f"## 商品情報\n\n"
    body += f"| 項目 | 内容 |\n|---|---|\n"
    body += f"| 商品名 | {product['name']} |\n"
    body += f"| 定価 | {product['price']} |\n"
    body += f"| 会員価格 | {product['member_price']} |\n\n"
    body += f"**→ [公式サイトで詳しく見る]({product['affiliate_url']})**\n\n"
    if product.get("qr_image"):
        body += (
            f'<p style="font-size:0.85em;color:#666;">スマホでQRコードを読み取って商品ページへ<br>\n'
            f'<img src="{site_path(product["qr_image"])}" alt="{product["name"]} 商品ページQRコード" width="120"></p>\n'
        )

    write_post(filename, front_matter, body)


def generate_health_post():
    topic, image_query = TOPICS[today.toordinal() % len(TOPICS)]

    prompt = f"""あなたは健康分野の専門ライターです。
以下の条件で日本語のブログ記事を1本書いてください。

テーマ: {topic}
文字数: 800〜1200字程度
構成: タイトル(# 見出し)、導入、見出し付きの本文(##)、まとめ
文体: 丁寧で分かりやすく、具体的で今日から実践できる内容にすること
段落: 1段落は2〜4文程度に収め、段落ごとに空行を入れて区切ること(スマホで読んだときに文字が詰まって見えないようにするため)
出力形式: Markdownの本文のみ。前置きや説明文は一切つけないこと。
"""
    article_md = call_claude(prompt)

    title_match = re.search(r"^#\s+(.+)$", article_md, re.MULTILINE)
    title = title_match.group(1).strip() if title_match else topic
    article_md = re.sub(r"^#\s+.+\n?", "", article_md, count=1, flags=re.MULTILINE)

    image_url = fetch_featured_image(image_query)
    slug = build_slug(title)
    filename = f"_posts/{date_str}-{slug}.md"

    front_matter = {
        "layout": "post",
        "title": title,
        "date": f"{date_str} 07:00:00 +0900",
        "category": "health",
        "image": image_url or None,
        "description": extract_description(article_md),
    }

    write_post(filename, front_matter, "\n" + article_md.strip() + "\n")


def post_exists_for_today() -> bool:
    """今日の日付から始まる記事が既に_postsに存在するか確認する(重複投稿防止)。"""
    posts_dir = os.path.join(os.path.dirname(__file__), "..", "_posts")
    if not os.path.isdir(posts_dir):
        return False
    return any(name.startswith(date_str + "-") for name in os.listdir(posts_dir))


if __name__ == "__main__":
    if post_exists_for_today():
        print(f"{date_str}の記事は既に存在するため、生成をスキップします。")
    elif weekday in PRODUCT_DAYS:
        generate_product_post()
    else:
        generate_health_post()
