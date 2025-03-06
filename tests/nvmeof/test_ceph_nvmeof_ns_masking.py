import json
from copy import deepcopy

from ceph.ceph import Ceph
from ceph.ceph_admin import CephAdmin
from ceph.ceph_admin.common import fetch_method
from ceph.nvmegw_cli import NVMeGWCLI
from ceph.nvmeof.initiator import Initiator
from ceph.utils import get_node_by_id
from tests.rbd.rbd_utils import initial_rbd_config
from utility.log import Log
from utility.retry import retry
from utility.utils import generate_unique_id, run_fio

LOG = Log(__name__)


def validate_namespace_masking(
    nsid,
    subnqn,
    namespaces_sub,
    hostnqn_dict,
    ns_visibility,
    command,
    expected_visibility,
):
    """Validate that the namespace visibility is correct."""

    if command == "add_host":
        LOG.info(command)
        # Calculate the number of namespaces per node
        num_namespaces_per_node = namespaces_sub // len(hostnqn_dict)

        # Determine the node responsible for this nsid based on the calculated range
        node_index = (
            nsid - 1
        ) // num_namespaces_per_node  # Get the node index based on nsid
        expected_host = list(hostnqn_dict.values())[
            node_index
        ]  # Get the expected host NQN

        # Log the visibility of the namespace
        if expected_host in ns_visibility:
            LOG.info(
                f"Validated - Namespace {nsid} of {subnqn} has the correct nqn {ns_visibility}"
            )
        else:
            LOG.error(
                f"Namespace {nsid} of {subnqn} has incorrect NQN. Expected {expected_host}, but got {ns_visibility}"
            )
            raise Exception(
                f"Namespace {nsid} of {subnqn} has incorrect NQN. Expected {expected_host}, but got {ns_visibility}"
            )

    elif command == "del_host":
        LOG.info(command)
        # Calculate the number of namespaces per node
        num_namespaces_per_node = namespaces_sub // len(hostnqn_dict)

        # Determine the node responsible for this nsid based on the calculated range
        node_index = (
            nsid - 1
        ) // num_namespaces_per_node  # Get the node index based on nsid
        expected_host = list(hostnqn_dict.values())[
            node_index
        ]  # Get the expected host NQN

        # Log the visibility of the namespace
        if expected_host not in ns_visibility:
            LOG.info(
                f"Validated - Namespace {nsid} of {subnqn} does not has {expected_host}"
            )
        else:
            LOG.error(
                f"Namespace {nsid} of {subnqn} has {ns_visibility} which was removed"
            )
            raise Exception(
                f"Namespace {nsid} of {subnqn} has incorrect NQN. Not expecting {ns_visibility} in {expected_host}"
            )

    else:
        # Validate visibility based on the expected value (for non-add/del host commands)
        ns_visibility = str(ns_visibility)
        LOG.info(command)
        if ns_visibility.lower() == expected_visibility.lower():
            LOG.info(
                f"Validated - Namespace {nsid} has correct visibility: {ns_visibility}"
            )
        else:
            LOG.info("esle")
            LOG.error(
                f"Namespace {nsid} of {subnqn} has incorrect visibility. Expected {expected_visibility}, but got {ns_visibility}"
            )
            raise Exception(
                f"Namespace {nsid} of {subnqn} has incorrect visibility. Expected {expected_visibility}, but got {ns_visibility}"
            )


def add_namespaces(config, _cls, command, hostnqn_dict, rbd_obj):
    subsystems = config["args"].pop("subsystems")
    namespaces = config["args"].pop("namespaces")
    image_size = config["args"].pop("image_size")
    group = config["args"].pop("group", None)
    pool = config["args"].pop("pool")
    no_auto_visible = config["args"].pop("no-auto-visible")
    namespaces_sub = int(namespaces / subsystems)

    base_cmd_args = {"format": "json"}
    subnqn_template = "nqn.2016-06.io.spdk:cnode{}"
    expected_visibility = "False" if no_auto_visible == "true" else "True"
    # Iterate over subsystems
    for sub_num in range(1, subsystems + 1):
        # Update the initiator configuration
        subnqn = f"{subnqn_template.format(sub_num)}{f'.{group}' if group else ''}"
        name = generate_unique_id(length=4)
        LOG.info(f"Subsystem {sub_num}/{subsystems}")

        # Iterate over namespaces for each subsystem
        for num in range(1, namespaces_sub + 1):
            # Create the image
            rbd_obj.create_image(pool, f"{name}-image{num}", image_size)
            LOG.info(f"Creating image {name}-image{num}/{namespaces}")

            # Prepare the config for namespace creation

            config = {
                "base_cmd_args": {"format": "json"},
                "args": {
                    "rbd-image": f"{name}-image{num}",
                    "rbd-pool": pool,
                    "subsystem": subnqn,
                },
            }
            if no_auto_visible:
                config["args"]["no-auto-visible"] = ""
                expected_visibility = "false"

            # create the namespace
            namespace_func = fetch_method(_cls, command)
            _, namespaces_response = namespace_func(**config)
            nsid = json.loads(namespaces_response)["nsid"]

            # listing namespace
            _config = {
                "base_cmd_args": base_cmd_args,
                "args": {
                    "nsid": nsid,
                    "subsystem": subnqn,
                },
            }

            # list namespaces and check visibility
            namespace_list = fetch_method(_cls, "list")
            _, namespace_response = namespace_list(**_config)
            ns_visibility = json.loads(namespace_response)["namespaces"][0][
                "auto_visible"
            ]
            LOG.info(ns_visibility)
            validate_namespace_masking(
                num,
                subnqn,
                namespaces_sub,
                hostnqn_dict,
                ns_visibility,
                command,
                expected_visibility,
            )


def add_host(config, _cls, command, hostnqn_dict):
    """Add host NQN to namespaces"""
    subsystems = config["args"].pop("subsystems")
    namespaces = config["args"].pop("namespaces")
    group = config["args"].pop("group", None)
    LOG.info(hostnqn_dict)
    expected_visibility = False

    namespaces_sub = int(namespaces / subsystems)
    subnqn_template = "nqn.2016-06.io.spdk:cnode{}"
    num_namespaces_per_initiator = namespaces_sub // len(hostnqn_dict)

    # Iterate over each subsystem
    for sub_num in range(1, subsystems + 1):
        LOG.info(sub_num)

        # Iterate over each namespace in the current subsystem
        for num in range(1, namespaces_sub + 1):
            LOG.info(num)

            # Clear previous args and set up a new configuration
            config["args"].clear()

            # Create the Subsystem NQN with the optional group
            subnqn = f"{subnqn_template.format(sub_num)}{f'.{group}' if group else ''}"

            # Determine the correct host for the current namespace `num`
            for i, node in enumerate(
                hostnqn_dict.keys(), start=1
            ):  # Iterate over hostnqn_dict
                # Calculate the range of namespaces this node should handle
                if num in range(
                    (i - 1) * num_namespaces_per_initiator + 1,
                    i * num_namespaces_per_initiator + 1,
                ):
                    host = hostnqn_dict[node]
                    break  # Exit once the correct node is found

            config = {
                "base_cmd_args": {"format": "json"},
                "args": {
                    "nsid": num,
                    "host": host,
                    "subsystem": subnqn,
                },
            }

            namespace_func = fetch_method(_cls, command)
            _, namespaces = namespace_func(**config)

            _config = {
                "base_cmd_args": {"format": "json"},
                "args": {
                    "nsid": num,
                    "subsystem": subnqn,
                },
            }
            # list namespaces and check visibility
            namespace_list = fetch_method(_cls, "list")
            _, namespace_response = namespace_list(**_config)
            ns_visibility = json.loads(namespace_response)["namespaces"][0]["hosts"]
            validate_namespace_masking(
                num,
                subnqn,
                namespaces_sub,
                hostnqn_dict,
                ns_visibility,
                command,
                expected_visibility,
            )


def del_host(config, _cls, command, hostnqn_dict):
    """Delete host NQN to namespaces"""
    subsystems = config["args"].pop("subsystems")
    namespaces = config["args"].pop("namespaces")
    group = config["args"].pop("group", None)
    LOG.info(hostnqn_dict)
    namespaces_sub = int(namespaces / subsystems)
    subnqn_template = "nqn.2016-06.io.spdk:cnode{}"
    num_namespaces_per_initiator = namespaces_sub // len(hostnqn_dict)

    for sub_num in range(1, subsystems + 1):
        LOG.info(sub_num)
        for num in range(1, namespaces_sub + 1):
            LOG.info(num)
            config["args"].clear()
            subnqn = f"{subnqn_template.format(sub_num)}{f'.{group}' if group else ''}"

            # Determine the correct host for the current namespace `num`
            for i, node in enumerate(
                hostnqn_dict.keys(), start=1
            ):  # Iterate over hostnqn_dict
                # Calculate the range of namespaces this node should handle
                if num in range(
                    (i - 1) * num_namespaces_per_initiator + 1,
                    i * num_namespaces_per_initiator + 1,
                ):
                    host = hostnqn_dict[node]
                    break  # Exit once the correct node is found

            config = {
                "base_cmd_args": {"format": "json"},
                "args": {
                    "nsid": num,
                    "host": host,
                    "subsystem": subnqn,
                },
            }

            namespace_func = fetch_method(_cls, command)
            _, namespaces = namespace_func(**config)

            _config = {
                "base_cmd_args": {"format": "json"},
                "args": {
                    "nsid": num,
                    "subsystem": subnqn,
                },
            }
            # list namespaces and check visibility
            namespace_list = fetch_method(_cls, "list")
            _, namespace_response = namespace_list(**_config)
            ns_visibility = json.loads(namespace_response)["namespaces"][0]["hosts"]
            expected_visibility = []
            validate_namespace_masking(
                num,
                subnqn,
                namespaces_sub,
                hostnqn_dict,
                ns_visibility,
                command,
                expected_visibility,
            )


def change_visibility(config, _cls, command, hostnqn_dict):
    """Change visibility of namespaces."""
    # Extract parameters from config
    subsystems = config["args"].pop("subsystems")
    namespaces = config["args"].pop("namespaces")
    group = config["args"].pop("group", None)
    auto_visible = config["args"].pop("auto-visible")
    namespaces_sub = int(namespaces / subsystems)

    # Base configuration values
    base_cmd_args = {"format": "json"}
    subnqn_template = "nqn.2016-06.io.spdk:cnode{}"

    # Determine visibility setting for a namespace
    expected_visibility = "True" if auto_visible == "yes" else "False"

    # Iterate over subsystems
    for sub_num in range(1, subsystems + 1):
        # Update the initiator configuration
        subnqn = f"{subnqn_template.format(sub_num)}{f'.{group}' if group else ''}"
        LOG.info(f"Subsystem {sub_num}/{subsystems}")

        # Iterate over namespaces for each subsystem
        for num in range(1, namespaces_sub + 1):
            config["args"].clear()
            config = {
                "base_cmd_args": {"format": "json"},
                "args": {
                    "nsid": num,
                    "subsystem": subnqn,
                    "auto-visible": auto_visible,
                },
            }

            namespace_func = fetch_method(_cls, command)
            _, namespaces = namespace_func(**config)
            # Listing namespace and checking visibility
            _config = {
                "base_cmd_args": base_cmd_args,
                "args": {"nsid": num, "subsystem": subnqn},
            }
            namespace_list = fetch_method(_cls, "list")
            _, namespace_response = namespace_list(**_config)
            ns_visibility = json.loads(namespace_response)["namespaces"][0][
                "auto_visible"
            ]

            # Validate the visibility of the namespace
            validate_namespace_masking(
                num,
                subnqn,
                namespaces_sub,
                hostnqn_dict,
                ns_visibility,
                command,
                expected_visibility,
            )


def run(ceph_cluster: Ceph, **kwargs) -> int:
    LOG.info("Add ns and Configure NS masking")
    config = deepcopy(kwargs["config"])
    node = get_node_by_id(ceph_cluster, config["node"])
    port = config.get("port", 5500)
    rbd_pool = config.get("rbd_pool", "rbd_pool")
    service = config.get("service")

    kwargs["config"].update(
        {
            "do_not_create_image": True,
            "rep-pool-only": True,
            "rep_pool_config": {"pool": rbd_pool},
        }
    )
    rbd_obj = initial_rbd_config(**kwargs)["rbd_reppool"]

    overrides = kwargs.get("test_data", {}).get("custom-config")
    for key, value in dict(item.split("=") for item in overrides).items():
        if key == "nvmeof_cli_image":
            NVMeGWCLI.NVMEOF_CLI_IMAGE = value
            break
    nvmegwcli = NVMeGWCLI(node, port)

    try:
        steps = config.get("steps", [])
        init_nodes = config.get("initiators")
        hostnqn_dict = {}
        LOG.info(init_nodes)

        for node in init_nodes:
            initiator_node = get_node_by_id(ceph_cluster, node)
            initiator = Initiator(initiator_node)
            initiator.disconnect_all()
            hostnqn, _ = initiator_node.exec_command(
                cmd="cat /etc/nvme/hostnqn",
                sudo=True,
            )
            hostnqn = hostnqn.strip()
            hostnqn_dict[node] = hostnqn
        LOG.info("Hostnqn's are: %s", hostnqn_dict)

        for step in steps:
            cfg = step["config"]
            command = cfg.pop("command")

            _cls = fetch_method(nvmegwcli, service)

            if command == "add":
                add_namespaces(cfg, _cls, command, hostnqn_dict, rbd_obj)
            elif command == "add_host":
                add_host(cfg, _cls, command, hostnqn_dict)
            elif command == "del_host":
                del_host(cfg, _cls, command, hostnqn_dict)
            elif command == "change_visibility":
                change_visibility(cfg, _cls, command, hostnqn_dict)
            else:
                func = fetch_method(_cls, command)

                if "args" not in cfg:
                    cfg["args"] = {}

                func(**cfg["args"])

    except BaseException as be:
        LOG.error(be, exc_info=True)
        return 1

    return 0
