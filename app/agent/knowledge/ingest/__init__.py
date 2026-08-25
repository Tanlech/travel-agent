"""知识库专门入库子包：attraction（景点）/ qa（问答）/ chat（对话）"""

from app.agent.knowledge.ingest.attraction import build_attraction_entries, ingest_city
from app.agent.knowledge.ingest.chat import ingest_chat
from app.agent.knowledge.ingest.common import ATTRACTION_COLLECTION, CHAT_COLLECTION, QA_COLLECTION
from app.agent.knowledge.ingest.qa import load_qa_documents

__all__ = [
    "ATTRACTION_COLLECTION",
    "QA_COLLECTION",
    "CHAT_COLLECTION",
    "build_attraction_entries",
    "ingest_city",
    "load_qa_documents",
    "ingest_chat",
]