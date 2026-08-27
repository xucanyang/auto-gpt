import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "docker-compose.multi.yml"
DEPLOY_SCRIPT = ROOT / "deploy.sh"


def test_main_instance_is_an_unprofiled_persistent_service():
    source = COMPOSE_FILE.read_text(encoding="utf-8")
    match = re.search(
        r"(?ms)^  auto-gpt:\n(?P<body>.*?)(?=^  auto-gpt-plus:\n)",
        source,
    )

    assert match is not None
    service = match.group("body")
    assert "profiles:" not in service
    assert "restart: unless-stopped" in service
    assert '- "127.0.0.1:8000:8000"' in service
    assert "./data:/runtime" in service


def test_all_published_business_and_cliproxy_ports_are_loopback_only():
    source = COMPOSE_FILE.read_text(encoding="utf-8")

    expected = {
        '127.0.0.1:8000:8000',
        '127.0.0.1:8001:8000',
        '127.0.0.1:8003:8000',
        '127.0.0.1:18003:8000',
        '127.0.0.1:${CLIPROXYAPI_PORT_BIND:-8317}:8317',
        '127.0.0.1:${CLIPROXYAPI_PORT_BIND_PLUS:-8318}:8317',
        '127.0.0.1:${CLIPROXYAPI_PORT_BIND_PLUS2:-8320}:8317',
        '127.0.0.1:8319:8317',
        '127.0.0.1:8895:8889',
    }
    for binding in expected:
        assert f'- "{binding}"' in source

    forbidden = {
        '8000:8000',
        '8001:8000',
        '8003:8000',
        '18003:8000',
        '${CLIPROXYAPI_PORT_BIND:-8317}:8317',
        '${CLIPROXYAPI_PORT_BIND_PLUS:-8318}:8317',
        '${CLIPROXYAPI_PORT_BIND_PLUS2:-8320}:8317',
        '8319:8317',
        '8895:8889',
    }
    published = {
        line.strip()[3:-1]
        for line in source.splitlines()
        if line.strip().startswith('- "') and line.strip().endswith('"')
    }
    assert published.isdisjoint(forbidden)


def test_deploy_keeps_and_verifies_all_four_business_instances():
    source = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    assert (
        "ACTIVE_SERVICES=(phone-api-relay auto-gpt auto-gpt-plus auto-plus2 auto-plus3)"
        in source
    )
    assert "STANDBY_SERVICES" not in source
    assert "stop_standby_services" not in source
    assert 'docker stop "$service"' not in source

    expected_hot_syncs = {
        "hot_sync_service auto-gpt http://127.0.0.1:8000/api/health",
        "hot_sync_service auto-gpt-plus http://127.0.0.1:8001/api/health",
        "hot_sync_service auto-plus2 http://127.0.0.1:8003/api/health",
        "hot_sync_service auto-plus3 http://127.0.0.1:18003/api/health",
    }
    for command in expected_hot_syncs:
        assert command in source

    for instance, port in (
        ("auto-gpt", 8000),
        ("auto-gpt-plus", 8001),
        ("auto-plus2", 8003),
        ("auto-plus3", 18003),
    ):
        assert (
            f'smoke_url "{instance} health" '
            f'"http://127.0.0.1:{port}/api/health"'
        ) in source
        assert (
            f'smoke_url "{instance} index" "http://127.0.0.1:{port}/"'
        ) in source


def test_multi_release_builds_shared_image_exactly_once():
    compose_source = COMPOSE_FILE.read_text(encoding="utf-8")
    deploy_source = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    service_bodies = {}
    for service_name in (
        "phone-api-relay",
        "auto-gpt",
        "auto-gpt-plus",
        "auto-plus2",
        "auto-plus3",
    ):
        match = re.search(
            rf"(?ms)^  {re.escape(service_name)}:\n(?P<body>.*?)(?=^  [a-zA-Z0-9_-]+:\n|^networks:\n|\Z)",
            compose_source,
        )
        assert match is not None
        service_bodies[service_name] = match.group("body")
        assert "image: ${APP_IMAGE:-auto-gpt:latest}" in match.group("body")

    assert "    build:\n" in service_bodies["auto-gpt"]
    assert compose_source.count("    build:\n") == 1
    for service_name in ("phone-api-relay", "auto-gpt-plus", "auto-plus2", "auto-plus3"):
        assert "    build:\n" not in service_bodies[service_name]

    assert "compose_multi build auto-gpt" in deploy_source
    assert (
        'compose_multi up -d --no-build --remove-orphans "${ACTIVE_SERVICES[@]}"'
        in deploy_source
    )
    assert re.search(r"(?m)^\s*compose_multi build\s*$", deploy_source) is None


def test_auto_plus3_is_a_multi_service_with_isolated_registration_runtime():
    source = COMPOSE_FILE.read_text(encoding="utf-8")
    match = re.search(r"(?ms)^  auto-plus3:\n(?P<body>.*?)(?=^networks:\n)", source)

    assert match is not None
    service = match.group("body")
    assert "image: ${APP_IMAGE:-auto-gpt:latest}" in service
    assert "container_name: auto-plus3" in service
    assert "APP_INSTANCE_ID: auto-plus3" in service
    assert "/opt/auto-gpt-register/root.env" in service
    assert "/opt/auto-gpt-register/data:/runtime" in service
    assert "/opt/auto-gpt-register/_ext_targets:/_ext_targets" in service
    assert "/opt/auto-gpt-register/external_logs:/app/services/external_logs" in service
    assert '"127.0.0.1:18003:8000"' in service
    assert '"127.0.0.1:8319:8317"' in service
    assert '"127.0.0.1:8895:8889"' in service
    assert 'SHARED_CONFIG_DB: /runtime/shared_config.db' in service
    assert 'AUTH_BROWSER_MAX_CONCURRENCY: "30"' in service
    assert 'shm_size: "16gb"' in service
    assert 'pids_limit: ${REGISTRATION_NODE_PIDS_LIMIT:-256499}' in service
