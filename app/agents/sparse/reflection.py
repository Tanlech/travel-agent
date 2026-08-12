from __future__ import annotations

import json


def build_reflection_user_prompt(*, request: dict, draft: dict, attraction_candidates: list[dict], lodging_candidates: list[dict], weather: list[dict]) -> str:
    return "\n\n".join([
        "用户请求:\n" + json.dumps(request, ensure_ascii=False, indent=2),
        "当前草案:\n" + json.dumps(draft, ensure_ascii=False, indent=2),
        "景点候选:\n" + json.dumps(attraction_candidates, ensure_ascii=False, indent=2),
        "住宿候选:\n" + json.dumps(lodging_candidates, ensure_ascii=False, indent=2),
        "天气:\n" + json.dumps(weather, ensure_ascii=False, indent=2),
    ])
