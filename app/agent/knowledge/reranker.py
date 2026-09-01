from __future__ import annotations

import logging
import threading

from app.infrastructure.settings import settings
from app.agent.knowledge.schemas import RetrievalItem

logger = logging.getLogger(__name__)


class FastEmbedReranker:
    """cross-encoder 重排器：复用项目已依赖的 fastembed 文本重排模型（ONNX，轻量、无需 torch）

    检索索取候选后按与 query 的相关性精排并截取 top-k，提升喂给 LLM 的知识/名池质量。
    懒加载：仅当配置了 rerank_model 且模型可加载时启用；加载/推理任一失败优雅降级，
    回退到原始召回顺序，与项目"永不崩"原则一致。结果为确定性，可被结果缓存复用。
    """

    def __init__(self, model_name: str | None) -> None:
        self._model_name = model_name
        self._model = None
        self._load_error: str | None = None
        self._load_lock = threading.Lock()  # 保护懒初始化，避免并发首调重复构造/下载

    def is_enabled(self) -> bool:
        """是否可用：必须配置模型且能成功加载"""
        return self._load() is not None

    def _load(self):
        """懒加载底层模型；双检加锁保证并发下只构造一次；失败只记录一次，不重复尝试报错"""
        if self._model is not None:
            return self._model
        if self._load_error is not None:
            return None
        with self._load_lock:
            if self._model is not None:  # 其它线程已抢先加载完成
                return self._model
            if self._load_error is not None:
                return None
            if not self._model_name:
                self._load_error = "rerank_model 未配置"
                return None
            try:
                # fastembed>=0.8: 重排入口为 fastembed.rerank.cross_encoder.TextCrossEncoder（ONNX，
                # 构造时下载模型为 <org>/<model> ONNX 格式）。显式用 CPU provider，规避 GPU 探测副作用。
                from fastembed.rerank.cross_encoder import TextCrossEncoder
                self._model = TextCrossEncoder(
                    model_name=self._model_name,
                    providers=["CPUExecutionProvider"],
                )
                logger.info("reranker 已启用: model=%s", self._model_name)
                return self._model
            except Exception as exc:  # noqa: BLE001
                self._load_error = str(exc)
                logger.warning("reranker 加载失败，已禁用重排: %s", exc)
                return None

    def rerank(self, query: str, items: list[RetrievalItem], top_k: int) -> list[RetrievalItem]:
        """按与 query 的相关性精排，返回前 top_k 条；任一异常回退原召回顺序

        fastembed 0.8 的 rerank(query, documents) 按输入顺序返回分数，直接与 items 对齐
        """
        model = self._load()
        if model is None or not items:
            return items[:top_k]
        texts = [item.text for item in items]
        try:
            scores = list(model.rerank(query, texts))
        except Exception:  # noqa: BLE001
            logger.warning("rerank 推理失败，返回原始召回结果")
            return items[:top_k]
        if len(scores) != len(items):
            return items[:top_k]
        scored = sorted(zip(items, scores), key=lambda pair: float(pair[1]), reverse=True)
        out: list[RetrievalItem] = []
        for item, score in scored[:top_k]:
            copy = item.model_copy(deep=True)
            copy.distance = float(score)  # 用重排分覆盖召回分，与"越大越相关"语义一致
            out.append(copy)
        return out


# 进程内单例；模型未配置/加载失败时 is_enabled() 为 False，不影响纯向量/混合检索路径
reranker = FastEmbedReranker(settings.rerank_model)