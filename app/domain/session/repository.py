from __future__ import annotations

from typing import Protocol

from app.domain.session.schema import SessionState


# repository 只定义读写接口，具体存储方式后续再决定
class SessionRepository(Protocol):
    def load(self, session_id: str) -> SessionState | None:
        ...

    def save(self, session_state: SessionState) -> None:
        ...
