#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hebingyingshi 核心流水线：抓取四可信源+yingshi发现 -> 归一 -> 去重 -> 过滤 -> 探活 -> 分层合并。
复用 common.py（移植自用户已跑通的 yingshi）。纯标准库。"""
import datetime
import hashlib
import json
import os
import re
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (TZ, now_str, http_get, fetch_text_with_fallback,
                    parse_json_lenient, is_tvbox_config, is_multi_warehouse,
                    absolutize, _join_base, UA, APP_UAS)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "config")
DATA = os.path.join(ROOT, "data")
SNAP = os.path.join(DATA, "snapshots")
UNAVAIL = os.path.join(DATA, "unavailable")
OUTPUT = os.path.join(ROOT, "output")
STATE_FILE = os.path.join(DATA, "state.json")
for d in (DATA, SNAP, UNAVAIL, OUTPUT):
    os.makedirs(d, exist_ok=True)


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj, ensure_ascii=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=ensure_ascii, indent=2)


def norm_url(u):
    """URL 归一化：去协议、去 query、去尾斜杠、小写，用作去重键的一部分。"""
    if not isinstance(u, str):
        return ""
    u = u.strip()
    try:
        p = urllib.parse.urlsplit(u)
        host = (p.netloc or "").lower()
        path = p.path.rstrip("/")
        return host + path
    except Exception:
        return u.lower().rstrip("/")


def site_key(site):
    """站点去重键：api 归一化 + name（小写）。同一线路不同名字也算不同，但 api 相同优先合并。"""
    api = norm_url(str(site.get("api", "")))
    name = str(site.get("name", "")).strip().lower()
    return api + "|" + name if api else ("name:" + name if name else "")


def line_key(urls):
    """线路去重键：主 URL 归一化。"""
    for u in (urls or []):
        n = norm_url(u)
        if n:
            return n
    return ""


# ---------------- 源清单解析 ----------------
def extract_from_apilinks(text):
    """apilinks.txt: 每行 '名称,URL,URL...'（也兼容纯 URL 行）。返回 [{name,urls}]。"""
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "//")):
            continue
        parts = [p.strip() for p in line.split(",") if p.strip()]
        urls = [p for p in parts if re.match(r"^https?://", p)]
        if not urls:
            continue
        name = next((p for p in parts if not re.match(r"^https?://", p)), "")
        if not name:
            name = urllib.parse.urlsplit(urls[0]).netloc
        out.append({"name": name, "urls": urls})
    return out


def extract_from_ysc(text):
    """ysc-config config/sources.json: [{name,urls:[...]}] 或 {sources:[...]}。"""
    try:
        d = json.loads(text)
    except Exception:
        return []
    items = d.get("sources", []) if isinstance(d, dict) else (d if isinstance(d, list) else [])
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        urls = it.get("urls") or ([it["url"]] if it.get("url") else [])
        urls = [u for u in urls if isinstance(u, str) and u.startswith("http")]
        if urls:
            out.append({"name": it.get("name") or it.get("id") or "", "urls": urls})
    return out


def extract_from_zhuiju(text):
    """awesome-zhuiju-free resources.json: category=tvbox_config 的条目取 url。"""
    try:
        d = json.loads(text)
    except Exception:
        return []
    out = []
    for r in d.get("resources", []) if isinstance(d, dict) else []:
        if not isinstance(r, dict):
            continue
        if r.get("category") != "tvbox_config":
            continue
        u = r.get("url") or r.get("link_url")
        # access.requires_login -> 需登录的不要
        acc = r.get("access") or {}
        if acc.get("requires_login"):
            continue
        if isinstance(u, str) and u.startswith("http"):
            out.append({"name": r.get("name") or "", "urls": [u]})
    return out


def extract_from_multi(text):
    """多仓 {urls:[{url,name}|str]} 或 {sites:...}。返回单条线路（外层就是一条订阅）。"""
    d = parse_json_lenient(text) if text else None
    if not d:
        return []
    if isinstance(d, dict) and isinstance(d.get("urls"), list):
        urls = []
        for u in d["urls"]:
            if isinstance(u, str):
                urls.append(u)
            elif isinstance(u, dict) and u.get("url"):
                urls.append(u["url"])
        urls = [u for u in urls if u.startswith("http")]
        return [{"name": "", "urls": urls, "multi": True}] if urls else []
    return []


def extract_from_auto(text):
    """yingshi auto_sources.json: {sources:[{name,type,urls,...}]}。"""
    try:
        d = json.loads(text)
    except Exception:
        return []
    out = []
    for it in d.get("sources", []) if isinstance(d, dict) else []:
        if not isinstance(it, dict):
            continue
        urls = [u for u in (it.get("urls") or []) if isinstance(u, str) and u.startswith("http")]
        if urls:
            out.append({"name": it.get("name") or it.get("id") or "", "urls": urls,
                        "type": it.get("type", "single")})
    return out


def extract_from_auto_live(text):
    """yingshi auto_lives.json: {lives:[...]} 或 {sources:[...]}，元素含 url 或 urls。"""
    try:
        d = json.loads(text)
    except Exception:
        return []
    items = d.get("lives", d.get("sources", [])) if isinstance(d, dict) else []
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        urls = it.get("urls") or ([it["url"]] if it.get("url") else [])
        urls = [u for u in urls if isinstance(u, str) and (u.startswith("http") or u.endswith(".m3u"))]
        if urls:
            out.append({"name": it.get("name") or "", "urls": urls})
    return out


PARSERS = {
    "apilinks": extract_from_apilinks,
    "ysc": extract_from_ysc,
    "zhuiju": extract_from_zhuiju,
    "multi": extract_from_multi,
    "auto": extract_from_auto,
    "auto_live": extract_from_auto_live,
    "single": extract_from_auto,
}

# 三个 fork 已全量归档到本仓库 fork-archive/。清单里若有订阅指向这些 fork 的
# 成品文件（tvbox/*.json、output/*.json 等），统一改指本仓库归档副本，
# 这样 fork 仓库删除后订阅依旧可用（上游刷新也冲不掉这层重写）。
_FORK_ARCHIVE_RE = re.compile(
    r'(?:gh-proxy\.com/)?https?://(?:raw\.)?github(?:usercontent)?\.com/liu666666667/'
    r'(tvbox-api-backup|tvbox-ysc-config|awesome-zhuiju-free)/'
    r'(?:blob/|raw/|refs/heads/)?[^/]+/'
    r'(.+)'
)


def rewrite_fork_url(u):
    """把指向三个 fork 成品文件的订阅 URL 改指本仓库 fork-archive 归档副本。"""
    if not isinstance(u, str):
        return u
    m = _FORK_ARCHIVE_RE.search(u)
    if not m:
        return u
    repo, path = m.group(1), m.group(2)
    path = urllib.parse.unquote(path).split("?")[0].rstrip("/")
    enc = urllib.parse.quote(path, safe="/")
    return ("https://gh-proxy.com/https://raw.githubusercontent.com/"
            "liu666666667/hebingyingshi/main/fork-archive/%s/%s" % (repo, enc))


def rewrite_lines(lines):
    for ln in lines:
        if isinstance(ln, dict) and isinstance(ln.get("urls"), list):
            ln["urls"] = [rewrite_fork_url(u) for u in ln["urls"]]
    return lines


def fetch_manifest(spec):
    """抓一个清单源的入口文件，解析成行列表。
    策略（数据固化优先，上游删了也能永久用）：
      1) 先读本地 manifests/ 固化副本 —— 保证永远有数据；
      2) 尝试拉上游最新，成功则解析之并写回固化副本（吸收上游维护成果）；
      3) 上游连不上/被删，直接用固化副本，不报错。
    多文件源（raws + manifest 列表）逐个对应处理。
    """
    typ = spec.get("type")
    parser = PARSERS.get(typ, extract_from_auto)
    # local_path：直接读本仓库内文件（discover.py 产物），完全不联网
    lp = spec.get("local_path")
    if lp:
        p = os.path.join(CONFIG, "..", lp)
        try:
            with open(p, encoding="utf-8") as f:
                text = f.read()
            return rewrite_lines(parser(text)), []
        except Exception as e:
            return [], ["local_path %s: %s" % (lp, str(e)[:50])]
    mf = spec.get("manifest")          # 本地固化文件名（单文件源）
    mfs = spec.get("manifests")        # 本地固化文件名列表（多文件源）
    raws = spec.get("raws") or ([spec["raw"]] if spec.get("raw") else [])
    fb = spec.get("fallback_raw")
    lines = []
    errors = []

    local_files = mfs or ([mf] if mf else [])

    def read_local(path):
        p = os.path.join(CONFIG, "..", "manifests", os.path.basename(path))
        try:
            with open(p, encoding="utf-8") as f:
                return f.read()
        except Exception:
            return None

    def write_local(path, text):
        p = os.path.join(CONFIG, "..", "manifests", os.path.basename(path))
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(text)
        except Exception:
            pass

    # 缓存本次抓到的上游文本，成功即写回固化副本
    fresh = {}
    urls_to_try = list(raws) + ([fb] if fb else [])
    for raw in urls_to_try:
        try:
            _, _, text = fetch_text_with_fallback([raw], retries=1, app_ua=True)
            if text and len(text) > 20:
                fresh[raw] = text
        except Exception as e:
            errors.append("%s: %s" % (raw.split("/")[-1], str(e)[:60]))

    # 逐入口：优先用上游新文本，否则用本地固化副本
    for i, raw in enumerate(raws):
        local = local_files[i] if i < len(local_files) else (mf or raw.split("/")[-1])
        text = fresh.get(raw)
        if text:
            write_local(local, text)          # 刷新固化副本
        else:
            text = read_local(local)          # 上游断了，用固化副本
        if not text:
            # 最后兜底：fallback raw 的文本
            text = fresh.get(fb) if fb else None
        if text:
            try:
                lines.extend(rewrite_lines(parser(text)))
            except Exception as e:
                errors.append("parse %s: %s" % (os.path.basename(local), str(e)[:50]))
    return rewrite_lines(lines), errors


# ---------------- 线路内容抓取（站点级） ----------------
def fetch_line_sites(line, timeout=8):
    """抓一条线路的配置内容，返回 (spider, sites[], ok)。多仓则递归展开子仓。"""
    urls = line.get("urls") or []
    if not urls:
        return "", [], False
    spider, sites, ok = "", [], False
    for u in urls[:3]:  # 最多试 3 个镜像
        try:
            _, used, text = fetch_text_with_fallback([u], retries=0, app_ua=True)
            d = parse_json_lenient(text)
            if not d:
                continue
            if is_multi_warehouse(d):
                # 展开子仓：逐个抓，合并站点
                subs = d["urls"]
                for s in subs[:30]:
                    su = s if isinstance(s, str) else (s.get("url") if isinstance(s, dict) else None)
                    if not su:
                        continue
                    try:
                        _, _, st = fetch_text_with_fallback([su], retries=0, app_ua=True)
                        sd = parse_json_lenient(st)
                        if sd and isinstance(sd.get("sites"), list):
                            sd = absolutize(sd, su)
                            sites.extend(sd["sites"])
                            spider = spider or sd.get("spider", "")
                    except Exception:
                        continue
                ok = bool(sites)
            elif isinstance(d.get("sites"), list):
                d = absolutize(d, used)
                sites = d["sites"]
                spider = d.get("spider", "") or ""
                ok = True
            if ok:
                break
        except Exception:
            continue
    return spider, sites, ok


# ---------------- 过滤与去重 ----------------
class Guard:
    """基于 discovery.json + rules.json 的源级/站点级过滤。"""
    def __init__(self, discovery_cfg, rules_cfg):
        self.login_kw = [w for w in discovery_cfg.get("login_block_keywords", []) if w]
        self.bad_ext = tuple(discovery_cfg.get("bad_spider_exts", []))
        self.go_kw = tuple(w.lower() for w in discovery_cfg.get("go_proxy_keywords", []))
        self.adult_kw = [w.lower() for w in discovery_cfg.get("adult_keywords", [])]
        self.adult_allow = [w.lower() for w in discovery_cfg.get("adult_allow_keywords", [])]
        self.default_spider = discovery_cfg.get("default_spider", "https://raw.liucn.cc/box/fty.jar")

        sf = rules_cfg.get("site_filters", {})
        self.site_kw = [w for w in sf.get("block_keywords", []) if w]
        self.site_domains = [d.lower() for d in sf.get("block_domains", []) if d]
        self.site_regex = [re.compile(p, re.I) for p in sf.get("block_regex", []) if p]
        self.wl_names = sf.get("whitelist_name_contains", [])
        self.action = rules_cfg.get("site_action", "drop")

    def source_reason(self, line, spider, sites):
        """源级禁用判定：返回原因字符串或 None。命中则整源剔除。"""
        blob = " ".join([line.get("name", ""), spider] +
                        [str(u) for u in (line.get("urls") or [])])[:40000]
        low = blob.lower()
        for w in self.login_kw:
            if w in blob:
                return "login"
        if isinstance(spider, str) and spider:
            head = spider.split(";", 1)[0].split("?", 1)[0].strip().lower()
            if head.endswith(self.bad_ext):
                return "image-spider"
            if any(k in head for k in self.go_kw):
                return "go-proxy"
        return None

    def site_action(self, site):
        """站点级：返回 keep / drop / isolate。"""
        if not isinstance(site, dict):
            return "drop"
        name = str(site.get("name", ""))
        api = str(site.get("api", "")) + " " + str(site.get("ext", "") or "")
        for wn in self.wl_names:
            if wn in name:
                return "keep"
        try:
            host = (urllib.parse.urlsplit(str(site.get("api", ""))).hostname or "").lower()
        except Exception:
            host = ""
        for d in self.site_domains:
            if d and (d in host or d in api.lower()):
                return "drop"
        for w in self.site_kw:
            if w and (w in name or w in api):
                return "drop"
        for rx in self.site_regex:
            if rx.search(name) or rx.search(api):
                return "drop"
        # 成人/伦理 -> isolate（待定隔离源，人工判断）
        txt = (name + " " + api).lower()
        if not any(a in txt for a in self.adult_allow):
            if any(w in txt for w in self.adult_kw):
                return "isolate"
        return "keep"

    def clean_spider(self, spider):
        """spider 学 tvbox-api-backup：只放行正规 .jar，图片伪装/空/非法回退默认 jar。"""
        if not isinstance(spider, str) or not spider:
            return self.default_spider
        url = spider.split(";", 1)[0].split("?", 1)[0].strip().lower()
        if url.endswith(self.bad_ext):
            return self.default_spider
        if any(k in url for k in self.go_kw):
            return self.default_spider
        if url.endswith(".jar") and url.startswith(("http://", "https://")):
            return spider
        return self.default_spider


def dedup_sites(sites):
    """站点级强力去重（跨全部源），保留首次出现。"""
    seen, out = set(), []
    for s in sites:
        k = site_key(s)
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(s)
    return out


def dedup_lines(lines):
    """线路级去重：按主 URL。合并同源多镜像（urls 合并）。"""
    order, merged = [], {}
    for ln in lines:
        k = line_key(ln.get("urls"))
        if not k:
            continue
        if k not in merged:
            merged[k] = dict(ln)
            merged[k]["urls"] = list(ln.get("urls") or [])
            order.append(k)
        else:
            for u in (ln.get("urls") or []):
                if u not in merged[k]["urls"]:
                    merged[k]["urls"].append(u)
    return [merged[k] for k in order]


# ---------------- 状态管理 ----------------
def load_state():
    st = load_json(STATE_FILE, None)
    if not st:
        st = {"lines": {}, "history": {}, "last_run": None, "seen": {}}
    st.setdefault("lines", {})
    st.setdefault("history", {})
    st.setdefault("seen", {})
    return st


def save_state(st):
    st["last_run"] = now_str()
    save_json(STATE_FILE, st)


def read_prev_snapshot(days_ago=1):
    """读取 N 天前的快照目录（用于兜底），返回行列表或 None。"""
    day = (datetime.datetime.now(TZ) - datetime.timedelta(days=days_ago)).strftime("%Y-%m-%d")
    p = os.path.join(SNAP, day, "lines.json")
    return load_json(p, None)


def save_snapshot(lines, day=None):
    day = day or datetime.datetime.now(TZ).strftime("%Y-%m-%d")
    save_json(os.path.join(SNAP, day, "lines.json"), lines)
    return day
