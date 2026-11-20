#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""正文提取：把任意网页压成干净的文章 HTML。

只保留阅读需要的标签，脚本/样式/表单/导航一律丢掉，
图片统一改走本地代理，避免防盗链导致全站图片裂开。
"""

import html
import re
import urllib.parse
from html.parser import HTMLParser

ALLOWED = {
    "p", "h1", "h2", "h3", "h4", "img", "a", "ul", "ol", "li",
    "blockquote", "code", "pre", "strong", "em", "b", "i", "br", "hr",
    "figure", "figcaption", "table", "thead", "tbody", "tr", "td", "th",
}
VOID = {"img", "br", "hr"}
DROP = {
    "script", "style", "noscript", "iframe", "svg", "form", "nav", "aside",
    "footer", "header", "button", "input", "select", "textarea", "canvas",
    "video", "audio", "object", "embed", "template",
}


class Cleaner(HTMLParser):
    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.out = []
        self.drop = 0
        self.text_len = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in DROP:
            if tag not in VOID:
                self.drop += 1
            return
        if self.drop:
            return
        d = dict(attrs)
        if tag == "img":
            src = d.get("src") or d.get("data-src") or d.get("data-original") or ""
            if not src or src.startswith("data:"):
                return
            src = urllib.parse.urljoin(self.base, src)
            self.out.append('<img src="/api/img?u=%s" loading="lazy">'
                            % urllib.parse.quote(src, safe=""))
            return
        if tag not in ALLOWED:
            return
        if tag == "a":
            href = (d.get("href") or "").strip()
            if href.startswith(("javascript:", "#", "mailto:", "tel:")):
                href = ""
            if href:
                href = urllib.parse.urljoin(self.base, href)
                self.out.append('<a href="%s" target="_blank" rel="noopener">'
                                % html.escape(href, quote=True))
            else:
                self.out.append("<a>")
            return
        if tag in VOID:
            self.out.append("<%s>" % tag)
            return
        self.out.append("<%s>" % tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in DROP:
            if tag not in VOID and self.drop:
                self.drop -= 1
            return
        if self.drop:
            return
        if tag in ALLOWED and tag not in VOID:
            self.out.append("</%s>" % tag)

    def handle_data(self, data):
        if self.drop:
            return
        t = data.replace("\u00a0", " ")
        if t.strip():
            self.text_len += len(t.strip())
        self.out.append(html.escape(t))


def _plain(s):
    s = re.sub(r"<(script|style)[^>]*>[\s\S]*?</\1>", " ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(re.sub(r"\s+", " ", s)).strip()


def pick_body(page):
    """先找 <article>，再找常见正文容器，最后退回所有段落。"""
    m = re.search(r"<article[^>]*>([\s\S]*?)</article>", page, re.I)
    if m and len(_plain(m.group(1))) > 200:
        return m.group(1), "article"

    best, best_len = None, 0
    for m in re.finditer(r"<(div|section)[^>]*\b(?:id|class)=\"[^\"]*"
                         r"(?:article|content|post|main|entry|detail|markdown)[^\"]*\"[^>]*>",
                         page, re.I):
        # 从起点往后截取一段，按长度粗筛，不追求严格配平
        chunk = page[m.end(): m.end() + 120000]
        l = len(_plain(chunk))
        if l > best_len and l > 300:
            best, best_len = chunk, l
    if best:
        return best, "container"

    paras = re.findall(r"<p[^>]*>([\s\S]{20,}?)</p>", page, re.I)
    if len("".join(paras)) > 300:
        return "<p>" + "</p><p>".join(paras) + "</p>", "paragraphs"
    return page, "full"


def extract(page, url):
    body, mode = pick_body(page)
    c = Cleaner(url)
    try:
        c.feed(body)
    except Exception:
        pass
    out = "".join(c.out)
    out = re.sub(r"(?:<p>\s*</p>|<p> ?</p>)+", "", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return {
        "html": out.strip(),
        "chars": c.text_len,
        "mode": mode,
    }
