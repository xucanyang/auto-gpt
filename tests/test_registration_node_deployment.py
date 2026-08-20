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
    for resource_limit in (
        "mem_limit:",
        "mem_reservation:",
        "memswap_limit:",
        "cpus:",
        "cpu_quota:",
    ):
        assert resource_limit not in text
    assert 'shm_size: "16gb"' in text
    assert "pids_limit: ${REGISTRATION_NODE_PIDS_LIMIT:-256499}" in text


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
    assert "AUTH_BROWSER_MAX_CONCURRENCY: \"30\"" in text
    assert "AUTH_BROWSER_CAPACITY_MODE: fixed" in text
    assert "AUTH_BROWSER_REGISTRATION_RESERVE: \"0\"" in text
    assert "AUTH_BROWSER_RECHECK_RESERVE: \"0\"" in text
    assert "AUTH_BROWSER_PID_RESERVE: \"0\"" in text
    assert "AUTH_BROWSER_PID_EMERGENCY_RESERVE: \"0\"" in text
    assert "AUTH_BROWSER_HOST_MEMORY_RESERVE_MIB: \"0\"" in text
    assert "AUTH_BROWSER_CPU_PSI_AVG10_LIMIT: \"0\"" in text
    assert "AUTH_BROWSER_LAUNCH_INTERVAL_SECONDS: \"0\"" in text
    assert "SOLVER_MAX_BROWSERS: \"15\"" in text
    assert "SOLVER_WARM_BROWSERS: \"0\"" in text


def test_dependency_tunnel_is_restricted_to_declared_private_targets():
    service = (DEPLOY / "auto-plus3-dependency-tunnel.service").read_text(encoding="utf-8")
    sshd = (DEPLOY / "auto-plus3-tunnel-sshd.conf").read_text(encoding="utf-8")

    assert "-L 172.20.0.1:8080:127.0.0.1:18083" in service
    assert "-L 172.20.0.1:18765:127.0.0.1:18765" in service
    assert "-L 172.20.0.1:18789:127.0.0.1:8789" in service
    assert "-L 172.20.0.1:18098:127.0.0.1:18097" in service
    assert "-R 127.0.0.1:18003:127.0.0.1:8000" in service
    assert "ExitOnForwardFailure=yes" in service
    assert "StrictHostKeyChecking=yes" in service
    assert "AllowTcpForwarding yes" in sshd
    assert "PermitOpen " in sshd
    assert "127.0.0.1:18765" in sshd
    assert "127.0.0.1:18097" in sshd
    assert "PermitListen 127.0.0.1:18003" in sshd
    assert "PasswordAuthentication no" in sshd
    assert "MaxSessions 0" in sshd


def test_public_nginx_site_only_proxies_the_reverse_tunnel_loopback_port():
    nginx = (DEPLOY / "auto-plus3.nginx.conf").read_text(encoding="utf-8")

    assert "server_name auto-plus3.cccy.me" in nginx
    assert "proxy_pass http://127.0.0.1:18003" in nginx
    assert "ssl_certificate /root/ssl/cccy.me.crt" in nginx
    assert "auto-gpt-cloudflare-realip.conf" in nginx
