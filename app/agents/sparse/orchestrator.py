from __future__ import annotations

import json


def build_orchestrator_user_prompt(*, request: dict, session: dict, has_session_artifacts: bool) -> str:
    return "\n\n".join(
        [
            "[request]\n" + json.dumps(request, ensure_ascii=False, indent=2),
            "[session]\n" + json.dumps(session, ensure_ascii=False, indent=2),
            "[routing_context]\n" + json.dumps({"has_session_artifacts": has_session_artifacts}, ensure_ascii=False, indent=2),
        ]
    )
