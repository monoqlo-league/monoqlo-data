"""データ置き場のファイル一覧(index.json)を作り、CSVを点検する。

- index.json: ランキングページが最初に1回だけ読む一覧。ファイルごとの最終更新日時(Gitの記録)と対局数を持つ。
  ページはこれを読めば、GitHub の API(1時間60回の制限)を使わずに済む。
- 点検: 今回の更新に関係するCSVに問題があれば、Discord に知らせる(DISCORD_ADMIN_WEBHOOK_URL、無ければ DISCORD_WEBHOOK_URL)。
  手動実行(workflow_dispatch)のときは、全部のCSVを点検して結果を送る(問題がなくても送る)。

DATA_KIND=team(チーム戦 event-data) / personal(個人戦 monoqlo-data)。
DRY_RUN=1 のときは index.json を書くだけで、Discord には送らない(内容は表示する)。
"""
import csv
import datetime
import io
import json
import os
import re
import subprocess
import sys
import unicodedata
import urllib.request
from collections import Counter, defaultdict

KIND = os.environ.get("DATA_KIND", "team")
INFO = "(情報)"          # この印で始まるものは、問題ではなくお知らせ。これだけのときは Discord に送らない(手動実行のときは送る)
ZERO = "0" * 40
PLAYER = re.compile(r"^\[[^\]]*\]\[(\d+)\](.*)$")


def git(*args):
    r = subprocess.run(["git", *args], capture_output=True)
    return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else None


def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        text = f.read()
    return list(csv.reader(io.StringIO(text)))


def rows_as_dicts(rows):
    if not rows:
        return [], []
    head = [h.strip() for h in rows[0]]
    out = []
    for r in rows[1:]:
        if not any(c.strip() for c in r):
            continue
        out.append({head[i]: (r[i] if i < len(r) else "") for i in range(len(head))})
    return head, out


def is_cpu(n):
    """CPU の席(対局者欄が「CPU」)。誰の席かわからないだけで、データの誤りではないので知らせない"""
    return unicodedata.normalize("NFKC", n or "").strip().upper() == "CPU"


def clean_name(n):
    n = unicodedata.normalize("NFKC", n or "").strip()
    n = re.sub(r"\s*[(（]\s*[)）]\s*$", "", n)
    return n.strip()


# ---------- 対局記録(牌譜履歴)の読み方。チーム戦の -gamesN.csv と、個人戦の YYYYMM.csv は同じ形 ----------
GAME_COLS = ["開始時間"] + [f"{i}位{x}" for i in range(1, 5) for x in ("プレイヤー名", "最終点数")]


def read_games(path, problems, where=None):
    """非表示を除いた対局の一覧。[{start, link, row, seats:[(id, name, pt, score)]}]。おかしな行は problems に入れる。
    where(行番号の一覧) は、問題の行がどこにあたるかの補足(個人戦では分割CSVの番号)を返す"""
    try:
        head, rows = rows_as_dicts(read_csv(path))
    except Exception as e:
        problems.append((path, f"CSVとして読めない({e})"))
        return []
    miss = [c for c in GAME_COLS if c not in head]
    if miss:
        problems.append((path, "必要な列がない: " + "、".join(miss)))
        return []
    games, bad_rows, sums, order, seen, dup = [], [], [], [], set(), []
    for n, r in enumerate(rows, start=2):
        if (r.get("非表示状態") or "").strip().lower() == "yes":
            continue
        seats = []
        for i in range(1, 5):
            cell = (r.get(f"{i}位プレイヤー名") or "").strip()
            m = PLAYER.match(cell)
            try:
                pt = float(r.get(f"{i}位最終点数") or "")
            except ValueError:
                pt = None
            try:
                sc = float(r.get(f"{i}位PT") or "") if (r.get(f"{i}位PT") or "").strip() else None
            except ValueError:
                sc = None
            if is_cpu(cell):
                seats.append(("CPU", "CPU", pt, sc))
                continue
            seats.append((m.group(1) if m else None, clean_name(m.group(2)) if m else "", pt, sc))
        if any(s[0] is None or s[2] is None for s in seats):
            bad_rows.append(n)
            continue
        if abs(sum(s[2] for s in seats)) > 0.11:
            sums.append(n)
        if all(s[3] is not None for s in seats):
            if abs(sum(s[3] for s in seats) - 100000) > 0.5:
                sums.append(n)
            if any(seats[i][3] < seats[i + 1][3] for i in range(3)):
                order.append(n)
        link = (r.get("牌譜リンク") or "").strip()
        key = link or (r.get("開始時間") or "").strip() + "/" + ",".join(s[0] for s in seats)
        if key in seen:
            dup.append(n)
            continue
        seen.add(key)
        games.append({"start": (r.get("開始時間") or "").strip(), "link": link, "row": n, "seats": seats})
    lim = lambda xs: "、".join(map(str, xs[:8])) + (f" ほか{len(xs) - 8}行" if len(xs) > 8 else "")
    w = lambda xs: where(xs) if where else ""
    if bad_rows:
        problems.append((path, f"プレイヤー名か点数が読めない行がある({lim(bad_rows)}行目){w(bad_rows)}。その対局は集計に入らない"))
    if sums:
        problems.append((path, f"4人の点数の合計が合わない対局がある({lim(sorted(set(sums)))}行目){w(sums)}"))
    if order:
        problems.append((path, f"終了時の点棒が着順どおりに並んでいない対局がある({lim(order)}行目){w(order)}"))
    if dup:
        problems.append((path, f"同じ対局が2回入っている({lim(dup)}行目){w(dup)}。2回目は数えない"))
    return games


def check_summary(path, games, problems, where_links=None):
    """詳細成績のもと(-summary.csv)。ゲームIDが対局記録にあるか、名前で結び付くか。
    where_links(ゲームIDの一覧) は、その対局がどこにあたるかの補足(個人戦では分割CSVの番号)を返す"""
    wl = where_links or (lambda xs: "")
    try:
        head, rows = rows_as_dicts(read_csv(path))
    except Exception as e:
        problems.append((path, f"CSVとして読めない({e})"))
        return
    miss = [c for c in ("ゲームID", "名前", "局数") if c not in head]
    if miss:
        problems.append((path, "必要な列がない: " + "、".join(miss)))
        return
    by_link = {g["link"]: g for g in games if g["link"]}
    if not by_link:
        problems.append((path, "対応する対局記録に「牌譜リンク」がないため、結び付けられない"))
        return
    unknown, per_game, unmatched, iid_map, pending = set(), Counter(), [], {}, []
    for r in rows:
        gid = (r.get("ゲームID") or "").strip()
        if not gid:
            continue
        g = by_link.get(gid)
        if not g:
            unknown.add(gid)
            continue
        per_game[gid] += 1
        name = clean_name(r.get("名前"))
        if is_cpu(name):
            continue
        hit = [s for s in g["seats"] if s[1] == name]
        if len(hit) == 1:
            iid_map[(r.get("内部ID") or "").strip()] = hit[0][0]
        else:
            pending.append((gid, name, (r.get("内部ID") or "").strip()))
    bad_links = []
    for gid, name, iid in pending:
        pid = iid_map.get(iid)
        if not pid or not any(s[0] == pid for s in by_link[gid]["seats"]):
            unmatched.append(name or "(名前なし)")
            bad_links.append(gid)
    if unknown:
        problems.append((path, f"対局記録にないゲームIDが{len(unknown)}件ある。その行は集計に入らない"))
    odd = [g for g, c in per_game.items() if c != 4]
    if odd:
        problems.append((path, f"4人分そろっていない対局が{len(odd)}件ある{wl(odd)}"))
    if unmatched:
        names = sorted(set(unmatched))
        problems.append((path, f"対局記録の誰とも結び付かない行が{len(unmatched)}行ある(名前: {'、'.join(names[:6])}{' ほか' if len(names) > 6 else ''}){wl(bad_links)}"))
    lacking = [l for l in by_link if l not in per_game]
    if lacking and len(lacking) < len(by_link):
        problems.append((path, f"{INFO}対局記録のうち{len(lacking)}対局が、まだ集計されていない(対局記録は{len(by_link)}対局){wl(lacking)}"))


# ---------- チーム戦(event-data) ----------
T_MASTER = re.compile(r"^(\d{8})-master\.csv$")
T_SCHED = re.compile(r"^(\d{8})-schedule\.csv$")
T_GAMES = re.compile(r"^(\d{8})-games(\d)\.csv$")
T_SUMM = re.compile(r"^(?:summary/)?(\d{8})-games(\d)-summary\.csv$")


def team_key(path):
    for rx in (T_MASTER, T_SCHED, T_GAMES, T_SUMM):
        m = rx.match(path)
        if m:
            return m.group(1)
    return None


def check_team(paths):
    problems, info = [], {}
    keys = sorted({k for p in paths for k in [team_key(p)] if k})
    for p in paths:
        if not p.lower().endswith(".csv"):
            continue
        if T_SUMM.match(p) and not p.startswith("summary/"):
            problems.append((p, "詳細成績のもとは summary フォルダに置くことになっている(このままでも読まれる)"))
        elif not (T_MASTER.match(p) or T_SCHED.match(p) or T_GAMES.match(p) or T_SUMM.match(p)):
            problems.append((p, "名前がルールと違うため、ランキングでは読み込まれない(例: 20260901-games1.csv)"))
    for key in keys:
        mpath = f"{key}-master.csv"
        roster, leagues, teams = {}, [], []
        if mpath in paths:
            head, rows = rows_as_dicts(read_csv(mpath))
            miss = [c for c in ("チーム名", "プレイヤーID") if c not in head]
            if miss:
                problems.append((mpath, "必要な列がない: " + "、".join(miss)))
            else:
                ids = Counter()
                for r in rows:
                    pid, team = (r.get("プレイヤーID") or "").strip(), (r.get("チーム名") or "").strip()
                    if not pid or not team:
                        problems.append((mpath, "チーム名かプレイヤーIDが空の行がある"))
                        continue
                    ids[pid] += 1
                    roster[pid] = (team, (r.get("プレイヤー名") or "").strip())
                    if team not in teams:
                        teams.append(team)
                dups = [roster[i][1] or i for i, c in ids.items() if c > 1]
                if dups:
                    problems.append((mpath, "同じプレイヤーIDが2回以上ある: " + "、".join(dups[:6])))
                plan = next(((r.get("リーグ構成") or "").strip() for r in rows if (r.get("リーグ構成") or "").strip()), "")
                for x in plan.split("/"):
                    parts = [y.strip() for y in x.split(":")]
                    if parts and parts[0]:
                        n = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
                        leagues.append((parts[0], n))
                for i, (name, n) in enumerate(leagues):
                    if i and (n < 4 or n > leagues[i - 1][1]):
                        problems.append((mpath, f"リーグ構成の{name}のチーム数({n})がおかしい"))
                if leagues and leagues[0][1] and leagues[0][1] != len(teams):
                    problems.append((mpath, f"リーグ構成では{leagues[0][0]}が{leagues[0][1]}チームだが、チームは{len(teams)}ある"))
        # スケジュール
        spath = f"{key}-schedule.csv"
        planned = {}
        if spath in paths:
            head, rows = rows_as_dicts(read_csv(spath))
            if "チーム" not in head or not any(re.fullmatch(r"第\d+戦", h) for h in head):
                problems.append((spath, "「チーム」「第1戦」の列がない"))
            else:
                by_lg = defaultdict(list)
                for r in rows:
                    by_lg[(r.get("リーグ") or "").strip()].append(r)
                names = [l[0] for l in leagues]
                for lg, rs in by_lg.items():
                    if names and lg not in names:
                        problems.append((spath, f"大会マスターにないリーグ名「{lg}」がある"))
                        continue
                    k = names.index(lg) if lg in names else 0
                    labels = [(r.get("チーム") or "").strip() for r in rs]
                    if k == 0 and teams:
                        ng = [t for t in labels if t not in teams]
                        lack = [t for t in teams if t not in labels]
                        if ng or lack:
                            problems.append((spath, f"{lg}のチーム名が大会マスターと合わない(マスターにない: {'、'.join(ng[:4]) or 'なし'} / スケジュールにない: {'、'.join(lack[:4]) or 'なし'})"))
                    seats = 0
                    for r in rs:
                        seats += sum(1 for c, v in r.items() if re.fullmatch(r"第\d+戦", c or "") and (v or "").strip() and "抜け番" not in v)
                    planned[k] = seats // 4
        # 対局記録(前のリーグにもある対局は数えない。ランキングページと同じ)
        seen, games_by = set(), {}
        for k in range(1, 10):
            gpath = f"{key}-games{k}.csv"
            if gpath not in paths:
                continue
            gs = read_games(gpath, problems)
            fresh = []
            for g in gs:
                gid = g["link"] or g["start"] + "/" + ",".join(s[0] for s in g["seats"])
                if gid in seen:
                    continue
                seen.add(gid)
                fresh.append(g)
            games_by[k] = gs
            info[gpath] = {"games": len(fresh)}
            if roster:
                outsiders = sorted({s[1] or s[0] for g in fresh for s in g["seats"] if s[0] not in roster and s[0] != "CPU"})
                if outsiders:
                    problems.append((gpath, f"大会マスターにないプレイヤーがいる(チーム順位に入らない): {'、'.join(outsiders[:6])}{' ほか' if len(outsiders) > 6 else ''}"))
            p = planned.get(k - 1)
            if p and len(fresh) > p:
                problems.append((gpath, f"対局の数({len(fresh)})が、スケジュールの対局の数({p})より多い"))
        for p in paths:
            m = T_SUMM.match(p)
            if m and m.group(1) == key:
                k = int(m.group(2))
                if k not in games_by:
                    problems.append((p, f"対応する対局記録({key}-games{k}.csv)がない"))
                else:
                    check_summary(p, games_by[k], problems)
    return problems, info


# ---------- 個人戦(monoqlo-data) ----------
P_MONTH = re.compile(r"^(\d{4})(\d{2})\.csv$")
P_SUMM = re.compile(r"^(?:summary/)?(\d{6})-summary\.csv$")


def split_numbers(path, ym):
    """分割CSV(splitdata-download の YYYYMM-連番.csv)の番号。月のCSVを開始時間の古い順に並べ、10対局ずつに分けたもの。
    行番号 → 番号、牌譜リンク → 番号 の2つを返す。月のCSVを分割したあとで書き換えると、番号はずれる"""
    rows = [(n, r) for n, r in enumerate(read_csv(path), start=1) if n > 1 and any(c.strip() for c in r)]
    head = [h.strip() for h in read_csv(path)[0]]
    ci, cl = (head.index("開始時間") if "開始時間" in head else 0), (head.index("牌譜リンク") if "牌譜リンク" in head else None)
    by_row, by_link = {}, {}
    for i, (n, r) in enumerate(sorted(rows, key=lambda x: x[1][ci] if ci < len(x[1]) else "")):
        label = f"{ym}-{i // 10 + 1:02d}"
        by_row[n] = label
        if cl is not None and cl < len(r) and r[cl].strip():
            by_link[r[cl].strip()] = label
    return by_row, by_link


def split_note(labels):
    labels = sorted(set(l for l in labels if l))
    if not labels:
        return ""
    return "〔分割CSV: " + "、".join(labels[:8]) + (f" ほか{len(labels) - 8}個" if len(labels) > 8 else "") + "〕"


def team_links():
    """チーム戦の対局(牌譜リンク)。event-data のフォルダ(環境変数 EVENT_DATA)の *-gamesN.csv から。無ければ None"""
    d = os.environ.get("EVENT_DATA", "")
    if not d or not os.path.isdir(d):
        return None
    out = set()
    for name in os.listdir(d):
        if re.fullmatch(r"\d{8}-games\d+\.csv", name):
            try:
                head, rows = rows_as_dicts(read_csv(os.path.join(d, name)))
            except Exception:
                continue
            out.update((r.get("牌譜リンク") or "").strip() for r in rows)
    out.discard("")
    return out


def previous_links(path):
    """このファイルを最後に変えたコミットの、ひとつ前の版にあった対局(牌譜リンク、非表示を除く)。前の版が無ければ None"""
    c = (git("log", "-1", "--format=%H", "--", path) or "").strip()
    text = git("show", f"{c}^:{path}") if c else None
    if text is None:
        return None
    head, rows = rows_as_dicts(list(csv.reader(io.StringIO(text.lstrip("\ufeff")))))
    return {(r.get("牌譜リンク") or "").strip() for r in rows if (r.get("非表示状態") or "").strip().lower() != "yes"} - {""}


SPLIT_STOP = 10   # 分割CSV(splitdata-download)は、分割済みの対局がこの数以上なくなった月を分割しない


def check_personal(paths):
    problems, info = [], {}
    team = team_links()
    if team is None:
        print("::warning::event-data が見つからないため、チーム戦の対局が混ざっていないかは確かめない")
    links = defaultdict(list)
    games_by, splits = {}, {}
    for p in paths:
        if not p.lower().endswith(".csv"):
            continue
        if P_SUMM.match(p) and not p.startswith("summary/"):
            problems.append((p, "詳細成績のもとは summary フォルダに置くことになっている(このままでも読まれる)"))
        elif not (P_MONTH.match(p) or P_SUMM.match(p) or p == "ban.csv"):
            problems.append((p, "名前がルールと違うため、ランキングでは読み込まれない(例: 202610.csv)"))
    for p in sorted(paths):
        m = P_MONTH.match(p)
        if not m:
            continue
        ym = m.group(1) + m.group(2)
        try:
            splits[ym] = split_numbers(p, ym)
        except Exception:
            splits[ym] = ({}, {})
        by_row = splits[ym][0]
        gs = read_games(p, problems, where=lambda rows, b=by_row: split_note(b.get(n) for n in rows))
        games_by[ym] = gs
        info[p] = {"games": len(gs)}
        off = [g for g in gs if re.sub(r"\D", "", g["start"])[:6] != ym]
        if off:
            problems.append((p, f"開始時間がこの月ではない対局が{len(off)}件ある({'、'.join(str(g['row']) for g in off[:8])}行目){split_note(by_row.get(g['row']) for g in off)}"))
        for g in gs:
            if g["link"]:
                links[g["link"]].append(p)
        # チーム戦の対局記録を誤って上げていないか
        if team:
            mixed = [g for g in gs if g["link"] in team]
            if mixed:
                whole = len(mixed) == len(gs)
                problems.append((p, (f"中身がすべてチーム戦の対局({len(gs)}対局)。チーム戦の対局記録を誤って上げた可能性が高い。"
                                     if whole else f"チーム戦の対局が{len(mixed)}対局入っている。") +
                                 "ランキングにそのまま出るので、正しいCSVで上書きするか「History」から前の版に戻す。分割CSVには入らない"))
        # 前の版にあった対局がなくなっていないか(月のCSVは、毎回その月の全対局を入れて上書きするので、ふつうは減らない)
        prev = previous_links(p)
        if prev:
            now = {g["link"] for g in gs}
            gone = prev - now - (team or set())
            if gone:
                stop = f"分割CSVは、正しいCSVに戻るまでこの月を分割しない。" if len(gone) >= SPLIT_STOP else ""
                problems.append((p, f"前の版にあった対局のうち{len(gone)}対局が、今回の版にない(前の版は{len(prev)}対局、今回は{len(gs)}対局)。"
                                    f"別の月やチーム戦のCSVを上げていないか確かめる。{stop}"))
    multi = defaultdict(list)
    for link, v in links.items():
        if len(v) > 1:
            multi[tuple(v)].append(link)
    for files, ls in multi.items():
        labels = [splits.get(P_MONTH.match(f).group(1) + P_MONTH.match(f).group(2), ({}, {}))[1].get(l) for f in files for l in ls]
        problems.append((files[-1], f"{'・'.join(files)} の両方に同じ対局が{len(ls)}件ある(通算では1回だけ数える){split_note(labels)}"))
    for p in paths:
        m = P_SUMM.match(p)
        if m:
            if m.group(1) not in games_by:
                problems.append((p, f"対応する対局記録({m.group(1)}.csv)がない"))
            else:
                bl = splits.get(m.group(1), ({}, {}))[1]
                check_summary(p, games_by[m.group(1)], problems, where_links=lambda ls, bl=bl: split_note(bl.get(l) for l in ls))
    if "ban.csv" in paths:
        bad = []
        for n, r in enumerate(read_csv("ban.csv"), start=1):
            if not any(c.strip() for c in r):
                continue
            a, b = (r[0] if r else "").strip(), (r[1] if len(r) > 1 else "").strip()
            okid = re.search(r"\[(\d+)\]", a) or re.fullmatch(r"\d+", a)
            okym = re.fullmatch(r"\d{4}(0[1-9]|1[0-2])", b)
            if n == 1 and not (okid and okym):
                continue                                  # 見出し行
            if not (okid and okym):
                bad.append(n)
        if bad:
            problems.append(("ban.csv", f"読めない行がある({'、'.join(map(str, bad[:8]))}行目)。1列目は [JP][ID]名前 かID、2列目は YYYYMM"))
    return problems, info


# ---------- 本体 ----------
def main():
    paths = sorted(p for p in (git("ls-files") or "").splitlines() if p and not p.startswith(".github/"))
    problems, info = (check_team if KIND == "team" else check_personal)(paths)

    files = {}
    for p in paths:
        if not p.lower().endswith(".csv"):
            continue
        ts = (git("log", "-1", "--format=%ct", "--", p) or "").strip()      # 最後にこのファイルを変えたコミットの日時(UTC にそろえる)
        t = datetime.datetime.fromtimestamp(int(ts), datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if ts.isdigit() else ""
        files[p] = {"updated": t, "size": os.path.getsize(p), **info.get(p, {})}
    latest = max((v["updated"] for v in files.values() if v["updated"]), default="")
    index = {"latest": latest, "files": files}
    old = None
    try:
        with open("index.json", encoding="utf-8") as f:
            old = json.load(f)
    except Exception:
        pass
    if not old or {k: v for k, v in old.items() if k != "generated"} != index:
        index = {"generated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), **index}
        with open("index.json", "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False, indent=1)
            f.write("\n")
        print("index.json を更新した")
    else:
        print("index.json は変わらない")

    # 今回の更新に関係するものだけ知らせる(手動実行のときは全部)
    manual = os.environ.get("EVENT") == "workflow_dispatch"
    if not manual:
        before, after = os.environ.get("BEFORE") or "", os.environ.get("AFTER") or "HEAD"
        if not before or before == ZERO or git("cat-file", "-e", before + "^{commit}") is None:
            before = (git("rev-parse", after + "^") or "").strip() or None
        changed = set((git("diff", "--name-only", before, after) or "").splitlines()) if before else set(paths)
        if KIND == "team":
            ckeys = {team_key(p) for p in changed} - {None}
            problems = [x for x in problems if x[0] in changed or team_key(x[0]) in ckeys]
        else:
            problems = [x for x in problems if x[0] in changed]
    for p, msg in problems:
        print(f"::warning file={p}::{msg}")

    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a", encoding="utf-8") as f:
            f.write("## データの点検\n\n" + ("\n".join(f"- `{p}` … {m}" for p, m in problems) if problems else "問題なし") + "\n")

    real = [x for x in problems if not x[1].startswith(INFO)]
    if not real and not manual:
        if problems:
            print("お知らせだけなので、Discord には送らない")
        return
    title = "チーム戦" if KIND == "team" else "個人戦"
    if real:
        lines = [f"⚠️ **{title}のデータの点検で、気になる点が見つかりました**"]
        problems = real + [x for x in problems if x[1].startswith(INFO)]
        for p, m in problems[:15]:
            lines.append(f"・`{p}`:{m}")
        if len(problems) > 15:
            lines.append(f"・ほか{len(problems) - 15}件(GitHub の Actions の画面で全部見られます)")
    else:
        lines = [f"✅ **{title}のデータの点検:問題なし**", f"CSV {len(files)}ファイルを点検しました。"]
        for p, m in problems[:15]:
            lines.append(f"・`{p}`:{m}")
    content = "\n".join(lines)[:1900]
    print(content)
    if os.environ.get("DRY_RUN"):
        return
    url = (os.environ.get("DISCORD_ADMIN_WEBHOOK_URL") or os.environ.get("DISCORD_WEBHOOK_URL") or "").strip()
    if not url:
        print("Discord のウェブフックが未設定のため、送信しない。")
        return
    body = json.dumps({"content": content, "allowed_mentions": {"parse": []}}).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "User-Agent": "monoqlo-data-check (1.0)"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            print("送信しました:", r.status)
    except urllib.error.HTTPError as e:
        print("Discordへの送信に失敗:", e.code, e.read().decode("utf-8", "replace")[:300])


if __name__ == "__main__":
    main()
