"""Config loading tests for the new observability env vars."""

from __future__ import annotations

import json

import pytest

from mcp_audit_logger.config import load_config


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Strip any obs env vars between tests so they don't leak."""
    for var in (
        "AUDIT_PROMETHEUS_URL", "PROMETHEUS_URL",
        "AUDIT_TEMPO_URL", "TEMPO_URL",
        "AUDIT_JAEGER_URL", "JAEGER_URL",
        "AUDIT_ALERTMANAGER_URL", "ALERTMANAGER_URL",
        "AUDIT_METRIC_NAMES",
    ):
        monkeypatch.delenv(var, raising=False)


def test_obs_defaults_applied():
    cfg = load_config()
    assert cfg.prometheus_url == "http://localhost:9090"
    assert cfg.tempo_url == "http://localhost:3200"
    assert cfg.jaeger_url == "http://localhost:16686"
    assert cfg.alertmanager_url == "http://localhost:9093"


def test_bare_env_var_overrides_default(monkeypatch):
    monkeypatch.setenv("PROMETHEUS_URL", "http://prom.internal:9090")
    cfg = load_config()
    assert cfg.prometheus_url == "http://prom.internal:9090"


def test_audit_prefixed_takes_precedence_over_bare(monkeypatch):
    monkeypatch.setenv("PROMETHEUS_URL", "http://bare:9090")
    monkeypatch.setenv("AUDIT_PROMETHEUS_URL", "http://prefixed:9090")
    cfg = load_config()
    assert cfg.prometheus_url == "http://prefixed:9090"


def test_explicit_empty_disables_backend(monkeypatch):
    """Setting URL to "" tells us "user explicitly opted out" — no client built."""
    monkeypatch.setenv("TEMPO_URL", "")
    cfg = load_config()
    assert cfg.tempo_url == ""


def test_metric_names_env_override(monkeypatch):
    monkeypatch.setenv(
        "AUDIT_METRIC_NAMES",
        json.dumps({"request_count": "http_requests_total", "service_label": "service"}),
    )
    cfg = load_config()
    assert cfg.metric_names == {
        "request_count": "http_requests_total",
        "service_label": "service",
    }


def test_metric_names_invalid_json_ignored(monkeypatch):
    monkeypatch.setenv("AUDIT_METRIC_NAMES", "not valid json {")
    cfg = load_config()
    assert cfg.metric_names == {}


def test_config_file_overrides_default(tmp_path):
    cfg_file = tmp_path / "cfg.json"
    cfg_file.write_text(
        json.dumps(
            {
                "prometheus_url": "http://from-file:9090",
                "metric_names": {"service_label": "svc"},
            }
        )
    )
    cfg = load_config(cfg_file)
    assert cfg.prometheus_url == "http://from-file:9090"
    assert cfg.metric_names == {"service_label": "svc"}


def test_env_overrides_config_file(tmp_path, monkeypatch):
    cfg_file = tmp_path / "cfg.json"
    cfg_file.write_text(json.dumps({"prometheus_url": "http://from-file:9090"}))
    monkeypatch.setenv("PROMETHEUS_URL", "http://from-env:9090")
    cfg = load_config(cfg_file)
    assert cfg.prometheus_url == "http://from-env:9090"
