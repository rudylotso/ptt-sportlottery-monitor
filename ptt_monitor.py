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
        author_tag = div.select_one("div.author")
        articles.append({
            "title": title_tag.text.strip(),
            "href": href,
            "url": PTT_URL + href,
            "ts": article_timestamp(href),
            "author": author_tag.text.strip() if author_tag else "",
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
    # 一般執行時改由 check_board_comments 用即時的看板首頁資料判斷新發文，
    # 避免 PTT 作者搜尋頁（search?q=author:）索引更新延遲導致通知變慢。
    # 這裡只在第一次建立基準點時使用，抓每個帳號過去的發文記錄。
    if not is_first_run:
        return

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
        print(f"[init] {account} 發文基準點建立，共 {len(articles)} 篇既有文章")

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
    # 時間戳解析失敗（ts=0，例如非標準格式的文章）的項目不列入判斷，避免拖累 min() 誤判成「沒有全新」而提前停止背抓
    max_extra_pages = 5
    pages_fetched = 0
    while True:
        valid_ts = [a["ts"] for a in articles if a["ts"] > 0]
        if not (valid_ts and min(valid_ts) > last_ts and prev_href and pages_fetched < max_extra_pages):
            break
        prev_html = fetch(PTT_URL + prev_href)
        articles = parse_article_list(prev_html) + articles
        prev_href = get_prev_page_href(prev_html)
        pages_fetched += 1

    push_notified = state.setdefault("push_notified", {})

    if is_first_run:
        print(f"[init] 板上新文章基準點建立，共 {len(articles)} 篇既有文章")
    else:
        # 重新掃描目前可見的每一篇文章（不只新文章），避免漏掉「舊文章底下新出現的推文」
        for a in articles:
            if a["author"] in target_accounts:
                acc_state = state["accounts"].setdefault(a["author"], {"last_ts": 0})
                if a["ts"] > acc_state["last_ts"]:
                    msg = f"📮 新發文｜{a['author']}\n{a['title']}\n{a['url']}"
                    send_discord(webhook_url, msg)
                    print(f"[notify] {msg}")

            try:
                article_html = fetch(a["url"])
            except requests.RequestException as e:
                print(f"[warn] 抓文章內容失敗 {a['url']}：{e}")
                continue

            article_soup = BeautifulSoup(article_html, "html.parser")
            target_pushes = []
            for push in article_soup.select("div.push"):
                user_tag = push.select_one("span.push-userid")
                content_tag = push.select_one("span.push-content")
                if not user_tag or not content_tag:
                    continue
                push_user = user_tag.text.strip()
                if push_user in target_accounts:
                    content = content_tag.text.lstrip(": ").strip()
                    target_pushes.append((push_user, content))

            already_notified = push_notified.get(a["href"], 0)
            for push_user, content in target_pushes[already_notified:]:
                msg = (
                    f"💬 新留言｜{push_user} 在《{a['title']}》推文\n"
                    f"「{content}」\n{a['url']}"
                )
                send_discord(webhook_url, msg)
                print(f"[notify] {msg}")

            if target_pushes:
                push_notified[a["href"]] = len(target_pushes)
            time.sleep(0.5)  # 避免對 PTT 發太快

        # 文章滑出目前追蹤範圍（首頁+補抓的頁數）後不再需要追蹤推文數，避免 state.json 無限長大
        current_hrefs = {a["href"] for a in articles}
        for href in list(push_notified.keys()):
            if href not in current_hrefs:
                del push_notified[href]

    if articles:
        state["board_last_ts"] = max(a["ts"] for a in articles)

        if not is_first_run:
            # 用這一輪掃到的所有文章更新各帳號的發文基準點（取最大值，避免同一輪多篇時互相干擾）
            for account in target_accounts:
                account_ts = [a["ts"] for a in articles if a["author"] == account]
                if account_ts:
                    acc_state = state["accounts"].setdefault(account, {"last_ts": 0})
                    acc_state["last_ts"] = max(acc_state["last_ts"], max(account_ts))


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
