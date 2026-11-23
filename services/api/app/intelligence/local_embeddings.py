from __future__ import annotations

import asyncio
import logging
import os
from collections import OrderedDict
from typing import Sequence

from ..config import Settings
from ..errors import AppError, provider_unavailable

log = logging.getLogger(__name__)


class EmbeddingInputTooLong(AppError):
    def __init__(self, index: int, tokens: int | None, limit: int):
        self.index = index
        self.tokens = tokens
        self.limit = limit
        detail = f"{tokens} tokens; limit {limit}" if tokens is not None else "input too large to tokenize safely"
        super().__init__("ingest_failed", f"Embedding input exceeds the local model limit ({detail}).")


def _batch_size_for_device(device: str | None, settings: Settings) -> int:
    """Use CPU-sized batches without changing GPU throughput.

    CPU batches pad every passage to the longest one in the batch, so a large
    GPU batch can be slower on a CPU with variable-length chunks. Measured on
    the reference i5-9400, batch 1 was about 20–25% faster than batch 8.
    """
    if device and device.startswith("cpu"):
        return max(1, getattr(settings, "local_embedding_cpu_batch_size", 1))
    return max(1, getattr(settings, "local_embedding_batch_size", 16))


class LocalEmbeddingIntelligence:
    """Sentence-Transformers embeddings running on the local machine.

    The model is loaded lazily on the first request, so the API and ingestion
    worker start quickly and a machine without the optional dependencies can
    still use dev or remote embeddings. The default Qwen3-Embedding-0.6B model
    uses roughly 1.2 GB at 1024 dimensions. On a 6 GB card that is also running
    a chat model, set LOCAL_EMBEDDING_DEVICE=cpu; the 0.6B model is usable on a
    6-core CPU at batch 1 and leaves the GPU to the chat model. CPU batching is
    deliberately small because padding to the longest item in a batch often
    costs more than the vectorization saves.
    """

    name = "local"

    def __init__(self, settings: Settings):
        self.settings = settings
        self._model = None
        self._device: str | None = None
        self._lock = asyncio.Lock()
        self._query_cache: OrderedDict[tuple[str, str], list[float]] = OrderedDict()

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
        self._device = "cpu" if device.startswith("cpu") else device

        threads = max(0, getattr(self.settings, "local_embedding_threads", 0))
        if threads:
            torch.set_num_threads(min(threads, os.cpu_count() or threads))
            log.info("local embedding threads limited to %s", torch.get_num_threads())

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
            "local embedding model ready: %s on %s (%s dimensions, batch %s)",
            self.settings.local_embedding_model,
            device,
            dimension,
            _batch_size_for_device(self._device, self.settings),
        )
        return model

    def _check_lengths(self, model, inputs: list[str]) -> None:
        # Count the same untruncated model input, including its default prompt
        # and special tokens, before encode can silently truncate it.
        prompt_name = getattr(model, "default_prompt_name", None)
        prompt = (getattr(model, "prompts", {}) or {}).get(prompt_name, "") if prompt_name else ""
        limit = model.max_seq_length
        if not isinstance(limit, int) or limit <= 0:
            raise provider_unavailable("The local embedding model has no usable sequence length limit.")
        for index, text in enumerate(inputs):
            # Bound untruncated tokenizer memory for very large or hostile files.
            if len(prompt) + len(text) > 50000:
                raise EmbeddingInputTooLong(index, None, limit)
            if hasattr(model, "preprocess"):
                features = model.preprocess(
                    [text], prompt=prompt,
                    processing_kwargs={"text": {"truncation": False}, "common": {"padding": False}},
                )
                ids = features.get("input_ids")
                if ids is None or len(ids) != 1:
                    raise provider_unavailable("Cannot validate local embedding token length for this model.")
                tokens = len(ids[0])
            else:
                # Sentence Transformers 4/5 tokenize plain text with the
                # underlying HF tokenizer and strip surrounding whitespace.
                tokenizer = getattr(model, "tokenizer", None)
                if tokenizer is None:
                    raise provider_unavailable("Cannot validate local embedding token length for this model.")
                tokens = len(tokenizer((prompt + text).strip(), truncation=False, add_special_tokens=True)["input_ids"])
            if tokens > limit:
                raise EmbeddingInputTooLong(index, tokens, limit)

    def _encode(self, model, inputs: list[str]) -> list[list[float]]:
        self._check_lengths(model, inputs)
        vectors = model.encode(
            inputs,
            batch_size=_batch_size_for_device(self._device, self.settings),
            normalize_embeddings=True,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        return [vector.tolist() for vector in vectors]

    def _cache_get(self, key: tuple[str, str]) -> list[float] | None:
        if getattr(self.settings, "local_embedding_query_cache_size", 0) <= 0:
            return None
        vector = self._query_cache.get(key)
        if vector is not None:
            self._query_cache.move_to_end(key)
        return vector

    def _cache_put(self, key: tuple[str, str], vector: list[float]) -> None:
        limit = int(getattr(self.settings, "local_embedding_query_cache_size", 0))
        if limit <= 0:
            return
        self._query_cache[key] = list(vector)
        self._query_cache.move_to_end(key)
        while len(self._query_cache) > limit:
            self._query_cache.popitem(last=False)

    async def check_embedding_lengths(self, texts: Sequence[str]) -> None:
        async with self._lock:
            if self._model is None:
                self._model = await asyncio.to_thread(self._load_model)
            await asyncio.to_thread(self._check_lengths, self._model, list(texts))

    async def embed(self, texts: Sequence[str], task: str = "document") -> list[list[float]]:
        inputs = list(texts)
        if not inputs:
            return []
        cache_keys = None
        if task == "query" and getattr(self.settings, "local_embedding_query_cache_size", 0) > 0:
            identity = self.settings.embedding_identity
            cache_keys = [(identity, text) for text in inputs]
            cached = [self._cache_get(key) for key in cache_keys]
            if all(vector is not None for vector in cached):
                return [list(vector) for vector in cached]

        if task == "query" and self.settings.local_embedding_query_instruction:
            instruction = self.settings.local_embedding_query_instruction
            inputs = [f"Instruct: {instruction}\nQuery: {text}" for text in inputs]

        async with self._lock:
            if cache_keys is not None:
                cached = [self._cache_get(key) for key in cache_keys]
                if all(vector is not None for vector in cached):
                    return [list(vector) for vector in cached]
            if self._model is None:
                self._model = await asyncio.to_thread(self._load_model)
            try:
                vectors = await asyncio.to_thread(self._encode, self._model, inputs)
            except RuntimeError as exc:
                if "out of memory" in str(exc).lower():
                    raise provider_unavailable(
                        "The local embedding model ran out of GPU memory. Lower "
                        "LOCAL_EMBEDDING_BATCH_SIZE or set LOCAL_EMBEDDING_DEVICE=cpu."
                    ) from exc
                raise
            if cache_keys is not None:
                for key, vector in zip(cache_keys, vectors):
                    self._cache_put(key, vector)
            return vectors
