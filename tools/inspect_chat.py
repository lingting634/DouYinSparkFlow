# -*- coding: utf-8 -*-
"""只读诊断：点开指定好友的会话，把「消息区」的真实 DOM 结构打出来（不发送任何消息）。

用途：抖音前端改版后，用这个工具拿到真实类名，再回去更新 core/auto_reply.py 的选择器。

用法：
    python tools/inspect_chat.py 奎奎
    python tools/inspect_chat.py 岩岩 --wait 8
"""
import argparse
import json
import os
import re
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(BASE)
sys.path.insert(0, BASE)
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = os.path.abspath(os.path.join(BASE, "chrome"))

if os.path.exists(".env"):
    from dotenv import load_dotenv

    load_dotenv(".env")

from playwright.sync_api import sync_playwright  # noqa: E402

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
REGION_CANDIDATES = [
    ".messageMessageListlist", ".messageMessageListwrapper",
    "[class*='messageMessageList']", "[class*='Message'][class*='List']",
    "[class*='message'][class*='list']",
]


def load_cookies():
    raw = open(".env", encoding="utf-8").read()
    m = re.search(r"^COOKIES_87392064267=(.*)$", raw, re.M)
    if not m:
        sys.exit("[ERROR] .env 里没有 COOKIES_87392064267")
    return json.loads(m.group(1).strip().strip('"'))


def to_pw(cookies):
    out = []
    for c in cookies:
        d = {"name": c["name"], "value": c["value"],
             "domain": c.get("domain", ".douyin.com"), "path": c.get("path", "/"),
             "secure": bool(c.get("secure", True)), "httpOnly": bool(c.get("httpOnly", False))}
        if c.get("expirationDate"):
            d["expires"] = c["expirationDate"]
        out.append(d)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("friend", nargs="?", default="", help="要点开的会话名（留空＝只列会话）")
    ap.add_argument("--wait", type=float, default=6.0, help="点开后等待秒数")
    args = ap.parse_args()

    p = sync_playwright().start()
    browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
    try:
        ctx = browser.new_context(user_agent=UA, locale="zh-CN", timezone_id="Asia/Shanghai",
                                  viewport={"width": 1440, "height": 900})
        ctx.add_cookies(to_pw(load_cookies()))
        page = ctx.new_page()
        page.goto("https://www.douyin.com/chat", wait_until="domcontentloaded", timeout=60000)
        for _ in range(30):
            if page.locator("[data-e2e='conversation-item']").count() > 0:
                break
            time.sleep(1)
        items = page.locator("[data-e2e='conversation-item']").all()
        print(f"[1] 会话项 {len(items)} 个")
        target = None
        for it in items:
            try:
                name = it.locator(".conversationConversationItemtitle").first.inner_text(timeout=1500).strip()
            except Exception:
                name = "?"
            unread = it.locator("[class*='badge']").count()
            print(f"    - {name} | 角标元素 {unread} 个")
            if args.friend and args.friend in name:
                target = it
        if target is None:
            print("[!] 没找到目标会话" if args.friend else "[i] 未指定会话，结束")
            return
        target.click()
        print(f"[2] 已点开，等待 {args.wait}s 让消息区渲染…")
        time.sleep(args.wait)

        print("[3] 消息区候选选择器命中情况：")
        hit = None
        for sel in REGION_CANDIDATES:
            try:
                n = page.locator(sel).count()
            except Exception:
                n = -1
            print(f"    {sel:<46} -> {n}")
            if n > 0 and hit is None:
                hit = sel
        if hit:
            region = page.locator(hit).first
            kids = region.locator("> *").all()
            print(f"[4] {hit} 的直接子元素 {len(kids)} 个，最后 8 个：")
            for k in kids[-8:]:
                cls = ""
                try:
                    cls = k.get_attribute("class") or ""
                except Exception:
                    pass
                try:
                    t = re.sub(r"\s+", " ", (k.inner_text(timeout=1500) or "")).strip()[:46]
                except Exception:
                    t = ""
                print(f"    [{cls[:64]}] {t}")

        html = page.content()
        os.makedirs(os.path.join("logs", "debug"), exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        out = os.path.join("logs", "debug", f"chat-panel-{stamp}.html")
        with open(out, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"[5] 整页 HTML 已存：{out}")
    finally:
        try:
            browser.close()
        except Exception:
            pass
        try:
            p.stop()
        except Exception:
            pass


if __name__ == "__main__":
    main()
