"""知识库专门入库子包：qa（问答）/ chat（对话）；attraction 的具体业务见 knowledge.attraction_kb"""

from app.agent.knowledge.ingest.chat import ingest_chat
from app.agent.knowledge.ingest.common import ATTRACTION_COLLECTION, CHAT_COLLECTION, QA_COLLECTION
from app.agent.knowledge.ingest.qa import load_qa_documents

__all__ = [
    "ATTRACTION_COLLECTION",
    "QA_COLLECTION",
    "CHAT_COLLECTION",
    "load_qa_documents",
    "ingest_chat",
]