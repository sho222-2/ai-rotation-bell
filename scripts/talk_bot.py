#!/usr/bin/env python3
"""AI三者トーク：Claude側の番判定・投稿スクリプト（段階1：海未＋Claude）

使い方:
  python scripts/talk_bot.py prepare --out work   # 番か判定し、Claude用プロンプトを作る
  python scripts/talk_bot.py post --work work      # Claudeの返事をNotionに追記し、SIGNAL.mdを更新

方針:
- 標準ライブラリだけで動く（追加インストール不要）。
- 公開リポジトリの実行記録に会話の中身が出ないよう、本文は一切 print しない。
- NotionトークンはこのスクリプトのステップだけでENVに渡し、Claudeのステップには渡さない。
"""
import argparse
import json
import os
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime
from zoneinfo import ZoneInfo

NOTION_VERSION = "2022-06-28"
JST = ZoneInfo("Asia/Tokyo")
SELF = "Claude"
ROTATION = ["MUSE", "ChatGPT", "Claude"]  # 正式な順番（参加者でない者は飛ばす）
ALIASES = {
    "海未": "MUSE", "うみ": "MUSE", "MUSE": "MUSE", "MUSE（海未）": "MUSE",
    "ChatGPT": "ChatGPT", "Claude": "Claude",
}
STOP_MARKS = ["【会話終了】", "【合意できず】"]
AGREE_MARK = "【合意】"
NEW_TOPIC_MARK = "【新しい話題】"
PAUSE_MARK, RESUME_MARK = "【自動話題 停止】", "【自動話題 再開】"
HEADER_RE = re.compile(r"^\s*(.+?)\s*[／/]\s*(\d{4}-\d{2}-\d{2}[ T]\d{1,2}:\d{2})")
RECENT_MESSAGES = 15     # Claudeに渡す直近の発言数
MAX_REPLY_CHARS = 1500   # 念のための上限（Notionの1ブロックは2000字まで）


# ---------- Notion API ----------
def notion(method, path, body=None):
    token = os.environ["NOTION_TOKEN"]
    req = urllib.request.Request(
        f"https://api.notion.com/v1{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        # 本文は出さず、ステータスだけ出す
        sys.exit(f"Notion API error: {method} {path.split('?')[0]} -> HTTP {e.code}")


def list_blocks(page_id):
    blocks, cursor = [], None
    while True:
        q = f"/blocks/{page_id}/children?page_size=100" + (f"&start_cursor={cursor}" if cursor else "")
        res = notion("GET", q)
        blocks += res["results"]
        if not res.get("has_more"):
            return blocks
        cursor = res["next_cursor"]


def block_text(b):
    data = b.get(b["type"], {})
    return "".join(t.get("plain_text", "") for t in data.get("rich_text", []))


# ---------- 会話の解析 ----------
def canonical(name):
    name = name.strip()
    if name in ALIASES:
        return ALIASES[name]
    for k, v in ALIASES.items():
        if name.startswith(k):
            return v
    return "HUMAN"  # 本人など、AI以外


def parse_messages(blocks):
    """「## トーク」見出し以降を、区切り線ごとの発言に分ける。"""
    msgs, started, cur = [], False, None
    for b in blocks:
        t = b["type"]
        if not started:
            if t.startswith("heading_") and "トーク" in block_text(b):
                started = True
            continue
        if t == "divider":
            if cur and cur["lines"]:
                msgs.append(cur)
            cur = {"lines": [], "last_block_id": None}
            continue
        text = block_text(b).strip()
        if not text:
            continue
        if cur is None:
            cur = {"lines": [], "last_block_id": None}
        cur["lines"].append(text)
        cur["last_block_id"] = b["id"]
    if cur and cur["lines"]:
        msgs.append(cur)

    out = []
    for m in msgs:
        # 見出し行（名前／日時）を探す。見出しの前に「【新しい話題】」などが置かれていてもよい
        idx, mm = None, None
        for i, line in enumerate(m["lines"]):
            mm = HEADER_RE.match(line.splitlines()[0])
            if mm:
                idx = i
                break
        if idx is None:
            continue  # 「（ここに、新しいものを下へ積みます）」などの説明文は飛ばす
        head = m["lines"][idx]
        rest_first = "\n".join(head.splitlines()[1:])
        body = "\n".join([x for x in m["lines"][:idx] + [rest_first] + m["lines"][idx + 1:] if x.strip()])
        out.append({
            "author": canonical(mm.group(1)),
            "author_raw": mm.group(1),
            "time": mm.group(2),
            "body": body,
            "id": m["last_block_id"],
        })
    return out


def current_topic(msgs):
    """直近の本人の発言、または【新しい話題】を含む発言から後を、今の話題とする。"""
    start = 0
    for i, m in enumerate(msgs):
        if m["author"] == "HUMAN" or NEW_TOPIC_MARK in m["body"]:
            start = i
    return msgs[start:]


def next_speaker(after, participants):
    order = [p for p in ROTATION if p in participants]
    if after not in order:  # 本人の発言の後は、順番の先頭から
        return order[0]
    return order[(order.index(after) + 1) % len(order)]


def decide(msgs, participants, max_turns):
    if not msgs:
        return False, "no-messages"
    last = msgs[-1]
    if last["author"] == SELF:
        return False, "last-is-self"
    topic = current_topic(msgs)
    for m in topic:
        if any(mark in m["body"] for mark in STOP_MARKS):
            return False, "stop-mark"
        if m["author"] == "HUMAN" and "止めて" in m["body"]:
            return False, "human-stop"
    ai_msgs = [m for m in topic if m["author"] != "HUMAN"]
    order = [p for p in ROTATION if p in participants]
    tail = ai_msgs[-len(order):]
    if (len(tail) == len(order) and {m["author"] for m in tail} == set(order)
            and all(AGREE_MARK in m["body"] for m in tail)):
        return False, "consensus"
    ai_turns = len(ai_msgs)
    if ai_turns >= max_turns:
        return False, f"max-turns({ai_turns}/{max_turns})"
    if next_speaker(last["author"], participants) != SELF:
        return False, f"not-my-turn(next={next_speaker(last['author'], participants)})"
    return True, f"my-turn(turn {ai_turns + 1}/{max_turns})"


def agree_streak(msgs):
    """今の話題の最後から数えて、【合意】が続いているAIの人数（Claude以外、重複なし）。"""
    names = []
    for m in reversed(current_topic(msgs)):
        if m["author"] == "HUMAN" or AGREE_MARK not in m["body"] or m["author"] in names:
            break
        names.append(m["author"])
    return names


def msg_time(m):
    return datetime.strptime(m["time"].replace("T", " "), "%Y-%m-%d %H:%M").replace(tzinfo=JST)


def decide_topic(msgs, participants, idle_hours, now):
    """会話が idle_hours 以上止まっていて、話題を出す番がClaudeなら True。"""
    bodies = [m["body"] for m in msgs]
    last_pause = max([i for i, b in enumerate(bodies) if PAUSE_MARK in b], default=-1)
    last_resume = max([i for i, b in enumerate(bodies) if RESUME_MARK in b], default=-1)
    if last_pause > last_resume:
        return False, "auto-topic-paused"
    if msgs:
        idle = (now - msg_time(msgs[-1])).total_seconds() / 3600
        if idle < idle_hours:
            return False, f"not-idle({idle:.1f}h)"
    order = [p for p in ROTATION if p in participants]
    starters = [m["author"] for m in msgs if m["author"] in order and NEW_TOPIC_MARK in m["body"]]
    starter = order[0] if not starters else order[(order.index(starters[-1]) + 1) % len(order)]
    if starter != SELF:
        return False, f"not-my-topic-turn(starter={starter})"
    return True, "my-topic-turn"


# ---------- コマンド ----------
def set_output(key, value):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            f.write(f"{key}={value}\n")


def read_signal(path="SIGNAL.md"):
    """SIGNAL.md の「key: value」行を辞書にする。無ければ空。"""
    sig = {}
    try:
        for line in open(path, encoding="utf-8"):
            if ":" in line and not line.startswith("#"):
                k, v = line.split(":", 1)
                sig[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return sig


def github(method, path, body=None):
    req = urllib.request.Request(
        f"https://api.github.com{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
                 "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(f"GitHub API error: {method} {path.split('?')[0]} -> HTTP {e.code}")


def cmd_notify(args):
    """次がChatGPTのとき、合図用Issueに1行コメントする（GitHubの通知メールでChatGPTを起こす）。
    同じ message_id には2回コメントしない。会話の本文は書かない。"""
    sig = read_signal()
    if sig.get("next") != "ChatGPT":
        print(f"notify: skip (next={sig.get('next')})")
        return
    mid = sig.get("message_id", "")
    repo, num = os.environ["GITHUB_REPOSITORY"], os.environ["BELL_ISSUE"]
    count = github("GET", f"/repos/{repo}/issues/{num}").get("comments", 0)
    last_page = max(1, (count + 99) // 100)
    recent = github("GET", f"/repos/{repo}/issues/{num}/comments?per_page=100&page={last_page}")
    if mid and any(mid in c.get("body", "") for c in recent):
        print("notify: already sent for this message_id")
        return
    body = (f"AI-TALK bell: {sig.get('from', '?')} -> ChatGPT\n\n"
            f"message_id: {mid}\nturn: {sig.get('turn', '?')}\n"
            f"page: AI-TALK-20261112-01\nupdated_at: {sig.get('updated_at', '?')}")
    github("POST", f"/repos/{repo}/issues/{num}/comments", {"body": body})
    print("notify: sent")


def cmd_prepare(args):
    page = os.environ["TALK_PAGE_ID"]
    participants = [p.strip() for p in os.environ.get("PARTICIPANTS", "MUSE,Claude").split(",")]
    max_turns = int(os.environ.get("MAX_TURNS", "10"))
    idle_hours = float(os.environ.get("IDLE_HOURS", "12"))
    event = os.environ.get("GITHUB_EVENT_NAME", "")
    sig_next = read_signal().get("next")
    msgs = None
    mode = None
    # 1) 返事：呼び鈴（SIGNAL.md）の宛先とトークの順番の両方がClaudeのとき
    if event == "workflow_dispatch" or not sig_next or sig_next == SELF:
        msgs = parse_messages(list_blocks(page))
        run, reason = decide(msgs, participants, max_turns)
        print(f"decision(reply): run={run} reason={reason} messages={len(msgs)}")  # 中身は出さない
        if run:
            mode = "reply"
    else:
        print(f"decision(reply): run=False reason=signal-next={sig_next}")
    # 2) 話題出し：定期実行のときだけ。半日止まっていて、話題を出す番がClaudeなら
    if mode is None and event == "schedule":
        if msgs is None:
            msgs = parse_messages(list_blocks(page))
        run, reason = decide_topic(msgs, participants, idle_hours, datetime.now(JST))
        print(f"decision(topic): run={run} reason={reason}")
        if run:
            mode = "topic"
    set_output("run", "true" if mode else "false")
    set_output("mode", mode or "none")
    if not mode:
        return

    os.makedirs(args.out, exist_ok=True)
    rules = open(os.path.join(os.path.dirname(__file__), "..", "prompts", "claude_talk_rules.md"),
                 encoding="utf-8").read()
    recent = msgs[-RECENT_MESSAGES:]
    convo = "\n\n".join(f"[{m['author_raw']}／{m['time']}]\n{m['body']}" for m in recent)
    if mode == "reply":
        agreed = agree_streak(msgs)
        hint = ""
        if agreed:
            hint = (f"\n\n（参考：直前に【合意】を書いているのは {'、'.join(agreed)} です。"
                    "あなたも本当に納得しているなら【合意】を書いてかまいません。"
                    "これで全員そろう場合は、合意した内容を2〜3行でまとめてから最後の行に【合意】と書いてください。"
                    "納得していないなら、合わせずに理由を書いてください。）")
        task = "上の会話の続きとして、Claudeの次の発言の本文だけを出力してください。" + hint
        turn = sum(1 for m in current_topic(msgs) if m["author"] != "HUMAN") + 1
    else:
        task = ("会話が半日以上止まっているので、あなたが新しい話題を1つ出す番です。"
                "自分で考えた話題でも、ウェブ検索で見つけた最近の話題でもかまいません。"
                "検索した場合は、本文の最後に「出典：サイト名 URL」を1行添えてください。"
                "3人で意見を出し合い、納得できるところまで話せる問いの形にしてください。"
                "本人の個人情報、資産やFXなどの非公開情報には触れないでください。"
                "【新しい話題】の印や名前・日時は書かず、話題の本文だけを出力してください（300字以内）。")
        turn = 1
    prompt = f"{rules}\n\n<conversation>\n{convo}\n</conversation>\n\n{task}"
    with open(os.path.join(args.out, "prompt.txt"), "w", encoding="utf-8") as f:
        f.write(prompt)
    with open(os.path.join(args.out, "state.json"), "w", encoding="utf-8") as f:
        json.dump({"replied_to": msgs[-1]["id"] if msgs else None, "participants": participants,
                   "turn": turn, "mode": mode}, f)


def paragraph(text):
    return {"object": "block", "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": text[:2000]}}]}}


def cmd_post(args):
    page = os.environ["TALK_PAGE_ID"]
    state = json.load(open(os.path.join(args.work, "state.json"), encoding="utf-8"))
    reply = open(os.path.join(args.work, "reply.txt"), encoding="utf-8").read().strip()
    if not reply:
        sys.exit("empty reply; nothing posted")
    reply = reply[:MAX_REPLY_CHARS]

    # 書き込む直前にもう一度読み、返事を考えている間に状況が変わっていないか確かめる
    msgs = parse_messages(list_blocks(page))
    if (msgs[-1]["id"] if msgs else None) != state["replied_to"]:
        print("conversation changed while thinking; skip posting")
        set_output("posted", "false")
        return

    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    passed = reply.strip() == "【パス】"
    children = [{"object": "block", "type": "divider", "divider": {}},
                paragraph(f"Claude／{now} JST")]
    if state.get("mode") == "topic":
        children.append(paragraph(NEW_TOPIC_MARK))
    if passed:
        children.append(paragraph("（今回は書くことがないので、次へ回します）"))
    else:
        for para in [p.strip() for p in re.split(r"\n\s*\n", reply) if p.strip()]:
            children.append(paragraph(para))
    notion("PATCH", f"/blocks/{page}/children", {"children": children})
    print(f"posted: paragraphs={len(children) - 2} pass={passed}")

    nxt = next_speaker(SELF, state["participants"])
    msg_id = f"{datetime.now(JST).strftime('%Y%m%d-%H%M%S')}-Claude"
    with open("SIGNAL.md", "w", encoding="utf-8") as f:
        f.write(
            "# AI三者トーク 呼び鈴\n\n"
            f"from: Claude\n"
            f"next: {nxt}\n"
            f"turn: {state['turn']}\n"
            f"message_id: {msg_id}\n"
            f"page: AI-TALK-20261112-01\n"
            f"updated_at: {now} JST\n"
        )
    set_output("posted", "true")
    set_output("next", nxt)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("prepare"); p1.add_argument("--out", default="work")
    p2 = sub.add_parser("post"); p2.add_argument("--work", default="work")
    sub.add_parser("notify")
    args = ap.parse_args()
    {"prepare": cmd_prepare, "post": cmd_post, "notify": cmd_notify}[args.cmd](args)


if __name__ == "__main__":
    main()
