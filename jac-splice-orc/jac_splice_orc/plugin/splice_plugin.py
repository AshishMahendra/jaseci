"""JAC Splice-Orchestrator Plugin."""

import os
import types
from typing import Optional, Union

from kubernetes import client, config, utils

from jac_splice_orc.managers.proxy_manager import ModuleProxy

import pluggy
import logging
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)

hookimpl = pluggy.HookimplMarker("jac")


class SpliceOrcPlugin:
    """JAC Splice-Orchestrator Plugin."""

    def __init__(self):
        """Constructor for SpliceOrcPlugin."""
        logging.info("Initializing SpliceOrcPlugin")
        namespace = "jac-splice-orc"
        self.create_namespace(namespace)
        self.create_service_account(namespace)
        self.apply_pod_manager_yaml(namespace)
        self.configure_pod_manager_url(namespace)

    def create_namespace(self, namespace_name):
        """Create a new namespace if it does not exist."""
        logging.info(f"Creating namespace '{namespace_name}'")
        try:
            config.load_kube_config()
        except config.ConfigException:
            config.load_incluster_config()
        v1 = client.CoreV1Api()

        # Check if the namespace exists
        namespaces = v1.list_namespace()
        if any(ns.metadata.name == namespace_name for ns in namespaces.items):
            logging.info(f"Namespace '{namespace_name}' already exists.")
        else:
            ns = client.V1Namespace(metadata=client.V1ObjectMeta(name=namespace_name))
            v1.create_namespace(ns)
            logging.info(f"Namespace '{namespace_name}' created.")

    def create_service_account(self, namespace):
        v1 = client.CoreV1Api()
        service_account_name = "smartimportsa"

        # Check if the ServiceAccount already exists
        try:
            v1.read_namespaced_service_account(
                name=service_account_name, namespace=namespace
            )
            logging.info(
                f"ServiceAccount '{service_account_name}' already exists in namespace '{namespace}'."
            )
        except client.exceptions.ApiException as e:
            if e.status == 404:
                # Create the ServiceAccount
                sa = client.V1ServiceAccount(
                    metadata=client.V1ObjectMeta(name=service_account_name)
                )
                v1.create_namespaced_service_account(namespace=namespace, body=sa)
                logging.info(
                    f"ServiceAccount '{service_account_name}' created in namespace '{namespace}'."
                )
            else:
                logging.error(f"Error creating ServiceAccount: {e}")
                raise

        # Create the Role and RoleBinding
        self.create_role_and_binding(namespace, service_account_name)

    def create_role_and_binding(self, namespace, service_account_name):
        rbac_api = client.RbacAuthorizationV1Api()

        role_name = "smartimport-role"
        role_binding_name = "smartimport-rolebinding"

        # Define the Role with updated permissions
        role = client.V1Role(
            metadata=client.V1ObjectMeta(name=role_name, namespace=namespace),
            rules=[
                # Permissions for pods and services
                client.V1PolicyRule(
                    api_groups=[""],
                    resources=[
                        "pods",
                        "services",
                        "configmaps",
                    ],
                    verbs=["get", "watch", "list", "create", "update", "delete"],
                ),
                # Permissions for deployments
                client.V1PolicyRule(
                    api_groups=["apps"],
                    resources=["deployments"],
                    verbs=["get", "watch", "list", "create", "update", "delete"],
                ),
            ],
        )
        try:
            rbac_api.read_namespaced_role(name=role_name, namespace=namespace)
            logging.info(
                f"Role '{role_name}' already exists in namespace '{namespace}'."
            )
        except client.exceptions.ApiException as e:
            if e.status == 404:
                # Create the Role
                rbac_api.create_namespaced_role(namespace=namespace, body=role)
                logging.info(f"Role '{role_name}' created in namespace '{namespace}'.")
            else:
                logging.error(f"Error creating Role: {e}")
                raise

        # Define the RoleBinding
        role_binding = client.V1RoleBinding(
            metadata=client.V1ObjectMeta(name=role_binding_name, namespace=namespace),
            subjects=[
                client.RbacV1Subject(
                    kind="ServiceAccount",
                    name=service_account_name,
                    namespace=namespace,
                )
            ],
            role_ref=client.V1RoleRef(
                kind="Role",
                name=role_name,
                api_group="rbac.authorization.k8s.io",
            ),
        )

        # Check if the RoleBinding exists
        try:
            rbac_api.read_namespaced_role_binding(
                name=role_binding_name, namespace=namespace
            )
            logging.info(
                f"RoleBinding '{role_binding_name}' already exists in namespace '{namespace}'."
            )
        except client.exceptions.ApiException as e:
            if e.status == 404:
                # Create the RoleBinding
                rbac_api.create_namespaced_role_binding(
                    namespace=namespace, body=role_binding
                )
                logging.info(
                    f"RoleBinding '{role_binding_name}' created in namespace '{namespace}'."
                )
            else:
                logging.error(f"Error creating RoleBinding: {e}")
                raise

    def apply_pod_manager_yaml(self, namespace):
        try:
            config.load_kube_config()
        except config.ConfigException:
            config.load_incluster_config()

        k8s_client = client.ApiClient()
        yaml_file = os.path.join(
            os.path.dirname(__file__), "..", "managers", "pod_manager_deployment.yml"
        )
        yaml_file = os.path.abspath(yaml_file)

        logging.info(f"Applying {yaml_file} in namespace {namespace}")

        try:
            utils.create_from_yaml(k8s_client, yaml_file, namespace=namespace)
            logging.info(f"Successfully applied {yaml_file}")
        except utils.FailToCreateError as failure:
            for err in failure.api_exceptions:
                if err.status == 409:
                    logging.info(f"Resource already exists: {err.reason}")
                else:
                    logging.error(f"Error creating resource: {err}")
                    raise
        except Exception as e:
            logging.error(f"An unexpected error occurred: {e}")
            raise

    def configure_pod_manager_url(self, namespace):
        service_name = "pod-manager-service"
        url = self.get_loadbalancer_url(service_name, namespace)
        if url:
            pod_manager_url_local = f"http://{url}:8000"
            env_file_path = os.path.join(os.getcwd(), ".env")

            # Read existing .env file variables
            env_vars = {}
            if os.path.exists(env_file_path):
                with open(env_file_path, "r") as env_file:
                    for line in env_file:
                        if line.strip():
                            key, value = line.strip().split("=", 1)
                            env_vars[key] = value.strip('"')

            # Update or add POD_MANAGER_URL
            env_vars["POD_MANAGER_URL"] = pod_manager_url_local

            # Write back to .env file
            with open(env_file_path, "w") as env_file:
                for key, value in env_vars.items():
                    env_file.write(f'{key}="{value}"\n')

            logging.info(
                f"Pod manager URL updated in .env file: {pod_manager_url_local}"
            )
        else:
            logging.error("Failed to retrieve the pod_manager_url.")

    def get_loadbalancer_url(self, service_name, namespace):
        try:
            config.load_kube_config()
        except config.ConfigException:
            config.load_incluster_config()

        v1 = client.CoreV1Api()
        try:
            service = v1.read_namespaced_service(name=service_name, namespace=namespace)
            ingress = service.status.load_balancer.ingress
            if ingress:
                ip = ingress[0].ip
                hostname = ingress[0].hostname
                logging.info(f"ip: {ip}, host: {hostname}")
                if ip:
                    return ip
                elif hostname:
                    return hostname
            return None
        except client.exceptions.ApiException as e:
            logging.error(f"Error retrieving LoadBalancer URL: {e}")
            return None

    @staticmethod
    @hookimpl
    def jac_import(
        target: str,
        base_path: str,
        absorb: bool,
        cachable: bool,
        mdl_alias: Optional[str],
        override_name: Optional[str],
        lng: Optional[str],
        items: Optional[dict[str, Union[str, Optional[str]]]],
        reload_module: Optional[bool],
    ) -> tuple[types.ModuleType, ...]:
        """Core Import Process with Kubernetes Pod Integration."""
        from jaclang.runtimelib.importer import (
            ImportPathSpec,
            JacImporter,
            PythonImporter,
        )
        from jaclang.runtimelib.machine import JacMachine, JacProgram
        from jaclang.settings import settings

        if (
            target in settings.module_config
            and settings.module_config[target]["load_type"] == "remote"
        ):
            pod_manager_url = os.getenv("POD_MANAGER_URL")
            proxy = ModuleProxy(pod_manager_url)
            remote_module_proxy = proxy.get_module_proxy(
                module_name=target, module_config=settings.module_config[target]
            )
            logging.info(f"Loading remote module {remote_module_proxy}")
            return (remote_module_proxy,)

        spec = ImportPathSpec(
            target,
            base_path,
            absorb,
            cachable,
            mdl_alias,
            override_name,
            lng,
            items,
        )

        jac_machine = JacMachine.get(base_path)
        if not jac_machine.jac_program:
            jac_machine.attach_program(JacProgram(mod_bundle=None, bytecode=None))

        if lng == "py":
            import_result = PythonImporter(JacMachine.get()).run_import(spec)
        else:
            import_result = JacImporter(JacMachine.get()).run_import(
                spec, reload_module
            )

        return (
            (import_result.ret_mod,)
            if absorb or not items
            else tuple(import_result.ret_items)
        )
