# -*- coding: utf-8 -*-
"""只读登录态体检：用 `.env` 里的 Cookie 打开抖音聊天页，判断登录态是否有效。

**不会输入、不会发送任何消息**，只做一次页面加载 + 选择器检测。

用法：
    python tools/check_login.py
    python tools/check_login.py --unique-id 87392064267

退出码：0 = 登录态有效；2 = 登录态失效（需要重新导出 Cookie）
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

from playwright.sync_api import sync_playwright  # noqa: E402

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
LIST_SELECTORS = [
    ".conversationConversationListwrapper",
    "[class*='conversation'][class*='List']",
    "[class*='Conversation'][class*='List']",
]
LOGIN_WORDS = ["扫码登录", "验证码登录", "密码登录"]


def load_cookies(unique_id):
    raw = open(os.path.join(BASE, ".env"), encoding="utf-8").read()
    m = re.search(rf"^COOKIES_{unique_id.upper()}=(.*)$", raw, re.M)
    if not m:
        sys.exit(f"[ERROR] .env 里没有 COOKIES_{unique_id.upper()}")
    return json.loads(m.group(1).strip().strip('"'))


def to_pw(cookies):
    out = []
    for c in cookies:
        d = {"name": c["name"], "value": c["value"],
             "domain": c.get("domain", ".douyin.com"),
             "path": c.get("path", "/"),
             "secure": bool(c.get("secure", True)),
             "httpOnly": bool(c.get("httpOnly", False))}
        if c.get("expirationDate"):
            d["expires"] = c["expirationDate"]
        out.append(d)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--unique-id", default="87392064267")
    args = ap.parse_args()

    cookies = load_cookies(args.unique_id)
    print(f"读取 Cookie {len(cookies)} 个，开始只读体检（不会发送任何消息）…")

    p = sync_playwright().start()
    browser = p.chromium.launch(
        headless=True,
        args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
    )
    try:
        ctx = browser.new_context(user_agent=UA, locale="zh-CN",
                                  timezone_id="Asia/Shanghai",
                                  viewport={"width": 1280, "height": 800})
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        ctx.add_cookies(to_pw(cookies))
        page = ctx.new_page()
        try:
            page.goto("https://www.douyin.com/chat", wait_until="domcontentloaded",
                      timeout=60000)
        except Exception as e:
            print(f"  页面加载告警（可忽略）：{str(e)[:80]}")
        time.sleep(10)

        hit = None
        for s in LIST_SELECTORS:
            try:
                if page.locator(s).count() > 0:
                    hit = s
                    break
            except Exception:
                continue
        try:
            body = page.locator("body").inner_text(timeout=5000)
        except Exception:
            body = ""
        showed_login = any(w in body for w in LOGIN_WORDS)

        # 留一张截图，方便核对会话列表里最后一条消息和时间（发送后核验用）
        shot = os.path.join("logs", "debug", "check-login.png")
        try:
            os.makedirs(os.path.dirname(shot), exist_ok=True)
            page.screenshot(path=shot, timeout=20000)
            print(f"  已保存截图: {shot}")
        except Exception as e:
            print(f"  截图失败（可忽略）：{str(e)[:60]}")

        print(f"  URL: {page.url}")
        print(f"  聊天列表选择器命中: {hit or '未命中'}")
        print(f"  是否弹出登录框: {'是' if showed_login else '否'}")
        if hit and not showed_login:
            print("\n结论：登录态【有效】✅ 可以走本机通道发送（推荐；云端本月起会被风控作废登录态）")
            rc = 0
        else:
            print("\n结论：登录态【已失效】❌ 需要重新导出 Cookie（tools/login_and_export.py）")
            rc = 2
    finally:
        browser.close()
        p.stop()
    sys.exit(rc)


if __name__ == "__main__":
    main()
