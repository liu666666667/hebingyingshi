#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探测采集接口的"假搜索"：搜一个绝不存在的词却仍返回整库 -> 判定为假搜索站。
命中的域名写入 rules.json 的 block_domains，以后每天主流程自动排除。
在 GitHub Actions 云端跑（可直连第三方采集站，本地直连会大量超时）。"""
import concurrent.futures
import json
import os
import random
import string
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FINAL = os.path.join(ROOT, "output", "final.json")
RULES = os.path.join(ROOT, "config", "rules.json")
REPORT = os.path.join(ROOT, "config", "fake_search_report.json")


def collect_sites():
    if not os.path.exists(FINAL):
        return []
    d = json.load(open(FINAL, encoding="utf-8"))
    hosts = {}
    for s in d.get("sites", []):
        api = str(s.get("api", ""))
        if not api.startswith("http") or api.endswith(".js"):
            continue
        if not any(k in api for k in ("provide", "api.php", "cjapi", "/vod", "/api/")):
            continue
        host = urllib.parse.urlsplit(api).hostname or ""
        if host and host not in hosts:
            hosts[host] = api
    return [(h, a) for h, a in hosts.items()]


def api_with_wd(api, wd):
    p = urllib.parse.urlsplit(api)
    base = urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, "", ""))
    return base + "?wd=" + urllib.parse.quote(wd)


def names(api, wd, timeout=9):
    url = api_with_wd(api, wd)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        j = json.loads(r.read(400000).decode("utf-8", "ignore"))
    lst = j.get("list") or j.get("data") or j.get("videos") or []
    if not isinstance(lst, list):
        return []
    return [str(x.get("vod_name") or x.get("name") or x.get("title", "")) for x in lst[:4]]


def rand_word(tag):
    return tag + "".join(random.choices(string.ascii_lowercase, k=9)) + "绝不存在9876"


def probe(item):
    host, api = item
    try:
        a = names(api, rand_word("zzq"))
        b = names(api, rand_word("qxw"))
    except Exception:
        return None
    # 两个毫不相干的不存在词，若返回相同且非空 -> 搜索被忽略 = 假搜索
    if len(a) >= 2 and a[:2] == b[:2]:
        return host
    return None


def main():
    items = collect_sites()
    print("[PROBE] 采集站 host 数: %d" % len(items))
    fake = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=24) as ex:
        for r in ex.map(probe, items):
            if r:
                fake.append(r)
    fake = sorted(set(fake))
    print("[PROBE] 假搜索站 %d 个:" % len(fake))
    for h in fake:
        print("[PROBE]   FAKE", h)
    json.dump({"fake_domains": fake}, open(REPORT, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    rules = json.load(open(RULES, encoding="utf-8"))
    sf = rules["site_filters"]
    domains = sf.setdefault("block_domains", [])
    add = [h for h in fake if h not in domains]
    sf["block_domains"] = domains + add
    json.dump(rules, open(RULES, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("[PROBE] 已写入 rules.json block_domains: +%d" % len(add))


if __name__ == "__main__":
    main()
