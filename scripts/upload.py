#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用 GitHub REST API 把本地仓库文件推送到 main 分支（绕开 git 代理问题）。"""
import base64
import json
import os
import sys
import time
import urllib.request

TOKEN = os.environ.get("HB_TOKEN", "")
REPO = "liu666666667/hebingyingshi"
BRANCH = "main"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = "https://api.github.com"

def api(path, method="GET", data=None):
    url = API + path
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", "Bearer " + TOKEN)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "hb-upload")
    if body:
        req.add_header("Content-Type", "application/json")
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            if attempt == 3:
                raise
            time.sleep(2 * (attempt + 1))

def collect_files():
    skip_dirs = {"__pycache__", ".git"}
    files = []
    for dp, dns, fns in os.walk(ROOT):
        dns[:] = [d for d in dns if d not in skip_dirs]
        for fn in fns:
            if fn.endswith(".pyc"):
                continue
            full = os.path.join(dp, fn)
            rel = os.path.relpath(full, ROOT).replace("\\", "/")
            # 只上传必要文件，跳过本地 data/output（由 Actions 生成）
            if rel.startswith("data/") or rel.startswith("output/"):
                continue
            files.append((rel, full))
    return files

def main():
    if not TOKEN:
        print("缺少 HB_TOKEN"); sys.exit(1)
    files = collect_files()
    print("待上传文件:", len(files))
    # 获取当前 main 的 tree sha
    ref = api("/repos/%s/git/ref/heads/%s" % (REPO, BRANCH))
    commit_sha = ref["object"]["sha"]
    commit = api("/repos/%s/git/commits/%s" % (REPO, commit_sha))
    base_tree = commit["tree"]["sha"]
    # 构造 blobs
    tree = []
    for rel, full in files:
        with open(full, "rb") as f:
            content = f.read()
        blob = api("/repos/%s/git/blobs" % REPO, "POST",
                   {"content": base64.b64encode(content).decode(), "encoding": "base64"})
        mode = "100644"
        tree.append({"path": rel, "mode": mode, "type": "blob", "sha": blob["sha"]})
        print("  blob", rel)
    new_tree = api("/repos/%s/git/trees" % REPO, "POST",
                   {"base_tree": base_tree, "tree": tree})
    new_commit = api("/repos/%s/git/commits" % REPO, "POST", {
        "message": "初始化 hebingyingshi 汇总仓库",
        "tree": new_tree["sha"],
        "parents": [commit_sha],
    })
    api("/repos/%s/git/refs/heads/%s" % (REPO, BRANCH), "POST",
        {"sha": new_commit["sha"], "force": True})
    print("推送完成，commit:", new_commit["sha"][:10])

if __name__ == "__main__":
    main()
