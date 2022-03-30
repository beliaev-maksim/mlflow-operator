# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from pathlib import Path
from random import choices
from string import ascii_lowercase
from time import sleep

import pytest
import yaml
from lightkube.core.client import Client
from lightkube.models.rbac_v1 import PolicyRule
from lightkube.resources.rbac_authorization_v1 import Role
from pytest_lazyfixture import lazy_fixture
from pytest_operator.plugin import OpsTest
from selenium.common.exceptions import JavascriptException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.support import expected_conditions
from selenium.webdriver.support.ui import WebDriverWait
from seleniumwire import webdriver

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
    obj_storage = OBJ_STORAGE_NAME
    await ops_test.model.deploy("charmed-osm-mariadb-k8s", application_name=db)
    await ops_test.model.deploy(obj_storage)

    my_charm = await ops_test.build_charm(".")
    image_path = METADATA["resources"]["oci-image"]["upstream-source"]
    resources = {"oci-image": image_path}
    await ops_test.model.deploy(my_charm, resources=resources)
    await ops_test.model.add_relation(CHARM_NAME, obj_storage)
    await ops_test.model.add_relation(CHARM_NAME, db)
    await ops_test.model.wait_for_idle(status="active")


@pytest.mark.assertions
async def test_successful_deploy(ops_test: OpsTest):
    assert ops_test.model.applications[CHARM_NAME].units[0].workload_status == "active"


async def test_default_bucket_created(ops_test: OpsTest):
    default_bucket_name = await ops_test.model.applications[CHARM_NAME].get_config()

    ret_code, stdout, stderr, kubectl_cmd = await does_minio_bucket_exist(default_bucket_name, ops_test)
    assert ret_code == 0, f"Unable to find bucket named {default_bucket_name}, got " \
                          f"stdout='{stdout}', stderr={stderr}.  Used command {kubectl_cmd}"


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

    aws_cmd = f"aws --endpoint-url {obj_storage_url} s3api head-bucket --bucket={bucket_name}"

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
        aws_cmd
    )

    ret_code, stdout, stderr, = await ops_test.run(*kubectl_cmd)
    return ret_code, stdout, stderr, " ".join(kubectl_cmd)


def generate_random_string(length: int = 4):
    """Returns a randomly generated string of lower case alphabetic characters and given length"""
    return ''.join(choices(ascii_lowercase, k=length))


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
async def url_with_ingress(ops_test: OpsTest):
    status = await ops_test.model.get_status()
    url = f"http://{status['applications']['istio-gateway']['public-address']}.nip.io/mlflow/"
    yield url


@pytest.fixture
async def url_without_ingress(ops_test: OpsTest):
    status = await ops_test.model.get_status()
    unit_name = ops_test.model.applications[CHARM_NAME].units[0].name
    url = f"http://{status['applications'][CHARM_NAME]['units'][unit_name]['address']}:5000"
    yield url


@pytest.mark.assertions
@pytest.mark.parametrize(
    "url", [lazy_fixture("url_without_ingress"), lazy_fixture("url_with_ingress")]
)
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
        wait.until(
            expected_conditions.presence_of_element_located(
                (By.CLASS_NAME, "experiment-view-container")
            )
        )
        Path(f"/tmp/selenium-{request.node.name}.har").write_text(driver.har)
