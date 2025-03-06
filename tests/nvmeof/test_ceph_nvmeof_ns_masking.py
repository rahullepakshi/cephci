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


def fetch_drive(node, uuid):
    """Fetch Drive from node using UUID."""
    # lsblk -np -r -o "name,wwn"
    out, _ = node.exec_command(cmd=f"lsblk -np -r -o name,wwn | grep {uuid}", sudo=True)
    if out:
        return out.split(" ")[0]


def initiators(ceph_cluster, gateway):
    """Configure NVMe Initiators."""
    client = get_node_by_id(ceph_cluster, config["node"])
    initiator = Initiator(client)
    initiator.disconnect_all()

    connect_cmd_args = {
        "transport": "tcp",
        "traddr": gateway.ip_address,
        "trsvcid": 8009,
    }
    LOG.debug(initiator.connect_all(**connect_cmd_args))
    LOG.debug(initiator.list())


@retry(Exception, tries=5, delay=10)
def run_io(ceph_cluster, ns_uuid, io):
    """Run IO on newly added namespace."""
    LOG.info(io)
    client = get_node_by_id(ceph_cluster, io["node"])
    device_path = fetch_drive(client, ns_uuid)

    if device_path is None:
        raise Exception(f"NVMe volume not found for {ns_uuid}")

    LOG.debug(device_path)
    io_args = {
        "device_name": device_path,
        "client_node": client,
        "run_time": "10",
        "io_type": io["io_type"],
        "long_running": True,
        "cmd_timeout": 600,
    }
    result = run_fio(**io_args)
    if result != 0:
        raise Exception("FIO failure")


def add_namespaces(config, _cls, command, ceph_cluster, node, rbd_obj):
    subsystems = config["args"].pop("subsystems")
    namespaces = config["args"].pop("namespaces")
    image_size = config["args"].pop("image_size")
    group = config["args"].pop("group", None)
    pool = config["args"].pop("pool")
    no_auto_visible = config["args"].pop("no-auto-visible")
    namespaces_sub = int(namespaces / subsystems)

    base_cmd_args = {"format": "json"}
    subnqn_template = "nqn.2016-06.io.spdk:cnode{}"

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
                "Visibility"
            ]

            # Log the visibility of the namespace
            if ns_visibility == "Restrictive":
                LOG.info(f"Validated - {nsid} is Restrictive")
            else:
                LOG.error(f"{nsid} is open to all hosts")  # Log error message
                raise Exception(
                    f"Namespace {nsid} is open to all hosts, which is not allowed."
                )


def add_host(config, _cls, command, hostnqn_dict):
    """Add host NQN to namespaces"""
    subsystems = config["args"].pop("subsystems")
    namespaces = config["args"].pop("namespaces")
    group = config["args"].pop("group", None)
    LOG.info(hostnqn_dict)
    namespaces_sub = int(namespaces / subsystems)
    subnqn_template = "nqn.2016-06.io.spdk:cnode{}"

    for sub_num in range(1, subsystems + 1):
        LOG.info(sub_num)
        for num in range(1, namespaces_sub + 1):
            LOG.info(num)
            config["args"].clear()
            subnqn = f"{subnqn_template.format(sub_num)}{f'.{group}' if group else ''}"
            config = {
                "base_cmd_args": {"format": "json"},
                "args": {
                    "nsid": num,
                    "host": (
                        hostnqn_dict["node1"]
                        if num in [1, 2]
                        else hostnqn_dict["node2"]
                        if num in [3, 4]
                        else hostnqn_dict["node3"]
                        if num in [5, 6]
                        else hostnqn_dict["node4"]
                        if num in [7, 8]
                        else hostnqn_dict["node5"]
                        if num in [9, 10]
                        else None
                    ),
                    "subsystem": subnqn,
                },
            }

            namespace_func = fetch_method(_cls, command)
            _, namespaces = namespace_func(**config)
            nsid = json.loads(namespaces)["nsid"]

            _config = {
                "base_cmd_args": {"format": "json"},
                "args": {
                    "nsid": nsid,
                    "subsystem": subnqn,
                },
            }
            # list namespaces and check visibility
            namespace_list = fetch_method(_cls, "list")
            _, namespace_response = namespace_list(**_config)
            ns_visibility = json.loads(namespace_response)["namespaces"][0][
                "Visibility"
            ]

            expected_host = (
                hostnqn_dict["node1"]
                if nsid in [1, 2]
                else hostnqn_dict["node2"]
                if nsid in [3, 4]
                else hostnqn_dict["node3"]
                if nsid in [5, 6]
                else hostnqn_dict["node4"]
                if nsid in [7, 8]
                else hostnqn_dict["node5"]
                if nsid in [9, 10]
                else None
            )

            # Log the visibility of the namespace
            if ns_visibility == expected_host:
                LOG.info(f"Validated - {nsid} has right nqn {ns_visibility}")
            else:
                LOG.error(
                    f"{nsid} is open to all hosts or incorrect nqn"
                )  # Log error message
                raise Exception(
                    f"Namespace {nsid} is open to all hosts or has incorrect nqn"
                )


def del_host(config, _cls, command, hostnqn_dict):
    """Add host NQN to namespaces"""
    subsystems = config["args"].pop("subsystems")
    namespaces = config["args"].pop("namespaces")
    group = config["args"].pop("group", None)
    LOG.info(hostnqn_dict)
    namespaces_sub = int(namespaces / subsystems)
    subnqn_template = "nqn.2016-06.io.spdk:cnode{}"

    for sub_num in range(1, subsystems + 1):
        LOG.info(sub_num)
        for num in range(1, namespaces_sub + 1):
            LOG.info(num)
            config["args"].clear()
            subnqn = f"{subnqn_template.format(sub_num)}{f'.{group}' if group else ''}"
            config = {
                "base_cmd_args": {"format": "json"},
                "args": {
                    "nsid": num,
                    "host": (
                        hostnqn_dict["node1"]
                        if num in [1, 2]
                        else hostnqn_dict["node2"]
                        if num in [3, 4]
                        else hostnqn_dict["node3"]
                        if num in [5, 6]
                        else hostnqn_dict["node4"]
                        if num in [7, 8]
                        else hostnqn_dict["node5"]
                        if num in [9, 10]
                        else None
                    ),
                    "subsystem": subnqn,
                },
            }

            namespace_func = fetch_method(_cls, command)
            _, namespaces = namespace_func(**config)
            nsid = json.loads(namespaces)["nsid"]

            _config = {
                "base_cmd_args": {"format": "json"},
                "args": {
                    "nsid": nsid,
                    "subsystem": subnqn,
                },
            }
            # list namespaces and check visibility
            namespace_list = fetch_method(_cls, "list")
            _, namespace_response = namespace_list(**_config)
            ns_visibility = json.loads(namespace_response)["namespaces"][0][
                "Visibility"
            ]

            # Log the visibility of the namespace
            if ns_visibility == "Restrictive":
                LOG.info(f"Validated - {nsid} is Restrictive")
            else:
                LOG.error(f"{nsid} is open to all hosts")  # Log error message
                raise Exception(
                    f"Namespace {nsid} is open to all hosts, which is not allowed."
                )


def change_visibility(config, _cls, command):
    """change_visibility of namespaces"""
    subsystems = config["args"].pop("subsystems")
    namespaces = config["args"].pop("namespaces")
    group = config["args"].pop("group", None)
    auto_visible = config["args"].pop("auto-visible")
    namespaces_sub = int(namespaces / subsystems)

    base_cmd_args = {"format": "json"}
    subnqn_template = "nqn.2016-06.io.spdk:cnode{}"

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
            config["args"] = {
                "nsid": {num},
                "subsystem": subnqn,
            }
            if auto_visible:
                config["args"]["auto-visible"] = ""

            # change visibility of the namespace
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
                "Visibility"
            ]

            # Log the visibility of the namespace
            if ns_visibility == "All Hosts":
                LOG.info(f"Validated - {nsid} is open to all hosts")
            else:
                LOG.error(
                    f"Namespace {nsid} of {subnqn} is open to all hosts"
                )  # Log error message
                raise Exception(f"Namespace {nsid} of {subnqn} is restrictive.")


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

        for node in init_nodes:
            initiator_node = get_node_by_id(ceph_cluster, node)
            LOG.info(initiator_node)
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
                add_namespaces(cfg, _cls, command, ceph_cluster, node, rbd_obj)
            elif command == "add_host":
                add_host(cfg, _cls, command, hostnqn_dict)
            elif command == "del_host":
                del_host(cfg, _cls, command, hostnqn_dict)
            elif command == "change_visibility":
                change_visibility(cfg, _cls, command)
            else:
                func = fetch_method(_cls, command)

                if "args" not in cfg:
                    cfg["args"] = {}

                func(**cfg["args"])

    except BaseException as be:
        LOG.error(be, exc_info=True)
        return 1

    return 0
