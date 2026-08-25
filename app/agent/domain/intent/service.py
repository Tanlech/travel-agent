from __future__ import annotations

import logging
import re

from app.agent.domain.common.dates import normalize_date
from app.agent.domain.common.planning import compute_missing_fields
from app.agent.domain.intent.prompt import INTENT_RECOGNITION_SYSTEM_PROMPT, build_intent_recognition_prompt
from app.agent.domain.intent.schema import (
    IntentPlanningRequest,
    IntentRecognitionInput,
    IntentRecognitionOutput,
)
from app.infrastructure.llm_client import get_llm_client

logger = logging.getLogger(__name__)


class IntentRecognizer:
    # 双路径识别：LLM 语义理解优先，失败走规则 fallback 兜底

    # 统一入口：优先 LLM，失败走 fallback，并记录识别结果
    def recognize(self, intent_input: IntentRecognitionInput) -> IntentRecognitionOutput:
        llm_result = self._recognize_with_llm(intent_input)
        if llm_result is not None:
            # 规则闸：LLM 把纯寒暄误判成 clarification/new_plan 时纠正为 qa（确定性兜底，
            # 仅当命中 qa 关键词且不含任何规划信号才触发，不影响正常规划判定）
            llm_result = self._postprocess_llm_qa(intent_input, llm_result)
            # qa 保留原话里明确提到的规划字段（如"我想去阳江，有什么好玩的吗"的目的地），
            # 让闲聊中透露的目的地累计进会话；但已确认目的地时随口提到的另一地点不得覆盖
            if llm_result.intent_type == "qa":
                llm_result = self._guard_qa_patch(intent_input, llm_result)
            logger.info(
                "intent[llm] raw=%r -> intent=%s patch=%s missing=%s scope=%s",
                intent_input.raw_message,
                llm_result.intent_type,
                llm_result.extracted_request_patch,
                llm_result.missing_fields,
                llm_result.revision_scope_hint,
            )
            return llm_result
        fallback_result = self._fallback(intent_input)
        logger.info(
            "intent[fallback] raw=%r -> intent=%s patch=%s missing=%s scope=%s",
            intent_input.raw_message,
            fallback_result.intent_type,
            fallback_result.extracted_request_patch,
            fallback_result.missing_fields,
            fallback_result.revision_scope_hint,
        )
        return fallback_result

    # 调用 LLM 做结构化意图识别（重试由 LLM client 内部负责：最多 3 次带解析失败反馈的重试，
    # 这里不再外层重试，避免同一 prompt 重复发送放大 LLM 成本且无反馈收益）
    def _recognize_with_llm(self, intent_input: IntentRecognitionInput) -> IntentRecognitionOutput | None:
        llm_client = get_llm_client()
        if not llm_client.is_enabled():
            return None
        prompt = build_intent_recognition_prompt(intent_input)
        return llm_client.generate_intent_recognition(
            system_prompt=INTENT_RECOGNITION_SYSTEM_PROMPT,
            user_prompt=prompt,
        )

    # 规则闸：LLM 判定为"需补字段"（clarification/new_plan）但原话实为闲聊时，纠正为 qa。
    # 判定用原则式 _is_casual_talk（无规划信号且不含出行动词），不依赖"列举寒暄词"，因此
    # 任意闲聊说法（包括例子里没写到的）都能兜住；含目的地/日期/人数/出行动词则不误伤。
    def _postprocess_llm_qa(
        self, intent_input: IntentRecognitionInput, llm_result: IntentRecognitionOutput
    ) -> IntentRecognitionOutput:
        if llm_result.intent_type not in ("clarification", "new_plan"):
            return llm_result
        raw_message = (intent_input.raw_message or "").strip()
        if not _is_casual_talk(raw_message):
            return llm_result
        return llm_result.model_copy(
            update={
                "intent_type": "qa",
                "extracted_request_patch": {},
                "missing_fields": [],
                "reasoning": f"{llm_result.reasoning or ''}（规则闸：闲聊兜底为 qa）",
            }
        )

    # qa 分支保留原话里明确提到的规划字段（schema 不再清空 qa 的 patch），
    # 让闲聊中透露的目的地也能累计进会话需求；但当前已确认目的地时，闲聊随口提到的
    # 另一个地点不得覆盖（与 fallback _parse_patch_from_message 的 destination-only-when-empty 对齐）
    def _guard_qa_patch(
        self, intent_input: IntentRecognitionInput, llm_result: IntentRecognitionOutput
    ) -> IntentRecognitionOutput:
        patch = dict(llm_result.extracted_request_patch or {})
        if not patch:
            return llm_result
        current_destination = (
            str(getattr(intent_input.planning_request, "destination", None) or "").strip()
            if intent_input.planning_request is not None
            else ""
        )
        qa_destination = patch.get("destination")
        if current_destination and qa_destination and qa_destination != current_destination:
            patch.pop("destination")
            return llm_result.model_copy(update={"extracted_request_patch": patch})
        return llm_result

    # LLM 不可用时的降级识别（按优先级）
    def _fallback(self, intent_input: IntentRecognitionInput) -> IntentRecognitionOutput:
        raw_message = (intent_input.raw_message or "").strip()
        # 日期/人数/目的地各只提取一次，后续 confirm/unknown/qa 判定通过 parsed 复用，
        # 避免同一消息被多条分支重复正则扫描
        start_date, end_date = extract_dates(raw_message)
        travelers = extract_travelers(raw_message)
        destination = extract_destination(raw_message)
        # patch 只从原话解析，不把已累计 request 当 patch
        request_patch = self._parse_patch_from_message(
            intent_input,
            start_date=start_date,
            end_date=end_date,
            travelers=travelers,
            destination=destination,
        )
        # ---- 判定优先级 1：end_session ----
        # 告别语无条件优先
        if _is_end_session_message(raw_message):
            return IntentRecognitionOutput(
                intent_type="end_session",
                reasoning="好的，祝你旅途愉快！再见。",
            )

        # ---- 判定优先级 2：reject ----
        # 明确拒绝优先，避免被误判为 new_plan/clarification
        if _is_reject_message(raw_message):
            return IntentRecognitionOutput(
                intent_type="reject",
                reasoning="好的，那就不规划了。有需要随时找我。",
            )

        # ---- 判定优先级 3：revise_plan ----
        # 需已有行程（latest_plan_summary 或 session 阶段/修订计数）且原话含改稿话术
        if self._has_existing_plan(intent_input) and _is_revision_message(raw_message):
            return IntentRecognitionOutput(
                intent_type="revise_plan",
                extracted_request_patch=request_patch,
                revision_scope_hint=_infer_revision_scope(raw_message),
                reasoning="LLM intent 识别不可用，根据已有行程 + 改稿话术判定为 revise_plan。",
            )

        # ---- 判定优先级 4：confirm ----
        # 已有行程 + 确认话术；若带规划信号（如"好的，8月10号"）则走字段判定而非 confirm
        if (
            self._has_existing_plan(intent_input)
            and _is_confirm_message(raw_message)
            and not _has_planning_signal(raw_message, parsed=(start_date, end_date, travelers, destination))
        ):
            return IntentRecognitionOutput(
                intent_type="confirm",
                reasoning="好的，行程已确认。",
            )

        # ---- 判定优先级 5：qa ----
        # 问候/闲聊/感谢，且不含任何规划信号
        if _is_qa_message(raw_message, parsed=(start_date, end_date, travelers, destination)):
            return IntentRecognitionOutput(
                intent_type="qa",
                reasoning="LLM intent 识别不可用，按问候/闲聊关键词判定为 qa。",
            )

        # ---- 判定优先级 6：按字段缺失判定 clarification / new_plan ----
        # 合并历史累计需求 + 本轮 patch，据此判断还缺什么
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
            # 字段已齐但本轮无新信息/规划信号：不判 new_plan，避免闲聊触发重规划
            if not request_patch and not _has_planning_signal(raw_message, parsed=(start_date, end_date, travelers, destination)):
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

    # 判断"是否已有行程"，任一判据命中即成立：
    # has_plan（产物落库标记）> latest_plan_summary（轻量摘要）> revision_count（改过稿必有行程）
    # > conversation_stage 处于已产出行程的阶段（revise_ready/revise_collecting/completed/closed；
    #   planning/qa 不算：planning 是执行中，qa 不保证有行程）
    def _has_existing_plan(self, intent_input: IntentRecognitionInput) -> bool:
        if intent_input.has_plan:
            return True
        if intent_input.latest_plan_summary:
            return True
        ctx = intent_input.session_context
        if ctx.revision_count:
            return True
        return ctx.conversation_stage in ("revise_ready", "revise_collecting", "completed", "closed")

    # 关键字段缺失判定收敛在 common 层（与 session merge / orchestrator 校验共用同一实现）
    def _collect_missing_fields(self, request) -> list[str]:
        return compute_missing_fields(request)

    # 从本轮原话解析可可靠提取的 patch（fallback 专用，遵守 patch-only）
    # 只解析日期/人数/目的地；destination 仅当当前未确认时提取，避免补日期时顺口带出目的地覆盖已确认值
    # 已解析值可由调用方传入复用（fallback 全程一次解析），默认 None 时自行提取
    def _parse_patch_from_message(
        self,
        intent_input: IntentRecognitionInput,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        travelers: int | None = None,
        destination: str | None = None,
    ) -> dict[str, object]:
        raw_message = intent_input.raw_message or ""
        request = intent_input.planning_request
        patch: dict[str, object] = {}

        if start_date is None and end_date is None:
            start_date, end_date = extract_dates(raw_message)
        if start_date:
            patch["start_date"] = start_date
        if end_date:
            patch["end_date"] = end_date

        if travelers is None:
            travelers = extract_travelers(raw_message)
        if travelers:
            patch["travelers"] = travelers

        current_destination = str(getattr(request, "destination", None) or "").strip() if request else ""
        if not current_destination:
            if destination is None:
                destination = extract_destination(raw_message)
            if destination:
                patch["destination"] = destination

        return patch


intent_recognizer = IntentRecognizer()


# ============================================================
# fallback 解析辅助（模块级纯函数，可独立单测）
# ============================================================

def extract_dates(message: str) -> tuple[str | None, str | None]:
    """从中文消息解析起止日期，返回 (start_date, end_date)，格式 YYYY-MM-DD"""
    text = message.replace("～", "到").replace("~", "到").replace("—", "到").replace("–", "到").replace("至", "到")

    # 双完整日期：2026-08-10 到 2026-08-12
    m = re.search(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})\s*(?:到)\s*(\d{4})[/-](\d{1,2})[/-](\d{1,2})", text)
    if m:
        return (
            normalize_date(f"{m.group(1)}-{m.group(2)}-{m.group(3)}"),
            normalize_date(f"{m.group(4)}-{m.group(5)}-{m.group(6)}"),
        )

    # 中文日期对：8月15到8月17号（两端"号/日"可省略，第二段月份可缺省沿用第一个）
    m = re.search(r"(\d{1,2})月(\d{1,2})(?:号|日)?\s*(?:到|[-–])\s*(?:(\d{1,2})月)?(\d{1,2})(?:号|日)?", text)
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
    """解析总人数：3个人 / 3人 / 2大1小 / 两个人 → 总人数；解析不到返回 None。"""
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


# 目的地提取时要剔除的引导词/动词/助词/问候语
_DESTINATION_STOPWORDS = (
    "我想", "我要", "打算", "准备", "计划", "规划", "安排", "想去", "带我们", "帮我",
    "要", "去", "玩", "旅游", "旅行", "游玩", "逛逛", "看看", "看一下", "一下", "的", "个",
    "吧", "啊", "呢", "我们", "帮忙", "给", "推荐", "行程", "路线", "攻略", "景点", "到",
    "哪里", "哪儿", "哪", "什么", "啥", "地方",
    "你好", "您好", "嗨", "哈喽", "在吗", "谢谢", "感谢", "辛苦了", "太棒了", "不错", "hello", "hi",
    "太感谢了", "谢谢啦", "感谢你", "多谢", "谢谢你们",
    "再见", "拜拜", "结束", "退出", "下次再聊", "就到这里", "不聊了",
    "可以", "好的", "行", "没问题", "就这样", "确认", "挺好",
)


# 出行/规划语义信号词：命中任一才认为句子可能含目的地（防"今天天气不错"被误判成规划请求）
_PLANNING_VERB_SIGNALS = (
    "去", "到", "玩", "游", "逛", "旅游", "旅行", "游玩", "逛逛", "看看", "攻略",
    "行程", "规划", "安排", "打算", "准备", "计划", "推荐", "好玩", "有意思", "出发",
)


def _is_clean_destination_name(name: str) -> bool:
    """目的地候选后置校验：剔除启发式提取残留的疑问/陈述碎片。

    "改/换"是改稿动词残留信号（如"不去了，改成上海"→"不了改成上海"），
    宁可返回 None 交给追问，也不让脏值当确认目的地入库。
    """
    for token in ("有", "怎么", "什么", "吗", "呢", "要", "是", "好玩", "推荐", "看看", "呀", "改", "换"):
        if token in name:
            return False
    return True


def extract_destination(message: str) -> str | None:
    """尽力提取目的地：剔除日期/数字/引导词后，剩余 1~6 字文本作为候选。

    提取不到返回 None（交给追问）。仅用于 fallback，LLM 正常时不依赖它。
    """
    # 前置门槛：句子必须含出行/规划信号，否则"今天天气不错"会被当成目的地
    if not any(signal in message for signal in _PLANNING_VERB_SIGNALS):
        return None
    cleaned = message
    # 第一步：剔除日期/人数/数字片段（含不带"号/日"的"8月15"）
    for token in re.findall(
        r"\d{4}[/-]\d{1,2}[/-]\d{1,2}|\d{1,2}月\d{1,2}(?:号|日)?|\d{1,2}(?:号|日)|"
        r"\d+\s*大\s*\d+\s*小|\d+\s*(?:个)?人|[一二两三四五六七八九十]+(?:个)?人|"
        r"\d{1,2}月|\d+\s*天",
        cleaned,
    ):
        cleaned = cleaned.replace(token, "")
    # 第二步：剔除引导词/语气词/问候语（按长度降序，长词先替换避免被短词破坏）
    for word in sorted(_DESTINATION_STOPWORDS, key=len, reverse=True):
        cleaned = cleaned.replace(word, "")
    # 第三步：剔除标点与空白，剩余是否是一个合理的短地名
    cleaned = re.sub(r"[\s，。,.！？!?、；;：:（）()「」『』【】]", "", cleaned)
    if 1 <= len(cleaned) <= 6 and _is_clean_destination_name(cleaned):
        return cleaned
    return None


# ============================================================
# fallback 意图判定辅助（模块级纯函数，可独立单测）
# ============================================================

def _compile_keywords(*keywords: str) -> re.Pattern[str]:
    """把关键词组编译成"任一命中"的正则。"""
    return re.compile("|".join(re.escape(kw) for kw in keywords))


# 强改稿信号：出现即判定为改稿（无需对象词）
_REVISION_STRONG_RE = _compile_keywords("换成", "改成", "改到", "调整", "修改", "优化", "更新", "去掉", "删除", "取消", "调整一下")
# 弱改稿动词："改/换" 需配合行程对象词才判定，避免"改天/换话题"误判
_REVISION_WEAK_RE = _compile_keywords("改", "换")
_REVISION_OBJECT_RE = _compile_keywords("天", "行程", "安排", "住宿", "酒店", "交通", "节奏", "顺序", "景点", "餐厅", "预算", "方案", "路线")

# reject 关键词；不含"取消"（"取消"是 revise 语义，避免冲突）
_REJECT_RE = _compile_keywords("不用了", "不要了", "不规划了", "不需要", "不做了", "算了")

# end_session 关键词：结束会话的告别语
_END_SESSION_RE = _compile_keywords("再见", "拜拜", "结束", "退出", "不聊了", "下次再聊", "就到这里")

# confirm 关键词（与澄清过程的"好的"区分，见 _is_confirm_message）
_CONFIRM_RE = _compile_keywords("没问题", "就这样", "可以", "好的", "行", "确认", "挺好", "不错", "ok")

# qa 关键词：问候/闲聊/感谢/能力询问（与规划信号互斥，见 _is_qa_message）
_QA_RE = _compile_keywords(
    "你好", "您好", "嗨", "哈喽", "hello", "hi", "在吗",
    "谢谢", "感谢", "辛苦了", "太棒了", "不错",
    "你是谁", "叫什么", "能做什么", "会什么", "怎么用", "介绍一下", "有什么功能", "什么功能",
)


def _is_revision_message(message: str) -> bool:
    """判断原话是否为改稿请求（"改天/改日"先排除；强关键词即判，弱动词需配对象词）。"""
    if "改天" in message or "改日" in message:
        return False
    if _REVISION_STRONG_RE.search(message):
        return True
    if _REVISION_WEAK_RE.search(message):
        return _REVISION_OBJECT_RE.search(message) is not None
    return False


def _infer_revision_scope(message: str) -> str:
    """推断改动范围：整体/全部/全局/整个 → global；第N天 → day_level；否则 block_level。"""
    if any(kw in message for kw in ("整体", "全部", "全局", "整个", "从头")):
        return "global"
    if re.search(r"第[一二两三四五六七八九十\d]+天", message):
        return "day_level"
    return "block_level"


def _is_reject_message(message: str) -> bool:
    """判断原话是否为明确拒绝"""
    return _REJECT_RE.search(message) is not None


def _is_end_session_message(message: str) -> bool:
    """判断原话是否为告别/结束语"""
    return _END_SESSION_RE.search(message) is not None


def _is_confirm_message(message: str) -> bool:
    """判断原话是否为确认（排除"不行/行不行"）"""
    lowered = message.lower()
    if "不行" in lowered or "行不行" in lowered:
        return False
    return _CONFIRM_RE.search(lowered) is not None


def _has_planning_signal(message: str, *, parsed: tuple | None = None) -> bool:
    """原话是否含规划信号（目的地/日期/人数任一）"""
    if parsed is not None:
        start_date, end_date, travelers, destination = parsed
        return bool(start_date or end_date or travelers or destination)
    start_date, end_date = extract_dates(message)
    if start_date or end_date:
        return True
    if extract_travelers(message):
        return True
    if extract_destination(message):
        return True
    return False


def _is_qa_message(message: str, *, parsed: tuple | None = None) -> bool:
    """判断原话是否为闲聊/问答（命中 qa 关键词且不含规划信号）。"""
    if not _QA_RE.search(message.lower()):
        return False
    return not _has_planning_signal(message, parsed=parsed)


def _is_casual_talk(message: str) -> bool:
    """原则式闲聊判定（不依赖列举寒暄词）：原话无任何规划信号、也不含出行/规划动词
    （去/玩/游/逛/规划/推荐…）时视为闲聊。这样例子里没写到的闲聊说法也能命中，
    不会被误判成"需补字段"。含目的地/日期/人数或出行动词时返回 False，避免误伤规划意图。"""
    raw = (message or "").strip()
    if not raw:
        return False
    if _has_planning_signal(raw):
        return False
    if any(signal in raw for signal in _PLANNING_VERB_SIGNALS):
        return False
    return True
