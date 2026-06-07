"""Tests for benchmark.py — hardware-aware token throughput detection."""

from __future__ import annotations

from omega.resource.benchmark import (
    BenchData,
    BenchResult,
    _fallback_data,
    get_tok_s,
    load_benchmark,
    reset_benchmark_cache,
)


class TestBenchResult:
    def test_default_fields(self):
        r = BenchResult(avg_tok_s=31.9, avg_tokens_per_call=256)
        assert r.avg_tok_s == 31.9
        assert r.avg_tokens_per_call == 256

    def test_zero_rates(self):
        r = BenchResult(avg_tok_s=0, avg_tokens_per_call=0)
        assert r.avg_tok_s == 0


class TestBenchData:
    def setup_method(self):
        reset_benchmark_cache()

    def test_empty_get_tok_s_fallback(self):
        data = BenchData()
        # Should return fallback for unknown model
        tok_s = data.get_tok_s("unknown-model")
        assert tok_s > 0
        assert isinstance(tok_s, float)

    def test_exact_match(self):
        data = BenchData(models={
            "deepseek-r1:8b": BenchResult(avg_tok_s=31.9, avg_tokens_per_call=256),
        })
        assert data.get_tok_s("deepseek-r1:8b") == 31.9

    def test_prefix_match(self):
        data = BenchData(models={
            "deepseek-r1:8b": BenchResult(avg_tok_s=31.9, avg_tokens_per_call=256),
        })
        assert data.get_tok_s("deepseek") == 31.9
        assert data.get_tok_s("deepseek-r1") == 31.9

    def test_estimate_tokens(self):
        data = BenchData(models={
            "test-model": BenchResult(avg_tok_s=10.0, avg_tokens_per_call=50),
        })
        assert data.estimate_tokens("test-model", 100.0) == 1000
        assert data.estimate_tokens("test-model", 3600.0) == 36000

    def test_estimate_12h(self):
        data = BenchData(models={
            "test-model": BenchResult(avg_tok_s=31.9, avg_tokens_per_call=256),
        })
        est = data.estimate_12h_tokens("test-model")
        assert est == int(31.9 * 43200)
        assert est > 1_000_000  # at least 1M tokens in 12h

    def test_to_dict_roundtrip(self):
        data = BenchData(
            models={"m1": BenchResult(avg_tok_s=31.9, avg_tokens_per_call=256)},
            measured_at="2026-01-01",
            hardware="test",
        )
        d = data.to_dict()
        restored = BenchData.from_dict(d)
        assert restored.get_tok_s("m1") == 31.9
        assert restored.measured_at == "2026-01-01"
        assert restored.hardware == "test"

    def test_save_and_load(self, tmp_path):
        import json
        from pathlib import Path
        # Temporarily override the benchmark path
        import omega.resource.benchmark as bench_mod
        orig = bench_mod.BENCHMARK_PATH
        bench_mod.BENCHMARK_PATH = tmp_path / "benchmark.json"
        try:
            data = BenchData(
                models={"m1": BenchResult(avg_tok_s=50.0, avg_tokens_per_call=100)},
                measured_at="test_time",
                hardware="test_hw",
            )
            data.save()
            assert (tmp_path / "benchmark.json").is_file()

            loaded = bench_mod.load_benchmark()
            assert loaded.get_tok_s("m1") == 50.0
            assert loaded.hardware == "test_hw"
        finally:
            bench_mod.BENCHMARK_PATH = orig
            reset_benchmark_cache()


class TestFallbackData:
    def test_all_models_have_fallback(self):
        data = _fallback_data()
        assert len(data.models) >= 4  # at least 4 known models
        for name, r in data.models.items():
            assert r.avg_tok_s > 0

    def test_fallback_is_reasonable(self):
        data = _fallback_data()
        # Fastest model should be gemma4
        tok_s_list = [(n, r.avg_tok_s) for n, r in data.models.items()]
        fastest = max(tok_s_list, key=lambda x: x[1])
        assert fastest[1] >= 30.0  # gemma4 should be >= 30 tok/s


class TestGetTokS:
    def setup_method(self):
        reset_benchmark_cache()

    def test_with_prefix_stripping(self):
        """get_tok_s should strip 'ollama/' and 'local/' prefixes."""
        # Since benchmark cache is empty, this will fallback
        tok_s = get_tok_s("ollama/deepseek-r1:8b")
        assert tok_s > 0
        assert isinstance(tok_s, float)

    def test_with_local_prefix(self):
        tok_s = get_tok_s("local/model")
        assert tok_s > 0

    def test_unknown_model_fallback(self):
        tok_s = get_tok_s("completely/unknown/123")
        assert tok_s > 0


class TestLoadBenchmark:
    def setup_method(self):
        reset_benchmark_cache()

    def test_load_no_cache_runs_fallback(self):
        """Without a cache file, load_benchmark should use fallback data."""
        data = load_benchmark(refresh=False)
        assert len(data.models) > 0
        for r in data.models.values():
            assert r.avg_tok_s > 0
