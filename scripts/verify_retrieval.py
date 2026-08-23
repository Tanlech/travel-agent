"""检索层真实回归（需 Qdrant 运行 + embedding/LLM key 就绪）

验证三项：
  1) 混合检索（dense+sparse → RRF 融合）能走通并召回相关块
  2) query 向量缓存：相同问题重复检索不重复调用 embedding
  3) sparse 兜底：dense 向量不可用时退化为纯稀疏检索仍能返回结果

运行（在项目根）：
  python scripts/verify_retrieval.py   # 用 agent4travel 环境解释器运行
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.knowledge.retriever import _EMBED_CACHE, retrieve  # noqa: E402
from app.knowledge.schemas import TextChunk  # noqa: E402
from app.knowledge.service import KnowledgeService  # noqa: E402
from app.knowledge.store import qdrant_store  # noqa: E402


def main() -> None:
    collection = "regression_retrieval"
    qdrant_store.delete(collection)  # 从干净状态开始

    # 用独立 service 实例 + 计数包装，隔离全局单例、便于统计 embedding 调用
    svc = KnowledgeService()

    class CountEmbed:
        def __init__(self, inner):
            self.inner = inner
            self.n = 0

        def is_enabled(self):
            return self.inner.is_enabled()

        def embed_text(self, t):
            self.n += 1
            return self.inner.embed_text(t)

        def embed_texts(self, t):
            return self.inner.embed_texts(t)

    dense = CountEmbed(svc.embedder)
    sparse = CountEmbed(svc.sparse_embedder)
    svc.embedder = dense
    svc.sparse_embedder = sparse

    chunks = [
        TextChunk(text="北京地铁覆盖城六区，票价按里程计价，可用亿通行扫码进站。", metadata={"city": "北京", "topic": "交通"}),
        TextChunk(text="故宫博物院位于北京中轴线，需提前预约，周一闭馆。", metadata={"city": "北京", "topic": "景点"}),
        TextChunk(text="成都大熊猫繁育研究基地早上开放，看熊猫建议赶早去。", metadata={"city": "成都", "topic": "景点"}),
        TextChunk(text="上海外滩夜景是浦江两岸标志性景观，适合晚饭后漫步。", metadata={"city": "上海", "topic": "景点"}),
    ]
    n = svc.ingest_chunks(collection, chunks)
    print(f"[setup] 入库 {n} 条")

    # ---- 1) 混合 RRF ----
    query = "去北京看故宫交通怎么安排"
    res = svc.retrieve(collection, query, top_k=3)
    texts = [i.text for i in res.items]
    print(f"[1] 混合检索返回 {len(res.items)} 条；命中相关块: {any('故宫' in t or '北京' in t for t in texts)}")
    for it in res.items:
        print(f"    score={it.distance:.4f} city={it.metadata.get('city')}，{it.text[:24]}…")

    # ---- 2) 缓存：相同 query 第二次不再调 embedding ----
    _EMBED_CACHE.clear()
    dense_before, sparse_before = dense.n, sparse.n
    svc.retrieve(collection, query, top_k=3)
    svc.retrieve(collection, query, top_k=3)
    print(f"[2] 两次同 query：dense 调 {dense.n - dense_before} 次，sparse 调 {sparse.n - sparse_before} 次（应为 1）")

    # ---- 3) sparse 兜底：dense 不可用时仍返回结果 ----
    real_sparse = svc.sparse_embedder.embed_text("故宫 预约")
    if real_sparse:
        class EmptyDense:
            def embed_text(self, t):
                return []

        res = retrieve(qdrant_store, EmptyDense(), collection, query, top_k=3, query_sparse=real_sparse)
        print(f"[3] dense 不可用 + sparse 兜底返回 {len(res.items)} 条（应 >0）")

    qdrant_store.delete(collection)
    print("[done] 已清理回归集合")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] {type(e).__name__}: {e}")
        print("提示：请确认 Qdrant 已运行（如 localhost:6333）且 embedding/LLM key 已配置。")
        sys.exit(1)