from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "docker-compose.registration-node.yml"
DEPLOY = ROOT / "deploy" / "registration-node"


def test_registration_node_is_an_image_only_independent_instance():
    text = COMPOSE.read_text(encoding="utf-8")

    assert "container_name: auto-plus3" in text
    assert "APP_INSTANCE_ID: auto-plus3" in text
    assert "build:" not in text
    assert "127.0.0.1:8000:8000" in text
    assert "SHARED_CONFIG_DB: /runtime/shared_config.db" in text
    assert "/opt/auto-gpt/shared_config" not in text
    assert "pids_limit: 6144" in text
    assert 'shm_size: "4gb"' in text


def test_registration_node_preserves_private_service_names_over_host_tunnel():
    text = COMPOSE.read_text(encoding="utf-8")

    for alias in (
        "tempmail-api-1:172.20.0.1",
        "gpt-cccy-me:172.20.0.1",
        "phone-api-relay:172.20.0.1",
        "openai-pay-long-link:172.20.0.1",
        "team-manage-app:172.20.0.1",
        "sms-gateway:172.20.0.1",
    ):
        assert alias in text
    assert "HME_READY_INTERNAL_API_URL: http://172.20.0.1:18765" in text
    assert "OAIPAY_SUBMIT_URL: http://172.20.0.1:18789" in text
    assert "PAYPAL_AGREEMENT_INTERNAL_BASE_URL: http://172.20.0.1:18098" in text
    assert "com.docker.network.bridge.name: br-auto-plus3" in text
    assert "subnet: 172.20.0.0/16" in text
    assert "AUTH_BROWSER_MAX_CONCURRENCY: \"15\"" in text
    assert "SOLVER_WARM_BROWSERS: \"2\"" in text


def test_dependency_tunnel_is_restricted_to_declared_private_targets():
    service = (DEPLOY / "auto-plus3-dependency-tunnel.service").read_text(encoding="utf-8")
    sshd = (DEPLOY / "auto-plus3-tunnel-sshd.conf").read_text(encoding="utf-8")

    assert "-L 172.20.0.1:8080:127.0.0.1:18083" in service
    assert "-L 172.20.0.1:18789:127.0.0.1:8789" in service
    assert "-L 172.20.0.1:18098:172.20.0.1:18098" in service
    assert "ExitOnForwardFailure=yes" in service
    assert "StrictHostKeyChecking=yes" in service
    assert "AllowTcpForwarding local" in sshd
    assert "PermitOpen " in sshd
    assert "PasswordAuthentication no" in sshd
    assert "MaxSessions 0" in sshd


def test_public_nginx_site_only_proxies_the_loopback_application_port():
    nginx = (DEPLOY / "auto-plus3.nginx.conf").read_text(encoding="utf-8")

    assert "server_name auto-plus3.cccy.me" in nginx
    assert "proxy_pass http://127.0.0.1:8000" in nginx
    assert "ssl_certificate /etc/letsencrypt/live/auto-plus3.cccy.me/fullchain.pem" in nginx


def test_cloudflare_ingress_runs_as_a_restricted_service():
    service = (DEPLOY / "auto-plus3-cloudflared.service").read_text(encoding="utf-8")

    assert "User=cloudflared" in service
    assert "--config /etc/cloudflared/auto-plus3.yml tunnel run" in service
    assert "NoNewPrivileges=true" in service
    assert "ProtectSystem=strict" in service
