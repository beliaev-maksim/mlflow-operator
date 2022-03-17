# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
from pathlib import Path
from time import sleep

import pytest
import requests
import yaml
from lightkube.core.client import Client
from lightkube.models.rbac_v1 import PolicyRule
from lightkube.resources.rbac_authorization_v1 import Role
from pytest_operator.plugin import OpsTest

log = logging.getLogger(__name__)

METADATA = yaml.safe_load(Path("./metadata.yaml").read_text())
CHARM_NAME = METADATA["name"]


@pytest.mark.abort_on_fail
async def test_build_and_deploy(ops_test: OpsTest):
    db = "mlflow-db"
    obj_storage = "minio"
    await ops_test.model.deploy("charmed-osm-mariadb-k8s", application_name=db)
    await ops_test.model.deploy(obj_storage)

    my_charm = await ops_test.build_charm(".")
    image_path = METADATA["resources"]["oci-image"]["upstream-source"]
    resources = {"oci-image": image_path}
    await ops_test.model.deploy(my_charm, resources=resources)
    await ops_test.model.add_relation(CHARM_NAME, obj_storage)
    await ops_test.model.add_relation(CHARM_NAME, db)
    await ops_test.model.wait_for_idle(
        status="active", raise_on_blocked=False, raise_on_error=False
    )

    assert ops_test.model.applications[CHARM_NAME].units[0].workload_status == "active"


async def test_access_dashboard(ops_test: OpsTest, request):
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

    status = await ops_test.model.get_status()
    url = f"http://{status['applications'][istio_gateway]['public-address']}.nip.io/mlflow/"

    for _ in range(60):
        try:
            requests.get(url, timeout=60)
            break
        except requests.ConnectionError:
            sleep(5)
    r = requests.get(url)
    assert r.status_code == 200