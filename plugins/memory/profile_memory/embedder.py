"""Lazy local BGE embedding for profile memory."""

from __future__ import annotations

import importlib
import math
import threading
from pathlib import Path
from typing import Any, List


DEFAULT_MODEL_ID = "bge-small-zh-v1.5-f16"
DEFAULT_DIMENSION = 512
DEFAULT_QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："


def normalize_vector(vector: List[float]) -> List[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        return vector
    return [value / norm for value in vector]


class LocalBgeEmbedder:
    """Load one local GGUF embedding model on first use.

    The implementation intentionally depends only on ``llama-cpp-python``;
    OpenViking is neither imported nor required.
    """

    def __init__(
        self,
        *,
        model_path: str,
        model_id: str = DEFAULT_MODEL_ID,
        dimension: int = DEFAULT_DIMENSION,
        query_instruction: str = DEFAULT_QUERY_INSTRUCTION,
    ) -> None:
        self.model_path = Path(model_path).expanduser().resolve()
        self.model_id = str(model_id or DEFAULT_MODEL_ID)
        self.dimension = int(dimension)
        self.query_instruction = str(query_instruction or "")
        self._llama: Any = None
        self._lock = threading.RLock()

    def availability_error(self) -> str:
        if not self.model_path.is_file():
            return f"embedding model not found: {self.model_path}"
        try:
            module = importlib.import_module("llama_cpp")
        except ImportError:
            return "llama-cpp-python is not installed"
        if getattr(module, "Llama", None) is None:
            return "llama_cpp.Llama is unavailable"
        return ""

    def is_available(self) -> bool:
        return not self.availability_error()

    def _ensure_loaded(self) -> Any:
        with self._lock:
            if self._llama is not None:
                return self._llama
            error = self.availability_error()
            if error:
                raise RuntimeError(error)
            module = importlib.import_module("llama_cpp")
            self._llama = module.Llama(
                model_path=str(self.model_path),
                embedding=True,
                verbose=False,
            )
            return self._llama

    @staticmethod
    def _extract_vector(payload: Any) -> List[float]:
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, list) and data:
                first = data[0]
                if isinstance(first, dict) and isinstance(first.get("embedding"), list):
                    return [float(value) for value in first["embedding"]]
            if isinstance(payload.get("embedding"), list):
                return [float(value) for value in payload["embedding"]]
        raise RuntimeError("unexpected llama-cpp-python embedding response")

    def embed(self, text: str, *, is_query: bool = False) -> List[float]:
        clean = str(text or "").strip()
        if not clean:
            return []
        formatted = (
            f"{self.query_instruction}{clean}"
            if is_query and self.query_instruction
            else clean
        )
        with self._lock:
            model = self._ensure_loaded()
            vector = self._extract_vector(model.create_embedding(formatted))
        if len(vector) != self.dimension:
            raise RuntimeError(
                f"embedding dimension mismatch: expected {self.dimension}, got {len(vector)}"
            )
        return normalize_vector(vector)

    def close(self) -> None:
        with self._lock:
            model = self._llama
            self._llama = None
            close = getattr(model, "close", None)
            if callable(close):
                close()
