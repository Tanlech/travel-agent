from __future__ import annotations

import re

from app.domain.intent.prompt import INTENT_RECOGNITION_SYSTEM_PROMPT, build_intent_recognition_prompt
from app.domain.intent.schema import (
    IntentPlanningRequest,
    IntentRecognitionInput,
    IntentRecognitionOutput,
    REQUIRED_PATCH_FIELDS,
    normalize_date,
)
from app.infrastructure.llm.client import get_llm_client

class IntentRecognizer:
    # ============================================================
    # 入口：双路径意图识别
    # 主路径：LLM 语义理解（prompt 已约束日期区间成对/目的地/人数解析，见 prompt.py）
    # 兜底：仅当 LLM 不可用/超时/解析失败时，用规则 fallback 保证对话不中断
    # 注意：规则 fallback 只是极端兜底，不是解析主力——口语语义（省略、区间、
    # 跨月、中文数字等）永远优先交给 LLM 理解，不要靠正则逐字补齐。
    # ============================================================

    # recognize 是 intent 模块的统一入口：优先走 LLM，失败再走最薄 fallback
    def recognize(self, intent_input: IntentRecognitionInput) -> IntentRecognitionOutput:
        llm_result = self._recognize_with_llm(intent_input)
        if llm_result is not None:
            return llm_result
        return self._fallback(intent_input)

    # 这里负责调用 LLM 做结构化意图识别
    def _recognize_with_llm(self, intent_input: IntentRecognitionInput) -> IntentRecognitionOutput | None:
        llm_client = get_llm_client()
        if not llm_client.is_enabled():
            return None
        prompt = build_intent_recognition_prompt(intent_input)
        result = llm_client.generate_intent_recognition(
            system_prompt=INTENT_RECOGNITION_SYSTEM_PROMPT,
            user_prompt=prompt,
        )
        if result is None:
            return None
        return result

    # fallback 只做最薄规则兜底：按优先级识别
    #   revise_plan（已有行程 + 改稿话术）→ qa（问候/闲聊）→ clarification/new_plan（字段判定）→ unknown
    # confirm / end_session 依赖 LLM 判断（后续可补）
    # 优先级设计意图（从高到低）：
    #   1. end_session  告别语无条件优先——用户要走，别的都不重要
    #   2. revise_plan  改稿是"动作性"最强的意图，且必须有已有行程才成立
    #   3. confirm      确认当前行程（需要已有行程 + 确认话术 + 无新规划信号）
    #   4. qa           闲聊/问答（无任何规划信号时才成立）
    #   5. 字段判定      落到 clarification（缺字段）或 new_plan（字段齐）
    #   6. unknown      以上都不满足
    # ⚠️ 本方法是 LLM 不可用时的降级预案；日常运行应走 LLM 语义理解（prompt.py）
    def _fallback(self, intent_input: IntentRecognitionInput) -> IntentRecognitionOutput:
        raw_message = (intent_input.raw_message or "").strip()

        # 本轮 patch 只从原话解析（日期/人数/目的地），不再把已累计 request 全量当 patch
        request_patch = self._parse_patch_from_message(intent_input)

        # ---- 判定优先级 1：end_session ----
        # 结束语无条件优先：再见/拜拜/结束/退出
        if _is_end_session_message(raw_message):
            return IntentRecognitionOutput(
                intent_type="end_session",
                reasoning="好的，祝你旅途愉快！再见。",
            )

        # ---- 判定优先级 2：revise_plan ----
        # 需要"已有行程"上下文（latest_plan_summary 或 session 阶段/修订计数），
        # 且原话含改稿话术（换成/调整/第N天改...）
        if self._has_existing_plan(intent_input) and _is_revision_message(raw_message):
            return IntentRecognitionOutput(
                intent_type="revise_plan",
                extracted_request_patch=request_patch,
                revision_scope_hint=_infer_revision_scope(raw_message),
                should_load_existing_artifacts=True,
                reasoning="LLM intent 识别不可用，根据已有行程 + 改稿话术判定为 revise_plan。",
            )

        # ---- 判定优先级 3：confirm ----
        # 已有行程 + 确认话术（可以/没问题/就这样）→ 确认当前行程
        # 注意：确认话术里若带规划信号（如"好的，8月10号"），应走字段判定而非 confirm
        if (
            self._has_existing_plan(intent_input)
            and _is_confirm_message(raw_message)
            and not _has_planning_signal(raw_message)
        ):
            return IntentRecognitionOutput(
                intent_type="confirm",
                reasoning="好的，行程已确认。",
            )

        # ---- 判定优先级 4：qa ----
        # 问候/闲聊/感谢，且原话不含任何规划信号（目的地/日期/人数）
        if _is_qa_message(raw_message):
            return IntentRecognitionOutput(
                intent_type="qa",
                reasoning="LLM intent 识别不可用，按问候/闲聊关键词判定为 qa。",
            )

        # ---- 判定优先级 5：按字段缺失判定 clarification / new_plan ----
        # 合并"历史累计需求 + 本轮解析 patch"，得到解析后的需求视图，据此判断还缺什么
        # 这样即使第一轮还没有结构化 request（如"我想去北京玩"），也能靠解析出的 patch 判 clarification
        merged_payload: dict[str, object] = {}
        if intent_input.planning_request is not None:
            merged_payload.update(intent_input.planning_request.model_dump())
        merged_payload.update(request_patch)

        if merged_payload:
            merged_request = IntentPlanningRequest(**merged_payload)
            missing_fields = self._collect_missing_fields(merged_request)
            if missing_fields:
                return IntentRecognitionOutput(
                    intent_type="clarification",
                    extracted_request_patch=request_patch,
                    missing_fields=missing_fields,
                    reasoning="LLM intent 识别不可用，退回到字段缺失 fallback。",
                )
            # 字段已齐但本轮没有任何新信息/规划信号（如闲聊被带到此处）：
            # 不判 new_plan，避免在已有完整需求时被一句闲聊触发重规划
            if not request_patch and not _has_planning_signal(raw_message):
                return IntentRecognitionOutput(
                    intent_type="unknown",
                    reasoning="LLM intent 识别不可用，字段已齐但本轮无新规划信息。",
                )
            return IntentRecognitionOutput(
                intent_type="new_plan",
                extracted_request_patch=request_patch,
                reasoning="LLM intent 识别不可用，退回到结构化 request fallback。",
            )
        if raw_message:
            return IntentRecognitionOutput(
                intent_type="unknown",
                reasoning="LLM intent 识别不可用，且没有足够结构化信息。",
            )
        return IntentRecognitionOutput(
            intent_type="unknown",
            reasoning="空输入。",
        )

    # fallback 判断"是否已有行程"：
    # 优先看 latest_plan_summary（LLM 路径的 revise 判定依据，填充见 orchestrator）；
    # 否则退化到 session 阶段/修订计数，判断本次会话是否已经产出过行程
    def _has_existing_plan(self, intent_input: IntentRecognitionInput) -> bool:
        # 三重判据，任一命中即认为已有行程：
        # 1. latest_plan_summary：orchestrator 每次规划/改稿后写入的轻量摘要
        # 2. revision_count > 0：改过稿说明必然已有一版行程
        # 3. conversation_stage 处于"已产出过行程"的阶段（revise_ready/revise_collecting/completed/closed）
        #    注意：planning/qa 阶段不在此列——planning 是规划执行中，qa 不保证有行程
        if intent_input.latest_plan_summary:
            return True
        ctx = intent_input.session_context or {}
        if bool(ctx.get("revision_count")):
            return True
        return ctx.get("conversation_stage") in ("revise_ready", "revise_collecting", "completed", "closed")

    # 当前先把“目的地 + 游玩日期”作为关键字段；预算暂时不强制要求
    # 字段集合与 schema 层 REQUIRED_PATCH_FIELDS 同源，保持 intent 内部口径一致
    def _collect_missing_fields(self, request) -> list[str]:
        return [f for f in REQUIRED_PATCH_FIELDS if not str(getattr(request, f, None) or "").strip()]

    # 从本轮原话解析能可靠提取的字段 patch（fallback 专用，遵守 patch-only 原则）
    # 只解析三类字段：
    # - 日期（start_date/end_date）：能解析就放 patch
    # - 人数（travelers）：能解析就放 patch
    # - 目的地（destination）：仅当当前 request 缺 destination 时才尝试，避免覆盖已确认目的地
    # 其余偏好/景点字段无法可靠解析，不放进 patch（保持追问）
    def _parse_patch_from_message(self, intent_input: IntentRecognitionInput) -> dict[str, object]:
        raw_message = intent_input.raw_message or ""
        request = intent_input.planning_request
        patch: dict[str, object] = {}

        start_date, end_date = extract_dates(raw_message)
        if start_date:
            patch["start_date"] = start_date
        if end_date:
            patch["end_date"] = end_date

        travelers = extract_travelers(raw_message)
        if travelers:
            patch["travelers"] = travelers

        # 关键：destination 只在"当前还没确认"时才提取，否则用"8月10号去北京"这种话
        # 可能把已确认的目的地覆盖掉（补日期轮次用户常常顺口带出目的地名）
        current_destination = str(getattr(request, "destination", None) or "").strip() if request else ""
        if not current_destination:
            destination = extract_destination(raw_message)
            if destination:
                patch["destination"] = destination

        return patch


intent_recognizer = IntentRecognizer()


# ============================================================
# fallback 解析辅助（模块级纯函数，可独立单测）
# ============================================================

def extract_dates(message: str) -> tuple[str | None, str | None]:
    """从中文消息解析游玩起止日期，返回 (start_date, end_date)，格式 YYYY-MM-DD。

    支持格式（fallback 尽力解析；LLM 正常时不依赖它）：
    - 2026-08-10 到 2026-08-12
    - 8月10号到8月12号 / 8月10日到12号 / 8月10号-8月12号（支持跨月：9月30号到10月2号）
    - 8月10号（只有单日时 end_date 返回 None，由系统追问补全）

    解析出的年月日统一交给 schema.normalize_date 做合法性 + 跨年校验，与 LLM 路径语义一致：
    - 非法日期（如 8月40号 / 2月30号）返回 None，不产出坏数据
    - 无年份日期沿用"当年，早于今天 60 天以上进位次年"规则（如 12 月说"1月2号"→ 次年 1 月 2 号）
    """
    text = message.replace("～", "到").replace("~", "到").replace("—", "到").replace("–", "到").replace("至", "到")

    # 双完整日期：2026-08-10 到 2026-08-12
    m = re.search(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})\s*(?:到)\s*(\d{4})[/-](\d{1,2})[/-](\d{1,2})", text)
    if m:
        return (
            normalize_date(f"{m.group(1)}-{m.group(2)}-{m.group(3)}"),
            normalize_date(f"{m.group(4)}-{m.group(5)}-{m.group(6)}"),
        )

    # 中文日期对：8月15到8月17号 / 8月15号到8月17号 / 8月10号到12号 / 9月30号到10月2号 / 8月10号-8月12号
    # 注意第一段"号/日"可省略（用户常写"8月15到8月17号"），第二段月份可缺省（沿用第一个）
    m = re.search(r"(\d{1,2})月(\d{1,2})(?:号|日)?\s*(?:到|[-–])\s*(?:(\d{1,2})月)?(\d{1,2})(?:号|日)", text)
    if m:
        month1, day1, month2, day2 = m.group(1), m.group(2), m.group(3), m.group(4)
        end_month = month2 if month2 else month1
        return normalize_date(f"{month1}月{day1}号"), normalize_date(f"{end_month}月{day2}号")

    # 单个中文日期：8月10号
    m = re.search(r"(\d{1,2})月(\d{1,2})(?:号|日)", text)
    if m:
        return normalize_date(f"{m.group(1)}月{m.group(2)}号"), None

    return None, None


def extract_travelers(message: str) -> int | None:
    """解析总人数：3个人 / 3人 / 2大1小 / 两个人 → 返回总人数。

    解析优先级：
    1. "2大1小"组合：直接相加（家庭出行最精确）
    2. 阿拉伯数字 + 人（"3个人"/"3人"）
    3. 中文数字 + 人（"两个人"），经 _cn_number 转换
    解析不到返回 None，由系统追问，避免误把"1周"当人数。
    """
    m = re.search(r"(\d+)\s*大\s*(\d+)\s*小", message)
    if m:
        return int(m.group(1)) + int(m.group(2))
    m = re.search(r"(\d+)\s*(?:个)?人", message)
    if m:
        return int(m.group(1))
    m = re.search(r"([一二两三四五六七八九十]+)\s*(?:个)?人", message)
    if m:
        return _cn_number(m.group(1))
    return None


def _cn_number(text: str) -> int | None:
    """中文数字转阿拉伯数字：一~九、十、十二、二十、二十五。"""
    digits = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if text in digits:
        return digits[text]
    if "十" in text:
        parts = text.split("十")
        tens = digits.get(parts[0], 1) if parts[0] else 1
        ones = digits.get(parts[1], 0) if len(parts) > 1 and parts[1] else 0
        return tens * 10 + ones
    return None


# 目的地提取时要去掉的常见引导词/动词/助词/问候词
_DESTINATION_STOPWORDS = (
    "我想", "我要", "打算", "准备", "计划", "规划", "安排", "想去", "带我们", "帮我",
    "去", "玩", "旅游", "旅行", "游玩", "逛逛", "看看", "看一下", "一下", "的", "个",
    "吧", "啊", "呢", "我们", "帮忙", "给", "推荐", "行程", "路线", "攻略", "到",
    "哪里", "哪儿", "哪", "什么", "啥", "地方",
    "你好", "您好", "嗨", "哈喽", "在吗", "谢谢", "感谢", "辛苦了", "太棒了", "不错", "hello", "hi",
    "太感谢了", "谢谢啦", "感谢你", "多谢", "谢谢你们",
    "再见", "拜拜", "结束", "退出", "下次再聊", "就到这里", "不聊了",
    "可以", "好的", "行", "没问题", "就这样", "确认", "挺好",
)


# 出行/规划语义信号词：命中任一才认为句子可能含目的地（fallback 提取的前置门槛，
# 防止"今天天气不错"这类闲聊被启发式当成目的地、误判成规划请求）
_PLANNING_VERB_SIGNALS = (
    "去", "到", "玩", "游", "逛", "旅游", "旅行", "游玩", "逛逛", "看看", "攻略",
    "行程", "规划", "安排", "打算", "准备", "计划", "推荐", "好玩", "有意思", "出发",
)


def _is_clean_destination_name(name: str) -> bool:
    """目的地候选的后置校验：剔除启发式提取残留的疑问/陈述碎片。

    fallback 提取是"剔词"式启发式，"北京有什么好玩的"会残留"北京有好玩"这类
    带动词/疑问词的脏文本，命中以下词根即视为不可靠，放弃提取（交给追问）。
    """
    for token in ("有", "怎么", "什么", "吗", "呢", "要", "是", "好玩", "推荐", "看看", "呀"):
        if token in name:
            return False
    return True


def extract_destination(message: str) -> str | None:
    """尽力从消息提取目的地。

    策略：去掉日期/人数/数字片段和常见引导词后，剩余 1~6 字文本作为候选。
    提取不到（剩余为空或过长）返回 None，由系统追问，避免误判。

    注意：这是"尽力而为"的启发式提取，仅用于 fallback；
    LLM 正常时目的地提取交给模型，不依赖这里的精度。
    """
    # 前置门槛：句子必须含出行/规划语义信号才提取目的地，
    # 否则"今天天气不错"这类闲聊会被当成目的地（fallback 误判为 new_plan）
    if not any(signal in message for signal in _PLANNING_VERB_SIGNALS):
        return None
    cleaned = message
    # 第一步：先剔除日期/人数/数字等结构化片段（这些不可能是目的地）
    # 注意"8月15"这类不带"号/日"的日期也要剔除（用户常省略后缀）
    for token in re.findall(
        r"\d{4}[/-]\d{1,2}[/-]\d{1,2}|\d{1,2}月\d{1,2}(?:号|日)?|\d{1,2}(?:号|日)|"
        r"\d+\s*大\s*\d+\s*小|\d+\s*(?:个)?人|[一二两三四五六七八九十]+(?:个)?人|"
        r"\d{1,2}月|\d+\s*天",
        cleaned,
    ):
        cleaned = cleaned.replace(token, "")
    # 第二步：剔除引导词/语气词/问候语
    # 按长度降序替换：长词（太感谢了）要先于短词（感谢）替换，否则短词会先破坏长词
    for word in sorted(_DESTINATION_STOPWORDS, key=len, reverse=True):
        cleaned = cleaned.replace(word, "")
    # 第三步：剔除标点与空白，看剩余是否是一个合理的短地名
    cleaned = re.sub(r"[\s，。,.！？!?、；;：:（）()「」『』【】]", "", cleaned)
    if 1 <= len(cleaned) <= 6 and _is_clean_destination_name(cleaned):
        return cleaned
    return None


# ============================================================
# fallback 意图判定辅助（模块级纯函数，可独立单测）
# ============================================================

# 强改稿信号：出现即判定为改稿（无需对象词）
_REVISION_STRONG_KEYWORDS = ("换成", "改成", "改到", "调整", "修改", "优化", "更新", "去掉", "删除", "取消", "调整一下")

# 弱改稿动词："改/换" 需要配合行程对象词才判定，避免"改天/换话题"误判
_REVISION_WEAK_VERBS = ("改", "换")
_REVISION_OBJECT_WORDS = (
    "天", "行程", "安排", "住宿", "酒店", "交通", "节奏", "顺序", "景点", "餐厅", "预算", "方案", "路线",
)


def _is_revision_message(message: str) -> bool:
    """判断原话是否为改稿请求（fallback 专用）。

    规则：
    - "改天"这类闲聊话术直接排除，防止误判
    - 强关键词（换成/调整/修改...）出现即判定
    - 弱动词（改/换）需要同时出现行程对象词（第N天/行程/酒店...）才判定
    """
    if "改天" in message or "改日" in message:
        return False
    if any(kw in message for kw in _REVISION_STRONG_KEYWORDS):
        return True
    if any(verb in message for verb in _REVISION_WEAK_VERBS):
        return any(obj in message for obj in _REVISION_OBJECT_WORDS)
    return False


def _infer_revision_scope(message: str) -> str:
    """从改稿话术推断改动范围（fallback 尽力）：
    - 提到整体/全部/全局/整个 → global
    - 提到第N天 → day_level
    - 否则默认 block_level（最局部）
    """
    if any(kw in message for kw in ("整体", "全部", "全局", "整个", "从头")):
        return "global"
    if re.search(r"第[一二两三四五六七八九十\d]+天", message):
        return "day_level"
    return "block_level"


# end_session 关键词：结束会话的告别语
_END_SESSION_KEYWORDS = ("再见", "拜拜", "结束", "退出", "不聊了", "下次再聊", "就到这里")


def _is_end_session_message(message: str) -> bool:
    return any(kw in message for kw in _END_SESSION_KEYWORDS)


# confirm 关键词：确认当前行程（注意与澄清过程的"好的"区分，见 _is_confirm_message）
_CONFIRM_KEYWORDS = ("没问题", "就这样", "可以", "好的", "行", "确认", "挺好", "不错", "OK", "ok")


def _is_confirm_message(message: str) -> bool:
    lowered = message.lower()
    return any(kw in lowered for kw in _CONFIRM_KEYWORDS)


def _has_planning_signal(message: str) -> bool:
    """原话是否含规划信号（目的地/日期/人数任一）。

    用于把"好的，8月10号"这类带补充信息的确认，与纯确认（"可以"）区分开，
    避免把补信息的对话误判为 confirm/qa。
    """
    start_date, end_date = extract_dates(message)
    if start_date or end_date:
        return True
    if extract_travelers(message):
        return True
    if extract_destination(message):
        return True
    return False


# qa 关键词：问候/闲聊/感谢/能力询问。注意与规划信号互斥（见 _is_qa_message）
_QA_KEYWORDS = (
    "你好", "您好", "嗨", "哈喽", "hello", "hi", "在吗",
    "谢谢", "感谢", "辛苦了", "太棒了", "不错",
    "你是谁", "叫什么", "能做什么", "会什么", "怎么用", "介绍一下", "有什么功能", "什么功能",
)


def _is_qa_message(message: str) -> bool:
    """判断原话是否为闲聊/问答（fallback 专用）。

    规则：命中 qa 关键词，且不含任何规划信号（目的地/日期/人数）。
    这样"你好，8月10号到12号去北京"会优先走规划判定，而不是被误判为 qa。
    """
    lowered = message.lower()
    if not any(kw in lowered for kw in _QA_KEYWORDS):
        return False
    start_date, end_date = extract_dates(message)
    if start_date or end_date:
        return False
    if extract_travelers(message):
        return False
    if extract_destination(message):
        return False
    return True
