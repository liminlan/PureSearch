#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日热词更新脚本

从 B 站热搜榜取当天热门词，只保留生活 / 娱乐 / 数码 / 学习这类「兴趣领域」的词，
再合并几个固定的常用词，写进 hot.json。首页的「今日热搜」和输入联想就读这个文件。

用法：
    python update_daily.py        手动更新一次（也可以双击 update_hot.bat）

挂成每天自动跑（Windows 计划任务，在项目目录里执行一次即可）：
    schtasks /create /tn "PureSearch-hot" /tr "pythonw update_daily.py" /sc daily /st 08:00

脚本不会改动 hot.json 以外的任何文件；服务不用重启，刷新网页即可看到新词。
"""

import json
import os
import re
import sys
import time
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
HOT_FILE = os.path.join(BASE, "hot.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 只收这些兴趣领域的词，其它一律跳过（白名单策略，宁缺毋滥）
NICHE = [
    "剪辑", "教程", "编程", "代码", "python", "java", "ai", "人工智能", "软件", "电脑",
    "手机", "数码", "测评", "相机", "摄影", "后期", "设计", "画", "建模", "动画",
    "游戏", "电竞", "手游", "主机", "单机", "攻略", "开黑",
    "电影", "剧", "综艺", "动漫", "番剧", "纪录片", "预告", "名场面",
    "音乐", "歌", "翻唱", "舞蹈", "演奏", "钢琴", "吉他", "演唱会",
    "美食", "做饭", "菜谱", "烘焙", "探店", "咖啡", "奶茶",
    "旅行", "旅游", "攻略", "风景", "露营", "徒步",
    "健身", "跑步", "篮球", "足球", "球赛", "瑜伽", "减脂",
    "考研", "英语", "学习", "考试", "论文", "面试", "求职", "简历",
    "穿搭", "美妆", "护肤", "好物", "省钱", "手工", "宠物", "猫", "狗",
    "搞笑", "沙雕", "整活", "vlog", "生活", "日常",
]

# 固定保留的常用词，保证首页永远有几个能直接用的词
KEEP = ["剪辑教程", "PPT 模板", "Python 爬虫", "考研数学", "AI 绘画", "长沙美食", "电影解说", "手机摄影"]

TARGET = 12
MAX_LEN = 14
SHORT_MAX = 8   # 首页词条要短，长句塞不下


def fetch_json(url, timeout=10):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Referer": "https://www.bilibili.com/",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def normalize(word):
    word = re.sub(r"\s+", " ", str(word or "")).strip().strip("#＃ ")
    return word if 2 <= len(word) <= MAX_LEN else ""


def in_niche(word):
    low = word.lower()
    return any(k in low for k in NICHE)


def collect():
    words = []
    try:
        d = fetch_json("https://api.bilibili.com/x/web-interface/search/square?limit=20")
        trending = ((d.get("data") or {}).get("trending") or {}).get("list") or []
        kept = 0
        for it in trending:
            w = normalize(it.get("keyword") or it.get("name"))
            if w and in_niche(w) and len(w) <= SHORT_MAX:
                words.append(w)
                kept += 1
        print("[hot] 热搜 %d 条，命中兴趣领域 %d 条" % (len(trending), kept))
    except Exception as e:
        print("[hot] 热搜获取失败：%s: %s" % (type(e).__name__, e))

    for w in KEEP:
        words.append(w)

    seen, out = set(), []
    for w in words:
        if w in seen:
            continue
        seen.add(w)
        out.append(w)
    return out[:TARGET]


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    words = collect()
    payload = {
        "words": words,
        "at": time.strftime("%Y-%m-%d %H:%M"),
        "source": "bilibili-trending+builtin",
    }
    with open(HOT_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print("[hot] 已写入 hot.json")
    print("[hot] 共 %d 个词：%s" % (len(words), "、".join(words)))
    print("[hot] 刷新网页即可看到，服务不用重启")


if __name__ == "__main__":
    main()
