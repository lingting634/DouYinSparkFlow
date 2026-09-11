# -*- coding: utf-8 -*-
"""离线单元测试：验证好友匹配统计、发送结果日志、试运行开关。

不联网、不启动浏览器、不发送任何消息。
    python tests/test_send_stats.py
"""
import io
import logging
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(BASE)
sys.path.insert(0, BASE)

os.environ.setdefault("TASKS", "[]")
os.environ.setdefault("LOG_LEVEL", "Info")

import core.tasks as t  # noqa: E402
import utils.config as uc  # noqa: E402

FAILURES = []


def reload_config():
    """清掉 config 的模块级缓存，让新设置的 DRY_RUN 生效。"""
    uc.config = None
    t.config = uc.get_config()
    return t.config


def check(name, cond, detail=""):
    flag = "PASS" if cond else "FAIL"
    print(f"  [{flag}] {name}" + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


# ---------------- 假的 Playwright 对象 ----------------
class FakeTitle:
    def __init__(self, name):
        self.name = name

    @property
    def first(self):
        return self

    def inner_text(self, timeout=0):
        return self.name


class FakeElement:
    def __init__(self, name):
        self.name = name
        self.clicked = 0

    def locator(self, sel):
        return FakeTitle(self.name)

    def inner_text(self, timeout=0):
        return self.name

    def click(self, **kw):
        self.clicked += 1


class FakeHandle:
    pass


class FakeLocator:
    def __init__(self, items):
        self.items = items

    def all(self):
        return self.items

    def count(self):
        return len(self.items)

    @property
    def first(self):
        return self

    def element_handle(self, timeout=0):
        return FakeHandle()

    def wait_for(self, **kw):
        return None


class FakePage:
    """模拟聊天列表页：items 是所有会话项，scroll 控制是否能滚动。"""

    def __init__(self, names, scroll_moves=False):
        self.elements = [FakeElement(n) for n in names]
        self.scroll_moves = scroll_moves
        self._eval_count = 0
        self.url = "https://www.douyin.com/chat"

    def locator(self, sel):
        return FakeLocator(self.elements)

    def evaluate(self, expr, arg):
        self._eval_count += 1
        if not self.scroll_moves:
            return 0
        return 0 if self._eval_count % 2 == 1 else 800

    def add_init_script(self, *a, **k):
        pass

    def on(self, *a, **k):
        pass

    def goto(self, *a, **k):
        pass

    def title(self):
        return "抖音聊天"

    def screenshot(self, **kw):
        raise RuntimeError("测试环境不截图")


class FakeInput:
    """模拟输入框：默认回车后内容被清空（=成功发出）。"""

    def __init__(self, leftover=""):
        self.leftover = leftover

    def type(self, *a, **k):
        pass

    def press(self, *a, **k):
        pass

    def inner_text(self, timeout=0):
        return self.leftover

    def input_value(self, timeout=0):
        return self.leftover


class FakeContext:
    def __init__(self, page):
        self.page = page

    def new_page(self):
        return self.page

    def set_default_navigation_timeout(self, *a):
        pass

    def set_default_timeout(self, *a):
        pass

    def add_cookies(self, *a):
        pass

    def close(self):
        pass


class FakeBrowser:
    def __init__(self, page):
        self.page = page

    def new_context(self, **kw):
        return FakeContext(self.page)


class LogCatcher(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def setup_targets(n=21):
    """构造 n 个目标好友，并填充 userIDDict（短ID -> 昵称映射）。"""
    names = [f"好友{i:02d}" for i in range(1, n + 1)]
    ids = [str(1000 + i) for i in range(1, n + 1)]
    t.userIDDict.clear()
    for name, tid in zip(names, ids):
        t.userIDDict[name] = [tid, tid, f"sec{tid}", name, name]
    return names, ids


def capture_logs(func):
    handler = LogCatcher()
    t.logger.addHandler(handler)
    t.logger.setLevel(logging.INFO)
    old = t.logger.propagate
    t.logger.propagate = False
    try:
        func()
    finally:
        t.logger.removeHandler(handler)
        t.logger.propagate = old
    return handler.lines


print("=" * 66)
print("【用例 1】全部好友都能匹配 -> 统计应为 21/21，remaining 为空")
names, ids = setup_targets(21)
page = FakePage(names)
stats = {}
found = list(t.scroll_and_select_user(page, "测试号", ids, ".sel", stats))
check("匹配到全部 21 位", len(found) == 21, f"实际 {len(found)}")
check("remaining 为空", stats.get("remaining") == set(), str(stats.get("remaining")))
check("scanned=21", stats.get("scanned") == 21, str(stats.get("scanned")))
check("names 映射可用", stats.get("names", {}).get(ids[0]) == names[0], str(stats.get("names")))
check("每个会话项都被点击过", all(e.clicked >= 1 for e in page.elements))

print()
print("【用例 2】只有 5 位在列表里 -> 统计应为 5 匹配 + 16 未匹配")
names, ids = setup_targets(21)
page = FakePage(names[:5])
stats = {}
found = list(t.scroll_and_select_user(page, "测试号", ids, ".sel", stats))
check("只匹配到 5 位", len(found) == 5, f"实际 {len(found)}")
check("remaining=16", len(stats.get("remaining", set())) == 16, str(len(stats.get("remaining", set()))))
check("scanned 有值", stats.get("scanned") == 5, str(stats.get("scanned")))

print()
print("【用例 3】do_user_task 真实发送路径 -> 每位好友都有 INFO 发送记录 + 汇总")
names, ids = setup_targets(21)
page = FakePage(names)
os.environ["DRY_RUN"] = ""
cfg = reload_config()
check("dryRun 关闭", cfg.get("dryRun") is False, str(cfg.get("dryRun")))
t.wait_for_chat_ready = lambda p, u: ".sel"
t.first_visible_locator = lambda p, sels, timeout=0: (sels[0], FakeInput())
t.build_message = lambda: "[盖瑞]测试消息"
lines = capture_logs(lambda: t.do_user_task(FakeBrowser(page), "测试号", [], ids))
sent_lines = [l for l in lines if "已发送 [" in l]
summary = [l for l in lines if "任务汇总" in l]
check("21 条发送记录", len(sent_lines) == 21, f"实际 {len(sent_lines)}")
check("首条为 [1/21]", sent_lines and "[1/21]" in sent_lines[0], sent_lines[0] if sent_lines else "")
check("末条为 [21/21]", sent_lines and "[21/21]" in sent_lines[-1], sent_lines[-1] if sent_lines else "")
check("含汇总行", len(summary) == 1, str(summary))
check("汇总显示确认发送 21 位", summary and "确认发送 21 位" in summary[0], summary[0] if summary else "")
check("汇总显示未匹配 0 位", summary and "未匹配 0 位" in summary[0], summary[0] if summary else "")
check("发送记录带好友昵称", sent_lines and names[0] in sent_lines[0], sent_lines[0] if sent_lines else "")

print()
print("【用例 4】试运行开关 -> 不能有任何真实输入/发送")
names, ids = setup_targets(21)
page = FakePage(names)
os.environ["DRY_RUN"] = "1"
cfg = reload_config()
check("dryRun 开启", cfg.get("dryRun") is True, str(cfg.get("dryRun")))
t.wait_for_chat_ready = lambda p, u: ".sel"

called = {"editor": 0}


def spy_editor(p, sels, timeout=0):
    called["editor"] += 1
    return (sels[0], FakeInput())


t.first_visible_locator = spy_editor
lines = capture_logs(lambda: t.do_user_task(FakeBrowser(page), "测试号", [], ids))
dry_lines = [l for l in lines if "[试运行]" in l]
check("21 条试运行记录", len(dry_lines) == 21, f"实际 {len(dry_lines)}")
check("未调用发送输入框", called["editor"] == 0, f"调用了 {called['editor']} 次")
check("试运行不写入已发送标记", all("已发送 [" not in l for l in lines))
check("汇总仍为 21", any("确认发送 21 位" in l for l in lines))

print()
print("【用例 5】回车后输入框仍有内容 -> 必须报未确认，不能算发送成功")
names, ids = setup_targets(21)
page = FakePage(names)
os.environ["DRY_RUN"] = ""
cfg = reload_config()
t.wait_for_chat_ready = lambda p, u: ".sel"
t.first_visible_locator = lambda p, sels, timeout=0: (sels[0], FakeInput("残留的一句消息"))
t.build_message = lambda: "[盖瑞]测试消息"
lines = capture_logs(lambda: t.do_user_task(FakeBrowser(page), "测试号", [], ids))
stuck = [l for l in lines if "未确认发送 [" in l]
summary = [l for l in lines if "任务汇总" in l]
check("21 条未确认记录", len(stuck) == 21, f"实际 {len(stuck)}")
check("没有误报已发送", all("已发送 [" not in l for l in lines))
check("汇总确认发送 0 位", summary and "确认发送 0 位" in summary[0], summary[0] if summary else "")
check("汇总未确认 21 位", summary and "未确认 21 位" in summary[0], summary[0] if summary else "")

print()
print("【用例 6】回车后只剩不可见占位字符（零宽空格/不换行空格）-> 必须算发送成功")
names, ids = setup_targets(21)
page = FakePage(names)
os.environ["DRY_RUN"] = ""
reload_config()
t.wait_for_chat_ready = lambda p, u: ".sel"
t.first_visible_locator = lambda p, sels, timeout=0: (sels[0], FakeInput("\u200b\u00a0\u200b"))
t.build_message = lambda: "[盖瑞]测试消息"
lines = capture_logs(lambda: t.do_user_task(FakeBrowser(page), "测试号", [], ids))
sent_lines = [l for l in lines if "已发送 [" in l]
summary = [l for l in lines if "任务汇总" in l]
check("21 条已发送记录", len(sent_lines) == 21, f"实际 {len(sent_lines)}")
check("没有误报未确认", all("未确认发送 [" not in l for l in lines))
check("汇总确认发送 21 位", summary and "确认发送 21 位" in summary[0], summary[0] if summary else "")

print()
print("【用例 7】可见残留 + 不可见字符混合 -> 仍必须报未确认")
names, ids = setup_targets(21)
page = FakePage(names)
t.first_visible_locator = lambda p, sels, timeout=0: (sels[0], FakeInput("\u200b还没发出去\u00a0"))
lines = capture_logs(lambda: t.do_user_task(FakeBrowser(page), "测试号", [], ids))
stuck = [l for l in lines if "未确认发送 [" in l]
check("21 条未确认记录", len(stuck) == 21, f"实际 {len(stuck)}")
check("残留原文写进日志", stuck and "还没发出去" in stuck[0], stuck[0] if stuck else "")

print()
print("=" * 66)
if FAILURES:
    print(f"结果：{len(FAILURES)} 项失败 -> {FAILURES}")
    sys.exit(1)
print("结果：全部通过")
