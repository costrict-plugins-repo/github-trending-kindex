"""Tests for ingest limit semantics (issue #16).

The code adapter is fully local (no LLM), so its default is unlimited;
--limit 0 means unlimited everywhere; truncation is loudly reported.
"""
from __future__ import annotations

import sys

import pytest

from kindex.adapters import code
from kindex.adapters.base import AdapterMeta, IngestResult
from kindex.adapters.pipeline import IngestConfig, run_adapter
from kindex.config import Config
from kindex.store import Store


def _make_repo(tmp_path, count):
    repo = tmp_path / "repo"
    repo.mkdir()
    for i in range(count):
        (repo / f"mod{i}.py").write_text(f"def f{i}():\n    return {i}\n")
    return repo


@pytest.fixture
def local_only(monkeypatch):
    monkeypatch.setattr(code, "_run_ctags", lambda *_args: [])
    monkeypatch.setattr(code, "_check_cscope", lambda: False)
    monkeypatch.setattr(code, "_check_treesitter", lambda _lang: None)


@pytest.fixture
def store(tmp_path):
    cfg = Config(data_dir=str(tmp_path / "kindex"))
    s = Store(cfg)
    yield s
    s.close()


def test_ingest_code_default_is_unlimited(tmp_path, local_only, store):
    repo = _make_repo(tmp_path, 5)
    result = code.ingest_code(store, repo)
    assert result.created == 5
    assert result.warnings == []


def test_ingest_code_limit_zero_means_unlimited(tmp_path, local_only, store):
    repo = _make_repo(tmp_path, 5)
    result = code.ingest_code(store, repo, limit=0)
    assert result.created == 5
    assert result.warnings == []


def test_ingest_code_negative_limit_means_unlimited(tmp_path, local_only, store):
    repo = _make_repo(tmp_path, 5)
    result = code.ingest_code(store, repo, limit=-1)
    assert result.created == 5
    assert result.warnings == []


def test_ingest_code_limit_truncates_with_warning(tmp_path, local_only, store):
    repo = _make_repo(tmp_path, 5)
    result = code.ingest_code(store, repo, limit=2)
    assert result.created == 2
    assert len(result.warnings) == 1
    assert "limit 2" in result.warnings[0]
    assert "of 5 files" in result.warnings[0]
    assert "WARNING: limit 2" in str(result)


def test_ingest_code_exact_limit_no_false_warning(tmp_path, local_only, store):
    repo = _make_repo(tmp_path, 5)
    result = code.ingest_code(store, repo, limit=5)
    assert result.created == 5
    assert result.warnings == []


def test_reingest_unchanged_under_limit_no_false_warning(tmp_path, local_only, store):
    repo = _make_repo(tmp_path, 5)
    code.ingest_code(store, repo)
    result = code.ingest_code(store, repo, limit=1)
    assert result.created == 0
    assert result.skipped == 5
    assert result.warnings == []


class _LimitRecordingAdapter:
    meta = AdapterMeta(name="limit-recorder", description="Records limit kwarg")

    def __init__(self):
        self.seen = "unset"

    def is_available(self) -> bool:
        return True

    def ingest(self, store, *, limit=123, since=None, verbose=False, **kwargs):
        self.seen = limit
        return IngestResult()


def test_pipeline_unset_limit_uses_adapter_default(store):
    adapter = _LimitRecordingAdapter()
    run_adapter(adapter, store, IngestConfig())
    assert adapter.seen == 123


def test_pipeline_limit_zero_normalized_to_unlimited_int(store):
    adapter = _LimitRecordingAdapter()
    run_adapter(adapter, store, IngestConfig(limit=0))
    assert adapter.seen == sys.maxsize


def test_pipeline_positive_limit_passes_through(store):
    adapter = _LimitRecordingAdapter()
    run_adapter(adapter, store, IngestConfig(limit=7))
    assert adapter.seen == 7
