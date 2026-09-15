# -*- coding: utf-8 -*-
"""自动回复入口（与每天续火花的 main.py 相互独立，可单独跑）。

用法：
    python main_auto_reply.py --once                # 跑一轮（模式取 auto_reply_config.json）
    python main_auto_reply.py --mode readonly       # 阶段1：只识别、只记日志，绝不发送
    python main_auto_reply.py --mode template       # 阶段2：关键词 + 模板话术回复
    python main_auto_reply.py --mode llm            # 阶段3：大模型生成回复（需在配置里填 Key）
    python main_auto_reply.py --once --inspect      # 额外把页面 HTML/截图落盘，用于校准选择器
    python main_auto_reply.py                       # 常驻循环：全天候，按 intervalSeconds 轮询
"""
import argparse
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE)
sys.path.insert(0, BASE)

# 必须先加载 .env（TASKS / COOKIES_* 都在里面），否则拿不到账号数据，
# 会和 main.py 一样在导入 core 之前完成这一步。
if os.path.exists(".env"):
    from dotenv import load_dotenv

    load_dotenv(".env")

from core.auto_reply import run_auto_reply  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["readonly", "template", "llm"], help="覆盖配置里的模式")
    ap.add_argument("--once", action="store_true", help="只跑一轮就退出")
    ap.add_argument("--inspect", action="store_true", help="把页面 HTML/截图 落盘（校准选择器用）")
    args = ap.parse_args()
    run_auto_reply(mode=args.mode, once=args.once, inspect=args.inspect)


if __name__ == "__main__":
    main()
