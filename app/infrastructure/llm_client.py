from __future__ import annotations

import json
import threading
import time
from typing import Any

from openai import AuthenticationError, OpenAI
from pydantic import ValidationError

from app.agents.schema.planning import ClusterPlanning, LodgingFitnessResult, PlanningSkeleton
from app.agents.schema.repair import RepairProposalSchema
from app.agents.schema.revise import BlockLevelReviseResultSchema, DayLevelReviseResultSchema, RevisionIntent
from app.domain.common.itinerary import ItineraryDraftSchema
from app.domain.intent.schema import IntentRecognitionOutput
from app.infrastructure.settings import settings


class LLMClient:
    def __init__(self) -> None:
        self.provider = settings.llm_provider.lower()
        self.enabled = not settings.enable_mock_llm and self.provider != "mock"
        self.model = settings.openai_model
        self.temperature = settings.llm_temperature
        self.timeout = settings.llm_timeout
        self.max_retries = 2
        self.client = None
        self._last_debug = threading.local()
        if self.enabled:
            self.client = OpenAI(
                api_key=settings.openai_api_key,
                base_url=settings.openai_base_url,
                timeout=self.timeout,
            )

    def is_enabled(self) -> bool:
        return self.enabled and self.client is not None

    @property
    def last_debug_info(self) -> dict[str, Any]:
        """当前线程最近一次调用的调试信息"""
        return getattr(self._last_debug, "value", {})

    @last_debug_info.setter
    def last_debug_info(self, value: dict[str, Any]) -> None:
        self._last_debug.value = value

    def generate_cluster_plan(self, *, system_prompt: str, user_prompt: str) -> ClusterPlanning | None:
        return self._generate_structured(
            schema=ClusterPlanning,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            retry_hints=[
                "Return JSON only.",
                "All selected_spots and optional_spots must come from the provided attraction candidates.",
                "Do not output final itinerary blocks.",
                "Keep the cluster rationale concise and structured.",
            ],
        )

    def generate_planning_skeleton(self, *, system_prompt: str, user_prompt: str) -> PlanningSkeleton | None:
        return self._generate_structured(
            schema=PlanningSkeleton,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            retry_hints=[
                "Return JSON only.",
                "Act as a travel planner, not a formatter.",
                "All selected spots must come from attraction candidates.",
            ],
        )

    def generate_lodging_fitness(self, *, system_prompt: str, user_prompt: str) -> LodgingFitnessResult | None:
        return self._generate_structured(
            schema=LodgingFitnessResult,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            retry_hints=[
                "Return JSON only.",
                "Assess whether the current lodging anchor fits the skeleton.",
                "Keep the reason concise and actionable.",
            ],
        )

    def generate_itinerary_draft(self, *, system_prompt: str, user_prompt: str) -> ItineraryDraftSchema | None:
        return self._generate_structured(
            schema=ItineraryDraftSchema,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            retry_hints=[
                "Return JSON only.",
                "Do not wrap the JSON in markdown fences.",
                "Do not add explanations before or after JSON.",
                "All spots must come from the provided candidates.",
                "Keep each detail concise and schema-safe.",
                "If unsure, prefer shorter valid strings over long prose.",
            ],
        )

    def generate_intent_recognition(self, *, system_prompt: str, user_prompt: str) -> IntentRecognitionOutput | None:
        return self._generate_structured(
            schema=IntentRecognitionOutput,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            retry_hints=[
                "Return JSON only.",
                "Do not generate itinerary content.",
                "Use route flags consistent with intent_type.",
                "If information is incomplete, prefer clarification or unknown over guessing.",
            ],
        )

    def generate_revision_intent(self, *, system_prompt: str, user_prompt: str) -> RevisionIntent | None:
        return self._generate_structured(
            schema=RevisionIntent,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            retry_hints=[
                "Return JSON only.",
                "Do not rewrite the itinerary.",
                "Focus on change scope, affected days, locked spots, and revision goal.",
            ],
        )

    def generate_block_level_revise(self, *, system_prompt: str, user_prompt: str) -> BlockLevelReviseResultSchema | None:
        return self._generate_structured(
            schema=BlockLevelReviseResultSchema,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            retry_hints=[
                "Return JSON only.",
                "Only modify affected days or affected blocks.",
                "Do not regenerate unchanged days.",
                "Preserve revision constraints and locked spots.",
            ],
        )

    def generate_day_level_revise(self, *, system_prompt: str, user_prompt: str) -> DayLevelReviseResultSchema | None:
        return self._generate_structured(
            schema=DayLevelReviseResultSchema,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            retry_hints=[
                "Return JSON only.",
                "Only regenerate the affected day plans.",
                "Keep unaffected days unchanged.",
                "Preserve revision constraints and locked spots.",
            ],
        )

    def generate_global_revise(self, *, system_prompt: str, user_prompt: str) -> ItineraryDraftSchema | None:
        return self._generate_structured(
            schema=ItineraryDraftSchema,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            retry_hints=[
                "Return JSON only.",
                "Reuse the current itinerary structure where possible.",
                "Only replan what is necessary to satisfy the revision goal.",
            ],
        )

    def generate_repair_proposal(self, *, system_prompt: str, user_prompt: str) -> RepairProposalSchema | None:
        return self._generate_structured(
            schema=RepairProposalSchema,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            retry_hints=[
                "Return JSON only.",
                "Keep changes local and coherent.",
                "Do not add explanations before or after JSON.",
            ],
        )

    # 自由文本对话：用于 qa 分支的正常 AI 回复
    # 与 _generate_structured 的区别：不强制 JSON，不重试解析，直接返回纯文本
    # LLM 不可用/报错/空回复时返回 None，由调用方降级
    def generate_chat_reply(self, *, system_prompt: str, user_prompt: str) -> str | None:
        if not self.is_enabled():
            self.last_debug_info = {"status": "disabled"}
            return None
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                temperature=self.temperature,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
        except Exception as exc:
            self.last_debug_info = {
                "status": "api_error",
                "reason": f"{type(exc).__name__}: {exc}",
            }
            return None
        content = response.choices[0].message.content if response.choices else None
        if not content or not content.strip():
            self.last_debug_info = {"status": "empty_response"}
            return None
        self.last_debug_info = {"status": "success"}
        return content.strip()

    # OpenAI function calling：LLM 自主决定调用哪些工具、传什么参数
    # 多轮循环：LLM 返回 tool_calls → 逐条执行（dispatch_tool_call）→ 结果回填 → 继续对话
    # 直到 LLM 不再请求工具（返回最终内容）或达到 max_rounds 上限
    def generate_with_tools(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = "auto",
        max_rounds: int = 6,
        execute_tool=None,
    ) -> str | None:
        if not self.is_enabled():
            self.last_debug_info = {"status": "disabled"}
            return None

        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        tool_trace: list[dict] = []
        try:
            for _round in range(max_rounds):
                response = self.client.chat.completions.create(
                    model=self.model,
                    temperature=self.temperature,
                    messages=messages,
                    tools=tools or [],
                    tool_choice=tool_choice if tools else None,
                )
                message = response.choices[0].message if response.choices else None
                if message is None:
                    self.last_debug_info = {"status": "empty_response", "tool_trace": tool_trace}
                    return None

                tool_calls = getattr(message, "tool_calls", None)
                if not tool_calls:
                    content = message.content or ""
                    if not content.strip():
                        self.last_debug_info = {"status": "empty_response", "tool_trace": tool_trace}
                        return None
                    self.last_debug_info = {"status": "success", "rounds": _round + 1, "tool_trace": tool_trace}
                    return content.strip()

                # 记录本轮 assistant 消息（含 tool_calls），随后逐条执行
                messages.append(
                    {
                        "role": "assistant",
                        "content": message.content or "",
                        "tool_calls": [
                            {
                                "id": call.id,
                                "type": "function",
                                "function": {"name": call.function.name, "arguments": call.function.arguments},
                            }
                            for call in tool_calls
                        ],
                    }
                )
                for call in tool_calls:
                    call_id = call.id
                    fn_name = call.function.name
                    arguments = self._parse_tool_arguments(call.function.arguments)
                    result = execute_tool(fn_name, arguments) if execute_tool else {"error": "no executor"}
                    tool_trace.append({"call_id": call_id, "name": fn_name, "arguments": arguments})
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": json.dumps(result, ensure_ascii=False),
                        }
                    )
        except Exception as exc:
            self.last_debug_info = {
                "status": "api_error",
                "reason": f"{type(exc).__name__}: {exc}",
                "tool_trace": tool_trace,
            }
            return None
        self.last_debug_info = {"status": "max_rounds_exceeded", "tool_trace": tool_trace}
        return None

    @staticmethod
    def _parse_tool_arguments(raw: str | None) -> dict:
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}

    def _generate_structured(
        self,
        *,
        schema,
        system_prompt: str,
        user_prompt: str,
        retry_hints: list[str],
    ) -> Any | None:
        if not self.is_enabled():
            self.last_debug_info = {"status": "disabled"}
            return None

        schema_instruction = self._build_schema_instruction(schema)
        last_error = "unknown"
        for attempt in range(self.max_retries + 1):
            messages = [
                {"role": "system", "content": system_prompt + "\n\n" + schema_instruction},
                {"role": "user", "content": user_prompt},
            ]
            if attempt > 0:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Your previous response could not be parsed. "
                            f"Reason: {last_error}. Please retry and strictly follow these rules: "
                            + " ".join(retry_hints)
                        ),
                    }
                )

            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    temperature=self.temperature,
                    response_format={"type": "json_object"},
                    messages=messages,
                )
            except Exception as exc:
                if isinstance(exc, AuthenticationError):
                    # 认证失败重试无意义，直接终止
                    self.last_debug_info = {"status": "auth_error", "reason": f"{type(exc).__name__}: {exc}"}
                    return None
                last_error = f"api_error:{type(exc).__name__}"
                self.last_debug_info = {
                    "status": "api_error",
                    "attempt": attempt,
                    "reason": last_error,
                }
                if attempt < self.max_retries:
                    # 简单线性退避后重试
                    time.sleep(0.5 * (attempt + 1))
                continue

            content = response.choices[0].message.content if response.choices else None
            if not content:
                last_error = "empty_response"
                self.last_debug_info = {
                    "status": "empty_response",
                    "attempt": attempt,
                    "reason": last_error,
                }
                continue

            parsed = self._parse_structured(schema, content)
            if parsed is not None:
                self.last_debug_info = {
                    "status": "success",
                    "attempt": attempt,
                    "reason": "parsed_json_object",
                }
                return parsed

            extracted = self._extract_json_object(content)
            if extracted is not None:
                parsed = self._parse_structured(schema, extracted)
                if parsed is not None:
                    self.last_debug_info = {
                        "status": "success",
                        "attempt": attempt,
                        "reason": "extracted_json_object",
                    }
                    return parsed

            last_error = "parse_failed"
            self.last_debug_info = {
                "status": "parse_failed",
                "attempt": attempt,
                "reason": last_error,
                "response_preview": content[:400],
            }

        return None

    def _build_schema_instruction(self, schema_model) -> str:
        schema = schema_model.model_json_schema()
        return (
            "You must output one JSON object matching this schema shape. "
            "Field names must be exact. Omit optional fields instead of using null. "
            "Schema: " + json.dumps(schema, ensure_ascii=False)
        )

    def _parse_structured(self, schema_model, content: str) -> Any | None:
        try:
            return schema_model.model_validate_json(content)
        except ValidationError as exc:
            self.last_debug_info = {
                **self.last_debug_info,
                "validation_error": str(exc)[:1200],
            }
            return None
        except Exception as exc:
            self.last_debug_info = {
                **self.last_debug_info,
                "parse_exception": f"{type(exc).__name__}: {exc}",
            }
            return None

    def _extract_json_object(self, content: str) -> str | None:
        stripped = content.strip()
        if stripped.startswith("```"):
            lines = stripped.splitlines()
            if len(lines) >= 3:
                stripped = "\n".join(lines[1:-1]).strip()

        start = stripped.find("{")
        end = stripped.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        candidate = stripped[start : end + 1]
        try:
            json.loads(candidate)
            return candidate
        except Exception:
            return None


_client: LLMClient | None = None


def get_llm_client() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client
