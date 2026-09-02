"""Contracts for the opt-in EC2 SMTP SOCKS egress deployment."""

from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "deploy/k8s/components/smtp-egress-proxy"
PROXY_MANIFEST = COMPONENT / "proxy.yaml"
COMPOSE = ROOT / "deploy/smtp-egress/docker-compose.yml"
ENTRYPOINT = ROOT / "deploy/smtp-egress/entrypoint.sh"
DANTE_CONFIG = ROOT / "deploy/smtp-egress/danted.conf.example"
INSTALL = ROOT / "scripts/cloud/smtp-egress/install.sh"
SMOKE = ROOT / "scripts/cloud/smtp-egress/smoke.sh"


def _documents(path: Path) -> list[dict[str, Any]]:
    return [doc for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")) if doc]


def _find(documents: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    for document in documents:
        if document.get("kind") == kind and (document.get("metadata") or {}).get("name") == name:
            return document
    raise AssertionError(f"{kind}/{name} not found")


def test_smtp_egress_proxy_is_internal_and_mounts_client_tls_read_only() -> None:
    documents = _documents(PROXY_MANIFEST)
    deployment = _find(documents, "Deployment", "smtp-egress-proxy")
    service = _find(documents, "Service", "smtp-egress-proxy")

    assert service["spec"]["type"] == "ClusterIP"
    assert service["spec"]["ports"] == [{"name": "socks", "port": 1080, "targetPort": "socks"}]
    assert deployment["spec"]["replicas"] == 0

    pod_spec = deployment["spec"]["template"]["spec"]
    container = pod_spec["containers"][0]
    tls_mount = next(mount for mount in container["volumeMounts"] if mount["name"] == "client-tls")
    assert tls_mount["readOnly"] is True
    tls_volume = next(volume for volume in pod_spec["volumes"] if volume["name"] == "client-tls")
    assert tls_volume["secret"]["defaultMode"] == 0o440
    assert container["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }


def test_ec2_runner_only_publishes_the_mtls_tunnel() -> None:
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services = compose["services"]

    assert set(services) == {"danted", "stunnel"}
    assert "ports" not in services["danted"]
    assert services["stunnel"]["ports"] == ["${SMTP_EGRESS_BIND_ADDRESS:-0.0.0.0}:8443:8443"]
    assert services["danted"]["image"] == "${SMTP_EGRESS_IMAGE:-manor-smtp-egress:local}"
    assert services["stunnel"]["image"] == "${SMTP_EGRESS_IMAGE:-manor-smtp-egress:local}"


def test_ec2_runner_uses_debian_dante_without_privilege_reconfiguration() -> None:
    assert "exec danted -f /etc/manor/danted/danted.conf" in ENTRYPOINT.read_text(encoding="utf-8")
    assert "-p /tmp/danted.pid" in ENTRYPOINT.read_text(encoding="utf-8")
    dante_config = DANTE_CONFIG.read_text(encoding="utf-8")
    assert "to: 198.18.0.0/15" in dante_config
    assert "to: 203.0.113.0/24" in dante_config
    assert not any(
        line.strip().startswith("user.notprivileged")
        for line in dante_config.splitlines()
    )


def test_proxy_network_boundaries_and_operator_scripts() -> None:
    documents = _documents(PROXY_MANIFEST)
    app_config = _find(documents, "ConfigMap", "smtp-egress-app-config")
    assert app_config["data"] == {
        "SMTP_EGRESS_MODE": "direct",
        "SMTP_SOCKS_HOST": "smtp-egress-proxy",
        "SMTP_SOCKS_PORT": "1080",
    }
    policies = {
        document["metadata"]["name"]: document
        for document in documents
        if document.get("kind") == "NetworkPolicy"
    }

    app_policy = policies["manor-smtp-egress-client-only"]
    assert app_policy["spec"]["policyTypes"] == ["Ingress"]
    assert app_policy["spec"]["ingress"][0]["ports"] == [{"protocol": "TCP", "port": 1080}]

    client_egress = policies["manor-runtime-smtp-proxy-egress"]
    assert client_egress["spec"]["policyTypes"] == ["Egress"]
    assert client_egress["spec"]["egress"][0]["ports"] == [{"protocol": "TCP", "port": 1080}]

    remote_policy = policies["manor-smtp-egress-remote-only"]
    assert remote_policy["spec"]["policyTypes"] == ["Egress"]
    assert remote_policy["spec"]["egress"][0]["ports"] == [{"protocol": "TCP", "port": 8443}]
    assert remote_policy["spec"]["egress"][0]["to"][0]["ipBlock"]["cidr"] == "0.0.0.0/0"

    install = INSTALL.read_text(encoding="utf-8")
    assert "docker compose" in install
    assert "COMPOSE=(docker-compose)" in install
    assert '"${COMPOSE[@]}" --project-name manor-smtp-egress --file "$COMPOSE_FILE" pull' in install
    assert "--no-build" in install
    assert "SMTP_EGRESS_DANTE_CONFIG" in install
    assert "SMTP_EGRESS_STUNNEL_CONFIG" in install
    assert "SMTP_EGRESS_TLS_DIR" in install
    assert "systemctl is-active --quiet docker" in install
    assert "sudo test -r" in install

    smoke = SMOKE.read_text(encoding="utf-8")
    assert "kubectl" in smoke
    assert "smtp-egress-proxy" in smoke
    assert "SOCKS5 handshake" in smoke


def test_runtime_workloads_optionally_load_the_smtp_egress_app_config() -> None:
    base_dir = ROOT / "deploy/k8s/base"
    workloads = {
        "manor-api": base_dir / "api.yaml",
        "manor-chat": base_dir / "chat.yaml",
        "manor-worker": base_dir / "workers.yaml",
        "manor-worker-heavy": base_dir / "workers.yaml",
        "manor-beat": base_dir / "workers.yaml",
    }

    for workload_name, manifest in workloads.items():
        deployment = _find(_documents(manifest), "Deployment", workload_name)
        for container in deployment["spec"]["template"]["spec"]["containers"]:
            assert {
                "configMapRef": {
                    "name": "smtp-egress-app-config",
                    "optional": True,
                }
            } in container["envFrom"]


def test_digitalocean_overlays_keep_smtp_egress_and_imaps_support() -> None:
    expected_ref = {
        "configMapRef": {
            "name": "smtp-egress-app-config",
            "optional": True,
        }
    }
    expected_workloads = {
        "manor-api": {"manor-api"},
        "manor-chat": {"manor-chat"},
        "manor-worker": {"manor-worker"},
        "manor-worker-heavy": {"manor-worker-heavy"},
        "manor-beat": {"manor-beat"},
    }
    for overlay in ("digitalocean-test", "digitalocean-prod"):
        documents = _documents(ROOT / "deploy/k8s/overlays" / overlay / "patches.yaml")
        for workload_name, containers in expected_workloads.items():
            deployment = _find(documents, "Deployment", workload_name)
            actual = {
                container["name"]: container["envFrom"]
                for container in deployment["spec"]["template"]["spec"]["containers"]
            }
            for container_name in containers:
                assert expected_ref in actual[container_name]

    dante_config = DANTE_CONFIG.read_text(encoding="utf-8")
    assert "port = 993" in dante_config
    assert "port = 0-65535" not in dante_config


def test_smtp_egress_operator_scripts_are_valid_bash() -> None:
    for script in (INSTALL, SMOKE):
        subprocess.run(["bash", "-n", str(script)], check=True)
