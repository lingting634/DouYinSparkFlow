# -*- coding: utf-8 -*-
"""通过 GitHub API 推送本地文件（git 走代理卡住时的备用通道）。

把若干本地文件的内容作为一个新提交直接写到远端分支，不需要 git 联网。

用法：
    python tools/gh_push.py core/tasks.py utils/config.py --message "说明"
    python tools/gh_push.py tools/ --branch dev
"""
import argparse
import base64
import json
import os
import re
import subprocess
import sys
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = "https://api.github.com"


def _remote():
    url = subprocess.run(["git", "config", "--get", "remote.origin.url"],
                         capture_output=True, text=True, cwd=BASE).stdout.strip()
    m = re.search(r"://[^:]+:([^@]+)@github\.com[/:]([^/]+/[^/.]+)", url)
    if not m:
        sys.exit("[ERROR] 无法从 git remote 解析 token / 仓库名")
    return m.group(1), m.group(2)


TOKEN, REPO = _remote()


def api(path, method="GET", payload=None):
    headers = {
        "Authorization": f"token {TOKEN}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "gh-push",
    }
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{API}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            body = r.read().decode()
            return r.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode()[:400]}


def collect(paths):
    files = []
    for p in paths:
        p = os.path.join(BASE, p) if not os.path.isabs(p) else p
        if os.path.isdir(p):
            for root, _, names in os.walk(p):
                for n in names:
                    if n.endswith((".pyc",)) or "__pycache__" in root:
                        continue
                    files.append(os.path.join(root, n))
        else:
            files.append(p)
    out = []
    for f in files:
        rel = os.path.relpath(f, BASE).replace("\\", "/")
        out.append((rel, open(f, "rb").read()))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--branch", default="dev")
    ap.add_argument("--message", default="chore: 通过 API 同步文件")
    args = ap.parse_args()

    files = collect(args.paths)
    if not files:
        sys.exit("[ERROR] 没有找到要推送的文件")
    print(f"仓库 {REPO} / 分支 {args.branch}，待提交 {len(files)} 个文件：")
    for rel, data in files:
        print(f"  - {rel} ({len(data)} 字节)")

    status, ref = api(f"/repos/{REPO}/git/ref/heads/{args.branch}")
    if status != 200:
        sys.exit(f"[ERROR] 取分支失败：{status} {ref}")
    head_sha = ref["object"]["sha"]
    status, commit = api(f"/repos/{REPO}/git/commits/{head_sha}")
    base_tree = commit["tree"]["sha"]
    print(f"当前 HEAD: {head_sha[:7]}")

    tree_items = []
    for rel, data in files:
        status, blob = api(f"/repos/{REPO}/git/blobs", "POST", {
            "content": base64.b64encode(data).decode(),
            "encoding": "base64",
        })
        if status != 201:
            sys.exit(f"[ERROR] 上传 {rel} 失败：{status} {blob}")
        tree_items.append({"path": rel, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        print(f"  [OK] blob {rel} -> {blob['sha'][:7]}")

    status, tree = api(f"/repos/{REPO}/git/trees", "POST",
                       {"base_tree": base_tree, "tree": tree_items})
    if status != 201:
        sys.exit(f"[ERROR] 建 tree 失败：{status} {tree}")

    status, new_commit = api(f"/repos/{REPO}/git/commits", "POST", {
        "message": args.message, "tree": tree["sha"], "parents": [head_sha],
    })
    if status != 201:
        sys.exit(f"[ERROR] 建提交失败：{status} {new_commit}")

    status, res = api(f"/repos/{REPO}/git/refs/heads/{args.branch}", "PATCH",
                      {"sha": new_commit["sha"]})
    if status not in (200, 201):
        sys.exit(f"[ERROR] 更新分支失败：{status} {res}")

    print(f"\n[OK] 已推送，新提交 {new_commit['sha'][:7]}")
    print(f"     https://github.com/{REPO}/commit/{new_commit['sha']}")


if __name__ == "__main__":
    main()
