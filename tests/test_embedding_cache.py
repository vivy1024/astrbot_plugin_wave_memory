"""embedding 缓存：同句只调一次 provider，并发同句合并，失败不缓存且不挂住等待者，返回副本。"""

from __future__ import annotations

import asyncio

import numpy as np

from engine.embedding import EmbeddingService


class _Provider:
    def __init__(self, *, delay=0.01, fail=False, short=False):
        self.calls: list[list[str]] = []
        self.delay, self.fail, self.short = delay, fail, short

    def meta(self):
        return type("M", (), {"id": "p"})()

    async def get_embeddings(self, texts):
        self.calls.append(list(texts))
        await asyncio.sleep(self.delay)
        if self.fail:
            raise ValueError("boom")
        vectors = [[float(len(t)), 1.0] for t in texts]
        return vectors[:-1] if self.short else vectors


def _service(provider):
    context = type("C", (), {"get_all_embedding_providers": lambda self: [provider]})()
    return EmbeddingService(context, "p", dimension=2)


def test_repeat_and_concurrent_requests_share_one_call():
    provider = _Provider()
    service = _service(provider)

    async def scenario():
        a, b = await asyncio.gather(service.get_embedding("张羽来了"), service.get_embedding("张羽来了"))
        c = await service.get_embedding("张羽来了")
        return a, b, c

    a, b, c = asyncio.run(scenario())
    assert provider.calls == [["张羽来了"]]
    assert np.allclose(a, b) and np.allclose(a, c)
    a[0] = 999.0  # 调用方改自己的副本不影响缓存
    assert asyncio.run(service.get_embedding("张羽来了"))[0] == 4.0
    assert service.stats["joined"] == 1 and service.stats["provider_calls"] == 1


def test_batch_fetches_only_misses():
    provider = _Provider()
    service = _service(provider)
    asyncio.run(service.get_embedding("一"))
    out = asyncio.run(service.get_embeddings(["一", "二二", "二二"]))
    assert provider.calls == [["一"], ["二二"]]
    assert [v[0] for v in out] == [1.0, 2.0, 2.0]


def test_failure_is_not_cached_and_releases_waiters(monkeypatch):
    import logging

    import engine.embedding as embedding_module

    # 其他测试可能把 astrbot.api.logger 换成没有 error() 的替身
    monkeypatch.setattr(embedding_module, "logger", logging.getLogger("test"))
    provider = _Provider(fail=True)
    service = _service(provider)

    async def scenario():
        return await asyncio.wait_for(asyncio.gather(service.get_embedding("x"), service.get_embedding("x")), 2)

    assert asyncio.run(scenario()) == [None, None]
    provider.fail = False
    assert asyncio.run(service.get_embedding("x")) is not None
    assert len(provider.calls) == 2


def test_short_provider_result_does_not_hang():
    service = _service(_Provider(short=True))
    out = asyncio.run(asyncio.wait_for(service.get_embeddings(["a", "bb"]), 2))
    assert out[0] is not None and out[1] is None
