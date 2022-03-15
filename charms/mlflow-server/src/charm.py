#!/usr/bin/env python3
# Copyright 2020 Luke Marsden
# See LICENSE file for licensing details.

"""Charm for the ML Flow Server.

https://github.com/canonical/mlflow-operator
"""

import json
import logging
import re
from base64 import b64encode

from oci_image import OCIImageResource, OCIImageResourceError
from ops.charm import CharmBase
from ops.main import main
from ops.model import (
    ActiveStatus,
    BlockedStatus,
    MaintenanceStatus,
    StatusBase,
    WaitingStatus,
)
from serialized_data_interface import (
    NoCompatibleVersions,
    NoVersionsListed,
    get_interfaces,
)

DB_NAME = "mlflow"


class Operator(CharmBase):
    """Charm for the ML Flow Server.

    https://github.com/canonical/mlflow-operator
    """

    def __init__(self, *args):
        super().__init__(*args)

        self.image = OCIImageResource(self, "oci-image")
        self.log = logging.getLogger(__name__)

        for event in [
            self.on.install,
            self.on.leader_elected,
            self.on.upgrade_charm,
            self.on.config_changed,
            self.on.db_relation_changed,
            self.on["object-storage"].relation_changed,
            self.on["ingress"].relation_changed,
        ]:
            self.framework.observe(event, self.main)

        # Register relation events
        for event in [
            self.on.pod_defaults_relation_joined,
            self.on.pod_defaults_relation_changed,
        ]:
            self.framework.observe(event, self._on_pod_defaults_relation_changed)

    def _on_pod_defaults_relation_changed(self, event):
        try:
            interfaces = self._get_interfaces()
        except CheckFailedError as check_failed:
            self.model.unit.status = check_failed.status
            return

        obj_storage = list(interfaces["object-storage"].get_data().values())[0]
        config = self.model.config
        endpoint = (
            f"http://{obj_storage['service']}.{obj_storage['namespace']}:{obj_storage['port']}"
        )
        tracking = f"{self.model.app.name}.{self.model.name}.svc.cluster.local"
        tracking = f"http://{tracking}:{config['mlflow_port']}"
        event.relation.data[self.app]["pod-defaults"] = json.dumps(
            {
                "minio": {
                    "env": {
                        "AWS_ACCESS_KEY_ID": obj_storage["access-key"],
                        "AWS_SECRET_ACCESS_KEY": obj_storage["secret-key"],
                        "MLFLOW_S3_ENDPOINT_URL": endpoint,
                        "MLFLOW_TRACKING_URI": tracking,
                    }
                }
            }
        )

        requirements = []
        try:
            for req in open("files/mlflow_requirements.txt", "r"):
                requirements.append(req.rstrip("\n"))
        except IOError as e:
            print("Error loading mlflow requirements file:", e)

        event.relation.data[self.unit]["requirements"] = str(requirements)

    def main(self, event):
        """Main function of the charm.

        Runs at install, update, config change and relation change.
        """
        try:
            self._check_leader()
            default_artifact_root = validate_s3_bucket_name(self.config["default_artifact_root"])
            interfaces = self._get_interfaces()
            image_details = self._check_image_details()
        except CheckFailedError as check_failed:
            self.model.unit.status = check_failed.status
            self.model.unit.message = check_failed.msg
            return

        self._configure_mesh(interfaces)
        config = self.model.config
        charm_name = self.model.app.name

        mysql = self.model.relations["db"]
        if len(mysql) > 1:
            self.model.unit.status = BlockedStatus("Too many mysql relations")
            return

        try:
            mysql = mysql[0]
            unit = list(mysql.units)[0]
            mysql = mysql.data[unit]
            mysql["database"]
        except (IndexError, KeyError):
            self.model.unit.status = WaitingStatus("Waiting for mysql relation data")
            return

        if not ((obj_storage := interfaces["object-storage"]) and obj_storage.get_data()):
            self.model.unit.status = WaitingStatus("Waiting for object-storage relation data")
            return

        self.model.unit.status = MaintenanceStatus("Setting pod spec")

        obj_storage = list(obj_storage.get_data().values())[0]
        secrets = [
            {
                "name": f"{charm_name}-minio-secret",
                "data": _minio_credentials_dict(obj_storage=obj_storage),
            },
            {
                "name": f"{charm_name}-seldon-init-container-s3-credentials",
                "data": _seldon_credentials_dict(obj_storage=obj_storage),
            },
            {"name": f"{charm_name}-db-secret", "data": _db_secret_dict(mysql=mysql)},
        ]

        self.model.pod.set_spec(
            {
                "version": 3,
                "containers": [
                    {
                        "name": "mlflow",
                        "imageDetails": image_details,
                        "ports": [{"name": "http", "containerPort": config["mlflow_port"]}],
                        "args": [
                            "--host",
                            "0.0.0.0",
                            "--backend-store-uri",
                            "$(MLFLOW_TRACKING_URI)",
                            "--default-artifact-root",
                            f"s3://{default_artifact_root}/",
                        ],
                        "envConfig": {
                            "db-secret": {"secret": {"name": f"{charm_name}-db-secret"}},
                            "aws-secret": {"secret": {"name": f"{charm_name}-minio-secret"}},
                            "AWS_DEFAULT_REGION": "us-east-1",
                            "MLFLOW_S3_ENDPOINT_URL": "http://{service}.{namespace}:{port}".format(
                                **obj_storage
                            ),
                        },
                    }
                ],
                "kubernetesResources": {
                    "secrets": secrets,
                    "services": [
                        {
                            "name": "mlflow-external",
                            "spec": {
                                "type": "NodePort",
                                "selector": {
                                    "app.kubernetes.io/name": "mlflow",
                                },
                                "ports": [
                                    {
                                        "protocol": "TCP",
                                        "port": config["mlflow_port"],
                                        "targetPort": config["mlflow_port"],
                                        "nodePort": config["mlflow_nodeport"],
                                    }
                                ],
                            },
                        },
                        {
                            "name": "kubeflow-external",
                            "spec": {
                                "type": "NodePort",
                                "selector": {
                                    "app.kubernetes.io/name": "istio-ingressgateway",
                                },
                                "ports": [
                                    {
                                        "protocol": "TCP",
                                        "port": config["kubeflow_port"],
                                        "targetPort": config["kubeflow_port"],
                                        "nodePort": config["kubeflow_nodeport"],
                                    }
                                ],
                            },
                        },
                        {
                            "name": "kubeflow-external-lb",
                            "spec": {
                                "type": "LoadBalancer",
                                "selector": {
                                    "app.kubernetes.io/name": "istio-ingressgateway",
                                },
                                "ports": [
                                    {
                                        "protocol": "TCP",
                                        "port": config["kubeflow_port"],
                                        "targetPort": config["kubeflow_port"],
                                    }
                                ],
                            },
                        },
                    ],
                },
            },
        )
        self.model.unit.status = ActiveStatus()

    def _configure_mesh(self, interfaces):
        if interfaces["ingress"]:
            interfaces["ingress"].send_data(
                {
                    "prefix": "/mlflow/",
                    "rewrite": "/",
                    "service": self.model.app.name,
                    "port": self.model.config["mlflow_port"],
                }
            )

    def _check_leader(self):
        if not self.unit.is_leader():
            # We can't do anything useful when not the leader, so do nothing.
            raise CheckFailedError("Waiting for leadership", WaitingStatus)

    def _get_interfaces(self):
        try:
            interfaces = get_interfaces(self)
        except NoVersionsListed as err:
            raise CheckFailedError(err, WaitingStatus)
        except NoCompatibleVersions as err:
            raise CheckFailedError(err, BlockedStatus)
        return interfaces

    def _check_image_details(self):
        try:
            image_details = self.image.fetch()
        except OCIImageResourceError as e:
            raise CheckFailedError(f"{e.status.message}", e.status_type)
        return image_details


def validate_s3_bucket_name(name):
    """Validates the name as a valid S3 bucket name, raising a CheckFailedError if invalid."""
    # regex from https://stackoverflow.com/a/50484916/5394584
    if re.match(
        r"(?=^.{3,63}$)(?!^(\d+\.)+\d+$)(^(([a-z0-9]|[a-z0-9][a-z0-9\-]*[a-z0-9])\.)*([a-z0-9]|[a-z0-9][a-z0-9\-]*[a-z0-9])$)",
        name,
    ):
        return name
    else:
        msg = (
            f"Invalid value for config default_artifact_root '{name}'"
            f" - value must be a valid S3 bucket name"
        )
        raise CheckFailedError(msg, BlockedStatus)


class CheckFailedError(Exception):
    """Raise this exception if one of the checks in main fails."""

    def __init__(self, msg, status_type=StatusBase):
        super().__init__()

        self.msg = str(msg)
        self.status_type = status_type
        self.status = status_type(self.msg)


def _b64_encode_dict(d):
    """Returns the dict with values being base64 encoded."""
    # Why do we encode and decode in utf-8 first?
    return {k: b64encode(v.encode("utf-8")).decode("utf-8") for k, v in d.items()}


def _minio_credentials_dict(obj_storage):
    """Returns a dict of minio credentials with the values base64 encoded."""
    minio_credentials = {
        "AWS_ENDPOINT_URL": f"http://{obj_storage['service']}.{obj_storage['namespace']}:{obj_storage['port']}",
        "AWS_ACCESS_KEY_ID": obj_storage["access-key"],
        "AWS_SECRET_ACCESS_KEY": obj_storage["secret-key"],
        "USE_SSL": str(obj_storage["secure"]).lower(),
    }
    return _b64_encode_dict(minio_credentials)


def _seldon_credentials_dict(obj_storage):
    """Returns a dict of seldon init-container object storage credentials, base64 encoded."""
    credentials = {
        "RCLONE_CONFIG_S3_TYPE": "s3",
        "RCLONE_CONFIG_S3_PROVIDER": "minio",
        "RCLONE_CONFIG_S3_ACCESS_KEY_ID": obj_storage["access-key"],
        "RCLONE_CONFIG_S3_SECRET_ACCESS_KEY": obj_storage["secret-key"],
        "RCLONE_CONFIG_S3_ENDPOINT": f"http://{obj_storage['service']}:{obj_storage['port']}",
        "RCLONE_CONFIG_S3_ENV_AUTH": "false",
    }
    return _b64_encode_dict(credentials)


def _db_secret_dict(mysql):
    """Returns a dict of db-secret credential data, base64 encoded."""
    db_secret = {
        "DB_ROOT_PASSWORD": mysql["root_password"],
        "MLFLOW_TRACKING_URI": f"mysql+pymysql://root:{mysql['root_password']}@{mysql['host']}"
        f":{mysql['port']}/{mysql['database']}",
    }
    return _b64_encode_dict(db_secret)


if __name__ == "__main__":
    main(Operator)
