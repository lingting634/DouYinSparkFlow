# -*- coding: utf-8 -*-
"""自动回复引擎（三阶段：只读识别 / 关键词模板回复 / 大模型回复）。

设计要点
- **全天候**：默认 24 小时都工作（`activeHours: null` 表示不设时段限制），轮询间隔可配。
- 阶段 1 `mode=readonly`：只识别、只记日志，绝不发送（用来先核对识别准不准）。
- 阶段 2 `mode=template`：关键词规则 -> 场景话术池 -> 兜底话术（零成本、结果可预测）。
- 阶段 3 `mode=llm`：用大模型生成（配置了才启用），失败自动降级到模板。
- 安全阀：白名单/黑名单、场景开关、只回最近 N 分钟、同好友冷却、每人每日上限、
  全局每日上限、敏感词拦截、不确定就不回（只记日志）。
- 判定与发送沿用 `core.tasks` 里已经过生产验证的实现（同一套选择器与发送校验）。
"""
import datetime
import json
import os
import random
import re
import time
import urllib.error
import urllib.request

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from core.browser import get_browser
from core.tasks import (
    CHAT_EDITOR_SELECTORS,
    CONVERSATION_ITEM_SELECTORS,
    CONVERSATION_LIST_SELECTORS,
    CONVERSATION_TITLE_SELECTORS,
    clean_editor_text,
    dump_debug_artifacts,
    first_visible_locator,
    handle_response,
    logger,
    send_message,
    userData,
    userIDDict,
    wait_for_chat_ready,
)

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE, "auto_reply_config.json")
STATE_PATH = os.path.join(BASE, "logs", "auto_reply_state.json")
HISTORY_PATH = os.path.join(BASE, "logs", "auto_reply_history.jsonl")

# 实测自 2026-09-15 的页面快照（logs/debug/auto-reply-inspect-*.html）：
#   消息列表  .messageMessageListlist / .messageMessageListwrapper
#   消息项    .messageMessageBoxmessageBox（自身带 isFromMe 类即为自己发的）
#   文本气泡  .MessageItemTextbubbleTextContent
#   分享卡片  .MessageItemShareAwemecontainer + .MessageItemShareAwemeauthorName
#   表情      .MessageItemEmojiemojiBox
#   会话项    [data-e2e="conversation-item"]，标题 .conversationConversationItemtitle
MESSAGE_REGION_SELECTORS = [
    ".messageMessageListlist",
    ".messageMessageListwrapper",
    "[class*='messageMessageList']",
    "[class*='Message'][class*='List']",
]
MESSAGE_ITEM_SELECTORS = [
    ".messageMessageBoxmessageBox",
    "[class*='MessageBox'][class*='messageBox']",
    "[data-e2e='message-item']",
]
MSG_TEXT_SELECTORS = [
    ".MessageItemTextbubbleTextContent",
    ".TextMessageTextpureText",
    ".MessageItemTextcontainer",
]
MSG_SHARE_AUTHOR_SELECTORS = [".MessageItemShareAwemeauthorName"]
MSG_EMOJI_SELECTORS = [".MessageItemEmojiemojiBox", ".MessageItemEmojiimage"]
CONV_ITEM_SELECTORS = ["[data-e2e='conversation-item']"] + CONVERSATION_ITEM_SELECTORS
CONV_PREVIEW_SELECTORS = [".ConversationItemDescleft", ".ConversationItemDescwrapper"]
BADGE_SELECTORS = [
    "[class*='badge']",
    "[class*='Badge']",
    "[class*='unread']",
    "[class*='Unread']",
    "[class*='count']",
]
CONV_TIME_SELECTORS = [".ConversationItemTagNextToTitletimeStr"]
TIME_LIKE = ("昨天", "今天", "星期", "分钟前", "刚刚", "小时前", "月", "日", ":")

DEFAULT_CONFIG = {
    "enabled": True,
    # readonly = 阶段1 只读识别；template = 阶段2 关键词+模板；llm = 阶段3 大模型
    "mode": "readonly",
    "intervalSeconds": 480,
    # null = 全天候（任何时间段都能回）；也可写成 ["09:00", "23:00"]
    "activeHours": None,
    "whitelist": [],
    "blacklist": [],
    # 只回复「账号好友列表里的 21 位目标好友」（用抖音 IM 接口缓存的名字->短ID 校验）；
    # 设成 false 表示所有单聊都会自动回复（不推荐）。
    "onlyKnownFriends": True,
    # 额外授权的好友 ID（抖音号/短ID）：即使不在续火花名单、昵称也不在白名单里，也允许自动回复
    "extraFriendIds": [],
    # 会话名里含这些字样一律不回（群聊/官方号/系统通知）
    "nameBlocklist": ["群", "粉丝团", "官方", "助手", "通知", "服务号"],
    "scenes": {
        "share": True,
        "chat": True,
        "question": True,
        "sticker": True,
        "call": False,
        "group": False,
    },
    "recentMinutes": 10,
    "cooldownMinutes": 30,
    "perFriendDailyLimit": 3,
    "globalDailyLimit": 30,
    "maxReplyChars": 30,
    "noRepeatHours": 24,
    "sensitiveWords": [
        "转账", "贷款", "借款", "刷单", "兼职", "博彩", "赌", "色情", "裸",
        "身份证", "银行卡", "验证码", "密码", "加微信", "私聊我",
    ],
    "keywordRules": [
        {"keywords": ["在吗", "在么", "在不在", "在嘛"], "replies": ["在的 咋啦", "在 你说", "在呢"]},
        {"keywords": ["早", "早上好", "早安"], "replies": ["早啊", "早 今天没课？", "早呀"]},
        {"keywords": ["晚安", "睡了", "先睡"], "replies": ["晚安", "睡吧 明天聊", "晚安好梦"]},
        {"keywords": ["吃了吗", "吃饭", "吃啥"], "replies": ["刚吃完 你呢", "还没 一会儿去", "吃了吃了"]},
        {"keywords": ["哈哈", "笑死", "离谱", "绝了"], "replies": ["哈哈哈哈", "真的离谱", "笑不活了"]},
        {"keywords": ["谢谢", "多谢", "感谢"], "replies": ["客气啥", "小事儿", "别谢"]},
        {"keywords": ["拜拜", "再见", "下了"], "replies": ["拜拜", "回聊", "行 那回头说"]},
    ],
    "sceneTemplates": {
        "share": ["刚看完了 有点东西", "这个我刷到过 确实好看", "收藏了 回头细看", "哈哈 你怎么刷到的"],
        "chat": ["嗯嗯", "确实", "哈哈是这样", "可以啊", "怎么了"],
        "question": ["我也不太确定 得查下", "应该是吧", "我想想 一会儿回你", "这个我还真不知道"],
        "sticker": ["哈哈", "笑死", "行行行", "咋啦"],
        "fallback": ["嗯嗯 在呢", "看到了", "回头细说"],
    },
    "llm": {
        "enabled": False,
        "baseUrl": "",
        "apiKey": "",
        "model": "",
        "temperature": 0.8,
        "timeoutSeconds": 20,
        "systemPrompt": (
            "你是抖音用户「讲七歌」本人（河南的大学生，说话口语、简短、有点幽默）。"
            "现在在回复好友的抖音私信，要求：只输出一条可以直接发出去的回复；"
            "2-15 个字为主；不用书面语、不加引号、不解释、不追问隐私；"
            "对方分享视频就顺着聊两句；不知道怎么接就简短应一声。"
        ),
    },
}


def load_config():
    """读取 auto_reply_config.json；不存在就用默认值生成一份，方便用户直接改。"""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG, ensure_ascii=False))
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                user_cfg = json.load(f)
            _deep_update(cfg, user_cfg)
        except Exception as e:
            logger.warning(f"自动回复配置读取失败，改用默认配置：{e}")
    else:
        try:
            save_config(cfg)
            logger.info(f"已生成默认自动回复配置：{CONFIG_PATH}")
        except Exception as e:
            logger.warning(f"写入默认配置失败：{e}")
    if os.getenv("AUTO_REPLY_MODE"):
        cfg["mode"] = os.getenv("AUTO_REPLY_MODE", "").strip()
    if os.getenv("AUTO_REPLY_ENABLED", "").strip().lower() in ("0", "false", "no", "off"):
        cfg["enabled"] = False
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def _deep_update(base, patch):
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_update(base[k], v)
        else:
            base[k] = v
    return base


# --------------------------------------------------------------------------- 状态


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"day": today(), "globalCount": 0, "friends": {}, "repliedKeys": [], "lastReplies": {}}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def today():
    return datetime.datetime.now().strftime("%Y-%m-%d")


def roll_day(state):
    """跨天则把当日计数清零（每人每日上限 / 全局上限都按自然日算）。"""
    if state.get("day") != today():
        state["day"] = today()
        state["globalCount"] = 0
        for v in state.get("friends", {}).values():
            v["count"] = 0
    return state


def record_history(entry):
    try:
        os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
        entry["time"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(HISTORY_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning(f"写自动回复历史失败：{e}")


# --------------------------------------------------------------------------- 场景识别


def classify_scene(text, has_link=False, is_sticker=False, conv_name=""):
    """识别消息属于哪个场景：group / share / sticker / question / chat。"""
    name = conv_name or ""
    if any(k and k in name for k in ("群", "粉丝团", "官方", "助手", "通知")):
        return "group"
    t = (text or "").strip()
    if is_sticker or (t and all(not ch.isalnum() for ch in t) and len(t) <= 4):
        return "sticker"
    if has_link or "分享" in t or "视频" in t or "作品" in t:
        return "share"
    if any(k in t for k in ("？", "?", "怎么", "为什么", "为啥", "吗", "帮", "能不能", "可以吗", "咋")):
        return "question"
    return "chat"


def in_active_hours(cfg, now=None):
    """全天候返回 True；配置了时段才按时段判断（支持跨零点，如 ["22:00","06:00"]）。"""
    hours = cfg.get("activeHours")
    if not hours:
        return True
    try:
        start, end = hours[0], hours[1]
        cur = (now or datetime.datetime.now()).strftime("%H:%M")
        if start <= end:
            return start <= cur <= end
        return cur >= start or cur <= end
    except Exception:
        return True


def is_recent(conv_time, recent_minutes, now=None):
    """会话列表里的时间串是不是「最近 N 分钟内」的消息（防止回复很久以前的旧消息）。

    认得的格式：刚刚 / N分钟前 / N小时前 / HH:MM；「昨天」「日期」等一律算旧。
    拿不到时间串时返回 True（不拦），由冷却与上限兜底。
    """
    t = (conv_time or "").strip()
    if not t or int(recent_minutes or 0) <= 0:
        return True
    if "刚刚" in t:
        return True
    m = re.match(r"^(\d+)\s*分钟前", t)
    if m:
        return int(m.group(1)) <= int(recent_minutes)
    m = re.match(r"^(\d+)\s*小时前", t)
    if m:
        return int(m.group(1)) * 60 <= int(recent_minutes)
    m = re.match(r"^(\d{1,2}):(\d{2})$", t)
    if m:
        now = now or datetime.datetime.now()
        hm = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        return abs((now - hm).total_seconds()) <= int(recent_minutes) * 60
    return False


# --------------------------------------------------------------------------- 安全阀


def check_safety(identity, friend_name, scene, text, cfg, state, now_ts=None, target_ok=False):
    """返回 (是否允许回复, 原因)。任何一个安全阀不过就只记日志、不发送。

    target_ok=True 表示调用方已经确认过「这是授权好友」（白名单昵称 / 目标短ID / extraFriendIds），
    此时不再用白名单昵称二次拦截（否则按 ID 授权的好友会被误拦）。
    """
    now_ts = now_ts if now_ts is not None else time.time()
    roll_day(state)
    if not cfg.get("enabled"):
        return False, "总开关关闭"
    scene_sw = cfg.get("scenes", {}).get(scene)
    if scene_sw is False:
        return False, f"场景 {scene} 已关闭"
    for kw in cfg.get("nameBlocklist", []):
        if kw and kw in (friend_name or ""):
            return False, f"会话名含「{kw}」（群聊/官方号等），不回复"
    key = str(identity)
    black = [str(x) for x in cfg.get("blacklist", [])]
    white = [str(x) for x in cfg.get("whitelist", [])]
    if name_matches(friend_name, black) or key in black:
        return False, "命中黑名单"
    # 只有开启「仅回复名单内好友」时，白名单才起限制作用；
    # onlyKnownFriends=false 表示所有单聊好友都回（白名单/extraFriendIds 变成不再必要的备注）
    if cfg.get("onlyKnownFriends", True) and white and not target_ok \
            and not name_matches(friend_name, white) and key not in white:
        return False, "不在白名单内"
    for w in cfg.get("sensitiveWords", []):
        if w and w in (text or ""):
            return False, f"命中敏感词「{w}」"
    fr = state.setdefault("friends", {}).setdefault(key, {"count": 0, "lastReplyTs": 0})
    cooldown = float(cfg.get("cooldownMinutes", 30)) * 60
    if cooldown and now_ts - float(fr.get("lastReplyTs", 0)) < cooldown:
        left = int(cooldown - (now_ts - float(fr.get("lastReplyTs", 0)))) // 60
        return False, f"会话冷却中（还需约 {left} 分钟）"
    limit = int(cfg.get("perFriendDailyLimit", 3))
    if limit and fr.get("count", 0) >= limit:
        return False, f"该好友今日已达上限（{limit} 条）"
    glimit = int(cfg.get("globalDailyLimit", 30))
    if glimit and state.get("globalCount", 0) >= glimit:
        return False, f"全局今日已达上限（{glimit} 条）"
    return True, "通过"


def mark_replied(state, identity, reply, now_ts=None):
    now_ts = now_ts if now_ts is not None else time.time()
    roll_day(state)
    key = str(identity)
    fr = state.setdefault("friends", {}).setdefault(key, {"count": 0, "lastReplyTs": 0})
    fr["count"] = fr.get("count", 0) + 1
    fr["lastReplyTs"] = now_ts
    state["globalCount"] = state.get("globalCount", 0) + 1
    state.setdefault("lastReplies", {})[key] = {"text": reply, "ts": now_ts}


def mark_handled(state, name, preview):
    """记下「这条消息已经处理过」，避免同一句话被反复回复（上限 200 条）。"""
    keys = state.setdefault("handledKeys", [])
    keys.append(f"{name}|{(preview or '')[:40]}")
    state["handledKeys"] = keys[-200:]


def name_matches(name, name_list):
    """昵称是否命中名单：完全相同，或一方包含另一方（处理「孙浩楠（保定）」这类带后缀的备注名）。"""
    name = (name or "").strip()
    for item in name_list or []:
        item = str(item).strip()
        if not item:
            continue
        if name == item:
            return True
        if len(item) >= 2 and (item in name or (name and name in item)):
            return True
    return False


def friend_ids(name):
    """取抖音好友接口缓存里这个名字对应的所有 ID（短ID / 抖音号 / sec_uid）。"""
    info = userIDDict.get(name)
    if not info:
        return set()
    return {str(x) for x in info if x and isinstance(x, (str, int)) and str(x).strip()}


def is_target_friend(name, cfg, target_ids):
    """判断是不是「我要自动回复的好友」：白名单昵称、续火花目标短ID、或额外授权的 ID。"""
    if name_matches(name, cfg.get("whitelist")):
        return True
    ids = friend_ids(name)
    if ids & {str(t) for t in target_ids}:
        return True
    extra = {str(x).strip() for x in (cfg.get("extraFriendIds") or []) if str(x).strip()}
    if ids & extra:
        return True
    return False


def is_candidate(conv, cfg, state, ours, target_ids):
    """挑出本轮要处理的会话。

    两种情况都算：
    1) 有未读角标（最标准）；
    2) 没有未读角标、但最后一条**不是我们发的**、且是最近 N 分钟内的——因为用户可能在手机上
       点开过导致未读被清掉（2026-09-15 奎奎就是这么被漏掉的）。
    """
    name = conv.get("name") or ""
    if conv.get("unread", 0) > 0:
        return True, "有未读角标"
    if not is_target_friend(name, cfg, target_ids):
        return False, "非目标好友"
    if not is_recent(conv.get("time"), cfg.get("recentMinutes")):
        return False, f"最后一条是「{conv.get('time')}」，超过 {cfg.get('recentMinutes')} 分钟"
    preview = (conv.get("preview") or "").strip()
    if not preview:
        return False, "没有预览内容"
    for o in ours:
        if o and (o in preview or preview in o):
            return False, "最后一条是我们自己发的"
    if f"{name}|{preview[:40]}" in (state.get("handledKeys") or []):
        return False, "这条消息已经处理过了"
    return True, "最近的新消息（无未读角标）"


# --------------------------------------------------------------------------- 生成回复


def match_keyword_rule(message, cfg):
    """按关键词规则匹配话术池；没命中返回空列表。"""
    text = message or ""
    for rule in cfg.get("keywordRules", []):
        for k in rule.get("keywords", []):
            if k and k in text:
                return list(rule.get("replies", []))
    return []


def pick_template_reply(scene, cfg, identity=None, state=None, rng=None, message=""):
    """关键词规则 -> 场景话术池 -> 兜底话术；同一好友 noRepeatHours 内不重复同一句。"""
    rng = rng or random
    pools = match_keyword_rule(message, cfg)
    if not pools:
        pools = list(cfg.get("sceneTemplates", {}).get(scene) or [])
        pools += list(cfg.get("sceneTemplates", {}).get("fallback") or [])
    pools = [p for p in pools if p]
    if not pools:
        return None
    last = ((state or {}).get("lastReplies", {}) or {}).get(str(identity), {})
    since = float(cfg.get("noRepeatHours", 24)) * 3600
    recent = set()
    if last and time.time() - float(last.get("ts", 0)) < since:
        recent.add(last.get("text"))
    candidates = [p for p in pools if p not in recent] or pools
    return rng.choice(candidates)


def llm_reply(cfg, scene, friend_name, message):
    """调用 OpenAI 兼容接口生成回复；任何异常都返回 None（由调用方降级到模板）。"""
    llm = cfg.get("llm", {}) or {}
    if not llm.get("enabled") or not llm.get("baseUrl") or not llm.get("apiKey"):
        return None
    url = llm["baseUrl"].rstrip("/") + "/chat/completions"
    payload = {
        "model": llm.get("model", ""),
        "temperature": float(llm.get("temperature", 0.8)),
        "messages": [
            {"role": "system", "content": llm.get("systemPrompt", "")},
            {
                "role": "user",
                "content": (
                    f"好友昵称：{friend_name}\n消息场景：{scene}\n对方刚发来：{message}\n"
                    f"请只输出你要发出去的那一句话（不超过 {cfg.get('maxReplyChars', 30)} 字）。"
                ),
            },
        ],
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {llm['apiKey']}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=float(llm.get("timeoutSeconds", 20))) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        return (text or "").strip() or None
    except Exception as e:
        logger.warning(f"大模型生成回复失败，降级用模板：{e}")
        return None


def build_reply(scene, cfg, identity, friend_name, message, state):
    """按当前模式产出要发出去的回复文本。"""
    reply = None
    if cfg.get("mode") == "llm":
        reply = llm_reply(cfg, scene, friend_name, message)
        if not reply and not cfg.get("llm", {}).get("fallbackToTemplate", True):
            return None
    if not reply:
        reply = pick_template_reply(scene, cfg, identity, state, message=message)
    if not reply:
        return None
    limit = int(cfg.get("maxReplyChars", 30))
    reply = reply.strip()
    if limit and len(reply) > limit:
        reply = reply[:limit]
    return reply


# --------------------------------------------------------------------------- 页面读取


def _first_text(locator, selectors, timeout=1000):
    for sel in selectors:
        try:
            sub = locator.locator(sel).first
            t = sub.inner_text(timeout=timeout)
            if t and t.strip():
                return t.strip()
        except Exception:
            continue
    return ""


def find_conversation_items(page):
    """返回 (选择器, 会话项列表)。全不命中返回 (None, [])。"""
    for sel in CONV_ITEM_SELECTORS:
        try:
            loc = page.locator(sel)
            if loc.count() > 0:
                return sel, loc.all()
        except Exception:
            continue
    return None, []


def read_badge(item):
    """尝试读未读角标数字；读不到返回 0（不代表没有新消息）。"""
    for sel in BADGE_SELECTORS:
        try:
            t = item.locator(sel).first.inner_text(timeout=800)
            digits = "".join(ch for ch in (t or "") if ch.isdigit())
            if digits:
                return int(digits)
        except Exception:
            continue
    return 0


def read_conversations(page):
    """读出会话列表：[{index, item, name, unread, preview, time, raw}]。"""
    sel, items = find_conversation_items(page)
    out = []
    for idx, item in enumerate(items):
        name = _first_text(item, CONVERSATION_TITLE_SELECTORS)
        preview = _first_text(item, CONV_PREVIEW_SELECTORS)
        tm = _first_text(item, CONV_TIME_SELECTORS)
        raw = ""
        try:
            raw = (item.inner_text(timeout=1200) or "").strip()
        except Exception:
            pass
        if not name:
            lines = [l.strip() for l in raw.splitlines() if l.strip()]
            name = lines[0] if lines else ""
        out.append(
            {
                "index": idx,
                "item": item,
                "name": name,
                "unread": read_badge(item),
                "preview": preview,
                "time": tm,
                "raw": raw,
            }
        )
    return out


def read_last_messages(page, limit=6):
    """读取当前会话的最后若干条消息：[{text, outgoing, kind, has_link, is_sticker}]。

    消息项 `.messageMessageBoxmessageBox`；自身或其后代带 `isFromMe` 类即为自己发的；
    文本取 `.MessageItemTextbubbleTextContent`，分享卡片取作者名，表情取 `.MessageItemEmojiemojiBox`。
    """
    region = None
    for sel in MESSAGE_REGION_SELECTORS:
        try:
            loc = page.locator(sel)
            if loc.count() > 0:
                region = loc.first
                break
        except Exception:
            continue
    items = []
    if region is not None:
        for sel in MESSAGE_ITEM_SELECTORS:
            try:
                items = region.locator(sel).all()
            except Exception:
                items = []
            if items:
                break
    messages = []
    for el in items[-limit:]:
        try:
            cls = el.get_attribute("class") or ""
        except Exception:
            cls = ""
        outgoing = "isFromMe" in cls
        if not outgoing:
            try:
                outgoing = el.locator("[class*='isFromMe']").count() > 0
            except Exception:
                pass
        text, kind = "", "text"
        t = _first_text(el, MSG_TEXT_SELECTORS)
        if t:
            text = t
        else:
            author = _first_text(el, MSG_SHARE_AUTHOR_SELECTORS)
            if author:
                kind, text = "share", f"[分享了视频] {author}"
            else:
                emoji = _first_text(el, MSG_EMOJI_SELECTORS)
                if emoji:
                    kind, text = "sticker", emoji
        if not text:
            try:
                text = (el.inner_text(timeout=1000) or "").strip()
            except Exception:
                text = ""
        if not text:
            continue
        messages.append(
            {
                "text": text,
                "outgoing": outgoing,
                "kind": kind,
                "has_link": kind == "share",
                "is_sticker": kind == "sticker",
            }
        )
    if not messages:
        # 退化方案：直接读消息区纯文本，过滤时间行
        try:
            raw = region.inner_text(timeout=2000) if region is not None else ""
        except Exception:
            raw = ""
        for line in [l.strip() for l in (raw or "").splitlines() if l.strip()][-limit:]:
            if any(k in line for k in TIME_LIKE):
                continue
            messages.append({"text": line, "outgoing": False, "kind": "text",
                             "has_link": False, "is_sticker": False})
    return messages


def pick_incoming(messages, our_texts):
    """从最后一条往前找「对方发来的」消息。"""
    for m in reversed(messages):
        if m.get("outgoing"):
            continue
        text = (m.get("text") or "").strip()
        if not text or text in our_texts:
            continue
        if any(k in text for k in TIME_LIKE) and len(text) <= 8:
            continue
        return m
    return None


def our_recent_texts(targets=None, limit=3):
    """我们发出去过的文本特征（用于识别「最后一条是自己发的」，别把自己的消息当对方消息回）。

    除了实时生成的模板，还写死几个固定标记：即使生成模板失败（比如一言接口不通），
    也不会把自己的火花消息误判成对方消息。
    """
    texts = {"[盖瑞]今日火花", "[续火花]", "每日一言"}
    try:
        from core.msg_builder import build_message

        msg = build_message().strip()
        texts.add(msg)
        texts.add(msg.splitlines()[0])
    except Exception as e:
        logger.warning(f"自动回复：预取消息模板失败（不影响识别固定标记）：{e}")
    try:
        state = load_state()
        for v in state.get("lastReplies", {}).values():
            if v.get("text"):
                texts.add(v["text"])
    except Exception:
        pass
    return {t for t in texts if t}


# --------------------------------------------------------------------------- 主流程


def wait_for_list_ready(page, timeout=30):
    """等会话列表的昵称真正加载出来（没加载完时标题是一串数字 ID，读了会误判）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            items = page.locator("[data-e2e='conversation-item']").all()
        except Exception:
            items = []
        names = []
        for it in items[:5]:
            try:
                names.append(it.locator(".conversationConversationItemtitle").first.inner_text(timeout=1200).strip())
            except Exception:
                names.append("")
        if any(n and not n.isdigit() for n in names):
            return True
        time.sleep(1.5)
    logger.warning("自动回复：会话昵称等了 30 秒还没加载出来（可能是登录态异常或网络慢）")
    return False


def scan_once(page, cfg, state, targets=None, inspect=False):
    """跑一轮：读列表 -> 找有未读的会话 -> 打开 -> 识别 -> （按需）回复。"""
    targets = targets if targets is not None else []
    target_ids = {str(t) for t in targets}
    ours = our_recent_texts()
    convs = read_conversations(page)
    if not convs:
        dump_debug_artifacts(page, "auto-reply", "conversation-list-not-found")
        logger.warning("自动回复：读不到会话列表（可能登录态失效或前端改版）")
        return {"scanned": 0, "replied": 0}

    candidates = []
    for c in convs:
        ok, why = is_candidate(c, cfg, state, ours, target_ids)
        if ok:
            candidates.append(c)
            logger.info(f"自动回复：本轮处理「{c['name']}」——{why}")
    if cfg.get("onlyKnownFriends", True):
        white = cfg.get("whitelist") or []
        extra = cfg.get("extraFriendIds") or []
        known = [n for n, v in userIDDict.items() if v and str(v[0]) in target_ids]
        logger.info(
            f"自动回复：授权范围 = 白名单 {len(white)} 位昵称 + 额外 ID {len(extra)} 个"
            f"（共 {len(white) + len(extra)} 位好友）；本轮好友缓存里命中 {len(known)} 个"
        )
        logger.info(
            "自动回复：授权好友名单：" + "、".join(str(x) for x in white)
            + ("  +ID:" + "、".join(str(x) for x in extra) if extra else "")
        )
    # 把会话列表逐条打出来（排查「朋友发了消息但没回」时看这里）
    for c in convs[:12]:
        logger.info(
            f"  会话[{c['index']}] {c['name']} | 未读 {c['unread']} | 时间「{c['time']}」 | "
            f"预览「{(c['preview'] or '')[:24]}」 | 目标好友: "
            f"{'是' if str((userIDDict.get(c['name']) or [''])[0]) in target_ids else '否'}"
        )
    logger.info(
        f"自动回复：会话 {len(convs)} 个，其中未读 {len([c for c in convs if c['unread'] > 0])} 个，"
        f"本轮检查 {len(candidates)} 个"
    )

    stats = {"scanned": len(convs), "replied": 0, "skipped": 0}
    for conv in candidates:
        name = conv["name"]
        # 直接用会话列表的预览判断对方说了什么（比解析消息气泡稳得多，气泡 DOM 常读不到）
        text = (conv.get("preview") or "").strip()
        has_link = any(k in text for k in ("分享", "视频", "作品", "直播", "http"))
        is_sticker = bool(text) and len(text) <= 4 and all(not ch.isalnum() for ch in text)
        scene = classify_scene(text, has_link, is_sticker, conv_name=name)
        identity = name
        target_ok = is_target_friend(name, cfg, target_ids)
        if cfg.get("onlyKnownFriends", True) and not target_ok:
            logger.info(
                f"自动回复：跳过「{name}」——不在授权好友名单里"
                f"（把昵称加进 whitelist，或把 ID 加进 extraFriendIds）"
            )
            record_history({"friend": name, "scene": scene, "incoming": text[:80],
                            "action": "skip", "reason": "非目标好友"})
            stats["skipped"] += 1
            mark_handled(state, name, text)
            save_state(state)
            continue
        # 标记这条已处理，避免同一句话下一轮又被重复判断
        mark_handled(state, name, text)
        ok, reason = check_safety(identity, name, scene, text, cfg, state, target_ok=target_ok)
        if ok and not is_recent(conv.get("time"), cfg.get("recentMinutes")):
            ok, reason = False, f"最近一条消息是「{conv.get('time')}」，超过 {cfg.get('recentMinutes')} 分钟，不翻旧账"
        if not ok:
            logger.info(f"自动回复：不回「{name}」——{reason}（对方消息：{text[:30]}）")
            record_history({"friend": name, "scene": scene, "incoming": text[:80],
                            "action": "skip", "reason": reason})
            stats["skipped"] += 1
            save_state(state)
            continue
        if cfg.get("mode") == "readonly":
            plan = build_reply(scene, cfg, identity, name, text, state)
            logger.info(f"自动回复[只读]：「{name}」{scene} 场景，对方说「{text[:30]}」，拟回复「{plan}」")
            record_history({"friend": name, "scene": scene, "incoming": text[:80],
                            "action": "readonly", "plan": plan})
            stats["skipped"] += 1
            save_state(state)
            continue
        reply = build_reply(scene, cfg, identity, name, text, state)
        if not reply:
            logger.info(f"自动回复：「{name}」没生成出回复内容，跳过")
            record_history({"friend": name, "scene": scene, "incoming": text[:80],
                            "action": "skip", "reason": "无可用话术"})
            stats["skipped"] += 1
            save_state(state)
            continue
        # 只有真要发消息时才点开会话（避免把消息无意义地标成已读）
        try:
            conv["item"].click()
            time.sleep(2.0)
        except Exception as e:
            logger.warning(f"自动回复：打开会话「{name}」失败：{e}")
            continue
        _, editor = first_visible_locator(page, CHAT_EDITOR_SELECTORS, timeout=10000)
        if editor is None:
            dump_debug_artifacts(page, "auto-reply", "chat-editor-not-found")
            logger.warning(f"自动回复：找不到输入框（{name}），本轮跳过")
            continue
        leftover = send_message(editor, reply)
        if clean_editor_text(leftover or ""):
            logger.warning(f"自动回复：给「{name}」发送未确认成功（残留 {clean_editor_text(leftover)!r}）")
            record_history({"friend": name, "scene": scene, "incoming": text[:80],
                            "action": "send-unconfirmed", "reply": reply})
        else:
            mark_replied(state, identity, reply)
            stats["replied"] += 1
            logger.info(f"自动回复：已回复「{name}」-> {reply}")
            record_history({"friend": name, "scene": scene, "incoming": text[:80],
                            "action": "replied", "reply": reply})
        save_state(state)
        time.sleep(random.uniform(1.5, 3.5))
    save_state(state)
    if inspect:
        dump_debug_artifacts(page, "auto-reply", "inspect")
        logger.info("自动回复：已把当前页面 HTML/截图 落盘到 logs/debug（用于校准选择器）")
    return stats


def run_auto_reply(mode=None, once=False, inspect=False):
    """循环（默认全天候）执行自动回复；once=True 只跑一轮。"""
    cfg = load_config()
    if mode:
        cfg["mode"] = mode
    state = load_state()
    if not cfg.get("enabled"):
        logger.info("自动回复：总开关关闭，直接退出")
        return
    logger.info(
        f"自动回复启动：模式={cfg.get('mode')}，间隔={cfg.get('intervalSeconds')}s，"
        f"时段={'全天候' if not cfg.get('activeHours') else cfg.get('activeHours')}"
    )
    if cfg.get("mode") == "llm" and not (cfg.get("llm", {}) or {}).get("enabled"):
        logger.warning("自动回复：mode=llm 但 llm.enabled=false（或没填 baseUrl/apiKey），本轮会退回模板话术")

    while True:
        if not in_active_hours(cfg):
            logger.info("自动回复：当前不在配置的时段内，等待下一轮")
        else:
            playwright, browser = get_browser()
            try:
                for user in userData:
                    cookies = user["cookies"]
                    targets = user["targets"]
                    username = user.get("username", "未知用户")
                    context = browser.new_context(
                        user_agent=(
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
                        ),
                        locale="zh-CN",
                        timezone_id="Asia/Shanghai",
                        viewport={"width": 1280, "height": 800},
                    )
                    try:
                        context.add_cookies(cookies)
                        page = context.new_page()
                        page.add_init_script(
                            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
                        )
                        # 复用 tasks 的响应钩子：缓存「昵称 -> 短ID」，用于校验是否为目标好友
                        page.on("response", handle_response)
                        page.goto("https://www.douyin.com/chat", wait_until="domcontentloaded",
                                  timeout=60000)
                        wait_for_chat_ready(page, username)
                        wait_for_list_ready(page)
                        stats = scan_once(page, cfg, state, targets, inspect=inspect)
                        logger.info(
                            f"自动回复[{username}] 本轮结束：会话 {stats['scanned']} 个 / "
                            f"已回复 {stats['replied']} 位 / 跳过 {stats['skipped']} 位"
                        )
                    finally:
                        try:
                            context.close()
                        except Exception:
                            pass
            except Exception as e:
                logger.error(f"自动回复本轮异常：{e}")
            finally:
                # 页面还有未完成请求时 driver 可能已断开，收尾失败不应影响下一轮
                try:
                    browser.close()
                except Exception:
                    pass
                try:
                    playwright.stop()
                except Exception:
                    pass
        if once:
            logger.info("自动回复：--once 单轮结束")
            return
        time.sleep(max(30, int(cfg.get("intervalSeconds", 480))))
