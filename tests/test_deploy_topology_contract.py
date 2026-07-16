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
    assert '- "8000:8000"' in service
    assert "./data:/runtime" in service


def test_deploy_keeps_and_verifies_all_three_business_instances():
    source = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    assert (
        "ACTIVE_SERVICES=(phone-api-relay auto-gpt auto-gpt-plus auto-plus2)"
        in source
    )
    assert "STANDBY_SERVICES" not in source
    assert "stop_standby_services" not in source
    assert 'docker stop "$service"' not in source

    expected_hot_syncs = {
        "hot_sync_service auto-gpt http://127.0.0.1:8000/api/health",
        "hot_sync_service auto-gpt-plus http://127.0.0.1:8001/api/health",
        "hot_sync_service auto-plus2 http://127.0.0.1:8003/api/health",
    }
    for command in expected_hot_syncs:
        assert command in source

    for instance, port in (
        ("auto-gpt", 8000),
        ("auto-gpt-plus", 8001),
        ("auto-plus2", 8003),
    ):
        assert (
            f'smoke_url "{instance} health" '
            f'"http://127.0.0.1:{port}/api/health"'
        ) in source
        assert (
            f'smoke_url "{instance} index" "http://127.0.0.1:{port}/"'
        ) in source
