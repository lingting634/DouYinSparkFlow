# -*- coding: utf-8 -*-
"""离线单元测试：自动回复引擎（场景识别 / 话术生成 / 安全阀 / 全天候）。

不联网、不启动浏览器、不发送任何消息。
    python tests/test_auto_reply.py
"""
import datetime
import json
import os
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(BASE)
sys.path.insert(0, BASE)

os.environ.setdefault("TASKS", "[]")
os.environ.setdefault("LOG_LEVEL", "Info")

import core.auto_reply as ar  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    flag = "PASS" if cond else "FAIL"
    print(f"  [{flag}] {name}" + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def cfg(extra=None):
    c = json.loads(json.dumps(ar.DEFAULT_CONFIG, ensure_ascii=False))
    for k, v in (extra or {}).items():
        if isinstance(v, dict):
            c[k].update(v)
        else:
            c[k] = v
    return c


def fresh_state():
    return {"day": ar.today(), "globalCount": 0, "friends": {}, "repliedKeys": [], "lastReplies": {}}


print("=" * 66)
print("【用例 1】场景识别：分享视频 / 提问 / 表情 / 日常聊天")
check("带链接 -> share", ar.classify_scene("https://v.douyin.com/xxx", has_link=True) == "share", "")
check("含“分享”字样 -> share", ar.classify_scene("分享[视频]") == "share", "")
check("含“怎么” -> question", ar.classify_scene("这个怎么弄") == "question", "")
check("含“？” -> question", ar.classify_scene("真的假的？") == "question", "")
check("纯表情 -> sticker", ar.classify_scene("😀") == "sticker", "")
check("日常闲聊 -> chat", ar.classify_scene("今天天气不错") == "chat", "")
check("元素标记表情 -> sticker", ar.classify_scene("x", is_sticker=True) == "sticker", "")
check("会话名含“群” -> group", ar.classify_scene("我发布了新作品", conv_name="飞雪 初代 粉丝群") == "group", "")
check("含“作品” -> share", ar.classify_scene("我发布了新作品，快来看看！") == "share", "")
check("粉丝团/官方号也归为 group", ar.classify_scene("你好", conv_name="某某官方") == "group", "")

print()
print("【用例 2】话术生成：关键词规则优先，其次场景话术池")
c = cfg()
pools = {tuple(r["keywords"]): r["replies"] for r in c["keywordRules"]}
reply = ar.pick_template_reply("question", c, "f1", fresh_state(), message="在吗")
check("“在吗”命中关键词话术", reply in pools[("在吗", "在么", "在不在", "在嘛")], str(reply))
reply2 = ar.pick_template_reply("chat", c, "f2", fresh_state(), message="今天天气不错")
check("闲聊走场景话术池", reply2 in (c["sceneTemplates"]["chat"] + c["sceneTemplates"]["fallback"]), str(reply2))
check("没命中关键词时 match_keyword_rule 返回空", ar.match_keyword_rule("随便说点什么", c) == [], "")

print()
print("【用例 3】同一好友 24 小时内不重复同一句话")
c3 = cfg({"sceneTemplates": {"chat": ["嗯嗯", "确实"], "fallback": []}})
st = fresh_state()
first = ar.pick_template_reply("chat", c3, "fA", st, message="你好")
ar.mark_replied(st, "fA", first)
second = ar.pick_template_reply("chat", c3, "fA", st, message="你好")
check("第二次换了另一句", second != first, f"{first} -> {second}")

print()
print("【用例 4】安全阀：白名单 / 黑名单 / 敏感词 / 场景开关 / 总开关")
c = cfg()
st = fresh_state()
ok, why = ar.check_safety("f1", "普通好友", "chat", "你好", c, st)
check("默认允许", ok is True, why)
ok, why = ar.check_safety("f2", "别理他", "chat", "你好", cfg({"blacklist": ["别理他"]}), fresh_state())
check("黑名单拦截", ok is False and "黑名单" in why, why)
ok, why = ar.check_safety("f3", "好友甲", "chat", "你好", cfg({"whitelist": ["好友乙"]}), fresh_state())
check("不在白名单拦截", ok is False and "白名单" in why, why)
ok, why = ar.check_safety("f4", "好友乙", "chat", "你好", cfg({"whitelist": ["好友乙"]}), fresh_state())
check("白名单内放行", ok is True, why)
ok, why = ar.check_safety("f5", "好友丙", "chat", "帮我转账 200", c, fresh_state())
check("敏感词拦截", ok is False and "敏感词" in why, why)
ok, why = ar.check_safety("f6", "群聊", "group", "大家好", c, fresh_state())
check("群聊场景默认关闭", ok is False, why)
ok, why = ar.check_safety("f7", "好友丁", "chat", "你好", cfg({"enabled": False}), fresh_state())
check("总开关关闭时拦截", ok is False and "总开关" in why, why)
ok, why = ar.check_safety("f8", "好友戊", "call", "邀请你通话", c, fresh_state())
check("通话邀请默认不自动回", ok is False, why)
ok, why = ar.check_safety("g1", "飞雪 初代 粉丝群", "chat", "我发布了新作品", c, fresh_state())
check("群名命中 nameBlocklist 拦截", ok is False and "不回复" in why, why)

print()
print("【用例 5】安全阀：会话冷却 / 每人每日上限 / 全局上限 / 只回最近消息")
c = cfg({"cooldownMinutes": 30, "perFriendDailyLimit": 3, "globalDailyLimit": 5})
st = fresh_state()
ar.mark_replied(st, "fB", "在的")
ok, why = ar.check_safety("fB", "好友己", "chat", "在吗", c, st)
check("冷却期内拦截", ok is False and "冷却" in why, why)
ok, why = ar.check_safety("fB", "好友己", "chat", "在吗", c, st, now_ts=time.time() + 31 * 60)
check("冷却结束后放行", ok is True, why)
st = fresh_state()
for _ in range(3):
    ar.mark_replied(st, "fC", "在的")
ok, why = ar.check_safety("fC", "好友庚", "chat", "在吗", c, st, now_ts=time.time() + 3600)
check("每人每日上限拦截", ok is False and "上限" in why, why)
st = fresh_state()
st["globalCount"] = 5
ok, why = ar.check_safety("fD", "好友辛", "chat", "在吗", c, st, now_ts=time.time() + 3600)
check("全局每日上限拦截", ok is False and "全局" in why, why)

print()
print("【用例 6】全天候：不设时段时任何时间都能回")
c = cfg({"activeHours": None})
t3 = datetime.datetime(2026, 9, 15, 3, 0)
t23 = datetime.datetime(2026, 9, 15, 23, 59)
check("凌晨 3 点也允许", ar.in_active_hours(c, t3) is True, "")
check("深夜 23:59 也允许", ar.in_active_hours(c, t23) is True, "")
c2 = cfg({"activeHours": ["09:00", "23:00"]})
check("设了时段后凌晨被挡", ar.in_active_hours(c2, t3) is False, "")
check("设了时段后白天放行", ar.in_active_hours(c2, datetime.datetime(2026, 9, 15, 10, 0)) is True, "")
c3 = cfg({"activeHours": ["22:00", "06:00"]})
check("跨零点时段 02:00 放行", ar.in_active_hours(c3, datetime.datetime(2026, 9, 15, 2, 0)) is True, "")
check("跨零点时段 12:00 被挡", ar.in_active_hours(c3, datetime.datetime(2026, 9, 15, 12, 0)) is False, "")

print()
print("【用例 7】大模型：未配置时返回 None，自动降级到模板话术")
c = cfg({"mode": "llm"})
check("llm 未启用 -> None", ar.llm_reply(c, "chat", "好友", "在吗") is None, "")
r = ar.build_reply("chat", c, "fZ", "好友", "在吗", fresh_state())
check("降级后仍有模板回复", bool(r) and isinstance(r, str), str(r))
check("回复长度受 maxReplyChars 限制",
      len(ar.build_reply("chat", cfg({"maxReplyChars": 5}), "fZ", "好友", "今天天气不错", fresh_state())) <= 5, "")

print()
print("【用例 8】状态跨天自动重置（每人每日上限按自然日算）")
st = fresh_state()
ar.mark_replied(st, "fE", "在的")
st["day"] = "2026-01-01"
ar.roll_day(st)
check("跨天后全局计数清零", st["globalCount"] == 0, str(st["globalCount"]))
check("跨天后好友计数清零", st["friends"]["fE"]["count"] == 0, str(st["friends"]["fE"]["count"]))

print()
print("【用例 9】只挑「对方发来的」消息回复")
msgs = [
    {"text": "我们 12:31 发的模板首行", "outgoing": True, "has_link": False, "is_sticker": False},
    {"text": "在吗", "outgoing": False, "has_link": False, "is_sticker": False},
]
inc = ar.pick_incoming(msgs, {"我们 12:31 发的模板首行"})
check("跳过自己发的，取对方那条", inc and inc["text"] == "在吗", str(inc))
only_ours = ar.pick_incoming([msgs[0]], {"我们 12:31 发的模板首行"})
check("只有自己发的则返回 None", only_ours is None, str(only_ours))

print()
print("【用例 10】只回最近 N 分钟内的消息（不翻旧账）")
now = datetime.datetime(2026, 9, 15, 17, 0)
check("刚刚 -> 算新", ar.is_recent("刚刚", 10, now) is True, "")
check("5分钟前 -> 算新", ar.is_recent("5分钟前", 10, now) is True, "")
check("30分钟前 -> 算旧", ar.is_recent("30分钟前", 10, now) is False, "")
check("16:55（5 分钟前）-> 算新", ar.is_recent("16:55", 10, now) is True, "")
check("10:56（6 小时前）-> 算旧", ar.is_recent("10:56", 10, now) is False, "")
check("2小时前 -> 算旧", ar.is_recent("2小时前", 10, now) is False, "")
check("昨天 -> 算旧", ar.is_recent("昨天", 10, now) is False, "")
check("时间串缺失 -> 不拦", ar.is_recent("", 10, now) is True, "")

print()
print("【用例 11】带后缀的备注名与「自己发的消息」识别")
check("「孙浩楠（保定）」能被「孙浩楠」匹配到",
      ar.name_matches("孙浩楠（保定）", ["孙浩楠"]) is True, "")
check("不相关昵称不会被误匹配", ar.name_matches("李四", ["孙浩楠"]) is False, "")
ok, why = ar.check_safety("孙浩楠（保定）", "孙浩楠（保定）", "chat", "你好",
                         cfg({"whitelist": ["孙浩楠"]}), fresh_state())
check("白名单支持带后缀备注名", ok is True, why)
ours = ar.our_recent_texts()
check("固定标记含自己的火花消息", "[盖瑞]今日火花" in ours and "[续火花]" in ours, str(sorted(ours))[:60])

print()
print("【用例 12】按 ID 授权好友（extraFriendIds）")
ar.userIDDict.clear()
ar.userIDDict["新朋友"] = ["66467852015", "dy_newfriend", "MS4wLjABAAAAsec", "新朋友", "新朋友"]
c_id = cfg({"whitelist": ["奎奎"], "extraFriendIds": ["66467852015"]})
check("ID 命中 -> 是授权好友", ar.is_target_friend("新朋友", c_id, {"1001", "1002"}) is True, "")
ok, why = ar.check_safety("新朋友", "新朋友", "chat", "你好", c_id, fresh_state(), target_ok=True)
check("按 ID 授权后不被白名单二次拦截", ok is True, why)
check("未授权的人仍然被拦",
      ar.is_target_friend("路人甲", c_id, {"1001"}) is False, "")
ar.userIDDict.clear()

print()
print("【用例 13】放开名单限制：所有好友都能回（onlyKnownFriends=false）")
c_all = cfg({"onlyKnownFriends": False, "whitelist": ["奎奎"]})
ok, why = ar.check_safety("路人乙", "路人乙", "chat", "你好", c_all, fresh_state(), target_ok=False)
check("非名单好友也放行", ok is True, why)
c_lim = cfg({"onlyKnownFriends": True, "whitelist": ["奎奎"]})
ok, why = ar.check_safety("路人乙", "路人乙", "chat", "你好", c_lim, fresh_state(), target_ok=False)
check("仍然可以切回名单模式", ok is False and "白名单" in why, why)
ok, why = ar.check_safety("群甲", "某某粉丝群", "group", "大家好", c_all, fresh_state(), target_ok=False)
check("放开名单后群聊照样不回", ok is False, why)
ok, why = ar.check_safety("敏感", "好友丙", "chat", "帮我转账款", c_all, fresh_state(), target_ok=False)
check("放开名单后敏感词照样拦", ok is False and "敏感词" in why, why)

print()
print("=" * 66)
if FAILURES:
    print(f"结果：{len(FAILURES)} 项失败 -> {FAILURES}")
    sys.exit(1)
print("结果：全部通过")
