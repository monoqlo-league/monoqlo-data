"""戦績CSVの更新内容をまとめて Discord のウェブフックへ送る。

DRY_RUN=1 のときは送らずに内容を表示するだけ。
"""
import csv
import io
import json
import os
import re
import subprocess
import sys
import urllib.request

MONTH = re.compile(r"^(\d{4})(\d{2})\.csv$")
SUMMARY = re.compile(r"^(\d{4})(\d{2})-summary\.csv$")
ZERO = "0" * 40


def git(*args):
    r = subprocess.run(["git", *args], capture_output=True)
    return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else None


def games(rev, name):
    """そのCSVの対局数(非表示を除く)。ファイルが無ければ None。"""
    text = git("show", f"{rev}:{name}") if rev else None
    if text is None:
        return None
    rows = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    return sum(1 for r in rows if (r.get("非表示状態") or "").strip().lower() != "yes")


def test_message():
    """手動実行のときのテスト送信。最新の月の対局数を添える。"""
    months = sorted(n for n in (git("ls-tree", "--name-only", "HEAD") or "").splitlines() if MONTH.match(n))
    lines = ["🔔 **通知のテスト**", "この通知が見えていれば、ランキング更新の通知はこのチャンネルに届きます。"]
    if months:
        m = MONTH.match(months[-1])
        lines.append(f"現在の最新:{m.group(1)}年{m.group(2)}月(計{games('HEAD', months[-1])}局)")
    return lines


def main():
    before = os.environ.get("BEFORE") or ""
    after = os.environ.get("AFTER") or "HEAD"
    if not before or before == ZERO or git("cat-file", "-e", before + "^{commit}") is None:
        before = (git("rev-parse", after + "^") or "").strip() or None
    diff = git("diff", "--name-only", before, after) if before else git("ls-tree", "--name-only", after)
    names = sorted(n for n in (diff or "").splitlines() if MONTH.match(n) or SUMMARY.match(n))

    lines = []
    if os.environ.get("EVENT") == "workflow_dispatch":
        names = []
        lines = test_message()
    for n in names:
        m = MONTH.match(n)
        if m:
            label = f"{m.group(1)}年{m.group(2)}月"
            old, new = games(before, n), games(after, n)
            if new is None:
                lines.append(f"・{label}の戦績を削除")
            elif old is None:
                lines.append(f"・{label}の戦績を追加(計{new}局)")
            elif new != old:
                lines.append(f"・{label}:{new - old:+d}局(計{new}局)")
            else:
                lines.append(f"・{label}の戦績を更新(計{new}局)")
            continue
        m = SUMMARY.match(n)
        label = f"{m.group(1)}年{m.group(2)}月"
        if games(after, n) is None:
            lines.append(f"・{label}の詳細成績を削除")
        else:
            lines.append(f"・{label}の詳細成績を更新")

    if not lines:
        print("通知する変更なし")
        return

    page = os.environ.get("PAGE_URL", "")
    head = [] if lines[0].startswith("🔔") else ["📊 **ランキングを更新しました**"]
    content = "\n".join(
        [*head, *lines, "", f"ランキング:{page}", "(ページへの反映に数分かかることがあります)"]
    )
    print(content)

    if os.environ.get("DRY_RUN"):
        return
    url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not url:
        print("DISCORD_WEBHOOK_URL が未設定のため、送信しない。")
        return
    body = json.dumps({"content": content, "allowed_mentions": {"parse": []}}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "monoqlo-data-notify (https://github.com/monoqlo-league/monoqlo-data, 1.0)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            print("送信しました:", r.status)
    except urllib.error.HTTPError as e:
        print("Discordへの送信に失敗:", e.code, e.read().decode("utf-8", "replace")[:300])
        sys.exit(1)


if __name__ == "__main__":
    main()
