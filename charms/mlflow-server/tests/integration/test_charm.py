# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.

import json
import logging
from base64 import b64encode
from pathlib import Path
from random import choices
from string import ascii_lowercase
from time import sleep

import pytest
import requests
import yaml
from lightkube.core.client import Client
from lightkube.models.rbac_v1 import PolicyRule
from lightkube.resources.core_v1 import Secret
from lightkube.resources.rbac_authorization_v1 import Role
from pytest_lazyfixture import lazy_fixture
from pytest_operator.plugin import OpsTest
from selenium.common.exceptions import JavascriptException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.support import expected_conditions
from selenium.webdriver.support.ui import WebDriverWait
from seleniumwire import webdriver
from tenacity import Retrying, stop_after_attempt, stop_after_delay, wait_exponential

log = logging.getLogger(__name__)

METADATA = yaml.safe_load(Path("./metadata.yaml").read_text())
CHARM_NAME = METADATA["name"]
OBJ_STORAGE_NAME = "minio"
OBJ_STORAGE_CONFIG = {
    "access-key": "minio",
    "secret-key": "minio123",
    "port": "9000",
}


@pytest.mark.abort_on_fail
async def test_build_and_deploy(ops_test: OpsTest):
    db = "mlflow-db"
    await ops_test.model.deploy("charmed-osm-mariadb-k8s", application_name=db)
    await ops_test.model.deploy(OBJ_STORAGE_NAME, config=OBJ_STORAGE_CONFIG)

    my_charm = await ops_test.build_charm(".")
    image_path = METADATA["resources"]["oci-image"]["upstream-source"]
    resources = {"oci-image": image_path}
    await ops_test.model.deploy(my_charm, resources=resources)
    await ops_test.model.add_relation(CHARM_NAME, OBJ_STORAGE_NAME)
    await ops_test.model.add_relation(CHARM_NAME, db)
    await ops_test.model.wait_for_idle(status="active")


@pytest.mark.assertions
async def test_successful_deploy(ops_test: OpsTest):
    assert ops_test.model.applications[CHARM_NAME].units[0].workload_status == "active"


@pytest.mark.abort_on_fail
async def test_relation_and_secrets(ops_test: OpsTest):
    """Test information propagation from relation to secrets."""
    # NOTE: This test depends on deployment done in test_build_and_deploy()
    test_namespace = ops_test.model_name
    lightkube_client = Client(namespace=test_namespace)

    minio_secret = lightkube_client.get(
        Secret, name=f"{CHARM_NAME}-minio-secret", namespace=test_namespace
    )
    assert minio_secret is not None

    seldon_secret = lightkube_client.get(
        Secret, name=f"{CHARM_NAME}-seldon-init-container-s3-credentials", namespace=test_namespace
    )
    assert seldon_secret is not None

    # check base64 encoding of endpoint URL
    test_storage_url = f"http://minio.{test_namespace}:9000"
    test_storage_url_b64 = b64encode(test_storage_url.encode("utf-8")).decode("utf-8")
    assert minio_secret.data["AWS_ENDPOINT_URL"] == test_storage_url_b64
    assert seldon_secret.data["RCLONE_CONFIG_S3_ENDPOINT"] == test_storage_url_b64


async def test_default_bucket_created(ops_test: OpsTest):
    """Tests whether the default bucket is auto-generated by mlflow.

    Note: We do not have a test coverage to assert if that the bucket is not created if
    create_default_artifact_root_if_missing==False.
    """
    config = await ops_test.model.applications[CHARM_NAME].get_config()
    default_bucket_name = config["default_artifact_root"]["value"]

    ret_code, stdout, stderr, kubectl_cmd = await does_minio_bucket_exist(
        default_bucket_name, ops_test
    )
    assert ret_code == 0, (
        f"Unable to find bucket named {default_bucket_name}, got "
        f"stdout=\n'{stdout}\n'stderr=\n{stderr}\nUsed command {kubectl_cmd}"
    )


async def does_minio_bucket_exist(bucket_name, ops_test: OpsTest):
    """Connects to the minio server and checks if a bucket exists, checking if a bucket exists.

    Returns:
        Tuple of the return code, stdout, and stderr
    """
    access_key = OBJ_STORAGE_CONFIG["access-key"]
    secret_key = OBJ_STORAGE_CONFIG["secret-key"]
    port = OBJ_STORAGE_CONFIG["port"]
    obj_storage_name = OBJ_STORAGE_NAME
    model_name = ops_test.model_name
    log.info(f"ops_test.model_name = {ops_test.model_name}")

    obj_storage_url = f"http://{obj_storage_name}.{model_name}.svc.cluster.local:{port}"

    # Region is not used and doesn't matter, but must be set to run in github actions as explained
    # in: https://florian.ec/blog/github-actions-awscli-errors/
    aws_cmd = (
        f"aws --endpoint-url {obj_storage_url} --region us-east-1 s3api head-bucket"
        f" --bucket={bucket_name}"
    )

    # Add random suffix to pod name to avoid collision
    this_pod_name = f"{CHARM_NAME}-minio-bucket-test-{generate_random_string()}"

    kubectl_cmd = (
        "microk8s",
        "kubectl",
        "run",
        "--rm",
        "-i",
        "--restart=Never",
        f"--namespace={ops_test.model_name}",
        this_pod_name,
        f"--env=AWS_ACCESS_KEY_ID={access_key}",
        f"--env=AWS_SECRET_ACCESS_KEY={secret_key}",
        "--image=amazon/aws-cli",
        "--command",
        "--",
        "sh",
        "-c",
        aws_cmd,
    )

    (
        ret_code,
        stdout,
        stderr,
    ) = await ops_test.run(*kubectl_cmd)
    return ret_code, stdout, stderr, " ".join(kubectl_cmd)


def generate_random_string(length: int = 4):
    """Returns a random string of lower case alphabetic characters and given length."""
    return "".join(choices(ascii_lowercase, k=length))


async def test_prometheus_grafana_integration(ops_test: OpsTest):
    """Deploy prometheus, grafana and required relations, then test the metrics."""
    prometheus = "prometheus-k8s"
    grafana = "grafana-k8s"
    prometheus_scrape_charm = "prometheus-scrape-config-k8s"
    scrape_config = {"scrape_interval": "5s"}

    await ops_test.model.deploy(prometheus, channel="latest/edge", trust=True)
    await ops_test.model.deploy(grafana, channel="latest/edge", trust=True)
    await ops_test.model.deploy(
        prometheus_scrape_charm, channel="latest/beta", config=scrape_config
    )
    await ops_test.model.add_relation(CHARM_NAME, prometheus_scrape_charm)
    await ops_test.model.add_relation(
        f"{prometheus}:grafana-dashboard", f"{grafana}:grafana-dashboard"
    )
    await ops_test.model.add_relation(
        f"{CHARM_NAME}:grafana-dashboard", f"{grafana}:grafana-dashboard"
    )
    await ops_test.model.add_relation(
        f"{prometheus}:metrics-endpoint", f"{prometheus_scrape_charm}:metrics-endpoint"
    )

    await ops_test.model.wait_for_idle(status="active", timeout=60 * 10)

    status = await ops_test.model.get_status()
    prometheus_unit_ip = status["applications"][prometheus]["units"][f"{prometheus}/0"]["address"]
    log.info(f"Prometheus available at http://{prometheus_unit_ip}:9090")

    for attempt in retry_for_5_attempts:
        log.info(
            f"Testing prometheus deployment (attempt " f"{attempt.retry_state.attempt_number})"
        )
        with attempt:
            r = requests.get(
                f"http://{prometheus_unit_ip}:9090/api/v1/query?"
                f'query=up{{juju_application="{CHARM_NAME}"}}'
            )
            response = json.loads(r.content.decode("utf-8"))
            response_status = response["status"]
            log.info(f"Response status is {response_status}")
            assert response_status == "success"

            response_metric = response["data"]["result"][0]["metric"]
            assert response_metric["juju_application"] == CHARM_NAME
            assert response_metric["juju_model"] == ops_test.model_name


# Helper to retry calling a function over 30 seconds or 5 attempts
retry_for_5_attempts = Retrying(
    stop=(stop_after_attempt(5) | stop_after_delay(30)),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    reraise=True,
)


@pytest.mark.abort_on_fail
async def test_deploy_with_ingress(ops_test: OpsTest):
    istio_pilot = "istio-pilot"
    istio_gateway = "istio-gateway"
    await ops_test.model.deploy(istio_pilot, channel="1.5/stable")
    await ops_test.model.deploy(istio_gateway, channel="1.5/stable")
    await ops_test.model.add_relation(istio_gateway, istio_pilot)
    await ops_test.model.add_relation(istio_pilot, CHARM_NAME)

    await ops_test.model.wait_for_idle(
        [istio_gateway],
        status="waiting",
        timeout=600,
    )

    # Patch the istio-gateway Role so that it can access it's own configmap
    # This can be removed when we move to the sidecar istio v1.11 charm
    lightkube_client = Client(
        namespace=ops_test.model_name,
    )

    await ops_test.model.set_config({"update-status-hook-interval": "15s"})
    istio_gateway_role_name = "istio-gateway-operator"

    new_policy_rule = PolicyRule(verbs=["*"], apiGroups=["*"], resources=["*"])
    this_role = lightkube_client.get(Role, istio_gateway_role_name)
    this_role.rules.append(new_policy_rule)
    lightkube_client.patch(Role, istio_gateway_role_name, this_role)

    sleep(50)
    await ops_test.model.set_config({"update-status-hook-interval": "5m"})

    await ops_test.model.wait_for_idle(status="active")


@pytest.fixture
@pytest.mark.asyncio
async def url_with_ingress(ops_test: OpsTest):
    status = await ops_test.model.get_status()
    url = f"http://{status['applications']['istio-gateway']['public-address']}.nip.io/mlflow/"
    yield url


@pytest.fixture
@pytest.mark.asyncio
async def url_without_ingress(ops_test: OpsTest):
    status = await ops_test.model.get_status()
    unit_name = ops_test.model.applications[CHARM_NAME].units[0].name
    url = f"http://{status['applications'][CHARM_NAME]['units'][unit_name]['address']}:5000"
    yield url


@pytest.mark.assertions
@pytest.mark.parametrize(
    "url", [lazy_fixture("url_without_ingress"), lazy_fixture("url_with_ingress")]
)
@pytest.mark.asyncio
async def test_access_dashboard(request, url):
    options = Options()
    options.headless = True
    options.log.level = "trace"
    max_wait = 20  # seconds

    kwargs = {
        "options": options,
        "seleniumwire_options": {"enable_har": True},
    }

    with webdriver.Firefox(**kwargs) as driver:
        wait = WebDriverWait(driver, max_wait, 1, (JavascriptException, StopIteration))
        for _ in range(60):
            try:
                driver.get(url)
                wait.until(
                    expected_conditions.presence_of_element_located(
                        (By.CLASS_NAME, "experiment-view-container")
                    )
                )
                break
            except WebDriverException:
                sleep(5)
        else:
            driver.get(url)

        yield driver, wait, url

        Path(f"/tmp/selenium-{request.node.name}.har").write_text(driver.har)
        driver.get_screenshot_as_file(f"/tmp/selenium-{request.node.name}.png")
