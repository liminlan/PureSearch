#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""纯净搜索 · 站内观看版

一个跑在本机的聚合搜索站：搜到的视频在本页直接播（B 站免费内容优先，
自配采集源兜底），文章在本页直接读，只用一个本地授权中心扫码登录一次。
"""

import base64
import hashlib
import html as htmlmod
import http.cookiejar
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import article

if getattr(sys, "frozen", False):
    # 打包成 exe：只读资源在解包目录，可写数据落在 exe 旁边
    RES_DIR = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    BASE_DIR = os.path.dirname(sys.executable)
else:
    RES_DIR = BASE_DIR = os.path.dirname(os.path.abspath(__file__))

STATIC_DIR = os.path.join(RES_DIR, "static")
SITES_FILE = os.path.join(RES_DIR, "sites.json")
SOURCES_FILE = os.path.join(BASE_DIR, "sources.json")
AUTH_FILE = os.path.join(BASE_DIR, "auth.json")
HOT_FILE = os.path.join(BASE_DIR, "hot.json")

# 首次运行把种子热搜复制到可写目录，之后由 update_daily.py 覆盖
if getattr(sys, "frozen", False) and not os.path.exists(HOT_FILE):
    _seed_hot = os.path.join(RES_DIR, "hot.json")
    if os.path.exists(_seed_hot):
        try:
            shutil.copyfile(_seed_hot, HOT_FILE)
        except Exception:
            pass

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
}

COOKIE_JAR = http.cookiejar.CookieJar()
OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(COOKIE_JAR))

LOOPBACK_OK = ("127.0.0.1", "localhost", "::1")


# =============================================================== 基础工具

def fetch(url, extra=None, timeout=9, opener=None):
    h = dict(HEADERS)
    if extra:
        h.update(extra)
    req = urllib.request.Request(url, headers=h)
    with (opener or OPENER).open(req, timeout=timeout) as r:
        raw = r.read()
        charset = "utf-8"
        m = re.search(r"charset=([\w-]+)", r.headers.get("Content-Type", ""))
        if m:
            charset = m.group(1)
        elif b"charset=" in raw[:2000]:
            m2 = re.search(rb'charset=["\']?([\w-]+)', raw[:2000])
            if m2:
                charset = m2.group(1).decode()
        return raw.decode(charset, "ignore")


def strip_tags(s):
    s = re.sub(r"<(script|style)[^>]*>[\s\S]*?</\1>", " ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", htmlmod.unescape(s)).strip()


def host_of(url):
    try:
        return urllib.parse.urlparse(url).netloc.replace("www.", "")
    except Exception:
        return ""


def unwrap_link(url):
    url = htmlmod.unescape(url or "")
    if "bing.com/ck/a" in url:
        import base64
        m = re.search(r"[?&]u=a1([^&]+)", url)
        if m:
            b64 = m.group(1) + "=" * (-len(m.group(1)) % 4)
            try:
                return base64.urlsafe_b64decode(b64).decode("utf-8", "ignore")
            except Exception:
                pass
    return "https:" + url if url.startswith("//") else url


def fmt_count(n):
    try:
        n = int(n)
    except Exception:
        return ""
    if n >= 100000000:
        return f"{n / 100000000:.1f}亿"
    if n >= 10000:
        return f"{n / 10000:.1f}万"
    return str(n)


def ssrf_ok(url):
    """只挡内网地址，避免这个本地代理被拿去访问局域网。"""
    try:
        scheme = urllib.parse.urlsplit(url).scheme
    except Exception:
        return False
    if scheme not in ("http", "https"):
        return False
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if not host:
        return False
    if host in LOOPBACK_OK:
        return False
    if re.match(r"^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|169\.254\.|127\.)", host):
        return False
    if host == "0.0.0.0":
        return False
    return True


# =============================================================== 封面 / 分片代理

_IMG_CACHE = {}
_IMG_LOCK = threading.Lock()


def proxy_image(url):
    """封面和正文图片都走这里：带上对应站点的 Referer 再转发。
    任意站点都放行（正文图片来自各个网站），但挡内网地址、只放行真正的图片。"""
    if not ssrf_ok(url):
        raise ValueError("不允许的图片地址")
    with _IMG_LOCK:
        hit = _IMG_CACHE.get(url)
    if hit:
        return hit
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Referer": referer_for(url),
                      "Accept": "image/*,*/*;q=0.8"})
    with OPENER.open(req, timeout=8) as r:
        ctype = (r.headers.get("Content-Type") or "").lower()
        if not ctype.startswith("image/"):
            raise ValueError("不是图片")
        data = r.read()
    if len(data) > 6_000_000:
        raise ValueError("图片过大")
    with _IMG_LOCK:
        if len(_IMG_CACHE) > 200:
            _IMG_CACHE.clear()
        _IMG_CACHE[url] = (ctype, data)
    return ctype, data


def referer_for(url):
    """按目标站点给出合适的 Referer：能过防盗链，又不冒充别的站。"""
    try:
        host = urllib.parse.urlparse(url).netloc or ""
    except Exception:
        host = ""
    if "bilibili" in host or "hdslb" in host:
        return "https://www.bilibili.com/"
    if "douban" in host:
        return "https://movie.douban.com/"
    return ("https://" + host + "/") if host else "https://cn.bing.com/"


def proxy_stream(handler, url, referer=None):
    """把上游的视频流/分片透传给浏览器，支持 Range，让进度条能拖。"""
    if not ssrf_ok(url):
        return handler._send(403, b"", "text/plain")
    headers = {"User-Agent": UA, "Referer": referer or referer_for(url)}
    rng = handler.headers.get("Range")
    if rng:
        headers["Range"] = rng
    try:
        req = urllib.request.Request(url, headers=headers)
        resp = OPENER.open(req, timeout=25)
    except urllib.error.HTTPError as e:
        resp = e
    except Exception:
        return handler._send(502, b"")

    ctype = resp.headers.get("Content-Type", "") or ""
    is_playlist = ("mpegurl" in ctype.lower()) or url.split("?")[0].endswith(".m3u8")

    if is_playlist:
        try:
            text = resp.read().decode("utf-8", "ignore")
        except Exception:
            return handler._send(502, b"")
        rewritten = _rewrite_m3u8(text, url, referer or referer_for(url))
        return handler._send(200, rewritten, "application/vnd.apple.mpegurl",
                             cache="no-store", cors=True)

    code = resp.getcode()
    handler.send_response(code)
    for h in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges", "ETag"):
        v = resp.headers.get(h)
        if v:
            handler.send_header(h, v)
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    try:
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            handler.wfile.write(chunk)
    except Exception:
        pass


def _rewrite_m3u8(text, base_url, referer):
    def prox(u):
        return "/api/stream?u=%s&r=%s" % (
            urllib.parse.quote(u, safe=""), urllib.parse.quote(referer, safe=""))

    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            out.append(line)
            continue
        if s.startswith("#"):
            s = re.sub(r'URI="([^"]+)"',
                       lambda m: 'URI="%s"' % prox(urllib.parse.urljoin(base_url, m.group(1))),
                       s)
            out.append(s)
        else:
            out.append(prox(urllib.parse.urljoin(base_url, s)))
    return "\n".join(out)


# =============================================================== B 站

_MIXIN_IDX = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43,
              5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16,
              24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59,
              6, 63, 57, 62, 11, 36, 20, 34, 44, 52]
_WBI = {"key": None, "at": 0}
_WBI_LOCK = threading.Lock()
_WBI_FILE = os.path.join(BASE_DIR, "wbi.json")
_BVIEW_CACHE = {}


def wbi_key(force=False):
    """B 站搜索接口要 wbi 签名。这个 nav 接口偶尔要 9 秒，
    所以把密钥落盘缓存 6 小时，重启也不用再等。"""
    with _WBI_LOCK:
        if not force:
            if _WBI["key"] and time.time() - _WBI["at"] < 6 * 3600:
                return _WBI["key"]
            try:
                with open(_WBI_FILE, "r", encoding="utf-8") as f:
                    d = json.load(f)
                if d.get("key") and time.time() - d.get("at", 0) < 6 * 3600:
                    _WBI.update({"key": d["key"], "at": d["at"]})
                    return d["key"]
            except Exception:
                pass
        body = fetch("https://api.bilibili.com/x/web-interface/nav",
                     {"Referer": "https://www.bilibili.com/"}, timeout=12)
        d = json.loads(body)["data"]["wbi_img"]
        img = d["img_url"].rsplit("/", 1)[1].split(".")[0]
        sub = d["sub_url"].rsplit("/", 1)[1].split(".")[0]
        raw = img + sub
        key = "".join(raw[i] for i in _MIXIN_IDX)
        _WBI["key"], _WBI["at"] = key, time.time()
        try:
            with open(_WBI_FILE, "w", encoding="utf-8") as f:
                json.dump({"key": key, "at": _WBI["at"]}, f)
        except Exception:
            pass
        return key


def wbi_sign(url, params, mk):
    params = dict(sorted(params.items()))
    params = {k: re.sub(r"[!'()*]", "", str(v)) for k, v in params.items()}
    qs = urllib.parse.urlencode(params)
    return f"{url}?{qs}&w_rid={hashlib.md5((qs + mk).encode()).hexdigest()}"


_BUVID = {}
_BUVID_LOCK = threading.Lock()
BUVID_FILE = os.path.join(BASE_DIR, "buvid.json")


def _install_buvid():
    for name, val in (("buvid3", _BUVID.get("b3")), ("buvid4", _BUVID.get("b4"))):
        if not val:
            continue
        COOKIE_JAR.set_cookie(http.cookiejar.Cookie(
            version=0, name=name, value=val, port=None, port_specified=False,
            domain=".bilibili.com", domain_specified=True, domain_initial_dot=True,
            path="/", path_specified=True, secure=False, expires=None,
            discard=False, comment=None, comment_url=None, rest={}, rfc2109=False))


def ensure_buvid(force=False):
    """B 站对没有 buvid3 的请求会直接甩 HTTP 412。这里拿一个匿名设备指纹补上。"""
    with _BUVID_LOCK:
        if not force and _BUVID.get("b3") and time.time() - _BUVID.get("at", 0) < 12 * 3600:
            return
        if not force:
            try:
                with open(BUVID_FILE, "r", encoding="utf-8") as f:
                    d = json.load(f)
                if d.get("b3") and time.time() - d.get("at", 0) < 12 * 3600:
                    _BUVID.update(d)
                    _install_buvid()
                    return
            except Exception:
                pass
        try:
            body = fetch("https://api.bilibili.com/x/frontend/finger/spi",
                         {"Referer": "https://www.bilibili.com/"}, timeout=8)
            data = json.loads(body)["data"]
            _BUVID.update({"b3": data["b_3"], "b4": data["b_4"], "at": time.time()})
            _install_buvid()
            try:
                with open(BUVID_FILE, "w", encoding="utf-8") as f:
                    json.dump(_BUVID, f)
            except Exception:
                pass
        except Exception:
            pass


def bili_search(q, limit=24, page=1):
    ensure_buvid()

    def _bili_get(url, extra=None):
        extra = extra or {"Referer": "https://search.bilibili.com/",
                          "Origin": "https://search.bilibili.com"}
        try:
            return fetch(url, extra, timeout=8)
        except urllib.error.HTTPError as e:
            if e.code in (412, 403):
                ensure_buvid(force=True)
                time.sleep(0.5)
                return fetch(url, extra, timeout=8)
            raise

    # 先走不带签名的老搜索接口：实测最稳，签名版经常 code=0 但结果为空。
    plain = ("https://api.bilibili.com/x/web-interface/search/type"
             "?search_type=video&page=%d&keyword=" % page) + urllib.parse.quote(q)
    d = {}
    try:
        d = json.loads(_bili_get(plain))
    except Exception:
        d = {}
    if d.get("code") != 0 or not (d.get("data") or {}).get("result"):
        # 兜底：换 wbi 签名版
        mk = wbi_key()
        params = {"keyword": q, "search_type": "video", "page": page, "wts": int(time.time())}
        try:
            body = _bili_get(wbi_sign("https://api.bilibili.com/x/web-interface/wbi/search/type",
                                      params, mk))
            d2 = json.loads(body)
        except Exception:
            d2 = {}
        if d2.get("code") == 0 and (d2.get("data") or {}).get("result"):
            d = d2
    if d.get("code") != 0:
        raise RuntimeError(f"B站搜索返回 {d.get('code')} {d.get('message')}")
    out = []
    for it in (d.get("data") or {}).get("result") or []:
        title = strip_tags(it.get("title") or "")
        if not title:
            continue
        cover = it.get("pic") or ""
        if cover.startswith("//"):
            cover = "https:" + cover
        dur = it.get("duration") or ""
        if isinstance(dur, (int, float)):
            dur = f"{int(dur // 60):02d}:{int(dur % 60):02d}"
        out.append({
            "title": title,
            "bvid": it.get("bvid") or "",
            "cover": cover,
            "author": it.get("author") or "",
            "play": fmt_count(it.get("play")),
            "duration": dur,
            "pubdate": it.get("pubdate") or 0,
            "source": "B站",
            "desc": (it.get("description") or "").strip()[:90],
            "playable": bool(it.get("bvid")),
            "url": "https://www.bilibili.com/video/" + (it.get("bvid") or ""),
        })
        if len(out) >= limit:
            break
    return out


def bili_cid(bvid):
    hit = _BVIEW_CACHE.get(bvid)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    body = fetch("https://api.bilibili.com/x/web-interface/view?bvid=" + bvid,
                 {"Referer": "https://www.bilibili.com/"})
    d = json.loads(body)
    if d.get("code") != 0:
        raise RuntimeError(d.get("message") or "取不到视频信息")
    data = d["data"]
    info = {"cid": data["cid"], "title": data.get("title", ""),
            "cover": data.get("pic", ""), "owner": (data.get("owner") or {}).get("name", ""),
            "pages": len(data.get("pages") or [])}
    _BVIEW_CACHE[bvid] = (time.time(), info)
    if len(_BVIEW_CACHE) > 200:
        _BVIEW_CACHE.clear()
    return info


def bili_play(bvid, qn=80, page=1):
    """优先 durl（mp4 直链，浏览器原生能播）；拿不到就报错让前端提示去原站。"""
    info = bili_cid(bvid)
    cid = info["cid"]
    if info["pages"] > 1 and page > 1:
        body = fetch("https://api.bilibili.com/x/player/pagelist?bvid=" + bvid,
                     {"Referer": "https://www.bilibili.com/"})
        d = json.loads(body)
        pl = (d.get("data") or [])
        if page - 1 < len(pl):
            cid = pl[page - 1]["cid"]
    mk = wbi_key()
    params = {"bvid": bvid, "cid": cid, "qn": qn, "fnval": 1, "fnver": 0,
              "fourk": 0, "platform": "html5", "wts": int(time.time())}
    body = fetch(wbi_sign("https://api.bilibili.com/x/player/wbi/playurl", params, mk),
                 {"Referer": f"https://www.bilibili.com/video/{bvid}/"})
    d = json.loads(body)
    if d.get("code") != 0:
        raise RuntimeError(_bili_err(d.get("code"), d.get("message")))
    data = d.get("data") or {}
    durl = data.get("durl") or []
    if not durl:
        raise RuntimeError("这个视频没有可直接播放的流（多为付费/大会员内容）")
    qmap = {16: "360P", 32: "480P", 64: "720P", 80: "1080P", 112: "1080P+", 116: "1080P60"}
    return {
        "ok": True,
        "title": info["title"],
        "quality": qmap.get(data.get("quality"), str(data.get("quality"))),
        "accept": [qmap.get(k, str(k)) for k in (data.get("accept_quality") or [])],
        "mp4": durl[0]["url"],
        "backup": [x["url"] for x in durl[0].get("backup_url") or []][:2],
        "pages": info["pages"],
    }


def _bili_err(code, msg):
    if code == -10403:
        return "这个视频需要大会员或地区限制，站内播不了，点「去 B 站看」"
    if code == -404:
        return "视频不存在或已下架"
    return f"B站返回 {code}：{msg}"


# =============================================================== Bing

BING_URL = "https://cn.bing.com/search?q={q}&count=10&first={first}"


def bing_page(q, first=1):
    html_txt = fetch(BING_URL.format(q=urllib.parse.quote(q), first=first))
    ads = len(re.findall(r'class="b_ad[ "]', html_txt))
    blocks = re.split(r'<li class="b_algo', html_txt)[1:]
    items = []
    for b in blocks:
        m = re.search(r'<h2[^>]*>[\s\S]{0,500}?<a[^>]+href="([^"]+)"[^>]*>([\s\S]*?)</a>', b)
        if not m:
            m = re.search(r'<a[^>]+href="([^"]+)"[^>]*>([\s\S]*?)</a>', b)
        if not m:
            continue
        link = unwrap_link(m.group(1))
        title = strip_tags(m.group(2))
        if not link.startswith("http") or not title:
            continue
        desc = ""
        for pat in (r'<p[^>]*class="[^"]*b_lineclamp[^"]*"[^>]*>([\s\S]*?)</p>',
                    r'<div[^>]*class="[^"]*b_caption[^"]*"[^>]*>[\s\S]{0,300}?<p[^>]*>([\s\S]*?)</p>',
                    r'<p[^>]*>([\s\S]*?)</p>'):
            d = re.search(pat, b)
            if d:
                desc = strip_tags(d.group(1))
                break
        items.append({"title": title, "url": link, "desc": desc, "host": host_of(link)})
    items = [i for i in items if not re.search(r"(广告|推广|赞助)", i["title"])]
    return items, ads


def bing_web(q, first=1, limit=24):
    items, ads = bing_page(q, first)
    seen, out = set(), []
    for it in items:
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        out.append(it)
    return out[:limit], ads


# =============================================================== 豆瓣

def douban_suggest(q):
    body = fetch("https://movie.douban.com/j/subject_suggest?q=" + urllib.parse.quote(q),
                 {"Referer": "https://movie.douban.com/"}, timeout=7)
    out = []
    for it in json.loads(body)[:10]:
        if it.get("type") not in ("movie", "tv", "show"):
            continue
        img = (it.get("img") or "").replace("/s_ratio_poster/", "/m_ratio_poster/")
        out.append({
            "title": it.get("title") or "",
            "year": it.get("year") or "",
            "type": {"movie": "电影", "tv": "剧集", "show": "综艺"}.get(it.get("type"), "影视"),
            "cover": img,
            "url": "https://movie.douban.com/subject/" + str(it.get("id") or ""),
            "playable": False,
            "source": "豆瓣",
        })
    return out


# =============================================================== 采集源（Apple CMS）

_SRC_LOCK = threading.Lock()


def load_sources():
    if not os.path.exists(SOURCES_FILE):
        return []
    try:
        with open(SOURCES_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get("sources", [])
    except Exception:
        return []


def save_sources(sources):
    with _SRC_LOCK:
        with open(SOURCES_FILE, "w", encoding="utf-8") as f:
            json.dump({"sources": sources}, f, ensure_ascii=False, indent=2)


def _split_eps(play_raw):
    """Apple CMS 的 play_url 形如「第01集$http://a.m3u8#第02集$http://b.m3u8」，
    多条线路之间用 $$$ 分隔，这里只取第一条线路。"""
    if not play_raw:
        return []
    raw = play_raw.strip()
    if "$$$" in raw:
        for group in raw.split("$$$"):
            if "$" in group and ("://" in group or "m3u8" in group or ".mp4" in group):
                raw = group
                break
    eps = []
    for seg in raw.split("#"):
        seg = seg.strip()
        if not seg:
            continue
        if "$" in seg:
            label, url = seg.split("$", 1)
        else:
            label, url = "", seg
        url = url.strip()
        if not url.startswith("http"):
            continue
        eps.append({"label": label.strip() or ("第%d集" % (len(eps) + 1)), "url": url})
    return eps


def _xml_videos(text):
    text = text.strip()
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    root = ET.fromstring(text)
    out = []
    for v in root.iter("video"):
        def g(tag):
            e = v.find(tag)
            return (e.text or "").strip() if e is not None and e.text else ""
        play_raw = g("play_url") or g("vod_play_url")
        eps = _split_eps(play_raw)
        out.append({
            "id": g("id") or g("vod_id"),
            "name": g("name") or g("vod_name"),
            "pic": g("pic") or g("vod_pic"),
            "year": g("year"), "area": g("area"), "type": g("type"),
            "note": g("note") or g("vod_remarks"),
            "des": g("des") or g("vod_content"),
            "eps": eps,
        })
    return out


def cms_one(src, q, limit=20):
    base = src["api"]
    sep = "&" if "?" in base else "?"
    url = f"{base}{sep}ac=videolist&wd={urllib.parse.quote(q)}&pg=1"
    text = fetch(url, timeout=9)
    if text.lstrip().startswith("{"):
        d = json.loads(text)
        lst = d.get("list") or []
        out = []
        for v in lst[:limit]:
            eps = _split_eps(v.get("vod_play_url") or "")
            out.append({
                "id": str(v.get("vod_id")), "name": v.get("vod_name"),
                "pic": v.get("vod_pic"), "year": v.get("vod_year"),
                "area": v.get("vod_area"), "type": v.get("vod_type"),
                "note": v.get("vod_remarks"), "des": (v.get("vod_content") or "")[:120],
                "eps": eps,
            })
    else:
        out = _xml_videos(text)[:limit]
    for v in out:
        v["source"] = src.get("name") or host_of(base)
        v["playable"] = bool(v["eps"])
        v["url"] = None
        v["_src"] = src.get("name") or host_of(base)
    return out


def cms_search(q):
    s = [x for x in load_sources() if x.get("enabled", True)]
    if not s:
        return [], []
    items, fails = [], []
    with ThreadPoolExecutor(max_workers=min(6, len(s))) as ex:
        futs = {ex.submit(cms_one, src, q): src for src in s}
        for f in as_completed(futs):
            try:
                items.extend(f.result())
            except Exception as e:
                fails.append(f"{futs[f].get('name') or '源'}({type(e).__name__})")
    return items, fails


# =============================================================== 站点推荐

_SITES = {"data": None, "at": 0}


DEFAULT_HOT = ["剪辑教程", "流浪地球", "Java 面试题", "PPT 模板", "考研数学", "长沙美食", "Python 爬虫", "AI 绘画"]


def load_hot():
    """热搜词由 update_daily.py 每天写一次；没更新过就回退到内置常用词。"""
    try:
        with open(HOT_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        words = [w for w in (d.get("words") or []) if isinstance(w, str) and w.strip()]
        at = d.get("at") or ""
        stale = False
        if at:
            try:
                stale = time.time() - time.mktime(time.strptime(at, "%Y-%m-%d %H:%M")) > 48 * 3600
            except Exception:
                stale = False
        if words:
            return {"words": words, "at": at, "stale": stale, "source": d.get("source") or "local"}
    except Exception:
        pass
    return {"words": DEFAULT_HOT, "at": "", "stale": False, "source": "builtin"}


def load_sites():
    if _SITES["data"] and time.time() - _SITES["at"] < 30:
        return _SITES["data"]
    with open(SITES_FILE, "r", encoding="utf-8") as f:
        _SITES["data"] = json.load(f)
    _SITES["at"] = time.time()
    return _SITES["data"]


def classify(q):
    data = load_sites()
    qlow = q.lower()
    scored = []
    for g in data["groups"]:
        score = 0
        for kw in g["kw"]:
            k = kw.lower()
            if not k:
                continue
            if re.search(r"[a-z0-9]", k):
                # 英文/数字关键词按“独立词”匹配，避免 ai 命中 said、go 命中 google
                if re.search(r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])", qlow):
                    score += 2
            elif k in qlow:
                score += 2 if len(k) >= 3 else 1
        if score:
            scored.append((score, g["id"], g["name"]))
    scored.sort(key=lambda x: -x[0])
    return [{"id": i, "name": n, "score": s} for s, i, n in scored]


# =========================================================== 官网下载直达
SOFTWARE_FILE = os.path.join(RES_DIR, "software.json")
_SW = {"at": 0, "items": []}
_DL_WORDS = ("下载", "安装", "官网", "客户端", "最新版", "免费", "download", "setup", "install", "exe", "安装包")


def load_software():
    if _SW["items"] and time.time() - _SW["at"] < 30:
        return _SW["items"]
    try:
        with open(SOFTWARE_FILE, encoding="utf-8") as f:
            items = (json.load(f) or {}).get("items") or []
    except Exception:
        items = []
    _SW.update({"at": time.time(), "items": items})
    return items


def _alias_hit(text, alias):
    a = (alias or "").lower().strip()
    if not a:
        return False
    if re.search(r"[a-z0-9]", a):
        return re.search(r"(?<![a-z0-9])" + re.escape(a) + r"(?![a-z0-9])", text) is not None
    return a in text


def match_software(q, limit=3):
    """命中内置的「软件 ↔ 官网下载页」表就返回直达卡片。"""
    text = (q or "").lower()
    compact = text.replace(" ", "")
    wants_dl = any(w in text for w in _DL_WORDS)
    out = []
    for it in load_software():
        best = 0
        for n in [it.get("name") or ""] + list(it.get("aliases") or []):
            if not n:
                continue
            if compact == n.lower().replace(" ", ""):
                best = max(best, 4)
            elif _alias_hit(compact, n):
                best = max(best, 2)
        if best:
            out.append({"name": it["name"], "url": it["url"], "note": it.get("note", ""),
                        "official": True, "intent_download": wants_dl,
                        "_s": best + (2 if wants_dl else 0)})
    out.sort(key=lambda x: -x["_s"])
    for x in out:
        x.pop("_s", None)
    return out[:limit]


# =========================================================== 语音转文字
# 本机离线识别：faster-whisper 的 small 模型（int8 CPU），首次用到时加载并常驻。
ASR_DIR = os.path.join(BASE_DIR, "models")
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")  # 国内可达的模型镜像
_ASR = {"model": None, "lock": threading.Lock(), "err": ""}


def asr_available():
    # exe 版没有随包模型时不自动去下载 460MB，静默降级
    if getattr(sys, "frozen", False) and not os.path.isdir(ASR_DIR):
        return False
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:
        return False


def _asr_model():
    if _ASR["model"] is not None:
        return _ASR["model"]
    with _ASR["lock"]:
        if _ASR["model"] is None:
            from faster_whisper import WhisperModel
            _ASR["model"] = WhisperModel("small", device="cpu", compute_type="int8",
                                         download_root=ASR_DIR)
    return _ASR["model"]


def _wav_to_array(path):
    """把 16bit PCM wav 读成 float32 数组（绕过 faster-whisper 对 av 版本的要求）。"""
    import wave
    import numpy as np
    with wave.open(path, "rb") as w:
        ch, sr, width = w.getnchannels(), w.getframerate(), w.getsampwidth()
        frames = w.readframes(w.getnframes())
    if width != 2:
        raise ValueError("只支持 16bit PCM 音频")
    arr = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        arr = arr.reshape(-1, ch).mean(axis=1).astype(np.float32)
    if sr != 16000:
        idx = np.linspace(0, len(arr) - 1, int(len(arr) * 16000 / sr))
        arr = np.interp(idx, np.arange(len(arr)), arr).astype(np.float32)
    return arr


def asr_text(wav_bytes):
    if not wav_bytes:
        raise ValueError("没有收到音频")
    if len(wav_bytes) > 16 * 1024 * 1024:
        raise ValueError("录音太长，请控制在 60 秒内")
    if not asr_available():
        raise RuntimeError("本机没装语音识别库：pip install faster-whisper")
    fp = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False, dir=BASE_DIR) as f:
            f.write(wav_bytes)
            fp = f.name
        audio = _wav_to_array(fp)
        if len(audio) < 16000 * 0.3:
            raise ValueError("录音太短")
        model = _asr_model()
        segs, info = model.transcribe(audio, language="zh", beam_size=3,
                                      initial_prompt="以下是简体中文普通话的句子。",
                                      vad_filter=True, condition_on_previous_text=False)
        text = "".join(s.text for s in segs).strip()
        try:
            from zhconv import convert as _zh
            text = _zh(text, "zh-cn")   # whisper 偶尔吐繁体，统一转简体
        except Exception:
            pass
        text = text.strip().rstrip("。，、．.,!！?？；;")
        return {"ok": True, "text": text, "lang": info.language,
                "sec": round(len(audio) / 16000, 1), "model": "faster-whisper-small-int8"}
    finally:
        if fp and os.path.exists(fp):
            try:
                os.remove(fp)
            except Exception:
                pass


# =========================================================== 加载更多
MORE_MAX = {"video": 6, "web": 4}

def more_results(q, kind, page):
    """加载更多：视频翻 B 站第 N 页，网页翻必应第 N 页。翻到头给原站链接。"""
    try:
        page = max(2, min(int(page or 2), MORE_MAX.get(kind, 4)))
    except Exception:
        page = 2
    if kind == "web":
        got = cached("w2:%s:%d" % (q, page), 300, lambda: bing_web(q, (page - 1) * 10 + 1))
        items, ads = got if isinstance(got, tuple) else (got, 0)
        return {"type": "web", "page": page, "items": items, "ads_filtered": ads,
                "has_more": len(items) >= 8 and page < MORE_MAX["web"],
                "more_url": "https://cn.bing.com/search?q=" + urllib.parse.quote(q) + "&count=50"}
    items = cached("v2:%s:%d" % (q, page), 900, lambda: bili_search(q, 24, page))
    return {"type": "video", "page": page, "items": items,
            "has_more": len(items) >= 15 and page < MORE_MAX["video"],
            "more_url": "https://search.bilibili.com/all?keyword=" + urllib.parse.quote(q)}


def match_sites(q, groups_hit, limit=9):
    data = load_sites()
    by_id = {g["id"]: g for g in data["groups"]}
    chosen = [by_id[g["id"]] for g in groups_hit[:2] if g["id"] in by_id]
    if not chosen:
        fb = data.get("fallback_group", "general")
        chosen = [by_id[fb]] if fb in by_id else [data["groups"][0]]
    out, seen = [], set()
    for g in chosen:
        for s in g["sites"]:
            if s["name"] in seen:
                continue
            seen.add(s["name"])
            sp = urllib.parse.urlsplit(s["search"])
            host = sp.netloc
            # 搜索子域要还原成站本身：search.bilibili.com → www.bilibili.com
            for pre in ("search.", "so.", "s.", "www.m.", "m."):
                if host.startswith(pre):
                    host = host[len(pre):]
                    break
            if not host.startswith("www."):
                host = "www." + host
            out.append({"name": s["name"], "tag": s.get("tag", ""), "desc": s.get("desc", ""),
                        "group": g["name"],
                        # 搜：带当前关键词冲进该站搜索页；去首页：回这个站本身
                        "url": s["search"].replace("{q}", urllib.parse.quote(q)),
                        "home": "%s://%s/" % (sp.scheme or "https", host)})
    return out[:limit]


# =============================================================== 登录

_AUTH = {"cookies": {}, "uname": "", "uid": 0, "at": 0}
_QR = {}
_AUTH_LOCK = threading.Lock()


def load_auth():
    if not os.path.exists(AUTH_FILE):
        return
    try:
        with open(AUTH_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        _AUTH.update(d)
        for k, v in (d.get("cookies") or {}).items():
            ck = http.cookiejar.Cookie(
                version=0, name=k, value=v, port=None, port_specified=False,
                domain=".bilibili.com", domain_specified=True, domain_initial_dot=True,
                path="/", path_specified=True, secure=False, expires=None,
                discard=False, comment=None, comment_url=None, rest={}, rfc2109=False)
            COOKIE_JAR.set_cookie(ck)
    except Exception:
        pass


def save_auth():
    with _AUTH_LOCK:
        with open(AUTH_FILE, "w", encoding="utf-8") as f:
            json.dump(_AUTH, f, ensure_ascii=False, indent=2)


def qr_generate():
    body = fetch("https://passport.bilibili.com/x/passport-login/web/qrcode/generate",
                 {"Referer": "https://www.bilibili.com/"})
    d = json.loads(body)
    if d.get("code") != 0:
        raise RuntimeError(d.get("message") or "二维码生成失败")
    data = d["data"]
    _QR[data["qrcode_key"]] = time.time()
    return {"key": data["qrcode_key"], "url": data["url"]}


def qr_poll(key):
    body = fetch("https://passport.bilibili.com/x/passport-login/web/qrcode/poll?qrcode_key="
                 + urllib.parse.quote(key), {"Referer": "https://www.bilibili.com/"})
    d = json.loads(body)
    code = ((d.get("data") or {}).get("code"))
    msg = {86101: "等待扫码", 86090: "已扫码，请在手机上确认", 86038: "二维码已失效", 0: "登录成功"}
    if code != 0:
        return {"ok": False, "code": code, "msg": msg.get(code, "未知状态")}

    cookies = {}
    for ck in COOKIE_JAR:
        if "bilibili" in ck.domain:
            cookies[ck.name] = ck.value
    _AUTH["cookies"] = cookies
    _AUTH["at"] = int(time.time())
    try:
        nav = json.loads(fetch("https://api.bilibili.com/x/web-interface/nav",
                               {"Referer": "https://www.bilibili.com/"}))
        if nav.get("code") == 0:
            data = nav.get("data") or {}
            _AUTH["uname"] = data.get("uname") or ""
            _AUTH["uid"] = data.get("mid") or 0
    except Exception:
        pass
    save_auth()
    return {"ok": True, "code": 0, "msg": "登录成功",
            "uname": _AUTH["uname"], "uid": _AUTH["uid"]}


def auth_status():
    logged = bool(_AUTH.get("cookies"))
    if logged:
        try:
            nav = json.loads(fetch("https://api.bilibili.com/x/web-interface/nav",
                                   {"Referer": "https://www.bilibili.com/"}))
            if nav.get("code") == 0:
                d = nav.get("data") or {}
                _AUTH["uname"] = d.get("uname") or _AUTH["uname"]
                _AUTH["uid"] = d.get("mid") or _AUTH["uid"]
                logged = bool(d.get("isLogin"))
                if not logged:
                    _AUTH["cookies"] = {}
                    save_auth()
        except Exception:
            pass
    return {"logged": logged, "uname": _AUTH.get("uname") or "", "uid": _AUTH.get("uid") or 0}


# =============================================================== 搜索聚合

_CACHE = {}
_CK = threading.Lock()


def _cache_evict():
    """清掉过期的，再按最久未更新淘汰，避免 _CACHE 无限膨胀。"""
    now = time.time()
    dead = [k for k, e in _CACHE.items() if now - e[0] > 3600]
    for k in dead:
        _CACHE.pop(k, None)
    if len(_CACHE) > 200:
        for k in sorted(_CACHE, key=lambda x: _CACHE[x][0])[: len(_CACHE) - 150]:
            _CACHE.pop(k, None)


def cached(key, ttl, fn):
    with _CK:
        hit = _CACHE.get(key)
        if hit and time.time() - hit[0] < (hit[2] if len(hit) > 2 else ttl):
            return hit[1]
    v = fn()
    try:
        size = len(v[0]) if isinstance(v, tuple) else len(v)
    except Exception:
        size = 1
    # 空结果只短存 60 秒：既不会把风控/超时的失败状态钉死一整个 TTL，
    # 也不至于同一关键词每敲一次都重新打远端。
    use_ttl = ttl if size else min(ttl, 60)
    with _CK:
        _CACHE[key] = (time.time(), v, use_ttl)
        _cache_evict()
    return v


def safe(fn, *a, **kw):
    try:
        return fn(*a, **kw), None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


# 每个源独立的等待上限：慢的源直接放弃，不拖累整页返回
JOB_TIMEOUT = {"web": 4.0, "video": 4.5, "film": 2.5, "cms": 5.0}


def _qgrams(q):
    """中文取 2 字片段，英文/数字整词匹配（避免 ai 这种碎片到处命中）。"""
    parts = [p for p in re.split(r"\s+", (q or "").strip().lower()) if p]
    out = []
    for p in parts:
        if re.search(r"[\u4e00-\u9fff]", p):
            out += [p] if len(p) <= 2 else [p[i:i + 2] for i in range(len(p) - 1)]
        else:
            out.append(p)
    return out


def relevance_note(q, videos, web):
    """搜索站对不认识的关键词会塞热门内容，这里给个“相关性低”的提示。"""
    grams = _qgrams(q)
    titles = [v.get("title") or "" for v in videos] + [w.get("title") or "" for w in web]
    if not grams or len(titles) < 5:
        return None
    good = sum(1 for t in titles if any(g in t.lower() for g in grams))
    if good / len(titles) < 0.12:
        return "没搜到与「%s」直接相关的内容，下面是搜索站返回的相近 / 热门结果，换个更具体的词会更准" % q
    return None


def do_search(q, tab="all"):
    t0 = time.time()
    intent = classify(q)
    ids = {i["id"] for i in intent}
    wants_tech = "tech" in ids
    has_sources = any(s.get("enabled", True) for s in load_sources())

    jobs = {"web": lambda: cached("w:" + q, 300, lambda: bing_web(q)),
            "video": lambda: cached("v:" + q, 900, lambda: bili_search(q)),
            "film": lambda: cached("f:" + q, 1800, lambda: douban_suggest(q))}
    # GitHub 的接口在国内经常要 20 秒以上，会把整个搜索拖死，所以不参与聚合。
    # 技术类关键词照样能从网页结果和「技术开发」站点直那里拿到想要的东西。
    if has_sources:
        jobs["cms"] = lambda: cached("c:" + q, 600, lambda: cms_search(q))

    res, errs, times = {}, {}, {}
    ex = ThreadPoolExecutor(max_workers=6)
    try:
        futs = {k: ex.submit(safe, f) for k, f in jobs.items()}
        for k, fu in futs.items():
            st = time.time()
            try:
                v, e = fu.result(timeout=JOB_TIMEOUT.get(k, 4.0))
            except FuturesTimeout:
                errs[k] = "timeout"
                times[k] = int((time.time() - st) * 1000)
                continue
            times[k] = int((time.time() - st) * 1000)
            if e:
                errs[k] = e
            else:
                res[k] = v
    finally:
        # 不等慢线程跑完，直接放它们自生自灭，保证本页立刻返回
        ex.shutdown(wait=False, cancel_futures=True)

    web, ads = res.get("web", ([], 0))
    film = res.get("film", [])
    cms, cms_fails = res.get("cms", ([], []))
    videos = list(res.get("video", [])) + list(cms)

    if film and "film" not in ids and len(film) >= 3:
        if not intent or intent[0]["id"] == "general":
            intent.insert(0, {"id": "film", "name": "影视", "score": 5})

    return {
        "query": q,
        "tab": tab,
        "took_ms": int((time.time() - t0) * 1000),
        "intent": intent,
        "web": web,
        "video": videos,
        "film": film,
        "code": res.get("code", []),
        "sites": match_sites(q, intent),
        "software": match_software(q),
        "note": relevance_note(q, videos, web),
        "logged": bool(_AUTH.get("cookies")),
        "stats": {"ads_filtered": ads, "sources_ok": sorted(res.keys()),
                  "sources_fail": sorted(errs.keys()) + cms_fails,
                  "errors": errs, "has_sources": has_sources, "times": times},
    }


_COMMENT_CACHE = {}


def github_repos(q, limit=6):
    url = ("https://api.github.com/search/repositories?q=" + urllib.parse.quote(q)
           + f"&sort=stars&order=desc&per_page={limit}")
    body = fetch(url, {"Accept": "application/vnd.github+json"}, timeout=8)
    d = json.loads(body)
    return [{"name": it.get("full_name") or "", "desc": (it.get("description") or "").strip(),
             "stars": fmt_count(it.get("stargazers_count")), "lang": it.get("language") or "",
             "url": it.get("html_url") or ""} for it in d.get("items") or []]


# 这些站正文走 JS 渲染或强反爬，硬抓只会让页面转圈十几秒，直接明说
HARD_BLOCK = {
    "csdn.net": "CSDN 有反爬（返回 521），正文抓不到",
    "juejin.cn": "掘金页面已改为前端渲染，抓不到正文",
    "zhuanlan.zhihu.com": "知乎专栏要求登录才给完整正文",
    "zhihu.com": "知乎要求登录才给完整正文",
    "xiaohongshu.com": "小红书要求登录且强反爬",
    "douyin.com": "抖音页面由脚本渲染，抓不到正文",
    "baidu.com": "百度结果页有反爬",
    "weixin.qq.com": "公众号文章需在微信内打开",
}


def read_article(url):
    host = host_of(url)
    for bad, why in HARD_BLOCK.items():
        if host.endswith(bad):
            return {"ok": False, "title": host, "html": "", "chars": 0, "mode": "blocked",
                    "url": url, "site": host, "reason": why}
    page = fetch(url, {"Referer": "https://cn.bing.com/"}, timeout=12)
    r = article.extract(page, url)
    title = ""
    m = re.search(r"<title[^>]*>([\s\S]*?)</title>", page, re.I)
    if m:
        title = strip_tags(m.group(1)).split("_")[0].split(" - ")[0].strip()
    if not title:
        m = re.search(r'<h1[^>]*>([\s\S]*?)</h1>', page, re.I)
        title = strip_tags(m.group(1)) if m else host_of(url)
    return {"ok": r["chars"] >= 200, "title": title, "html": r["html"],
            "chars": r["chars"], "mode": r["mode"], "url": url, "site": host_of(url),
            "reason": "" if r["chars"] >= 200 else "这个站正文是脚本渲染的，抓不到干净内容，建议原站打开"}


def check_sources():
    """并发体检：每个源打一次最小的搜索请求，判 live / slow / dead。"""
    items = [s for s in load_sources() if s.get("enabled", True)]
    if not items:
        return {"ok": True, "results": []}

    def probe(s):
        api = (s.get("api") or "").strip()
        if not api.startswith("http"):
            return {"name": s.get("name") or "", "api": api, "status": "dead", "ms": 0,
                    "msg": "地址不合法"}
        url = api + ("&" if "?" in api else "?") + "ac=videolist&wd=%E9%80%9F&pg=1"
        t = time.time()
        try:
            body = fetch(url, timeout=6)
            ms = int((time.time() - t) * 1000)
            n = 1 if (("<vod" in body) or ("vod_id" in body)) else 0
            if not n:
                return {"name": s.get("name") or "", "api": api, "ms": ms,
                        "status": "dead", "msg": "返回内容不像采集接口"}
            return {"name": s.get("name") or "", "api": api, "ms": ms,
                    "status": "slow" if ms > 2500 else "live", "msg": "%d ms" % ms}
        except Exception as e:
            return {"name": s.get("name") or "", "api": api,
                    "ms": int((time.time() - t) * 1000), "status": "dead",
                    "msg": type(e).__name__}

    with ThreadPoolExecutor(max_workers=12) as ex:
        results = list(ex.map(probe, items))
    return {"ok": True, "results": results}


# =============================================================== HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "PureSearch/2.0"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", cache=None, cors=False):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache or "no-store")
        if cors:
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def do_GET(self):
        p = urllib.parse.urlparse(self.path)
        path, qs = p.path, urllib.parse.parse_qs(p.query)
        g = lambda k, d="": (qs.get(k, [d])[0] or d)

        try:
            if path == "/api/search":
                q = g("q").strip()
                if not q:
                    return self._json({"error": "缺少关键词"}, 400)
                return self._json(do_search(q, g("tab", "all")))

            if path == "/api/asr/status":
                return self._json({"available": asr_available(),
                                   "model_dir": os.path.isdir(ASR_DIR)})

            if path == "/api/more":
                q = g("q").strip()
                if not q:
                    return self._json({"error": "缺少关键词"}, 400)
                return self._json(more_results(q, g("type", "video"), g("page", "2")))

            if path == "/api/play":
                bvid = g("bvid")
                page = int(g("page", "1") or 1)
                try:
                    r = bili_play(bvid, 80, page)
                    r["stream"] = "/api/stream?u=%s&r=%s" % (
                        urllib.parse.quote(r["mp4"], safe=""),
                        urllib.parse.quote("https://www.bilibili.com/video/%s/" % bvid, safe=""))
                    r["backup_stream"] = [
                        "/api/stream?u=%s&r=%s" % (urllib.parse.quote(u, safe=""),
                        urllib.parse.quote("https://www.bilibili.com/", safe=""))
                        for u in r["backup"]]
                    return self._json(r)
                except Exception as e:
                    return self._json({"ok": False, "error": str(e)})

            if path == "/api/cms_play":
                src_name, vid = g("source"), g("id")
                eps = cms_detail(src_name, vid)
                return self._json({"ok": bool(eps), "eps": eps})

            if path == "/api/stream":
                return proxy_stream(self, g("u"), g("r"))

            if path == "/api/img":
                try:
                    ctype, data = proxy_image(g("u"))
                    return self._send(200, data, ctype, cache="public, max-age=3600")
                except Exception:
                    return self._send(404, b"")

            if path == "/api/read":
                return self._json(read_article(g("url")))

            if path == "/api/qr":
                return self._json(qr_generate())

            if path == "/api/qr/poll":
                return self._json(qr_poll(g("key")))

            if path == "/api/auth":
                return self._json(auth_status())

            if path == "/api/sources":
                return self._json({"sources": load_sources()})

            if path == "/api/sources/import":
                return self._json(import_sources(g("url")))

            if path == "/api/sources/check":
                return self._json(check_sources())

            if path == "/api/health":
                return self._json({"ok": True, "time": time.time()})

            if path == "/api/hot":
                return self._json(load_hot())

            if path in ("/", "/index.html"):
                fp = os.path.join(STATIC_DIR, "index.html")
                with open(fp, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")

            if path.startswith("/static/"):
                fp = os.path.join(STATIC_DIR, os.path.basename(path))
                if os.path.exists(fp):
                    ctype = "application/javascript" if fp.endswith(".js") else "application/octet-stream"
                    with open(fp, "rb") as f:
                        return self._send(200, f.read(), ctype, cache="public, max-age=86400")

            if path == "/favicon.ico":
                return self._send(204, b"")

            return self._json({"error": "not found"}, 404)
        except BrokenPipeError:
            pass
        except Exception as e:
            try:
                self._json({"error": f"{type(e).__name__}: {e}"}, 500)
            except Exception:
                pass

    def do_POST(self):
        p = urllib.parse.urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b"{}"
        try:
            data = json.loads(body or b"{}")
        except Exception:
            data = {}

        if p.path == "/api/asr":
            raw = (data.get("audio") or "").strip()
            if raw.startswith("data:"):
                raw = raw.split(",", 1)[-1]
            try:
                wav = base64.b64decode(raw, validate=False)
            except Exception:
                return self._json({"ok": False, "error": "音频数据解析失败"}, 400)
            try:
                return self._json(asr_text(wav))
            except Exception as e:
                return self._json({"ok": False, "error": "%s: %s" % (type(e).__name__, e)})

        if p.path == "/api/sources":
            srcs = data.get("sources")
            if not isinstance(srcs, list):
                return self._json({"error": "sources 必须是数组"}, 400)
            clean = []
            for s in srcs[:60]:
                api = (s.get("api") or "").strip()
                if not api.startswith("http"):
                    continue
                clean.append({"name": (s.get("name") or host_of(api))[:24],
                              "api": api, "enabled": bool(s.get("enabled", True))})
            save_sources(clean)
            return self._json({"ok": True, "sources": clean})

        if p.path == "/api/auth/logout":
            _AUTH.update({"cookies": {}, "uname": "", "uid": 0, "at": 0})
            save_auth()
            for ck in list(COOKIE_JAR):
                if "bilibili" in ck.domain:
                    try:
                        COOKIE_JAR.clear(ck.domain, ck.path, ck.name)
                    except Exception:
                        pass
            return self._json({"ok": True})

        return self._json({"error": "not found"}, 404)


def import_sources(url):
    """支持 LibreTV 那种源列表 JSON（数组或 {sources:[...]}），把 api 字段抽出来。"""
    if not url or not url.startswith("http"):
        raise ValueError("请填订阅链接")
    text = fetch(url, timeout=15)
    d = json.loads(text)
    items = d if isinstance(d, list) else (d.get("sources") or d.get("data") or [])
    conv = []
    for x in items[:30]:
        if not isinstance(x, dict):
            continue
        api = (x.get("api") or x.get("url") or "").strip()
        if not api.startswith("http"):
            continue
        conv.append({"name": (x.get("name") or host_of(api))[:24],
                     "api": api, "enabled": True})
    if not conv:
        raise ValueError("这个链接里没有可用的 api 字段")
    return {"ok": True, "sources": conv, "count": len(conv)}


def cms_detail(src_name, vid):
    for s in load_sources():
        if (s.get("name") or host_of(s["api"])) == src_name:
            base = s["api"]
            sep = "&" if "?" in base else "?"
            text = fetch(f"{base}{sep}ac=detail&ids={urllib.parse.quote(vid)}", timeout=9)
            vids = _xml_videos(text) if not text.lstrip().startswith("{") else []
            if vids:
                return vids[0]["eps"]
    return []


def find_port(preferred=8766):
    for p in [preferred, 8765, 8767, 8770, 8899]:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def main():
    def _log(msg):
        # 打包成 exe（无控制台）时也能排查启动问题
        try:
            with open(os.path.join(tempfile.gettempdir(), "puresearch_start.log"), "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
        except Exception:
            pass

    _log("boot: frozen=%s res=%s base=%s" % (getattr(sys, "frozen", False), RES_DIR, BASE_DIR))
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception as e:
        _log("stdout reconfigure failed: %r" % (e,))
    _log("step: load_auth")
    load_auth()
    _log("step: find_port")
    port = find_port()
    _log("port=%d" % port)
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    _log("bound")
    url = f"http://127.0.0.1:{port}/"
    print("=" * 52)
    print("  纯净搜索 · 站内观看版 已启动")
    print(f"  地址：{url}")
    st = auth_status()
    print("  登录：" + (f"{st['uname']}（已授权）" if st["logged"] else "未登录（可在页面右上角扫码）"))
    print("  关闭：Ctrl+C 或直接关掉这个窗口")
    print("=" * 52)
    if "--no-open" not in sys.argv:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        _log("serving " + url)
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()
