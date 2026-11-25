#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""公共库：网络、解析、路径重写、过滤。核心函数移植自用户已跑通的 yingshi 项目。"""
import datetime, json, os, re, ssl, time, urllib.parse, urllib.request

TZ = datetime.timezone(datetime.timedelta(hours=8))

# ---- 网络层 ----
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

UA = "Mozilla/5.0"
# 影视仓 App 真实 UA：部分接口对浏览器 UA 返回伪装图片/HTML，对 App UA 才返回真 JSON
APP_UAS = [
    ("Mozilla/5.0", ""),
    ("okhttp/3.15", "com.iptvbox"),
    ("TVBox/1.0.0", "com.iptvbox"),
]
DEFAULT_TIMEOUT = 15
MAX_BYTES = 12 * 1024 * 1024  # 单文件下载上限
TVBOX_KEYS = ("sites", "spiders", "lives", "parses", "flags", "exts")


def mask_own(text):
    """展示名脱敏：产物/页面/状态表里不显示仓库所有者账号。"""
    return str(text or "").replace("liu666666667", "***")


def now_str():
    return datetime.datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


def idn_encode(url):
    """URL 规范化：IDN 域名编码 + 非字母数字路径百分号编码（中文路径直接请求会崩溃）。"""
    try:
        p = urllib.parse.urlsplit(url)
        host = p.hostname or ""
        enc = host.encode("idna").decode("ascii")
        path = urllib.parse.quote(p.path or "", safe="/%")
        query = urllib.parse.quote(p.query or "", safe="=&?/%+~")
        netloc = enc + ((":" + str(p.port)) if p.port else "")
        return urllib.parse.urlunsplit((p.scheme, netloc, path, query, p.fragment))
    except Exception:
        return url


def http_get(url, timeout=DEFAULT_TIMEOUT, max_bytes=MAX_BYTES, ua=None, xrw=None):
    """GET 并返回文本。urllib + 放宽证书校验。可指定 UA（默认 Mozilla/5.0）。
    返回 (文本, 是否像文本)。二进制伪装响应（图片防盗）返回 (None, False)。"""
    target = idn_encode(url)
    headers = {
        "User-Agent": ua or UA,
        "Accept": "application/json,text/plain,*/*",
    }
    if xrw:
        headers["X-Requested-With"] = xrw
    req = urllib.request.Request(target, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
        data = resp.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("response too large")
    # 伪装图片/二进制响应检测：JPEG/PNG/GIF/BMP 魔数或高密度控制字符
    head = data[:16]
    if head[:3] == b"\xff\xd8\xff" or head[:8] == b"\x89PNG\r\n\x1a\n" \
       or head[:6] in (b"GIF89a", b"GIF87a") or head[:2] in (b"BM", b"\x00\x10") \
       or (len(data) and data[:1] == b"\x00"):
        return None, False
    # 官网页面响应检测（浏览器 UA 被导向 HTML 官网）：换 App UA 重拿
    low = data[:256].lstrip().lower()
    if low.startswith((b"<!doctype html", b"<html", b"<head")):
        return None, False
    text = data.decode("utf-8", "ignore")
    if text.count("\x00") > 3 or (len(text) and "\ufffd" in text[:200] and text.count("\ufffd") > len(text) * 0.3):
        return None, False
    return text, True


def fetch_text_with_fallback(url_list, retries=1, github_only_mirror=True, app_ua=False):
    """依次尝试 url_list；同地址失败重试 retries 次；github 直连失败自动加镜像前缀重试。
    app_ua=True 时启用 App UA 轮换（对付伪装图片反爬）。
    返回 (原始url, 实际使用url, 文本)。保证每个地址的总尝试时间可控。"""
    mirrors = ["https://gh-proxy.com/", "https://ghproxy.net/"]
    ua_list = APP_UAS if app_ua else [(UA, "")]
    last_err = None
    for url in url_list:
        t = 12 if len(url_list) > 1 else DEFAULT_TIMEOUT
        attempts = [url]
        if github_only_mirror and "githubusercontent.com" in url:
            attempts += [m + url for m in mirrors]
        for u in attempts:
            for ua, xrw in ua_list:
                for attempt in range(1, retries + 2):
                    try:
                        text, is_text = http_get(u, timeout=t, ua=ua, xrw=xrw)
                        if is_text:
                            return url, u, text
                        # 二进制伪装响应：不重试同 UA，直接换下一个 UA
                        last_err = ValueError("binary-disguise response")
                        break
                    except Exception as e:
                        last_err = e
                        if attempt <= retries:
                            time.sleep(1)
                        continue
    raise last_err or RuntimeError("no urls")


def parse_json_lenient(text):
    text = text.lstrip("\ufeff").strip()
    text = "\n".join(l for l in text.split("\n") if not l.lstrip().startswith("//"))
    try:
        return json.loads(text, strict=False)
    except json.JSONDecodeError:
        d = json.JSONDecoder(strict=False)
        obj, _ = d.raw_decode(text)
        return obj


def is_tvbox_config(data):
    if not isinstance(data, dict):
        return False
    if any(k in data for k in TVBOX_KEYS):
        return True
    if isinstance(data.get("urls"), list) and data["urls"]:
        return True
    return False


def is_multi_warehouse(data):
    return isinstance(data, dict) and isinstance(data.get("urls"), list) and data["urls"] and not data.get("sites")


# ---- 相对路径重写（核心：修复 jar/ext 404 导致的解析失败） ----
def _join_base(base_url, ref):
    """把相对引用 ref 拼到 base_url 的目录上，返回绝对 URL。保留 ;md5;xxx 后缀。"""
    if not ref or not isinstance(ref, str):
        return ref
    ref = ref.strip()
    if ref.startswith(("http://", "https://", "{", "[", "data:", "clan://", "file://")):
        return ref
    core, sep, tail = ref.partition(";")
    if not core:
        return ref
    if core.startswith("/"):
        p = urllib.parse.urlsplit(base_url)
        abs_core = "%s://%s%s" % (p.scheme, p.netloc, core)
    else:
        base_dir = base_url.rsplit("/", 1)[0]
        abs_core = base_dir + "/" + core.lstrip("./")
    return abs_core + sep + tail


def absolutize(data, base_url):
    """把配置里 spider / sites[].ext（含 dict 内 jar）的相对路径重写为以 base_url 为基的绝对地址。"""
    if not isinstance(data, dict) or not base_url:
        return data

    def fix_ext(ext):
        if isinstance(ext, str):
            return _join_base(base_url, ext)
        if isinstance(ext, dict):
            for k in ("jar", "api", "url"):
                if k in ext and isinstance(ext[k], str):
                    ext[k] = _join_base(base_url, ext[k])
        return ext

    spider = data.get("spider")
    if isinstance(spider, str) and spider and not spider.startswith(("http", "{", "[")):
        data["spider"] = _join_base(base_url, spider)
    for s in data.get("sites", []) or []:
        if isinstance(s, dict):
            if "ext" in s:
                s["ext"] = fix_ext(s["ext"])
            if isinstance(s.get("api"), str) and s["api"].endswith((".js", ".jar", ".py")) \
               and not s["api"].startswith(("http", "{")):
                s["api"] = _join_base(base_url, s["api"])
    return data


