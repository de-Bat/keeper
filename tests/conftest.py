import pytest


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    """Tests use made-up hostnames served by httpx.MockTransport: resolve them to a public IP,
    except the ones a test marks as internal."""
    async def resolve(host):
        if "internal" in host or host == "localhost":
            return ["10.0.0.5"]
        return ["93.184.216.34"]
    monkeypatch.setattr("keeper.fetch.resolve_host", resolve)


@pytest.fixture(autouse=True)
def fresh_page_cache():
    from keeper import enrich
    enrich._PAGE_CACHE.clear()
    yield
    enrich._PAGE_CACHE.clear()
