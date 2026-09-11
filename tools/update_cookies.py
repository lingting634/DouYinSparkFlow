# -*- coding: utf-8 -*-
"""一键更新抖音 Cookie：同时写入本地 .env 和 GitHub Repository Secret。

用法：
    python tools/update_cookies.py new_cookies.json
    python tools/update_cookies.py new_cookies.json --unique-id 87392064267
    python tools/update_cookies.py new_cookies.json --no-cloud      # 只更新本地
    python tools/update_cookies.py new_cookies.json --no-local      # 只更新云端

Cookie JSON 来源：浏览器 Cookie-Editor 插件在 www.douyin.com 上 Export -> JSON。
"""
import argparse
import base64
import json
import os
import re
import subprocess
import sys
import urllib.request
import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(BASE, ".env")

KEY_COOKIES = ["sessionid", "sessionid_ss", "sid_tt", "uid_tt",
               "passport_csrf_token", "ttwid", "odin_tt"]


def die(msg):
    print(f"[ERROR] {msg}")
    sys.exit(1)


def load_cookies(path):
    raw = open(path, encoding="utf-8").read().strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        die(f"Cookie 文件不是合法 JSON：{e}")
    # Cookie-Editor 有时会导出成 {"cookies":[...]} 或分号字符串
    if isinstance(data, dict):
        for k in ("cookies", "Cookies", "data"):
            if k in data and isinstance(data[k], list):
                data = data[k]
                break
    if not isinstance(data, list) or not data:
        die("Cookie JSON 应为数组（Cookie-Editor 的 Export -> JSON）")
    for c in data:
        if not isinstance(c, dict) or "name" not in c or "value" not in c:
            die(f"Cookie 项缺少 name/value：{c!r}")
    return data


def report(cookies):
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    names = {c["name"] for c in cookies}
    print(f"  共 {len(cookies)} 个 Cookie")
    missing = [k for k in KEY_COOKIES if k not in names]
    if missing:
        print(f"  [!] 缺少关键登录 Cookie：{', '.join(missing)}")
        print("      -> 请确认导出前已在 www.douyin.com 登录成功")
    else:
        print("  [OK] 关键登录 Cookie 齐全")
    for c in cookies:
        if c["name"] in ("sessionid", "sessionid_ss", "sid_tt", "uid_tt"):
            exp = c.get("expirationDate")
            if exp:
                left = (exp - now) / 86400
                print(f"      {c['name']:<16} 剩余 {left:.1f} 天")
    return not missing


def update_env(cookies, unique_id):
    key = f"COOKIES_{unique_id.upper()}"
    value = json.dumps(cookies, ensure_ascii=False, separators=(",", ":"))
    text = open(ENV_PATH, encoding="utf-8").read()
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.M)
    if pattern.search(text):
        text = pattern.sub(lambda m: f"{key}={value}", text, count=1)
    else:
        if not text.endswith("\n"):
            text += "\n"
        text += f"{key}={value}\n"
    open(ENV_PATH, "w", encoding="utf-8").write(text)
    print(f"  [OK] 已写入 {ENV_PATH}（{key}）")


def gh_token():
    url = subprocess.run(
        ["git", "config", "--get", "remote.origin.url"],
        capture_output=True, text=True, cwd=BASE,
    ).stdout.strip()
    m = re.search(r"://[^:]+:([^@]+)@", url)
    if not m:
        die("无法从 git remote 读取 token，请用 --token 指定")
    return m.group(1)


def repo_name():
    url = subprocess.run(
        ["git", "config", "--get", "remote.origin.url"],
        capture_output=True, text=True, cwd=BASE,
    ).stdout.strip()
    m = re.search(r"github\.com[/:]([^/]+/[^/.]+)", url)
    if not m:
        die("无法从 git remote 解析仓库名")
    return m.group(1)


def api(url, token, method="GET", payload=None):
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "spark-cookie-updater",
    }
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read().decode()
        return r.status, (json.loads(body) if body else {})


def update_cloud(cookies, secret_name, token, repo):
    from nacl import encoding, public

    status, pub = api(
        f"https://api.github.com/repos/{repo}/actions/secrets/public-key", token
    )
    if status != 200:
        die(f"获取仓库公钥失败：{status} {pub}")
    sealed = public.SealedBox(public.PublicKey(pub["key"].encode(), encoding.Base64Encoder))
    value = json.dumps(cookies, ensure_ascii=False, separators=(",", ":"))
    encrypted = base64.b64encode(sealed.encrypt(value.encode())).decode()
    status, res = api(
        f"https://api.github.com/repos/{repo}/actions/secrets/{secret_name}",
        token, "PUT", {"encrypted_value": encrypted, "key_id": pub["key_id"]},
    )
    if status in (201, 204):
        print(f"  [OK] 已更新 GitHub Secret：{repo} / {secret_name}")
    else:
        die(f"更新 Secret 失败：{status} {res}")


def verify(cookies, repo=None, token=None, unique_id=None):
    """列出云端所有 Secret 名称，确认目标存在（不读取值）。"""
    if not (repo and token):
        return
    try:
        status, res = api(f"https://api.github.com/repos/{repo}/actions/secrets", token)
        names = [s["name"] for s in res.get("secrets", [])]
        print(f"  云端现有 Secret：{', '.join(sorted(names))}")
    except Exception as e:
        print(f"  [!] 查询 Secret 列表失败（不影响本次更新）：{e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cookie_file")
    ap.add_argument("--unique-id", default="87392064267")
    ap.add_argument("--secret-name", default=None)
    ap.add_argument("--token", default=None)
    ap.add_argument("--no-local", action="store_true")
    ap.add_argument("--no-cloud", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.cookie_file):
        die(f"文件不存在：{args.cookie_file}")

    cookies = load_cookies(args.cookie_file)
    print("[1/3] 校验 Cookie")
    ok = report(cookies)
    if not ok:
        print("  [!] 关键 Cookie 缺失，仍会写入，但很可能依然登录不上")

    if not args.no_local:
        print("[2/3] 更新本地 .env")
        update_env(cookies, args.unique_id)
    else:
        print("[2/3] 跳过本地")

    if not args.no_cloud:
        print("[3/3] 更新云端 Secret")
        token = args.token or gh_token()
        repo = repo_name()
        secret = args.secret_name or f"COOKIES_{args.unique_id.upper()}"
        update_cloud(cookies, secret, token, repo)
        verify(cookies, repo, token)
    else:
        print("[3/3] 跳过云端")

    print("\n完成。可手动触发一次云端运行：")
    print(f"  https://github.com/{repo_name()}/actions/workflows/schedule_dev.yml")


if __name__ == "__main__":
    main()
