# -*- coding: utf-8 -*-
"""增量分类: 已有一轮完整分类后, 库里新增/移除/重装了游戏, 只处理差异部分。
总表 step2/preclassification.csv 是"已分类基准"的唯一来源, 不另设 state 文件。

用法 (在 skill 根目录运行):
  python scripts/incremental.py detect [--library 路径] [--table 路径] [appid ...]
      -> step5/diff_report.txt + step5/new_rows.csv (待处理款, 新增款 B/C/D/E 为空)
      位置参数 appid: 额外强制重分类的款 (上次待确认的/用户点名的), 会带上现有分类值
  python scripts/incremental.py fetch-info [--rows step5/new_rows.csv]
      -> 增量抓 api.steamcmd.net 元数据 (缓存 data/info.json), 回填 判断依据/标签 列
  python scripts/incremental.py prepare [--size 25]
      -> step5/batches/batch_NNN.txt + manifest.json (子代理协议同 references/methods-review.md)
  python scripts/incremental.py merge
      -> 修复并汇总 step5/results/batch_*.csv
      -> step5/preclassification_new.csv + pending_confirmation_new.csv + change_report.txt
  python scripts/incremental.py apply [--dry-run] [--prune] [--new 路径]
      -> 备份后合并进总表; 同步 installed 列并对变为未安装的行清空 A 状态
      --prune 移除已不在库中的行 (默认保留仅报告, 留在收藏集里无害)
"""
import csv, json, os, re, shutil, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
STEP1 = os.path.join(ROOT, "step1")
STEP2 = os.path.join(ROOT, "step2")
STEP5 = os.path.join(ROOT, "step5")
COLS = ["appid", "name", "installed", "A_status", "B_series", "C_types", "D_vendors", "E_other",
        "判断依据", "标签", "查询来源", "是否确定"]

def P(*p): return os.path.join(ROOT, *p)

def arg_val(flag, default=None):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv): return sys.argv[i + 1]
    return default

def read_csv(p):
    if not os.path.exists(p): return []
    return list(csv.DictReader(open(p, encoding="utf-8-sig")))

def dump(path, rows, cols=COLS):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as fp:
        w = csv.DictWriter(fp, fieldnames=cols); w.writeheader()
        for r in rows: w.writerow({k: r.get(k, "") for k in cols})

def lib_path():
    p = arg_val("--library")
    if p:
        if os.path.exists(p): return p
        print("ERROR: 找不到库存 CSV:", p); sys.exit(2)
    for c in (os.path.join(STEP1, "steam_library.csv"), P("steam_library.csv")):
        if os.path.exists(c): return c
    print("ERROR: 找不到 step1/steam_library.csv (先跑 scripts/fetch_library.py)"); sys.exit(2)

def table_path():
    p = arg_val("--table")
    if p:
        if os.path.exists(p): return p
        print("ERROR: 找不到分类表:", p); sys.exit(2)
    c = os.path.join(STEP2, "preclassification.csv")
    if os.path.exists(c): return c
    print("ERROR: 找不到 step2/preclassification.csv (先完成一轮步骤2)"); sys.exit(2)

def parse_basis(basis):
    typ = dev = pub = ""
    for part in (basis or "").split("|"):
        part = part.strip()
        if part.startswith("type="): typ = part[5:].strip()
        elif part.startswith("dev="): dev = part[4:].strip()
        elif part.startswith("pub="): pub = part[4:].strip()
    return typ, dev, pub

# ---------------------------------------------------------------- detect

def cmd_detect():
    lib = read_csv(lib_path()); tab = read_csv(table_path())
    libmap = {r["appid"]: r for r in lib}
    tabmap = {r["appid"]: r for r in tab}
    cur_ids = [r["appid"] for r in lib]
    forced = [a for a in sys.argv[1:] if a.isdigit()]
    ghost = [a for a in forced if a not in libmap]
    if ghost: print("WARNING: 指定的 appid 不在当前库存中, 忽略:", " ".join(ghost))
    new_ids = [a for a in cur_ids if a not in tabmap]
    forced_in_lib = [a for a in forced if a in libmap]
    removed = [a for a in tabmap if a not in libmap]
    inst_chg = [a for a in cur_ids if a in tabmap and a not in forced_in_lib and
                libmap[a].get("installed", "").lower() != (tabmap[a].get("installed") or "").lower()]

    targets = new_ids + forced_in_lib
    rows = []
    for a in targets:
        src = tabmap.get(a) or {}
        o = {k: src.get(k, "") for k in COLS}
        o["appid"] = a
        o["name"] = libmap[a].get("name", src.get("name", "")).strip()
        o["installed"] = libmap[a].get("installed", "").lower()
        rows.append(o)
    dump(os.path.join(STEP5, "new_rows.csv"), rows)
    open(os.path.join(STEP5, "new_appids.txt"), "w", encoding="utf-8").write("\n".join(targets))

    L = ["增量差异报告", "时间: %s" % time.strftime("%Y-%m-%d %H:%M:%S"),
         "当前库存 %d 款 | 分类总表 %d 款" % (len(cur_ids), len(tabmap)), ""]
    L.append("新增 (%d):" % len(new_ids))
    for a in new_ids: L.append("  + %s  %s  (installed=%s)" % (a, libmap[a].get("name"), libmap[a].get("installed")))
    L.append("")
    L.append("从库中移除 (%d) [默认保留在总表, apply --prune 才删除]:" % len(removed))
    for a in removed: L.append("  - %s  %s" % (a, tabmap[a].get("name")))
    L.append("")
    L.append("安装状态变化 (%d) [apply 会同步 installed 列, 变为 false 的清空 A 状态]:" % len(inst_chg))
    for a in inst_chg:
        L.append("  * %s  %s  %s -> %s" % (a, libmap[a].get("name"),
                 (tabmap[a].get("installed") or "-").lower(), libmap[a].get("installed", "").lower()))
    L.append("")
    L.append("强制重分类 (%d):" % len(forced_in_lib))
    for a in forced_in_lib: L.append("  ! %s  %s" % (a, libmap[a].get("name")))
    L.append("")
    L.append("待处理 %d 款 -> step5/new_rows.csv" % len(rows))
    L.append("下一步: 新装款问用户 A 状态 -> fetch-info -> 分类 (prepare/子代理或主会话) -> merge -> 向用户汇报 -> apply")
    open(os.path.join(STEP5, "diff_report.txt"), "w", encoding="utf-8").write("\n".join(L) + "\n")
    print("新增 %d | 移除 %d | 安装变化 %d | 强制重分类 %d -> step5/diff_report.txt" %
          (len(new_ids), len(removed), len(inst_chg), len(forced_in_lib)))
    if new_ids:
        need_a = [a for a in new_ids if libmap[a].get("installed", "").lower() == "true"]
        if need_a:
            print("注意: %d 款新装游戏需要用户指定 A 状态 (或按 categories.md 默认档填写)" % len(need_a))

# ---------------------------------------------------------------- fetch-info

def load_cfg():
    c = {}
    f = os.path.join(BASE, "local_config.json")
    if os.path.exists(f):
        try: c.update(json.load(open(f, encoding="utf-8")))
        except Exception as e: print("WARNING: 配置解析失败(%s), 用环境变量" % e)
    for k, e in (("proxy", "STEAM_PROXY"),):
        if os.environ.get(e): c[k] = os.environ[e]
    return c

CACHE = P("data", "info.json")
TAGMAP = P("data", "steam_tags_map.json")

def load_tag_map(proxy):
    """Steam 标签 id->name 映射 (~430 条, 首次自动抓取后缓存)。失败时返回空表, 标签列保留数字 ID。"""
    if os.path.exists(TAGMAP):
        try:
            return {str(x.get("tagid")): x.get("name") for x in json.load(open(TAGMAP, encoding="utf-8"))
                    if isinstance(x, dict)}
        except Exception: pass
    url = "https://store.steampowered.com/tagdata/populartags/english"
    for _ in range(3):
        try:
            r = requests.get(url, proxies=({"http": proxy, "https": proxy} if proxy else None),
                             headers={"User-Agent": "Mozilla/5.0"}, timeout=25)
            if r.status_code == 200:
                data = r.json()
                os.makedirs(os.path.dirname(TAGMAP), exist_ok=True)
                json.dump(data, open(TAGMAP, "w", encoding="utf-8"), ensure_ascii=False)
                m = {str(x.get("tagid")): x.get("name") for x in data if isinstance(x, dict)}
                print("标签映射已抓取: %d 条 -> %s" % (len(m), TAGMAP))
                return m
        except Exception:
            time.sleep(3)
    print("WARNING: 标签映射获取失败, 标签列保留数字 ID (不影响分类, 可按名称另行搜索)")
    return {}

def load_cache():
    if os.path.exists(CACHE):
        try: return json.load(open(CACHE, encoding="utf-8"))
        except Exception: pass
    return {}

def save_cache(d):
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    t = CACHE + ".tmp"
    json.dump(d, open(t, "w", encoding="utf-8"), ensure_ascii=False)
    os.replace(t, CACHE)

def fetch_one(appid, proxy, tries=4):
    s = requests.Session()
    s.headers.update({"User-Agent": "steam-collection-incremental/1.0"})
    if proxy: s.proxies = {"http": proxy, "https": proxy}
    s.trust_env = False
    for i in range(tries):
        try:
            r = s.get("https://api.steamcmd.net/v1/info/%s" % appid, timeout=30)
            if r.status_code == 200:
                d = (r.json().get("data") or {}).get(str(appid)) or {}
                if not d: return {"_missing": True}
                c = d.get("common") or {}; e = d.get("extended") or {}
                def _s(v):
                    if isinstance(v, list): return ", ".join(str(x) for x in v)
                    return v
                return {"name": c.get("name"), "type": c.get("type"),
                        "developer": _s(e.get("developer")), "publisher": _s(e.get("publisher")),
                        "genres": list((c.get("genres") or {}).values()),
                        "store_tags": list((c.get("store_tags") or {}).values())}
            if r.status_code in (429, 502, 503, 504):
                time.sleep(2 * (i + 1)); continue
            return {"_http": r.status_code}
        except Exception:
            time.sleep(1.5 * (i + 1))
    return None

def cmd_fetch_info():
    rows_p = arg_val("--rows", os.path.join(STEP5, "new_rows.csv"))
    rows = read_csv(rows_p)
    if not rows:
        print("ERROR: 找不到或为空:", rows_p, "(先跑 detect)"); sys.exit(2)
    proxy = load_cfg().get("proxy", "")
    info = load_cache()
    todo = [r["appid"] for r in rows if r["appid"] not in info or info.get(r["appid"]) is None]
    print("待抓 %d, 缓存命中 %d" % (len(todo), len(rows) - len(todo)), flush=True)
    done = 0
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(fetch_one, a, proxy): a for a in todo}
        for f in as_completed(futs):
            a = futs[f]
            try: res = f.result()
            except Exception: res = None
            if res is not None: info[a] = res
            done += 1
            if done % 50 == 0: save_cache(info); print("progress %d/%d" % (done, len(todo)), flush=True)
    if todo: save_cache(info)
    tmap = load_tag_map(proxy)
    miss = []
    for r in rows:
        d = info.get(r["appid"]) or {}
        if d.get("_missing") or d.get("_http") or d == {}:
            miss.append(r["appid"]); continue
        typ, dev, pub = d.get("type") or "", d.get("developer") or "", d.get("publisher") or ""
        basis = "|".join(x for x in ("type=" + typ, "dev=" + dev, "pub=" + pub) if x.split("=", 1)[1])
        if basis: r["判断依据"] = basis
        tags = d.get("store_tags") or []
        if tags:
            ids = [str(t) for t in tags[:20]]
            r["标签"] = ";".join(tmap.get(t, t) for t in ids)
        if basis: r["查询来源"] = "元数据(steamcmd)"
    dump(rows_p, rows)
    if miss:
        print("WARNING: %d 款无元数据: %s" % (len(miss), " ".join(miss[:20])))
        print("  -> 按名称 WebSearch 分类, 仍定不了 C 的进待确认表")
    print("已回填 %s (缓存 %d 条)" % (rows_p, len(info)))

# ---------------------------------------------------------------- prepare

def cmd_prepare():
    rows = read_csv(os.path.join(STEP5, "new_rows.csv"))
    if not rows:
        print("ERROR: 找不到或为空: step5/new_rows.csv (先跑 detect)"); sys.exit(2)
    size = 25
    if "--size" in sys.argv: size = max(5, int(arg_val("--size", "25")))
    rows.sort(key=lambda r: (0 if r.get("是否确定") == "否" else 1, int(r["appid"])))
    bd = os.path.join(STEP5, "batches"); rd = os.path.join(STEP5, "results")
    os.makedirs(bd, exist_ok=True); os.makedirs(rd, exist_ok=True)
    done = {f for f in os.listdir(rd) if re.match(r"batch_\d+\.csv$", f)}
    manifest, n = [], 0
    for idx, i in enumerate(range(0, len(rows), size), 1):
        name = "batch_%03d" % idx
        chunk = rows[i:i + size]
        lines = []
        for r in chunk:
            typ, dev, pub = parse_basis(r.get("判断依据", ""))
            tags = ";".join((r.get("标签") or "").split(";")[:14])
            lines.append(" | ".join([r["appid"], r["name"], "installed=" + r.get("installed", ""),
                "A=" + (r.get("A_status") or "-"), "B=" + (r.get("B_series") or "-"),
                "C=" + (r.get("C_types") or "-"), "D=" + (r.get("D_vendors") or "-"),
                "E=" + (r.get("E_other") or "-"), "type=" + (typ or "?"),
                "dev=" + (dev or "?"), "pub=" + (pub or "?"), "标签=" + (tags or "-")]))
        open(os.path.join(bd, name + ".txt"), "w", encoding="utf-8").write("\n".join(lines) + "\n")
        manifest.append({"batch": name, "count": len(chunk), "done": name + ".csv" in done})
        n += len(chunk)
    json.dump({"generated": time.strftime("%Y-%m-%d %H:%M:%S"), "total": n, "batch_size": size,
               "batches": manifest}, open(os.path.join(bd, "manifest.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("待处理 %d 款, 批次 %d (每批 %d), 已完成 %d" % (n, len(manifest), size,
          sum(1 for b in manifest if b["done"])))

# ---------------------------------------------------------------- merge

def cmd_merge():
    sys.path.insert(0, BASE)
    import review_tools as RT
    rows = read_csv(os.path.join(STEP5, "new_rows.csv"))
    if not rows:
        print("ERROR: 找不到或为空: step5/new_rows.csv (先跑 detect)"); sys.exit(2)
    rd = os.path.join(STEP5, "results")
    fixes, bad = {}, 0
    if os.path.isdir(rd):
        for fn in sorted(f for f in os.listdir(rd) if re.match(r"batch_\d+\.csv$", f)):
            for row in RT.norm_rows(os.path.join(rd, fn)):
                if row[0] not in {x["appid"] for x in rows}: bad += 1; continue
                fixes[row[0]] = {"B": row[1], "C": row[2], "D": row[3], "E": row[4],
                                 "certain": row[5] == "是", "basis": row[6]}
    pre, pend, examples = [], [], []
    for r in rows:
        o = dict(r); f = fixes.get(r["appid"])
        if f:
            for k, col in (("B", "B_series"), ("C", "C_types"), ("D", "D_vendors"), ("E", "E_other")):
                if f[k] != (o.get(col) or "") and len(examples) < 40:
                    examples.append("  %s %s | %s: %s -> %s" % (o["appid"], o["name"][:28], col,
                        (o.get(col) or "-")[:40], f[k][:40] or "-"))
                o[col] = f[k]
            o["判断依据"] = f["basis"] or o.get("判断依据", "")
            o["查询来源"] = "增量分类"
            if "E01" in (o.get("E_other") or ""): o["A_status"] = ""
            o["是否确定"] = "是" if (f["certain"] and o["C_types"]) else "否"
        (pre if o.get("是否确定") == "是" else pend).append(o)
    dump(os.path.join(STEP5, "preclassification_new.csv"), pre)
    dump(os.path.join(STEP5, "pending_confirmation_new.csv"), pend, COLS + ["待确认原因"])
    open(os.path.join(STEP5, "change_report.txt"), "w", encoding="utf-8").write("\n".join(
        ["增量分类汇总", "时间: %s" % time.strftime("%Y-%m-%d %H:%M:%S"),
         "待处理 %d | 有结果 %d | 确定 %d | 待确认 %d" % (len(rows), len(fixes), len(pre), len(pend)),
         "", "分类结果:"] +
        ["  %s  %s | A=%s B=%s C=%s D=%s E=%s" % (r["appid"], r["name"][:34], r.get("A_status") or "-",
            r.get("B_series") or "-", r.get("C_types") or "-", r.get("D_vendors") or "-",
            r.get("E_other") or "-") for r in pre] +
        ["", "变化样例:"] + examples +
        ["", "待确认 (%d):" % len(pend)] +
        ["  ? %s  %s" % (r["appid"], r["name"]) for r in pend]) + "\n")
    print("结果 %d/%d | 确定 %d 待确认 %d" % (len(fixes), len(rows), len(pre), len(pend)))
    if bad: print("跳过未知 appid %d 行" % bad)
    print("下一步: 向用户汇报 step5/change_report.txt, 确认后 apply")

# ---------------------------------------------------------------- apply

def cmd_apply():
    dry = "--dry-run" in sys.argv; prune = "--prune" in sys.argv
    src = arg_val("--new", os.path.join(STEP5, "preclassification_new.csv"))
    if not os.path.exists(src):
        print("ERROR: 缺少", src, "(先跑 merge)"); sys.exit(2)
    newrows = read_csv(src)
    tabp = table_path(); tab = read_csv(tabp)
    tabmap0 = {r["appid"]: r for r in tab}
    libp = lib_path(); lib = read_csv(libp); libmap = {r["appid"]: r for r in lib}

    inst_upd, cleared_a = [], []
    if lib:
        for r in tab:
            l = libmap.get(r["appid"])
            if not l: continue
            li = l.get("installed", "").lower()
            if li and li != (r.get("installed") or "").lower():
                r["installed"] = li
                inst_upd.append((r["appid"], r.get("name"), li))
                if li != "true" and r.get("A_status"):
                    cleared_a.append((r["appid"], r.get("A_status"))); r["A_status"] = ""
    pruned = [r for r in tab if r["appid"] not in libmap] if (prune and lib) else []

    newmap = {r["appid"]: r for r in newrows}
    out, replaced = [], set()
    for r in tab:
        if prune and lib and r["appid"] not in libmap: continue
        if r["appid"] in newmap:
            o = dict(newmap[r["appid"]])
            if not o.get("A_status") and r.get("A_status") and o.get("installed") == "true":
                o["A_status"] = r["A_status"]     # 新表 A 留空时保留旧表的 A
            out.append(o); replaced.add(r["appid"])
        else:
            out.append(r)
    appended = [newmap[a] for a in newmap if a not in replaced and a not in tabmap0]
    out += appended

    need_a = [(r["appid"], r.get("name")) for r in appended
              if r.get("installed") == "true" and not r.get("A_status")]
    need_a += [(a, n) for a, n, li in inst_upd
               if li == "true" and not (tabmap0.get(a) or {}).get("A_status")]
    need_a = sorted(set(need_a))

    pendp = os.path.join(STEP2, "pending_confirmation.csv")
    pend_old = read_csv(pendp)
    certain_now = {r["appid"] for r in newrows if r.get("是否确定") == "是"}
    newpend = read_csv(os.path.join(STEP5, "pending_confirmation_new.csv"))
    pend_out, seen = [], set()
    for r in list(pend_old) + list(newpend):
        if r["appid"] in certain_now or r["appid"] in seen: continue
        if prune and lib and r["appid"] not in libmap: continue
        pend_out.append(r); seen.add(r["appid"])

    print("追加 %d | 覆盖重分类 %d | installed 更新 %d | 清空 A %d | 移除 %d | 待确认表 %d -> %d%s" % (
        len(appended), len(replaced), len(inst_upd), len(cleared_a), len(pruned), len(pend_old), len(pend_out),
        " [DRY-RUN 未写入]" if dry else ""))
    for a, old, new in inst_upd[:10]: print("  installed: %s %s -> %s" % (a, old, new))
    for a, old in cleared_a[:10]: print("  清空A: %s (原 %s)" % (a, old))
    for a, n in need_a[:10]: print("  需指定A: %s %s (installed=true 但 A 为空)" % (a, n))
    if dry:
        print("DRY-RUN: 未写入。去掉 --dry-run 执行; 之后 build_collections.py -> 写前确认 -> write_steam.py")
        return
    ts = time.strftime("%Y%m%d_%H%M%S")
    bak = os.path.join(STEP2, "backup"); os.makedirs(bak, exist_ok=True)
    shutil.copy2(tabp, os.path.join(bak, "preclassification_%s.csv" % ts))
    dump(tabp, out)
    if os.path.exists(pendp) or pend_out: dump(pendp, pend_out, COLS + ["待确认原因"])
    print("已回写 %s (总 %d 行, 备份 %s)" % (tabp, len(out), os.path.join(bak, "preclassification_%s.csv" % ts)))
    print("下一步: build_collections.py -> 写前确认 -> write_steam.py")

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    {"detect": cmd_detect, "fetch-info": cmd_fetch_info, "prepare": cmd_prepare,
     "merge": cmd_merge, "apply": cmd_apply}.get(cmd, lambda: print(
        "用法: python incremental.py detect|fetch-info|prepare|merge|apply ... (详见文件头注释)"))()
