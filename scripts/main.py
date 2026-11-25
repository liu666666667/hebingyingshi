#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hebingyingshi 每日汇总主程序。
流程：抓四可信源+yingshi发现 -> 线路去重 -> 抓取站点 -> 过滤/去广告 -> 站点去重
     -> 健康检测/失效隔离 -> 分层合并(trusted优先) -> 产出稳定版/全量版/多仓/直播/变更/状态页。
用法：python scripts/main.py [--skip-probe] [--repo USER/hebingyingshi]
"""
import argparse
import datetime
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import now_str, TZ
import pipeline as P

ROOT = P.ROOT
CONFIG, DATA, OUTPUT = P.CONFIG, P.DATA, P.OUTPUT


def log(*a):
    print("[HB]", *a, flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-probe", action="store_true", help="跳过在线探活（调试/离线）")
    ap.add_argument("--repo", default="liu666666667/hebingyingshi")
    args = ap.parse_args()

    settings = P.load_json(os.path.join(CONFIG, "settings.json"), {})
    sources = P.load_json(os.path.join(CONFIG, "sources.json"), {})
    discovery = P.load_json(os.path.join(CONFIG, "discovery.json"), {})
    rules = P.load_json(os.path.join(CONFIG, "rules.json"), {})
    guard = P.Guard(discovery, rules)
    state = P.load_state()

    fail_limit = settings.get("fail_limit", 7)
    revive_days = settings.get("revive_days", 7)
    fb_max = settings.get("snapshot_fallback_max_days", 1)
    drop_alert = settings.get("drop_alert_pct", 30)
    cap = settings.get("max_sites_single", 1500)
    probe_on = settings.get("probe_enabled", True) and not args.skip_probe
    repo = args.repo

    # ---- 1. 抓清单 ----
    all_lines = []
    fetch_errors = []
    tier_map = {}  # line_key -> tier

    for group, tier in (("trusted", "trusted"), ("discovered", "discovered")):
        for spec in sources.get(group, []):
            log("抓取清单:", spec.get("name"))
            lines, errs = P.fetch_manifest(spec)
            fetch_errors.extend(errs)
            log("  -> %d 条 %s" % (len(lines), ("(失败:%s)" % ";".join(errs)) if errs else ""))
            for ln in lines:
                ln["tier"] = tier
                ln["origin"] = spec.get("id", "")
                all_lines.append(ln)

    # 上游拉取失败兜底：若某组整组为空，用最近快照
    if not any(l.get("tier") == "trusted" for l in all_lines):
        log("可信源全空，尝试快照兜底...")
        prev = None
        for d in range(1, fb_max + 1):
            prev = P.read_prev_snapshot(d)
            if prev:
                log("  使用 %d 天前快照 (%d 条)" % (d, len(prev)))
                all_lines.extend(prev)
                break
        if not prev:
            log("  无可用快照，本轮以空清单继续（产出保留上版）")

    # 线路级去重（合并镜像）
    all_lines = P.dedup_lines(all_lines)
    log("线路去重后:", len(all_lines))

    # 保存今日快照
    today = P.save_snapshot(all_lines)
    log("快照已存:", today)

    # ---- 2. 抓站点内容（只抓可信源全部 + discovered 中未禁用的） ----
    def load_sites(ln):
        sp, sites, ok = P.fetch_line_sites(ln)
        return ln, sp, sites, ok

    # 限制并发，避免打爆网络
    workers = 10
    line_data = {}  # key -> {spider, sites, ok, reason}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(load_sites, ln): ln for ln in all_lines}
        for f in as_completed(futs):
            ln, sp, sites, ok = f.result()
            k = P.line_key(ln.get("urls"))
            reason = guard.source_reason(ln, sp, sites) if ok else ("unreachable" if not ok else None)
            line_data[k] = {"line": ln, "spider": sp, "sites": sites, "ok": ok, "reason": reason}

    # ---- 3. 健康检测 + 失效淘汰（按历史 fail_streak） ----
    prev_state_lines = state["lines"]
    active_lines = []
    disabled_now = []
    for k, ld in line_data.items():
        ln = ld["line"]
        st = prev_state_lines.get(k, {"fail": 0, "ok_days": 0, "last_ok": None, "disabled": False})
        # 禁用特征（登录/GO/图片spider）命中 -> 立即淘汰，无缓冲
        if ld["reason"] in ("login", "go-proxy", "image-spider"):
            st["disabled"] = True
            st["reason"] = ld["reason"]
            disabled_now.append((k, ld["reason"]))
            state["lines"][k] = st
            continue
        revive_ok = True
        if st.get("disabled") and ld["reason"] != "unreachable" and ld["ok"]:
            # 尝试复活：距上次禁用超过 revive_days
            last = st.get("last_ok")
            if last:
                try:
                    d0 = datetime.datetime.strptime(last[:10], "%Y-%m-%d").date()
                    if (datetime.date.today() - d0).days < revive_days:
                        revive_ok = False
                except Exception:
                    pass
        if st.get("disabled") and not revive_ok:
            state["lines"][k] = st
            continue
        if ld["ok"]:
            st["fail"] = 0
            st["ok_days"] = st.get("ok_days", 0) + 1
            st["last_ok"] = today
            st["disabled"] = False
            st.pop("reason", None)
            active_lines.append((k, ld, st))
        else:
            st["fail"] = st.get("fail", 0) + 1
            if st["fail"] >= fail_limit:
                st["disabled"] = True
                st["reason"] = "fail_streak"
                disabled_now.append((k, "fail_streak"))
            state["lines"][k] = st
        # 人工禁用（unavailable 里手动标记）保持禁用
        state["lines"][k] = st

    log("过滤禁用:", len(disabled_now), "| 在线可用线路:", len(active_lines))

    # ---- 4. 站点级过滤 + 去重（trusted 优先） ----
    trusted_sites, discovered_sites = [], []
    isolated_sites = []   # 成人/误判 -> 待定隔离源
    blocked_count = 0

    # 排序：trusted 在前
    active_lines.sort(key=lambda x: (0 if x[1]["line"].get("tier") == "trusted" else 1))

    for k, ld, st in active_lines:
        bucket = trusted_sites if ld["line"].get("tier") == "trusted" else discovered_sites
        for site in ld["sites"]:
            act = guard.site_action(site)
            if act == "drop":
                blocked_count += 1
            elif act == "isolate":
                isolated_sites.append(site)
            else:
                s = dict(site)
                s["_from"] = ld["line"].get("origin", "")
                bucket.append(s)

    # 分层合并：trusted 全收，discovered 只补 trusted 没有的站点
    trusted_sites = P.dedup_sites(trusted_sites)
    trusted_keys = set(P.site_key(s) for s in trusted_sites)
    disc_fresh = [s for s in P.dedup_sites(discovered_sites) if P.site_key(s) not in trusted_keys]
    all_sites = (trusted_sites + disc_fresh)[:cap]
    isolated_sites = P.dedup_sites(isolated_sites)

    log("站点: trusted=%d discovered补位=%d 合计=%d | 隔离(待定)=%d | 拦截广告有害=%d" %
        (len(trusted_sites), len(disc_fresh), len(all_sites), len(isolated_sites), blocked_count))

    # spider 取出现最多的正规引擎
    spider_votes = {}
    for k, ld, st in active_lines:
        sp = guard.clean_spider(ld["spider"])
        spider_votes[sp] = spider_votes.get(sp, 0) + 1
    top_spider = max(spider_votes, key=spider_votes.get) if spider_votes else guard.default_spider

    # ---- 5. 产出 ----
    # 全量版 single
    final = {"sites": all_sites, "spider": top_spider}
    P.save_json(os.path.join(OUTPUT, "final.json"), final)

    # 稳定版：首日全量可用；之后只放 ok_days>=stable_min_days 或 trusted 来源的站点
    stable_min = settings.get("stable_min_days", 3)
    first_run = not state.get("seen", {}).get("initialized")
    if first_run:
        stable_sites = all_sites
        state["seen"]["initialized"] = True
        state["seen"]["init_date"] = today
    else:
        # 按线路 ok_days 判断：稳定线路的全部站点进稳定版
        stable_line_keys = set(k for k, ld, st in active_lines
                               if st.get("ok_days", 0) >= stable_min or ld["line"].get("tier") == "trusted")
        stable_sites = []
        seen_sk = set()
        for k, ld, st in active_lines:
            if k not in stable_line_keys:
                continue
            for s in ld["sites"]:
                if guard.site_action(s) != "keep":
                    continue
                sk = P.site_key(s)
                if not sk or sk in seen_sk:
                    continue
                seen_sk.add(sk)
                stable_sites.append(s)
            if len(stable_sites) >= cap:
                break
    P.save_json(os.path.join(OUTPUT, "stable.json"), {"sites": stable_sites, "spider": top_spider})

    # 多仓订阅
    multi_urls = []
    for k, ld, st in active_lines:
        for u in ld["line"].get("urls", [])[:1]:
            if u not in multi_urls:
                multi_urls.append(u)
    P.save_json(os.path.join(OUTPUT, "multi.json"), {"urls": multi_urls})

    # 待定隔离源（人工判断，不进公开入口）
    P.save_json(os.path.join(DATA, "unavailable", "isolated_sites.json"), isolated_sites)

    # 不可用线路留档
    unavail_list = []
    for k, st in state["lines"].items():
        if st.get("disabled"):
            ld = line_data.get(k, {})
            unavail_list.append({"key": k, "name": ld.get("line", {}).get("name", ""),
                                 "reason": st.get("reason"), "last_ok": st.get("last_ok")})
    P.save_json(os.path.join(DATA, "unavailable", "disabled_lines.json"), unavail_list)

    # 直播：抓直播源 -> 去重 -> 探活剔除失效
    live_lines = []
    for spec in sources.get("live", []):
        log("抓直播清单:", spec.get("name"))
        lines, _ = P.fetch_manifest(spec)
        live_lines.extend(lines)
    live_lines = P.dedup_lines(live_lines)
    live_out = collect_live_m3u(live_lines, probe_on, settings)
    with open(os.path.join(OUTPUT, "live.m3u"), "w", encoding="utf-8") as f:
        f.write("\n".join(live_out) + "\n")
    log("直播频道:", len([x for x in live_out if x.startswith("#EXTINF")]))

    # ---- 6. 变更记录 ----
    prev_sites = state.get("seen", {}).get("site_keys", [])
    cur_keys = set(P.site_key(s) for s in all_sites)
    prev_keys = set(prev_sites)
    added = len(cur_keys - prev_keys)
    removed = len(prev_keys - cur_keys)
    revived = sum(1 for k, st in state["lines"].items() if st.get("disabled") is False and st.get("revived_flag"))
    P.save_json(os.path.join(OUTPUT, "changelog_data.json"), {
        "date": today, "added": added, "removed": removed, "revived": revived,
        "isolated": len(isolated_sites), "disabled": len(disabled_now),
        "total_sites": len(all_sites), "stable_sites": len(stable_sites)
    })
    write_changelog(today, added, removed, revived, len(isolated_sites), len(disabled_now), len(all_sites))
    state["seen"]["site_keys"] = list(cur_keys)

    # ---- 7. 状态页数据 + 骤降告警 ----
    prev_total = state.get("seen", {}).get("site_count", len(all_sites))
    drop_pct = ((prev_total - len(all_sites)) / prev_total * 100) if prev_total else 0
    status = {
        "updated_at": now_str(),
        "repo": repo,
        "stats": {
            "total_sites": len(all_sites),
            "stable_sites": len(stable_sites),
            "trusted_lines": sum(1 for k, ld, st in active_lines if ld["line"].get("tier") == "trusted"),
            "discovered_lines": sum(1 for k, ld, st in active_lines if ld["line"].get("tier") == "discovered"),
            "blocked": blocked_count,
            "isolated": len(isolated_sites),
            "disabled": len(unavail_list),
            "live_channels": len([x for x in live_out if x.startswith("#EXTINF")]),
            "drop_pct": round(drop_pct, 1),
            "drop_alert": drop_pct >= drop_alert,
            "first_run": first_run,
        },
        "fetch_errors": fetch_errors[:20],
        "lines": [
            {"name": ld["line"].get("name", ""), "tier": ld["line"].get("tier", ""),
             "origin": ld["line"].get("origin", ""), "ok_days": st.get("ok_days", 0),
             "sites": len(ld["sites"]), "url": (ld["line"].get("urls") or [""])[0]}
            for k, ld, st in active_lines
        ],
        "changelog": P.load_json(os.path.join(OUTPUT, "changelog_data.json"), {}),
    }
    P.save_json(os.path.join(OUTPUT, "status.json"), status)
    state["seen"]["site_count"] = len(all_sites)
    log("状态: 站点%d 稳定%d 拦截%d 隔离%d 失效%d 骤降%.1f%%" %
        (len(all_sites), len(stable_sites), blocked_count, len(isolated_sites), len(unavail_list), drop_pct))

    # ---- 8. 订阅地址可用性自动探测 ----
    detect_subscribe_links(status, repo)

    # 复制 index.html（若存在）
    idx_src = os.path.join(ROOT, "index.html")
    if os.path.exists(idx_src):
        with open(idx_src, encoding="utf-8") as f:
            html = f.read()
        with open(os.path.join(OUTPUT, "index.html"), "w", encoding="utf-8") as f:
            f.write(html)
        with open(os.path.join(ROOT, "index.html"), "w", encoding="utf-8") as f:
            f.write(html)

    P.save_state(state)
    log("完成:", now_str())


def collect_live_m3u(live_lines, probe_on, settings):
    """抓直播源 m3u/txt，去重（按频道名+URL），探活剔除失效。"""
    from common import fetch_text_with_fallback, http_get
    seen, channels = set(), []
    for ln in live_lines:
        for u in (ln.get("urls") or [])[:3]:
            try:
                _, _, text = fetch_text_with_fallback([u], retries=0, app_ua=True)
            except Exception:
                continue
            lines = text.splitlines()
            i = 0
            while i < len(lines):
                line = lines[i].strip()
                if line.startswith("#EXTINF"):
                    name = line.split(",", 1)[-1].strip() if "," in line else line
                    url = lines[i + 1].strip() if i + 1 < len(lines) else ""
                    i += 2
                    # 过滤广告/博彩频道
                    low = name.lower()
                    if any(w in name for w in ("广告", "推广", "测试", "付费", "扫码", "购物", "卖药", "客服", "彩票", "棋牌")):
                        continue
                    if any(w in low for w in ("cctv", "卫视", "央视")):
                        pass
                    key = name + "|" + P.norm_url(url)
                    if not url or key in seen:
                        continue
                    seen.add(key)
                    channels.append((name, url, line))
                else:
                    i += 1
            break
    # 探活（可选）：并发 HEAD/GET 小范围，剔除明显失效
    if probe_on and channels:
        def alive(item):
            name, url, _ = item
            if not url.startswith("http"):
                return True  # 非 http 不动
            try:
                http_get(url, timeout=5, max_bytes=2048)
                return True
            except Exception:
                return False
        good = []
        with ThreadPoolExecutor(max_workers=16) as ex:
            futs = {ex.submit(alive, c): c for c in channels}
            for f in as_completed(futs):
                if f.result():
                    good.append(futs[f])
        channels = good
    out = ["#EXTM3U"]
    for name, url, infoline in channels:
        if not infoline.startswith("#EXTINF"):
            infoline = "#EXTINF:-1," + name
        out.append(infoline)
        out.append(url)
    return out


def detect_subscribe_links(status, repo):
    """生成订阅地址候选并探测可达性。首次部署文件尚未生效时探测会失败，
    但地址本身有效（提交后即生效），所以全部给出，仅用 reachable 标记状态。"""
    from common import http_get
    base = "https://%s.github.io/%s" % (repo.split("/")[0], repo.split("/")[1])
    candidates = {
        "stable_pages": base + "/output/stable.json",
        "final_pages": base + "/output/final.json",
        "stable_raw": "https://raw.githubusercontent.com/%s/main/output/stable.json" % repo,
        "live_pages": base + "/output/live.m3u",
    }
    links = {}
    reach = {}
    for key, url in candidates.items():
        ok = False
        try:
            http_get(url, timeout=5, max_bytes=2048)
            ok = True
        except Exception:
            pass
        links[key] = url
        reach[key] = ok
    status["subscribe_links"] = links
    status["subscribe_reachable"] = reach


def write_changelog(date, added, removed, revived, isolated, disabled, total):
    lines = [
        "# 每日变更记录", "",
        "更新时间：%s" % date, "",
        "- 新增站点：%d" % added,
        "- 移除站点：%d" % removed,
        "- 复活源：%d" % revived,
        "- 隔离（待定/误判）：%d" % isolated,
        "- 禁用线路：%d" % disabled,
        "- 当前站点总数：%d" % total, "",
        "> 隔离源存于 data/unavailable/isolated_sites.json，不进任何公开订阅，供人工判断。",
    ]
    with open(os.path.join(OUTPUT, "changelog.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    # 追加历史
    hist = os.path.join(OUTPUT, "changelog_history.md")
    with open(hist, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n\n---\n\n")


if __name__ == "__main__":
    main()
