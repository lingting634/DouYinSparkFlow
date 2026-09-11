# -*- coding: utf-8 -*-
"""扫码登录并自动导出抖音 Cookie（本地一键修复）。

会弹出一个浏览器窗口，用手机抖音 APP 扫码登录后，脚本自动：
  1. 抓取 douyin.com 全部 Cookie
  2. 校验登录后的聊天列表能否正常加载
  3. 写入本地 .env，并同步更新 GitHub Secret
  4. 可选：立即触发一次云端续火花（--trigger）

用法：
    python tools/login_and_export.py
    python tools/login_and_export.py --trigger          # 登录成功后立刻跑一次云端任务
    python tools/login_and_export.py --unique-id 87392064267 --timeout 600
"""
import argparse
import datetime
import json
import os
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(BASE)
sys.path.insert(0, BASE)

os.environ["PLAYWRIGHT_BROWSERS_PATH"] = os.path.abspath(os.path.join(BASE, "chrome"))

from playwright.sync_api import sync_playwright  # noqa: E402

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

LOGIN_HINT_WORDS = ["扫码登录", "验证码登录", "密码登录"]
CONV_SELECTORS = [
    ".conversationConversationListwrapper",
    "[class*='conversation'][class*='List']",
    "[class*='Conversation'][class*='List']",
]


def log(msg):
    print(msg, flush=True)


def is_logged_in(ctx, page):
    """判断是否已登录：sessionid 出现 且 页面上不再显示登录框。"""
    try:
        for c in ctx.cookies():
            if c["name"] in ("sessionid", "sessionid_ss") and len(c.get("value", "")) > 10:
                try:
                    body = page.locator("body").inner_text(timeout=3000)
                except Exception:
                    body = ""
                if any(w in body for w in LOGIN_HINT_WORDS):
                    return False
                return True
    except Exception:
        pass
    return False


def to_cookie_editor_format(pw_cookies):
    """把 Playwright cookie 转成 Cookie-Editor 导出的 JSON 结构（兼容项目解析）。"""
    same_map = {"None": "no_restriction", "Lax": "lax", "Strict": "strict", "Unspecified": "unspecified"}
    out = []
    for c in pw_cookies:
        item = {
            "domain": c.get("domain", ".douyin.com"),
            "name": c["name"],
            "value": c["value"],
            "path": c.get("path", "/"),
            "secure": bool(c.get("secure", False)),
            "httpOnly": bool(c.get("httpOnly", False)),
            "sameSite": same_map.get(c.get("sameSite", ""), "unspecified"),
            "hostOnly": not str(c.get("domain", "")).startswith("."),
            "session": c.get("expires", -1) in (-1, None),
        }
        if c.get("expires", -1) and c["expires"] > 0:
            item["expirationDate"] = c["expires"]
        out.append(item)
    return out


def trigger_cloud(unique_id):
    try:
        from tools.update_cookies import api, gh_token, repo_name
        token = gh_token()
        repo = repo_name()
        status, res = api(
            f"https://api.github.com/repos/{repo}/actions/workflows/schedule_dev.yml/dispatches",
            token, "POST", {"ref": "dev"},
        )
        if status in (201, 204):
            log(f"  [OK] 已触发云端任务：https://github.com/{repo}/actions")
        else:
            log(f"  [!] 触发云端任务失败：{status} {res}")
    except Exception as e:
        log(f"  [!] 触发云端任务异常：{e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--unique-id", default="87392064267")
    ap.add_argument("--timeout", type=int, default=300, help="等待扫码的最长秒数")
    ap.add_argument("--trigger", action="store_true", help="登录成功后立刻触发一次云端任务")
    ap.add_argument("--no-cloud", action="store_true")
    ap.add_argument("--no-local", action="store_true")
    args = ap.parse_args()

    from tools.update_cookies import load_cookies, report, update_env, update_cloud  # noqa

    log("=" * 62)
    log("抖音 Cookie 一键刷新（扫码登录 -> 自动导出 -> 自动同步）")
    log("=" * 62)

    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(
        headless=False,
        args=["--disable-blink-features=AutomationControlled",
              "--disable-infobars", "--start-maximized"],
    )
    ctx = browser.new_context(
        user_agent=UA, locale="zh-CN", timezone_id="Asia/Shanghai",
        viewport={"width": 1440, "height": 900},
    )
    ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
    page = ctx.new_page()

    log("\n[1/5] 打开抖音首页…")
    try:
        page.goto("https://www.douyin.com", wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        log(f"  页面加载较慢（可忽略）：{e}")

    log("\n" + "!" * 62)
    log("  请在弹出的浏览器窗口里用手机抖音 APP 扫码登录（或验证码登录）")
    log("  登录成功后本脚本会自动继续，无需手动操作")
    log(f"  最长等待 {args.timeout} 秒")
    log("!" * 62)

    deadline = time.time() + args.timeout
    ok = False
    while time.time() < deadline:
        if is_logged_in(ctx, page):
            ok = True
            break
        time.sleep(2)
        left = int(deadline - time.time())
        if left % 20 == 0 and left > 0:
            log(f"  …等待登录中（剩余 {left}s）")

    if not ok:
        log("\n[X] 等待超时，未检测到登录成功。请重新运行本脚本再试。")
        browser.close()
        playwright.stop()
        sys.exit(2)

    log("\n[2/5] 登录成功！正在读取 Cookie…")
    time.sleep(3)
    all_cookies = ctx.cookies()
    douyin = [c for c in all_cookies if "douyin.com" in c.get("domain", "")]
    log(f"  抓到 douyin.com 相关 Cookie {len(douyin)} 个（全部 {len(all_cookies)} 个）")

    log("\n[3/5] 校验聊天页能否正常加载…")
    try:
        page.goto("https://www.douyin.com/chat", wait_until="domcontentloaded", timeout=60000)
        time.sleep(10)
        hit = None
        for s in CONV_SELECTORS:
            try:
                if page.locator(s).count() > 0:
                    hit = s
                    break
            except Exception:
                continue
        log(f"  聊天列表选择器命中：{hit or '未命中'}")
        os.makedirs(os.path.join("logs", "debug"), exist_ok=True)
        page.screenshot(path=os.path.join("logs", "debug", "login-verify.png"), timeout=20000)
        log("  已验证截图：logs/debug/login-verify.png")
    except Exception as e:
        log(f"  [!] 聊天页校验异常（不影响导出）：{e}")

    ck = to_cookie_editor_format(douyin)
    out_path = os.path.join("logs", "new_cookies.json")
    json.dump(ck, open(out_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    log(f"  已保存原始导出：{out_path}")

    log("\n[4/5] 写入本地与云端…")
    if not report(ck):
        log("  [!] 关键 Cookie 缺失，请确认是否真的登录成功")
        browser.close()
        playwright.stop()
        sys.exit(3)
    if not args.no_local:
        update_env(ck, args.unique_id)
    if not args.no_cloud:
        from tools.update_cookies import gh_token, repo_name, update_cloud
        update_cloud(ck, f"COOKIES_{args.unique_id.upper()}", gh_token(), repo_name())

    log("\n[5/5] 收尾…")
    history = os.path.join("logs", "cookie-history")
    os.makedirs(history, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    json.dump(ck, open(os.path.join(history, f"cookies-{stamp}.json"), "w", encoding="utf-8"),
              ensure_ascii=False)
    log(f"  已归档备份：logs/cookie-history/cookies-{stamp}.json")

    if args.trigger and not args.no_cloud:
        trigger_cloud(args.unique_id)

    log("\n完成，浏览器将在 5 秒后自动关闭。")
    time.sleep(5)
    browser.close()
    playwright.stop()
    log("全部搞定。")


if __name__ == "__main__":
    main()
