import json
import os
import re
import sys
import time
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
STATE_PATH = os.path.join(BASE_DIR, "state.json")

PTT_URL = "https://www.ptt.cc"
HEADERS = {"User-Agent": "Mozilla/5.0"}
COOKIES = {"over18": "1"}
TAIPEI = ZoneInfo("Asia/Taipei")


def load_webhook_url():
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        raise RuntimeError("環境變數 DISCORD_WEBHOOK_URL 未設定")
    return url


def load_accounts():
    raw = os.environ.get("ACCOUNTS_JSON")
    if not raw:
        raise RuntimeError("環境變數 ACCOUNTS_JSON 未設定")
    return json.loads(raw)


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        config = json.load(f)
    config["accounts"] = load_accounts()
    return config


def load_state():
    if not os.path.exists(STATE_PATH):
        return {"board_last_ts": 0, "accounts": {}}
    with open(STATE_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def in_active_hours(config):
    now = datetime.now(TAIPEI).time()
    start = dtime.fromisoformat(config["active_hours"]["start"])
    end = dtime.fromisoformat(config["active_hours"]["end"])
    return start <= now <= end


def article_timestamp(href):
    m = re.search(r"M\.(\d+)\.A", href)
    return int(m.group(1)) if m else 0


def fetch(url):
    resp = requests.get(url, headers=HEADERS, cookies=COOKIES, timeout=10)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    return resp.text


def parse_article_list(html):
    soup = BeautifulSoup(html, "html.parser")
    articles = []
    for div in soup.select("div.r-ent"):
        title_tag = div.select_one("div.title a")
        if not title_tag:
            continue  # 文章被刪除時沒有連結，跳過
        href = title_tag["href"]
        articles.append({
            "title": title_tag.text.strip(),
            "href": href,
            "url": PTT_URL + href,
            "ts": article_timestamp(href),
        })
    return articles


def get_prev_page_href(html):
    # 板列表的分頁按鈕固定 4 個：最舊 / ‹ 上頁 / 下頁 › / 最新，取第 2 個「‹ 上頁」（較舊的一頁）
    soup = BeautifulSoup(html, "html.parser")
    links = soup.select("div.btn-group-paging a")
    if len(links) >= 2:
        return links[1].get("href")
    return None


def send_discord(webhook_url, message):
    try:
        requests.post(webhook_url, json={"content": message}, timeout=10)
    except requests.RequestException as e:
        print(f"[warn] Discord 通知發送失敗：{e}")
    time.sleep(1)  # 避免觸發 Discord webhook 速率限制


def check_account_posts(config, state, webhook_url, is_first_run):
    board = config["board"]
    for account in config["accounts"]:
        state["accounts"].setdefault(account, {"last_ts": 0})
        url = f"{PTT_URL}/bbs/{board}/search?q=author:{account}"
        try:
            html = fetch(url)
        except requests.RequestException as e:
            print(f"[warn] 抓 {account} 發文列表失敗：{e}")
            continue

        articles = parse_article_list(html)
        last_ts = state["accounts"][account]["last_ts"]

        if is_first_run:
            print(f"[init] {account} 發文基準點建立，共 {len(articles)} 篇既有文章")
        else:
            new_articles = sorted(
                [a for a in articles if a["ts"] > last_ts], key=lambda a: a["ts"]
            )
            for a in new_articles:
                msg = f"📮 新發文｜{account}\n{a['title']}\n{a['url']}"
                send_discord(webhook_url, msg)
                print(f"[notify] {msg}")

        if articles:
            state["accounts"][account]["last_ts"] = max(a["ts"] for a in articles)


def check_board_comments(config, state, webhook_url, is_first_run):
    board = config["board"]
    last_ts = state.get("board_last_ts", 0)
    target_accounts = set(config["accounts"])

    html = fetch(f"{PTT_URL}/bbs/{board}/index.html")
    articles = parse_article_list(html)
    prev_href = get_prev_page_href(html)

    # 若這一頁全部文章都比上次記錄新，代表新文章可能超過一頁，往前多抓幾頁避免漏抓
    max_extra_pages = 5
    pages_fetched = 0
    while (
        articles
        and min(a["ts"] for a in articles) > last_ts
        and prev_href
        and pages_fetched < max_extra_pages
    ):
        prev_html = fetch(PTT_URL + prev_href)
        articles = parse_article_list(prev_html) + articles
        prev_href = get_prev_page_href(prev_html)
        pages_fetched += 1

    if is_first_run:
        print(f"[init] 板上新文章基準點建立，共 {len(articles)} 篇既有文章")
    else:
        new_articles = sorted(
            [a for a in articles if a["ts"] > last_ts], key=lambda a: a["ts"]
        )
        for a in new_articles:
            try:
                article_html = fetch(a["url"])
            except requests.RequestException as e:
                print(f"[warn] 抓文章內容失敗 {a['url']}：{e}")
                continue

            article_soup = BeautifulSoup(article_html, "html.parser")
            for push in article_soup.select("div.push"):
                user_tag = push.select_one("span.push-userid")
                content_tag = push.select_one("span.push-content")
                if not user_tag or not content_tag:
                    continue
                push_user = user_tag.text.strip()
                if push_user in target_accounts:
                    content = content_tag.text.lstrip(": ").strip()
                    msg = (
                        f"💬 新留言｜{push_user} 在《{a['title']}》推文\n"
                        f"「{content}」\n{a['url']}"
                    )
                    send_discord(webhook_url, msg)
                    print(f"[notify] {msg}")
            time.sleep(0.5)  # 避免對 PTT 發太快

    if articles:
        state["board_last_ts"] = max(a["ts"] for a in articles)


def main():
    config = load_config()

    if not in_active_hours(config):
        print("[skip] 不在通知時段內，略過本次執行")
        return

    is_first_run = not os.path.exists(STATE_PATH)
    webhook_url = load_webhook_url()
    state = load_state()

    check_account_posts(config, state, webhook_url, is_first_run)
    check_board_comments(config, state, webhook_url, is_first_run)

    save_state(state)

    if is_first_run:
        print("[init] 基準點已建立，之後執行只會通知「新」發文與留言")


if __name__ == "__main__":
    main()
