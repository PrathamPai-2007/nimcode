import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import nimcode.config_models as cm
from nimcode.config import Config


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _FakeModels:
    def __init__(self, data, fail=False, no_kwargs=False):
        self._data = data
        self._fail = fail
        self._no_kwargs = no_kwargs

    def list(self, **kwargs):
        if self._fail:
            raise RuntimeError("boom")
        if self._no_kwargs and kwargs:
            raise TypeError("no kwargs supported")
        return _Obj(data=self._data)


class _FakeClient:
    def __init__(self, *args, **kwargs):
        self.models = _FakeModels(*args, **kwargs)


def _isolate_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(cm, "_cache_path", lambda: tmp_path / "models_cache.json")
    monkeypatch.setattr(cm, "_ttl_seconds", lambda: 3600)


def test_pretty_name_and_filter():
    assert cm.pretty_name("nvidia/nemotron-3-nano-30b-a3b") == "Nemotron 3 Nano 30B A3B"
    assert cm.pretty_name("deepseek-ai/deepseek-v4-flash") == "DeepSeek V4 Flash"
    assert cm.is_chat_model("nvidia/nemotron-3-nano-30b-a3b")
    for bad in (
        "nvidia/nemotron-3-embed-1b",
        "x/ocr-v1",
        "x/model-vl-8b",
        "x/guard-7b",
        "x/rerank-1b",
    ):
        assert not cm.is_chat_model(bad), bad


def test_fetch_models_normalizes_and_filters():
    client = _FakeClient(
        [
            _Obj(id="nvidia/nemotron-3-nano-30b-a3b"),
            {"id": "z-ai/glm-5.2", "max_model_len": 131072},
            _Obj(id="nvidia/nemotron-3-embed-1b"),
            _Obj(id=""),
        ]
    )
    catalog = cm.fetch_models(client)
    ids = [m["id"] for m in catalog]
    assert "nvidia/nemotron-3-nano-30b-a3b" in ids
    assert "z-ai/glm-5.2" in ids
    assert "nvidia/nemotron-3-embed-1b" not in ids
    by_id = {m["id"]: m for m in catalog}
    assert by_id["nvidia/nemotron-3-nano-30b-a3b"]["context_tokens"] == 1_000_000
    assert by_id["z-ai/glm-5.2"]["context_tokens"] == 131072
    assert by_id["z-ai/glm-5.2"]["name"]


def test_fetch_models_failure_returns_empty():
    assert cm.fetch_models(_FakeClient([], fail=True)) == []
    # Client whose list() rejects kwargs falls back to plain list().
    catalog = cm.fetch_models(_FakeClient([_Obj(id="a/b")], no_kwargs=True))
    assert [m["id"] for m in catalog] == ["a/b"]


def test_cache_round_trip_and_expiry(monkeypatch, tmp_path):
    _isolate_cache(monkeypatch, tmp_path)
    models = [{"id": "a/b", "name": "A B", "context_tokens": None}]
    cm.save_models_cache(models)
    loaded, age = cm.load_models_cache()
    assert loaded == models
    assert age < 60
    # Expired cache is ignored by the catalog (falls back to static).
    monkeypatch.setattr(cm, "_ttl_seconds", lambda: 0)
    catalog = cm.get_model_catalog()
    assert all(m["id"] != "a/b" for m in catalog)
    assert len(catalog) == 10


def test_get_model_catalog_prefers_fresh_cache(monkeypatch, tmp_path):
    _isolate_cache(monkeypatch, tmp_path)
    models = [{"id": "custom/model", "name": "Custom", "context_tokens": 5000}]
    cm.save_models_cache(models)
    assert cm.get_model_catalog() == models


def test_resolve_and_apply_context():
    assert cm.resolve_context("nvidia/nemotron-3-ultra-550b-a55b") == 1_000_000
    assert cm.resolve_context("mistralai/mixtral-8x7b-instruct-v0.1") == 32768
    assert cm.resolve_context("z-ai/glm-5.2") is None

    cfg = Config()
    assert cfg.context_budget_tokens == cm.DEFAULT_BUDGET
    changed, _ = cm.apply_model_context(cfg, "nvidia/nemotron-3-nano-30b-a3b")
    assert changed and cfg.context_budget_tokens == 1_000_000

    # Customized budget survives non-forced apply...
    cfg.context_budget_tokens = 50000
    changed, _ = cm.apply_model_context(cfg, "nvidia/nemotron-3-super-120b-a12b")
    assert not changed and cfg.context_budget_tokens == 50000
    # ...but explicit switches (force) adjust.
    changed, _ = cm.apply_model_context(
        cfg, "nvidia/nemotron-3-super-120b-a12b", force=True
    )
    assert changed and cfg.context_budget_tokens == 1_000_000

    # Unknown window never touches the budget.
    cfg.context_budget_tokens = 77777
    changed, _ = cm.apply_model_context(cfg, "z-ai/glm-5.2", force=True)
    assert not changed and cfg.context_budget_tokens == 77777


def test_reconcile_saved_model_falls_back(monkeypatch):
    import nimcode.config as config_mod

    monkeypatch.setattr(config_mod, "save_last_used_model", lambda *a, **k: None)
    cfg = Config()
    catalog = [
        {"id": "nvidia/nemotron-3-nano-30b-a3b", "name": "Nano", "context_tokens": 1},
        {"id": "other/model", "name": "Other", "context_tokens": None},
    ]
    cfg.fast_model = "retired/old-model"
    cfg.plan_model = "other/model"
    notice = cm.reconcile_saved_model(cfg, catalog)
    assert notice and "retired/old-model" in notice
    assert cfg.fast_model == "nvidia/nemotron-3-nano-30b-a3b"
    assert cfg.plan_model == "other/model"

    notice = cm.reconcile_saved_model(cfg, catalog)
    assert notice is None


def test_reconcile_empty_catalog_keeps_values():
    cfg = Config()
    cfg.fast_model = "whatever/id"
    assert cm.reconcile_saved_model(cfg, []) is None
    assert cfg.fast_model == "whatever/id"


def test_static_catalog_shape():
    catalog = cm.static_catalog()
    assert len(catalog) == 10
    for m in catalog:
        assert m["id"] and m["name"]
