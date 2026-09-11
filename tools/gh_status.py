# -*- coding: utf-8 -*-
"""查看云端续火花任务状态 / 拉取运行日志（无需 git 联网）。

用法：
    python tools/gh_status.py                     # 最近 8 次运行
    python tools/gh_status.py --log               # 最近一次运行的完整应用日志
    python tools/gh_status.py --log 56            # 指定 run 序号
    python tools/gh_status.py --issues            # 查看告警 Issue
"""
import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = "https://api.github.com"
WORKFLOW = "schedule_dev.yml"

_url = subprocess.run(["git", "config", "--get", "remote.origin.url"],
                      capture_output=True, text=True, cwd=BASE).stdout.strip()
_m = re.search(r"://[^:]+:([^@]+)@github\.com[/:]([^/]+/[^/.]+)", _url)
if not _m:
    sys.exit("[ERROR] 无法从 git remote 解析 token / 仓库名")
TOKEN, REPO = _m.group(1), _m.group(2)


def api(path):
    req = urllib.request.Request(
        f"{API}{path}",
        headers={"Authorization": f"token {TOKEN}",
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "gh-status"},
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]


def to_local(iso):
    """UTC ISO -> 北京时间字符串。"""
    if not iso:
        return "-"
    import datetime
    dt = datetime.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone(datetime.timedelta(hours=8))).strftime(
        "%m-%d %H:%M")


def list_runs(n=8):
    st, body = api(f"/repos/{REPO}/actions/runs?per_page={n}")
    if st != 200:
        sys.exit(f"[ERROR] {st} {body}")
    runs = json.loads(body)["workflow_runs"]
    print(f"仓库 {REPO} 最近 {len(runs)} 次运行（北京时间）：")
    for r in runs:
        mark = "✅" if r["conclusion"] == "success" else ("❌" if r["conclusion"] == "failure"
                                                          else "⏳" if r["status"] != "completed" else "⚪")
        print(f"  {mark} #{r['run_number']:>3} {r['name'][:18]:<18} {r['event']:<16} "
              f"{to_local(r['created_at'])}  {str(r['conclusion'])}")
    return runs


def _fetch_log_text(job_id):
    """拉取作业日志。

    GitHub 会 302 跳到 Azure Blob 的签名地址，如果把 Authorization 头一起带过去
    会被拒绝（403 Server failed to authenticate the request）。
    所以这里手动处理重定向：先拿 Location，再不带鉴权头去下载。
    """
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    url = f"{API}/repos/{REPO}/actions/jobs/{job_id}/logs"
    headers = {"Authorization": f"token {TOKEN}", "User-Agent": "gh-status"}
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(urllib.request.Request(url, headers=headers), timeout=60) as r:
            return r.read().decode("utf-8", "replace")   # 没重定向，直接给了内容
    except urllib.error.HTTPError as e:
        if e.code not in (301, 302, 303, 307, 308):
            raise
        location = e.headers.get("Location")
    with urllib.request.urlopen(
            urllib.request.Request(location, headers={"User-Agent": "gh-status"}),
            timeout=180) as r:
        return r.read().decode("utf-8", "replace")


def run_log(run_number=None):
    if run_number is None:
        st, body = api(f"/repos/{REPO}/actions/runs?per_page=1")
        runs = json.loads(body)["workflow_runs"]
    else:
        st, body = api(f"/repos/{REPO}/actions/runs?per_page=60")
        runs = [r for r in json.loads(body)["workflow_runs"] if r["run_number"] == run_number]
    if not runs:
        sys.exit("[ERROR] 找不到该运行")
    run = runs[0]
    print(f"run #{run['run_number']} | {run['conclusion']} | {to_local(run['created_at'])} "
          f"(北京时间)\n{'-' * 64}")
    st, body = api(f"/repos/{REPO}/actions/runs/{run['id']}/jobs")
    job = json.loads(body)["jobs"][0]
    try:
        raw = _fetch_log_text(job["id"])
    except Exception as e:
        sys.exit(f"[ERROR] 拉日志失败：{e}")
    for line in raw.splitlines():
        line = re.sub(r"^\S+ ", "", line)
        if " - app - " in line:
            print(line)


def issues():
    st, body = api(f"/repos/{REPO}/issues?state=open&labels=spark-alert&per_page=10")
    items = json.loads(body)
    if not items:
        print("没有被打开的告警 Issue ✅")
        return
    print(f"有 {len(items)} 个未关闭的告警 Issue：")
    for it in items:
        print(f"  #{it['number']} {it['title']}  ({it['created_at']})")
        print(f"     {it['html_url']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", nargs="?", const="latest", default=None)
    ap.add_argument("--issues", action="store_true")
    ap.add_argument("--runs", type=int, default=8)
    args = ap.parse_args()

    if args.issues:
        issues()
    elif args.log is not None:
        run_log(None if args.log == "latest" else int(args.log))
    else:
        list_runs(args.runs)


if __name__ == "__main__":
    main()
