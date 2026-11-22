from __future__ import annotations

import asyncio
import logging
from typing import Sequence

from ..config import Settings
from ..errors import provider_unavailable

log = logging.getLogger(__name__)


class LocalEmbeddingIntelligence:
    """Sentence-Transformers embeddings running on the local machine.

    The model is loaded lazily on the first request, so the API and ingestion
    worker start quickly and a machine without the optional dependencies can
    still use dev or remote embeddings. The default Qwen3-Embedding-0.6B model
    uses roughly 1.2 GB at 1024 dimensions. On a 6 GB card that is also running
    a chat model, set LOCAL_EMBEDDING_DEVICE=cpu; the 0.6B model embeds the demo
    corpus in a few seconds and leaves the GPU to the chat model.
    """

    name = "local"

    def __init__(self, settings: Settings):
        self.settings = settings
        self._model = None
        self._lock = asyncio.Lock()

    def _load_model(self):
        try:
            import torch
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise provider_unavailable(
                "Local embeddings are not installed. Run `make local-embeddings`, "
                "or set EMBEDDING_PROVIDER=dev."
            ) from exc

        device = self.settings.local_embedding_device
        if device.startswith("cuda") and not torch.cuda.is_available():
            log.warning("CUDA is unavailable; local embeddings will use the CPU")
            device = "cpu"

        dtype = torch.float16
        if device == "cpu" or self.settings.local_embedding_dtype == "float32":
            dtype = torch.float32

        try:
            model = SentenceTransformer(
                self.settings.local_embedding_model,
                device=device,
                model_kwargs={"torch_dtype": dtype},
            )
        except Exception as exc:  # noqa: BLE001 - model loading has vendor-specific failures
            raise provider_unavailable(
                f"Could not load local embedding model '{self.settings.local_embedding_model}': {exc}"
            ) from exc

        model.max_seq_length = self.settings.local_embedding_max_seq_length
        dimension = (
            model.get_embedding_dimension()
            if hasattr(model, "get_embedding_dimension")
            else model.get_sentence_embedding_dimension()
        )
        if dimension != self.settings.embedding_dim:
            raise provider_unavailable(
                f"Local embedding model returns {dimension} dimensions but the index expects "
                f"{self.settings.embedding_dim}. Choose a matching model or re-index the database."
            )
        log.info(
            "local embedding model ready: %s on %s (%s dimensions)",
            self.settings.local_embedding_model,
            device,
            dimension,
        )
        return model

    def _encode(self, model, inputs: list[str]) -> list[list[float]]:
        vectors = model.encode(
            inputs,
            batch_size=self.settings.local_embedding_batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        return [vector.tolist() for vector in vectors]

    async def embed(self, texts: Sequence[str], task: str = "document") -> list[list[float]]:
        inputs = list(texts)
        if not inputs:
            return []
        if task == "query" and self.settings.local_embedding_query_instruction:
            instruction = self.settings.local_embedding_query_instruction
            inputs = [f"Instruct: {instruction}\nQuery: {text}" for text in inputs]

        async with self._lock:
            if self._model is None:
                self._model = await asyncio.to_thread(self._load_model)
            try:
                return await asyncio.to_thread(self._encode, self._model, inputs)
            except RuntimeError as exc:
                if "out of memory" in str(exc).lower():
                    raise provider_unavailable(
                        "The local embedding model ran out of GPU memory. Lower "
                        "LOCAL_EMBEDDING_BATCH_SIZE or set LOCAL_EMBEDDING_DEVICE=cpu."
                    ) from exc
                raise
