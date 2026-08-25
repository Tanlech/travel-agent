"""Intent recognition layer package.

意图识别层：负责理解"用户这一轮想干什么"，输出意图类型 + 本轮新增字段 patch。
与 session 层通过 pipeline 串联、adapter 解耦：
  - schema.py   输入/输出强类型契约（8 类意图）
  - prompt.py   构造给 LLM 的意图识别 prompt（patch-only 约束）
  - adapter.py  session 视图 → intent 输入（层间解耦）
  - service.py  识别器：LLM 优先 + 规则 fallback 兜底
"""
