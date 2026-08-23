"""知识库集合（collection）命名空间：不同知识域隔离存储，互不干扰

- attraction: 城市景点知识库（attraction_tool 检索）
- qa:        攻略/文档问答（ask 通用问答）
- chat:      历史对话记忆（agent 跨会话上下文）

扩展新知识域：加一个常量 + 一个入库入口即可，引擎无需改动。
"""

ATTRACTION_COLLECTION = "attraction"
QA_COLLECTION = "qa_kb"
CHAT_COLLECTION = "chat_kb"
