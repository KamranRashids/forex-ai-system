"""Phase 14B monitoring-config validation tests.

These validate the *actual* Phase 14B artifacts under git (the Prometheus alert
rules, ``rule_files`` wiring + compose mount, the provisioned Grafana dashboard,
and the Alertmanager-off posture) against the metrics the repository actually
publishes (``app.core.metrics`` + the per-process ``forex_worker_*`` exporters).

Nothing here weakens existing tests or coverage; it is pure static validation.
"""

from __future__ import annotations

import json
import re
import warnings
from pathlib import Path

import pytest
import yaml
from prometheus_client import Counter, Gauge, Histogram

# backend/tests/unit/test_monitoring_config.py -> parents[3] == repo root.
REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_APP = REPO_ROOT / "backend" / "app"
PROM_DIR = REPO_ROOT / "infra" / "prometheus"
PROMETHEUS_YML = PROM_DIR / "prometheus.yml"
RULES_YML = PROM_DIR / "prometheus-rules.yml"
COMPOSE_YML = REPO_ROOT / "docker-compose.yml"
GRAFANA_DIR = REPO_ROOT / "infra" / "grafana"
DASHBOARD_JSON = GRAFANA_DIR / "dashboards" / "paper-trading.json"
DS_FILE = GRAFANA_DIR / "provisioning" / "datasources" / "prometheus.yml"

#: Fixed per-worker exporter metric names (worker_metrics.py constructs them at
#: runtime with literal names, so they are not visible in app/core/metrics.py).
WORKER_EXPORTER_METRICS: dict[str, tuple[str, ...]] = {
    "forex_worker_up": ("worker",),
    "forex_worker_heartbeat_timestamp_seconds": ("worker",),
    "forex_worker_cycles_total": ("worker",),
    "forex_worker_errors_total": ("worker",),
}

#: PromQL built-ins / keywords legitimately present in the Phase 14B config.
PROMQL_KEYWORDS = {
    "rate",
    "sum",
    "increase",
    "histogram_quantile",
    "max",
    "min",
    "by",
    "without",
    "and",
    "or",
    "unless",
    "bool",
    "on",
    "ignoring",
    "offset",
}

#: Label names that can appear inside ``by (...)`` / selectors without being a
#: metric (the introspected metric label set is unioned in dynamically).
EXTRA_LABEL_WORDS = {"le", "job", "instance", "service"}

#: Rule IDs expected to be ACTIVE (W10 is deferred — see
#: DISABLED_ALERT_NAMES — and W12/W13 are retained but DISABLED by default).
ACTIVE_ALERT_NAMES = {
    "WorkerProcessDown",
    "ApiDown",
    "WorkerRoleDown",
    "OrchestratorStalled",
    "PaperReconcileFailed",
    "PaperLifecycleDegraded",
    "PendingExpiryBurst",
    "StalenessBreach",
    "Api5xxRate",
    "GatePressureHigh",
}

#: Rule IDs retained in the rules file but NOT active: W10/DecisionLatencyHigh
#: is deferred because the current orch_decision_latency_seconds histogram's
#: highest explicit bucket is 2.0s, so its `> 5` condition is mathematically
#: unreachable; W12/W13 are optional and disabled by default (opt-in only).
DISABLED_ALERT_NAMES = {
    "DecisionLatencyHigh",
    "VetoChatter",
    "CatchupThrottled",
}

#: The known declared-but-unused metric (audit-verified: zero increment sites).
DEAD_METRIC = "risk_paper_total"

#: Metrics created *and* incremented entirely inside app/core/metrics.py (the
#: FastAPI middleware). Their only usage site is that file, so the liveness scan
#: (which excludes metrics.py) legitimately reports them; they are still live.
MIDDLEWARE_LOCAL_METRICS = {"http_requests_total", "http_request_latency_seconds"}

FOR_REGEX = re.compile(r"^(?:0m|[0-9]+[smhd])$")


def _app_metrics() -> dict[str, tuple[str, ...]]:
    """Introspect ``app.core.metrics`` -> {exported metric name: label names}.

    prometheus_client >= 0.21 stores ``_name`` without the ``_total`` counter
    suffix, so the exported name is reconstructed (``_total`` if missing) and
    cross-checked against the names Prometheus would actually expose.
    """
    import app.core.metrics as metrics
    from prometheus_client import generate_latest

    found: dict[str, tuple[str, ...]] = {}
    for attr_name in dir(metrics):
        obj = getattr(metrics, attr_name)
        if not isinstance(obj, (Counter, Gauge, Histogram)):
            continue
        base = obj._name
        if isinstance(obj, Counter):
            name = base if base.endswith("_total") else f"{base}_total"
        else:
            name = base
        found[name] = tuple(obj._labelnames or ())

    emitted = {
        parts[2]
        for parts in (
            line.split()
            for line in generate_latest().decode().splitlines()
            if line.startswith("# TYPE ")
        )
        if len(parts) == 4
        and parts[3] in ("counter", "gauge", "histogram")
        and not parts[2].endswith("_created")
    }
    assert set(found) <= emitted, (
        f"introspected metrics missing from live export: {set(found) - emitted}"
    )
    return found


@pytest.fixture(scope="module")
def metric_registry() -> dict[str, tuple[str, ...]]:
    app_metrics = _app_metrics()
    app_metrics.update(WORKER_EXPORTER_METRICS)
    return app_metrics


@pytest.fixture(scope="module")
def prometheus_config() -> dict:
    return yaml.safe_load(PROMETHEUS_YML.read_text())


@pytest.fixture(scope="module")
def rules_config() -> dict:
    return yaml.safe_load(RULES_YML.read_text())


@pytest.fixture(scope="module")
def rules_text() -> str:
    return RULES_YML.read_text()


@pytest.fixture(scope="module")
def dashboard_data() -> dict:
    return json.loads(DASHBOARD_JSON.read_text())


@pytest.fixture(scope="module")
def compose_data() -> dict:
    return yaml.safe_load(COMPOSE_YML.read_text())


@pytest.fixture(scope="module")
def scrape_jobs(
    prometheus_config: dict,
) -> set[str]:
    return {cfg["job_name"] for cfg in prometheus_config["scrape_configs"]}


# --- 1. Scrape jobs + config load -------------------------------------------


def test_prometheus_config_loads_with_phase14b_keys(prometheus_config: dict) -> None:
    assert "scrape_configs" in prometheus_config
    assert "rule_files" in prometheus_config


def test_scrape_jobs_cover_api_and_workers(scrape_jobs: set[str]) -> None:
    assert {"forex-ai-api", "forex-ai-workers"} <= scrape_jobs


def test_rule_files_reference_mounted_rules_file(
    prometheus_config: dict, compose_data: dict
) -> None:
    rule_files = prometheus_config["rule_files"]
    assert rule_files == ["/etc/prometheus/prometheus-rules.yml"]
    # The referenced container path must correspond to a real file in the repo.
    mounted = [r.split("/")[-1] for r in rule_files]
    assert all((PROM_DIR / name).is_file() for name in mounted)

    prometheus_volumes = compose_data["services"]["prometheus"]["volumes"]
    rules_mounts = [v for v in prometheus_volumes if "prometheus-rules.yml" in v]
    assert rules_mounts, "prometheus-rules.yml is not mounted in docker-compose.yml"
    assert "prometheus-rules.yml:ro" in rules_mounts[0]


# --- 2. Metric token validity -------------------------------------------------


def _strip_selectors(expr: str) -> str:
    return re.sub(r"\{[^}]*\}", "", expr)


def _expr_tokens(expr: str) -> set[str]:
    tokens = set(re.split(r"[^0-9A-Za-z_:]+", _strip_selectors(expr)))
    return {t for t in tokens if t and not re.match(r"^[0-9]", t)}


def _histogram_suffix_name(token: str, registry: dict[str, tuple[str, ...]]) -> str | None:
    for suffix in ("_bucket", "_sum", "_count"):
        if token.endswith(suffix):
            base = token[: -len(suffix)]
            if base in registry:
                return base
    return None


def test_all_rule_and_panel_metric_tokens_exist(
    rules_config: dict, dashboard_data: dict, metric_registry: dict[str, tuple[str, ...]]
) -> None:
    label_words = EXTRA_LABEL_WORDS | set().union(*metric_registry.values())
    allow = set(metric_registry) | {"up"} | PROMQL_KEYWORDS | label_words

    unknown: set[str] = set()
    for group in rules_config["groups"]:
        for rule in group["rules"]:
            for token in _expr_tokens(rule["expr"]):
                if token in allow:
                    continue
                if _histogram_suffix_name(token, metric_registry):
                    continue
                unknown.add(token)

    for panel in dashboard_data["panels"]:
        if panel.get("type") == "row":
            continue
        for target in panel.get("targets", []):
            for token in _expr_tokens(target["expr"]):
                if token in allow:
                    continue
                if _histogram_suffix_name(token, metric_registry):
                    continue
                unknown.add(token)

    assert not unknown, f"Unknown metric tokens referenced by Phase 14B config: {sorted(unknown)}"


# --- 3. Usage-liveness guard ---------------------------------------------------
# Best-effort (plan §10.2): scan the app package for references to each metric's
# variable name outside app/core/metrics.py; warn on declared-but-unused names.
# The rule/panel allow-set above must not reference any metric flagged unused.


def _metric_variable_names() -> dict[str, str]:
    import app.core.metrics as metrics

    found: dict[str, str] = {}
    for attr_name in dir(metrics):
        obj = getattr(metrics, attr_name)
        if not isinstance(obj, (Counter, Gauge, Histogram)):
            continue
        base = obj._name
        name = base if not isinstance(obj, Counter) or base.endswith("_total") else f"{base}_total"
        found[name] = attr_name
    return found


def _config_referenced_metrics(
    rules_config: dict, dashboard_data: dict, registry: dict[str, tuple[str, ...]]
) -> set[str]:
    refs: set[str] = set()
    for group in rules_config["groups"]:
        for rule in group["rules"]:
            for token in _expr_tokens(rule["expr"]):
                base = _histogram_suffix_name(token, registry)
                if base is not None:
                    refs.add(base)
                elif token in registry:
                    refs.add(token)
    for panel in dashboard_data["panels"]:
        if panel.get("type") == "row":
            continue
        for target in panel.get("targets", []):
            for token in _expr_tokens(target["expr"]):
                base = _histogram_suffix_name(token, registry)
                if base is not None:
                    refs.add(base)
                elif token in registry:
                    refs.add(token)
    return refs


def test_usage_liveness_flags_dead_metric(
    rules_config: dict,
    dashboard_data: dict,
    metric_registry: dict[str, tuple[str, ...]],
) -> None:
    var_names = _metric_variable_names()
    app_files = sorted(p for p in BACKEND_APP.rglob("*.py") if p.name != "metrics.py")

    unused = [
        name for name, var in var_names.items() if not any(var in f.read_text() for f in app_files)
    ]
    for name in unused:
        warnings.warn(
            f"declared-but-unused metric '{name}' (best-effort liveness scan)", stacklevel=2
        )

    # The audit-verified dead metric must be flagged by this guard.
    assert DEAD_METRIC in unused, f"'{DEAD_METRIC}' should be flagged declared-but-unused"

    # Metrics actually referenced by the Phase 14B rules/dashboard must be
    # genuinely live (used somewhere in app/ outside metrics.py). The
    # middleware-locals are the only legitimate exception — they are incremented
    # inside metrics.py itself. Declared-but-unreferenced metrics (e.g.
    # risk_paper_total) are not subject to this check; they are flagged above.
    referenced = _config_referenced_metrics(
        rules_config,
        dashboard_data,
        metric_registry,  # type: ignore[arg-type]
    )
    for name in referenced:
        if name in MIDDLEWARE_LOCAL_METRICS or name in WORKER_EXPORTER_METRICS:
            continue
        assert name not in unused, (
            f"Phase 14B references metric '{name}' but it is never used in app/"
        )


# --- 4. Alert rule structure ----------------------------------------------------


def test_active_alert_rules_have_required_shape(
    rules_config: dict,
) -> None:
    collected: set[str] = set()
    for group in rules_config["groups"]:
        for rule in group["rules"]:
            assert "alert" in rule
            assert "expr" in rule
            assert "for" in rule
            collected.add(rule["alert"])
            assert rule.get("labels", {}).get("severity") in {"warning", "critical"}
            ann = rule.get("annotations", {})
            assert "summary" in ann
            assert "description" in ann
            assert "investigation" in ann
            assert "runbook" in ann
    assert collected == ACTIVE_ALERT_NAMES


def test_for_durations_are_valid(rules_config: dict) -> None:
    for group in rules_config["groups"]:
        for rule in group["rules"]:
            assert FOR_REGEX.match(rule["for"]), (
                f"invalid 'for' duration {rule['for']!r} on {rule['alert']}"
            )


def test_disabled_optional_rules_are_not_active(rules_config: dict, rules_text: str) -> None:
    active = {
        rule["alert"]
        for group in rules_config["groups"]
        for rule in group["rules"]
        if "alert" in rule
    }
    # W10 is deferred (histogram bucket ceiling — see below) and W12/W13 are
    # optional, disabled-by-default (opt-in only). None may be active.
    for name in DISABLED_ALERT_NAMES:
        assert name not in active, f"disabled alert {name} must stay disabled"
        assert name in rules_text, (
            f"disabled alert {name} definition should be retained (commented)"
        )


def test_w10_is_disabled_and_retains_intended_threshold(rules_text: str) -> None:
    """W10/DecisionLatencyHigh must not be an active Prometheus rule, and its
    retained (commented) definition must still carry the intended `> 5` seconds
    expression. It stays deferred until a separately approved future histogram
    bucket expansion lets the p95 exceed the current 2.0s ceiling."""
    assert "DecisionLatencyHigh" in DISABLED_ALERT_NAMES
    active = {
        rule["alert"]
        for group in yaml.safe_load(rules_text).get("groups", [])
        for rule in group.get("rules", [])
        if "alert" in rule
    }
    assert "DecisionLatencyHigh" not in active
    assert "> 5" in rules_text, "W10 retained definition must keep the > 5s threshold"
    assert (
        "histogram_quantile(0.95, sum(rate(orch_decision_latency_seconds_bucket[5m])) by (le))"
        in rules_text
    ), "W10 retained definition must keep the intended p95 expression"


# --- 5. GitHub-flavored legend/label consistency -------------------------------


def test_dashboard_legend_labels_match_metric_labels(
    dashboard_data: dict, metric_registry: dict[str, tuple[str, ...]]
) -> None:
    legend_re = re.compile(r"\{\{([a-zA-Z0-9_]+)\}\}")
    for panel in dashboard_data["panels"]:
        if panel.get("type") == "row":
            continue
        for target in panel.get("targets", []):
            legend_labels = set(legend_re.findall(target.get("legendFormat", "")))
            if not legend_labels:
                continue
            expr = target["expr"]
            tokens = _expr_tokens(expr)
            metric_labels: set[str] = set()
            for token in tokens:
                base = _histogram_suffix_name(token, metric_registry)
                name = base if base else (token if token in metric_registry else None)
                if name is not None:
                    metric_labels |= set(metric_registry[name])
            for label in legend_labels:
                assert label in metric_labels, (
                    f"panel '{panel.get('title')}' legend uses '{{{{{label}}}}}' "
                    f"but no metric in its expr exposes that label"
                )


# --- 6. Built-in `up` rules use verified job names -----------------------------


def test_up_alerts_use_verified_job_names(rules_config: dict, scrape_jobs: set[str]) -> None:
    up_refs: set[str] = set()
    for group in rules_config["groups"]:
        for rule in group["rules"]:
            for m in re.finditer(r"up\{job=\"([^\"]+)\"\}", rule["expr"]):
                up_refs.add(m.group(1))
    assert up_refs, "expected at least one up{job=...} rule"
    assert up_refs <= scrape_jobs, (
        f"up{{job=...}} references jobs not present in scrape config: {up_refs - scrape_jobs}"
    )


def test_w1_w2_use_expected_up_targets(rules_config: dict) -> None:
    exprs = {
        rule["alert"]: rule["expr"] for group in rules_config["groups"] for rule in group["rules"]
    }
    assert exprs["WorkerProcessDown"] == 'up{job="forex-ai-workers"} == 0'
    assert exprs["ApiDown"] == 'up{job="forex-ai-api"} == 0'
    # The old unreliable mechanism (forex_worker_up == 0) must not be back.
    assert "forex_worker_up == 0" not in exprs["WorkerProcessDown"]


# --- 7. Dashboard sanity --------------------------------------------------------


def test_dashboard_is_valid_and_uses_prometheus_datasource(
    dashboard_data: dict,
) -> None:
    assert dashboard_data["title"] == "Paper Trading v0"
    assert dashboard_data["uid"] == "forex-ai-paper-trading"
    for panel in dashboard_data["panels"]:
        if panel.get("type") == "row":
            continue
        ds = panel["datasource"]
        assert ds == {"type": "prometheus", "uid": "prometheus"}, (
            f"panel '{panel.get('title')}' does not use the verified prometheus datasource"
        )


def test_dashboard_never_references_dead_metric(dashboard_data: dict) -> None:
    text = json.dumps(dashboard_data)
    assert DEAD_METRIC not in text, f"dashboard must not reference '{DEAD_METRIC}'"


def test_datasource_provisioning_points_at_prometheus() -> None:
    ds = yaml.safe_load(DS_FILE.read_text())["datasources"][0]
    assert ds["name"] == "Prometheus"
    assert ds["type"] == "prometheus"
    assert ds["url"] == "http://prometheus:9090"
    assert ds.get("isDefault") is True


# --- 8. Alertmanager discipline (DEFAULT-OFF) -----------------------------------


def test_alertmanager_not_implemented_and_no_alerting_block(
    prometheus_config: dict,
    compose_data: dict,
) -> None:
    alertmanager_dir = REPO_ROOT / "infra" / "alertmanager"
    assert not (alertmanager_dir / "alertmanager.yml").exists(), (
        "Alertmanager must not be enabled in the Phase 14B default deployment"
    )
    assert "alerting" not in prometheus_config, (
        "no alerting:/Alertmanager block may exist while Alertmanager is OFF"
    )
    assert "alertmanager" not in compose_data.get("services", {}), (
        "no alertmanager compose service in the default Phase 14B deployment"
    )


def test_no_outbound_receiver_or_secret_tokens_in_monitoring_config() -> None:
    """Alertmanager is OFF: no outbound channel or credential placeholder may
    appear in the *Phase 14B monitoring artifacts* (not docker-compose.yml,
    which legitimately carries the pre-existing dev SECRET_KEY scaffold)."""
    text = "\n".join(
        [
            PROMETHEUS_YML.read_text(),
            RULES_YML.read_text(),
            DASHBOARD_JSON.read_text(),
        ]
    ).lower()
    for token in ("webhook", "pagerduty", "slack", "smtp", "api_token", "apikey"):
        assert token not in text, (
            f"outbound/secret token '{token}' must not appear in monitoring config"
        )
