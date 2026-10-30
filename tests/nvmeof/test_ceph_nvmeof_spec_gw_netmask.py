"""
E2E for NVMeoF spec ``listener_network_masks`` (group-level auto listeners).

A subsystem keeps using its own ``network_mask``, or it opts into the masks
published on the gateway-group spec. The two sources are mutually exclusive,
and both are mutually exclusive with manual listeners.

Flow (requires >= 4 NVMeoF gateways, each with two IPv4 subnets):
  1. Apply an nvmeof spec whose listener_network_masks list is mask A
     (primary and secondary subnets). Orch and ceph-nvmeof.conf accept it.
  2. subsystem add --use-group-config-network-masks creates cnode1.
     subsystem add without that flag creates cnode2 (the flag stays false).
  3. subsystem list shows the effective network_mask. JSON and YAML include
     use_group_config_network_masks. The table does not.
  4. set_use_group_config_network_masks true makes cnode2 follow mask A.
  5. Reapply the spec with mask B (secondary subnet plus a non-matching CIDR).
     cnode1 and cnode2 move to mask B: mask A listeners are removed, mask B
     listeners are created, and the non-matching CIDR adds nothing.
  6. set_use_group_config_network_masks false on cnode2. The flag is false,
     cnode2 stops following the group masks, and manual listeners are unchanged.
  7. subsystem list --format json and --format yaml match that final state.

Scoped refresh and gateway scale run after the mask B reapply. They restore
the mask B listeners before cnode2 opts out. FIO runs while mask A is still
applied, because discovery uses the primary gateway address.

A subsystem that owns its own network_mask, and a subsystem that has a manual
listener, stay on that behavior through the spec change and the opt-out.
"""

import ipaddress
import json
import time
from copy import deepcopy
from types import SimpleNamespace

import yaml
from looseversion import LooseVersion

from ceph.ceph import Ceph, CommandFailed
from ceph.ceph_admin.orch import Orch
from ceph.parallel import parallel
from ceph.utils import get_node_by_id, get_nodes_by_ids
from tests.nvmeof.workflows.gateway_entities import (
    configure_hosts,
    configure_namespaces,
    disconnect_initiators,
    teardown,
)
from tests.nvmeof.workflows.initiator import NVMeInitiator
from tests.nvmeof.workflows.nvme_service import NVMeService
from tests.nvmeof.workflows.nvme_utils import (
    check_and_set_nvme_cli_image,
    discover_gateway_network_roles,
    list_listeners,
    refresh_gateway_network,
    secondary_iface_state_on_gateways,
)
from tests.rbd.rbd_utils import initial_rbd_config
from utility.log import Log
from utility.utils import get_ceph_version_from_cluster, run_fio

LOG = Log(__name__)

_ROLES = (
    "spec-opt-in-at-create",
    "opt-in-existing-then-out",
    "subsystem-network-mask",
    "manual-listener",
)
_PROBE_NQN = "nqn.2016-06.io.spdk:cnode-mask-exclusive"
_GROUP_FLAG_NAMES = (
    "use_group_config_network_masks",
    "use-group-config-network-masks",
)


def _as_bool(value):
    """Normalize a CLI boolean."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    raise RuntimeError(f"Cannot read boolean from {value!r}")


def _canon_mask(mask):
    """Return the canonical network address for a CIDR."""
    return str(ipaddress.ip_network(str(mask).strip().strip("\"'"), strict=False))


def _normalize_masks(value):
    """Flatten CLI/spec/conf mask values to a sorted unique CIDR list."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        items = list(value)
    else:
        text = str(value).strip()
        if not text or text in ("[]", "{}"):
            return []
        if text.startswith("[") and text.endswith("]"):
            text = text[1:-1]
        items = [part.strip() for part in text.split(",")]
    masks = []
    for item in items:
        if isinstance(item, (list, tuple)):
            masks.extend(_normalize_masks(item))
            continue
        text = str(item).strip().strip("\"'")
        if not text or text in ("[]", "{}"):
            continue
        masks.append(_canon_mask(text))
    return sorted(set(masks))


def _ips_covered_by(masks, ips):
    """Return the IPs that fall inside any of ``masks``."""
    nets = [
        ipaddress.ip_network(mask, strict=False) for mask in _normalize_masks(masks)
    ]
    covered = []
    for ip in ips:
        addr = ipaddress.ip_address(ip)
        if any(addr in net for net in nets):
            covered.append(ip)
    return sorted(set(covered))


def _is_infra_failure(text):
    low = text.lower()
    return any(
        token in low
        for token in (
            "failed to connect to all addresses",
            "execution time",
            "invalid packet",
            "timed out",
            "timeout",
            "no route to host",
            "connection refused",
        )
    )


def _is_unknown_cli(text):
    low = text.lower()
    return any(
        token in low
        for token in (
            "invalid choice",
            "no such command",
            "unknown command",
            "unrecognized arguments",
            "unexpected argument",
            "no such option",
            "got an unexpected keyword",
        )
    )


def _looks_like_cli_error(text):
    low = text.lower()
    if "success" in low and not any(
        token in low for token in ("fail", "error", "invalid")
    ):
        return False
    return any(
        token in low
        for token in (
            "fail",
            "error",
            "invalid",
            "not allowed",
            "exclusive",
            "cannot",
            "can't",
        )
    )


def _assert_mask_exclusion(text, context):
    """Rejection must describe network_mask vs spec-mask mutual exclusion."""
    low = text.lower()
    mentions_mask = any(
        token in low for token in ("network_mask", "network-mask", "network mask")
    )
    mentions_group = any(
        token in low
        for token in (
            "use_group_config_network_masks",
            "use-group-config-network-masks",
            "group config network",
            "group-config-network",
            "group_config_network",
        )
    )
    mentions_conflict = any(
        token in low
        for token in ("exclusive", "not allowed", "cannot", "can't", "conflict", "both")
    )
    if mentions_conflict and (mentions_mask or mentions_group):
        return
    if mentions_mask and mentions_group:
        return
    raise RuntimeError(
        f"{context}: rejection did not describe network-mask / spec-mask "
        f"mutual exclusion: {text}"
    )


def _assert_manual_exclusion(text, context):
    """Rejection must describe manual listeners vs auto listeners."""
    low = text.lower()
    mentions_manual = any(
        token in low
        for token in ("manual", "auto-listener", "auto listener", "auto_listener")
    )
    mentions_conflict = any(
        token in low
        for token in (
            "exclusive",
            "cannot",
            "can't",
            "not allowed",
            "already",
            "in use",
        )
    )
    if mentions_manual and mentions_conflict:
        return
    raise RuntimeError(
        f"{context}: rejection did not describe manual/auto listener "
        f"mutual exclusion: {text}"
    )


def _assert_listener_del_blocked(text, context):
    """listener del must refuse spec auto-listeners and say how to turn them off."""
    low = text.lower()
    mentions_feature = any(
        token in low
        for token in (
            "use_group_config_network_masks",
            "use-group-config-network-masks",
            "group config network",
            "group-config-network",
            "group_config_network",
            "set_use_group_config_network_masks",
        )
    )
    mentions_off = any(
        token in low
        for token in (
            "toggle",
            "disable",
            "turn off",
            "switch off",
            "opt-out",
            "opt out",
            "set_use_group_config_network_masks",
        )
    )
    if mentions_feature and mentions_off:
        return
    raise RuntimeError(
        f"{context}: listener del was rejected without telling the user to turn "
        f"off use-group-config-network-masks: {text}"
    )


def _conf_listener_masks(text):
    """Parse ``listener_network_masks`` from ceph-nvmeof.conf text.

    Prefer the ``[gateway]`` assignment. Return None when the key is absent.
    """
    section = ""
    found = []
    gateway_value = None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]") and "=" not in line:
            section = line[1:-1].strip().lower()
            continue
        if not line or line.startswith(("#", ";")):
            continue
        if "=" in line:
            key, value = line.split("=", 1)
        elif ":" in line:
            key, value = line.split(":", 1)
        else:
            continue
        if key.strip() != "listener_network_masks":
            continue
        masks = _normalize_masks(value)
        found.append(masks)
        if section == "gateway":
            gateway_value = masks
    if gateway_value is not None:
        return gateway_value
    if found:
        return found[-1]
    return None


def _orch_listener_masks(service):
    """Return the orch service's listener_network_masks, or None if absent."""
    spec = service.get("spec") if isinstance(service, dict) else None
    if isinstance(spec, dict) and "listener_network_masks" in spec:
        return spec["listener_network_masks"]
    inner = spec.get("spec") if isinstance(spec, dict) else None
    if isinstance(inner, dict) and "listener_network_masks" in inner:
        return inner["listener_network_masks"]
    return None


class SpecGatewayNetmaskE2E:
    """Drive the spec gateway-netmask scenarios against one deployed group."""

    def __init__(self, ceph_cluster, config, rbd_obj):
        self.ceph_cluster = ceph_cluster
        self.config = config
        self.rbd_obj = rbd_obj
        self.nvme_service = None
        self.networks = None
        self.primary_mask = None
        self.secondary_mask = None
        self.mask_a = []
        self.mask_b = []
        self.spec_masks = []
        self.manual_signature = None
        self.by_role = {}
        self.port = 4420
        self.original_gw_nodes = list(config.get("gw_nodes") or [])
        self.secondary_down = False
        self._closed = False

    @property
    def gateway(self):
        """CLI gateway. Subsystem state is group-wide; any live GW can query it."""
        if not self.nvme_service or not self.nvme_service.gateways:
            raise RuntimeError("NVMeoF gateways are not initialised")
        return self.nvme_service.gateways[0]

    def execute(self):
        """Run the spec-mask flow, then the exclusion and refresh checks."""
        self._prepare()
        self._deploy()
        self._tc04_opt_in_at_create()
        self._tc03_subsystem_without_mask()
        self._verify_opted_in_and_default()
        self._tc08_mask_sources_are_exclusive()
        self._tc09_manual_listeners_are_exclusive()
        self._tc05_opt_in_existing()
        # Discovery uses the primary gateway address, which mask A still covers.
        self._tc18_io()
        self._reapply_mask_b()
        self._tc06_listener_del_rejected()
        self._tc07_reject_both_flags_at_create()
        self._tc14_15_scoped_refresh()
        self._tc17_scale()
        self._tc10_opt_out()
        self._final_subsystem_list()

    def close(self):
        """Restore secondary IPv4s and the original gateway placement."""
        if self._closed:
            return
        self._closed = True
        if self.secondary_down and self.nvme_service and self.networks:
            try:
                LOG.info("Restoring secondary IPv4s after spec-netmask E2E")
                secondary_iface_state_on_gateways(
                    self.nvme_service.gateways, self.networks, state="up"
                )
            except Exception as exc:  # noqa: BLE001
                LOG.warning("Secondary IPv4 restore failed: %s", exc)
            self.secondary_down = False
        if not self.nvme_service:
            return
        current = list(self.nvme_service.config.get("gw_nodes") or [])
        if current != list(self.original_gw_nodes):
            try:
                LOG.info("Restoring NVMeoF placement to %s", self.original_gw_nodes)
                self._redeploy(self.original_gw_nodes)
            except Exception as exc:  # noqa: BLE001
                LOG.warning("Gateway placement restore failed: %s", exc)
            self.config["gw_nodes"] = list(self.original_gw_nodes)
            self.nvme_service.config["gw_nodes"] = list(self.original_gw_nodes)

    def _nqn(self, role):
        return self.by_role[role]["nqn"]

    def _prepare(self):
        """Discover GW subnets and index the four subsystem roles."""
        if len(self.original_gw_nodes) < 4:
            raise ValueError(
                "spec gateway-netmask E2E needs at least 4 nvmeof gateways, "
                f"got {self.original_gw_nodes}"
            )
        subsystems = list(self.config.get("subsystems") or [])
        if len(subsystems) < 4:
            raise ValueError(
                "spec gateway-netmask E2E needs at least 4 subsystems, "
                f"got {len(subsystems)}"
            )
        version = get_ceph_version_from_cluster(
            self.ceph_cluster.get_nodes(role="client")[0]
        )
        if LooseVersion(version) < LooseVersion("20.2.1"):
            raise RuntimeError(
                "Spec listener_network_masks requires ceph >= 20.2.1 "
                f"(found {version})"
            )

        by_role = {}
        for sub in subsystems:
            role = sub.get("role")
            if role in _ROLES:
                by_role[role] = sub
        unused = [sub for sub in subsystems if sub.get("role") not in _ROLES]
        for role in _ROLES:
            if role not in by_role:
                if not unused:
                    raise ValueError(
                        f"Subsystem role {role} is not configured and no extra "
                        "subsystem is left to assign"
                    )
                by_role[role] = unused.pop(0)
        self.by_role = by_role
        self.port = int(by_role[_ROLES[0]].get("listener_port", 4420))

        gw_nodes = get_nodes_by_ids(self.ceph_cluster, self.original_gw_nodes)
        spec_cfg = self.config.get("spec_gw_netmask") or {}
        roles = discover_gateway_network_roles(
            [SimpleNamespace(node=node) for node in gw_nodes],
            secondary_iface=spec_cfg.get("secondary_iface"),
        )
        self.primary_mask = (
            spec_cfg.get("primary_network_mask") or roles["primary_mask"]
        )
        self.secondary_mask = (
            spec_cfg.get("secondary_network_mask") or roles["secondary_mask"]
        )
        if not self.primary_mask or not self.secondary_mask:
            raise RuntimeError(
                "Unable to derive primary and secondary network masks from the "
                f"gateway IPv4 addresses (primary={self.primary_mask}, "
                f"secondary={self.secondary_mask})"
            )
        primary_net = ipaddress.ip_network(self.primary_mask, strict=False)
        secondary_net = ipaddress.ip_network(self.secondary_mask, strict=False)
        if primary_net.overlaps(secondary_net):
            raise RuntimeError(
                f"Primary mask {primary_net} overlaps secondary mask {secondary_net}. "
                "Set spec_gw_netmask.primary_network_mask and "
                "secondary_network_mask to two non-overlapping CIDRs."
            )
        self.networks = roles
        # Mask A is the two-subnet list applied at deploy. Mask B replaces it.
        self.mask_a = [self.primary_mask, self.secondary_mask]
        self.mask_b = [self.secondary_mask, self._dummy_mask()]
        self.spec_masks = list(self.mask_a)
        LOG.info(
            "Discovered spec-netmask networks: primary=%s %s secondary=%s %s",
            self.primary_mask,
            roles["primary_ips"],
            self.secondary_mask,
            roles["secondary_ips"],
        )

    def _deploy(self):
        """Apply listener_network_masks mask A and deploy the gateway group."""
        LOG.info(
            "Apply nvmeof spec listener_network_masks=%s (mask A)", self.spec_masks
        )
        self.config["spec_deployment"] = True
        self.config["nvmeof_spec"] = {
            "listener_network_masks": list(self.spec_masks),
        }
        self.nvme_service = NVMeService(self.config, self.ceph_cluster)
        if self.config.get("install", True):
            self.nvme_service.deploy()
        else:
            self.nvme_service.apply_nvmeof_spec(redeploy=True, wait_sec=60)
        self.nvme_service.init_gateways()
        self.nvme_service.wait_for_gateways_ready()
        if len(self.nvme_service.gateways) < 4:
            raise RuntimeError(
                "Need at least 4 live NVMeoF gateways after deploy, "
                f"found {len(self.nvme_service.gateways)}"
            )
        LOG.info("TC02 Validate orch spec and gateway ceph-nvmeof.conf")
        self._assert_published_masks(self.spec_masks)

    def _tc03_subsystem_without_mask(self):
        sub = self.by_role["opt-in-existing-then-out"]
        nqn = sub["nqn"]
        LOG.info("TC03 Create %s with no network mask and no spec opt-in", nqn)
        self._add_subsystem(sub)
        self._assert_state(nqn, opted_in=False, masks=[], context="TC03")
        self._assert_listeners(nqn, [], context="TC03 no listeners")

    def _tc04_opt_in_at_create(self):
        sub = self.by_role["spec-opt-in-at-create"]
        nqn = sub["nqn"]
        LOG.info("TC04 subsystem add --use-group-config-network-masks for %s", nqn)
        try:
            self._add_subsystem(sub, use_group_masks=True)
        except CommandFailed as exc:
            if _is_unknown_cli(str(exc)):
                raise RuntimeError(
                    "This ceph nvmeof build does not accept "
                    "--use-group-config-network-masks. listener_network_masks "
                    f"support is required. CLI error: {exc}"
                ) from exc
            raise
        self._assert_state(nqn, opted_in=True, masks=self.spec_masks, context="TC04")
        self._assert_listeners(
            nqn,
            self._expected_ips(self.spec_masks),
            manual=False,
            context="TC04 spec auto listeners",
        )
        self._assert_table_hides_flag(nqn, self.spec_masks)

    def _tc05_opt_in_existing(self):
        nqn = self._nqn("opt-in-existing-then-out")
        LOG.info("TC05 set_use_group_config_network_masks true on %s", nqn)
        self._set_group(nqn, True)
        self._assert_state(nqn, opted_in=True, masks=self.spec_masks, context="TC05")
        self._assert_listeners(
            nqn,
            self._expected_ips(self.spec_masks),
            manual=False,
            context="TC05 opt-in existing",
        )

    def _verify_opted_in_and_default(self):
        """subsystem list: effective mask, JSON/YAML flag, table hides the flag."""
        opted = self._nqn("spec-opt-in-at-create")
        default = self._nqn("opt-in-existing-then-out")
        LOG.info("Verify subsystem list for opted-in %s and default %s", opted, default)
        self._assert_state(
            opted, opted_in=True, masks=self.mask_a, context="cnode1 after add"
        )
        self._assert_listeners(
            opted,
            self._expected_ips(self.mask_a),
            manual=False,
            context="cnode1 auto listeners for mask A",
        )
        self._assert_state(default, opted_in=False, masks=[], context="cnode2 default")
        self._assert_listeners(default, [], context="cnode2 has no group listeners")
        self._assert_table_hides_flag(opted, self.mask_a)

    def _tc06_listener_del_rejected(self):
        nqn = self._nqn("spec-opt-in-at-create")
        LOG.info("TC06 listener del of a spec auto-listener on %s is rejected", nqn)
        before = self._listener_ips(nqn)
        listener = self._listeners(nqn)[0]

        def _delete():
            args = {
                "subsystem": nqn,
                "traddr": listener.get("traddr"),
                "trsvcid": listener.get("trsvcid", self.port),
            }
            host = (
                listener.get("host_name")
                or listener.get("host-name")
                or listener.get("hostname")
            )
            if host:
                args["host-name"] = host
            return self.gateway.listener.delete(**{"args": args})

        self._expect_rejected(
            _delete, "TC06 listener del", _assert_listener_del_blocked
        )
        after = self._listener_ips(nqn)
        if after != before:
            raise RuntimeError(
                f"TC06 listener del changed listeners on {nqn}: {before} -> {after}"
            )
        LOG.info("TC06 listeners unchanged after rejected listener del: %s", after)

    def _tc07_reject_both_flags_at_create(self):
        LOG.info("TC07 subsystem add with network-mask and spec opt-in is rejected")
        sub = {
            "nqn": _PROBE_NQN,
            "serial": 99,
            "listener_port": self.port,
        }
        try:
            self._expect_rejected(
                lambda: self._add_subsystem(
                    sub,
                    use_group_masks=True,
                    network_mask=self.primary_mask,
                ),
                "TC07 add with both mask sources",
                _assert_mask_exclusion,
            )
            self._assert_absent(_PROBE_NQN)
        finally:
            self._delete_if_present(_PROBE_NQN)

    def _tc08_mask_sources_are_exclusive(self):
        sub = self.by_role["subsystem-network-mask"]
        nqn = sub["nqn"]
        LOG.info("TC08 Create %s with subsystem network_mask only", nqn)
        self._add_subsystem(sub, network_mask=self.primary_mask)
        self._assert_state(
            nqn, opted_in=False, masks=[self.primary_mask], context="TC08 create"
        )
        self._assert_listeners(
            nqn,
            self._expected_ips([self.primary_mask]),
            manual=False,
            context="TC08 subsystem network_mask listeners",
        )
        LOG.info("TC08 set_use_group_config_network_masks true on %s is rejected", nqn)
        self._expect_rejected(
            lambda: self._set_group(nqn, True),
            "TC08 opt-in while network_mask is set",
            _assert_mask_exclusion,
        )
        self._assert_state(
            nqn, opted_in=False, masks=[self.primary_mask], context="TC08 after reject"
        )

        opted = self._nqn("spec-opt-in-at-create")
        LOG.info("TC08 add_network on opted-in %s is rejected", opted)
        before = self._listener_ips(opted)
        self._expect_rejected(
            lambda: self.gateway.subsystem.add_network(
                **{"args": {"subsystem": opted, "network-mask": self.secondary_mask}}
            ),
            "TC08 add_network on spec-opted subsystem",
            _assert_mask_exclusion,
        )
        self._assert_state(
            opted,
            opted_in=True,
            masks=self.spec_masks,
            context="TC08 opted-in unchanged",
        )
        if self._listener_ips(opted) != before:
            raise RuntimeError(f"TC08 add_network changed listeners on {opted}")

    def _tc09_manual_listeners_are_exclusive(self):
        sub = self.by_role["manual-listener"]
        nqn = sub["nqn"]
        gw = self.gateway
        LOG.info("TC09 Create %s and add one manual listener", nqn)
        self._add_subsystem(sub)
        gw.listener.add(
            **{
                "args": {
                    "subsystem": nqn,
                    "traddr": gw.node.ip_address,
                    "trsvcid": self.port,
                    "host-name": gw.node.hostname,
                }
            }
        )
        self._assert_state(nqn, opted_in=False, masks=[], context="TC09 manual")
        self._assert_listeners(
            nqn,
            [gw.node.ip_address],
            manual=True,
            context="TC09 manual listener",
        )
        LOG.info("TC09 opt-in is rejected while the manual listener exists")
        self._expect_rejected(
            lambda: self._set_group(nqn, True),
            "TC09 opt-in with a manual listener",
            _assert_manual_exclusion,
        )
        self._assert_state(nqn, opted_in=False, masks=[], context="TC09 still manual")
        self.manual_signature = self._listener_signature(nqn)
        LOG.info("Manual listener snapshot: %s", self.manual_signature)

        # Different port so this is not a duplicate of an auto listener.
        opted = self._nqn("spec-opt-in-at-create")
        LOG.info("TC09 manual listener add on opted-in %s is rejected", opted)
        self._expect_rejected(
            lambda: gw.listener.add(
                **{
                    "args": {
                        "subsystem": opted,
                        "traddr": gw.node.ip_address,
                        "trsvcid": int(self.port) + 1,
                        "host-name": gw.node.hostname,
                    }
                }
            ),
            "TC09 manual listener add while opted in",
            _assert_manual_exclusion,
        )
        self._assert_listeners(
            opted,
            self._expected_ips(self.spec_masks),
            manual=False,
            context="TC09 opted-in listeners unchanged",
        )
        self._assert_manual_unchanged("TC09")

    def _tc10_opt_out(self):
        nqn = self._nqn("opt-in-existing-then-out")
        stayed = self._nqn("spec-opt-in-at-create")
        LOG.info("Opt out %s with set_use_group_config_network_masks false", nqn)
        self._set_group(nqn, False)
        self._assert_state(nqn, opted_in=False, masks=[], context="cnode2 opt-out")
        self._assert_listeners(nqn, [], context="cnode2 group listeners removed")
        self._assert_state(
            stayed,
            opted_in=True,
            masks=self.mask_b,
            context="cnode1 still follows mask B",
        )
        self._assert_listeners(
            stayed,
            self._expected_ips(self.mask_b),
            manual=False,
            context="cnode1 listeners unchanged by cnode2 opt-out",
        )
        self._assert_manual_unchanged("cnode2 opt-out")
        self._assert_subsystem_mask_unchanged("cnode2 opt-out")

    def _reapply_mask_b(self):
        """Replace mask A with mask B. Both opted-in subsystems must follow."""
        LOG.info(
            "Reapply listener_network_masks from mask A %s to mask B %s",
            self.mask_a,
            self.mask_b,
        )
        self._reapply(self.mask_b)
        expected = self._expected_ips(self.mask_b)
        primary_ips = set(self._current_primary_ips())
        if primary_ips & set(expected):
            raise RuntimeError(
                f"Mask B {self.mask_b} still covers primary IPs {sorted(primary_ips)}"
            )
        if set(expected) != set(self._current_secondary_ips()):
            raise RuntimeError(
                "Mask B should listen only on secondary gateway IPs. "
                f"expected={expected} secondary={self._current_secondary_ips()}"
            )
        for role in ("spec-opt-in-at-create", "opt-in-existing-then-out"):
            nqn = self._nqn(role)
            self._assert_state(nqn, opted_in=True, masks=self.mask_b, context="mask B")
            self._assert_listeners(
                nqn,
                expected,
                manual=False,
                context="mask B auto listeners",
            )
            stale = primary_ips & set(self._listener_ips(nqn))
            if stale:
                raise RuntimeError(f"{nqn} still has mask A listeners {sorted(stale)}")
        self._assert_subsystem_mask_unchanged("mask B reapply")
        self._assert_manual_unchanged("mask B reapply")

    def _tc14_15_scoped_refresh(self):
        """Flap the secondary IPv4s and prove refresh_network is subsystem-scoped."""
        scoped = self._nqn("spec-opt-in-at-create")
        other = self._nqn("opt-in-existing-then-out")
        both = self._expected_ips(self.spec_masks)
        # Mask B matches only the secondary subnet, so a down secondary leaves
        # no listener until refresh_network runs.
        none_live = []
        try:
            LOG.info("TC14 Take secondary IPv4s down; listeners stay until refresh")
            self._set_secondary("down")
            time.sleep(5)
            self._assert_listener_ips(scoped, both, "TC14 stale before refresh")
            self._assert_listener_ips(other, both, "TC14 other subsystem stale")

            self._refresh(scoped, "TC14 secondary down")
            self._assert_listeners(
                scoped,
                none_live,
                context="TC14 scoped refresh removed secondary listeners",
            )
            self._assert_listener_ips(
                other, both, "TC14 non-refreshed subsystem unchanged"
            )
            self._assert_subsystem_mask_unchanged("TC14")
            self._assert_manual_unchanged("TC14")

            self._refresh(other, "TC14 catch up other opted-in subsystem")
            self._assert_listeners(
                other,
                none_live,
                context="TC14 other subsystem refreshed",
            )

            LOG.info(
                "TC15 Bring secondary IPv4s up; only a refreshed subsystem changes"
            )
            self._set_secondary("up")
            time.sleep(5)
            self._assert_listener_ips(scoped, none_live, "TC15 stale before refresh")
            self._assert_listener_ips(other, none_live, "TC15 other still empty")

            self._refresh(scoped, "TC15 secondary up")
            self._assert_listeners(
                scoped,
                both,
                manual=False,
                context="TC15 scoped refresh added secondary listeners",
            )
            self._assert_listener_ips(
                other, none_live, "TC15 non-refreshed subsystem unchanged"
            )
            self._refresh(other, "TC15 catch up other opted-in subsystem")
            self._assert_listeners(
                other,
                both,
                manual=False,
                context="TC15 other subsystem refreshed",
            )
            self._assert_subsystem_mask_unchanged("TC15")
            self._assert_manual_unchanged("TC15")
        finally:
            if self.secondary_down:
                LOG.info("Restoring secondary IPv4s after refresh scope")
                self._set_secondary("up")

    def _tc17_scale(self):
        steps = self.config.get("load_balancing") or [
            {"scale_down": [self.original_gw_nodes[-1]]},
            {"scale_up": [self.original_gw_nodes[-1]]},
        ]
        try:
            for step in steps:
                if step.get("scale_down"):
                    remove = list(step["scale_down"])
                    remaining = [
                        node for node in self.original_gw_nodes if node not in remove
                    ]
                    LOG.info("TC17 Scale down %s, remaining %s", remove, remaining)
                    self._redeploy(remaining)
                    self._assert_scaled("TC17 scale-down")
                if step.get("scale_up"):
                    LOG.info("TC17 Scale up to %s", self.original_gw_nodes)
                    self._redeploy(self.original_gw_nodes)
                    self._assert_scaled("TC17 scale-up")
        finally:
            current = list(self.nvme_service.config.get("gw_nodes") or [])
            if current != list(self.original_gw_nodes):
                LOG.info("TC17 restoring gateway placement %s", self.original_gw_nodes)
                self._redeploy(self.original_gw_nodes)

    def _tc18_io(self):
        nqn = self._nqn("spec-opt-in-at-create")
        LOG.info("TC18 Connect initiator and run FIO on %s", nqn)
        sub = dict(self.by_role["spec-opt-in-at-create"])
        sub["allow_host"] = sub.get("allow_host", "*")
        io_config = {"rbd_pool": self.config["rbd_pool"], "subsystems": [sub]}
        configure_hosts(self.gateway, io_config, ceph_cluster=self.ceph_cluster)
        configure_namespaces(self.gateway, io_config, rbd_obj=self.rbd_obj)
        for init_cfg in self.config.get("initiators") or []:
            self._run_io(init_cfg, nqn)

    def _assert_scaled(self, context):
        """Opted-in subsystems follow the live gateways; manual listeners stay."""
        expected = self._expected_ips(self.spec_masks)
        for role in ("spec-opt-in-at-create", "opt-in-existing-then-out"):
            self._assert_listeners(
                self._nqn(role),
                expected,
                manual=False,
                context=f"{context} {role}",
                timeout=180,
            )
        self._assert_subsystem_mask_unchanged(context)
        self._assert_manual_unchanged(context)

    def _listener_signature(self, nqn):
        rows = []
        for listener in self._listeners(nqn):
            port = listener.get("trsvcid", listener.get("port"))
            rows.append((listener.get("traddr"), str(port), _listener_manual(listener)))
        return tuple(sorted(rows, key=lambda row: (row[0] or "", row[1])))

    def _assert_manual_unchanged(self, context):
        nqn = self._nqn("manual-listener")
        got = self._listener_signature(nqn)
        if got != self.manual_signature:
            raise RuntimeError(
                f"{context}: manual listeners on {nqn} changed from "
                f"{self.manual_signature} to {got}"
            )
        self._assert_state(
            nqn, opted_in=False, masks=[], context=f"{context} manual subsystem"
        )
        LOG.info("%s manual listeners unchanged: %s", context, got)

    def _assert_subsystem_mask_unchanged(self, context):
        nqn = self._nqn("subsystem-network-mask")
        self._assert_state(
            nqn,
            opted_in=False,
            masks=[self.primary_mask],
            context=f"{context} subsystem mask",
        )
        self._assert_listeners(
            nqn,
            self._expected_ips([self.primary_mask]),
            manual=False,
            context=f"{context} subsystem mask listeners",
        )

    def _final_subsystem_list(self):
        """json and yaml must match the state after cnode2 opts out."""
        LOG.info("Final ceph nvmeof subsystem list --format json and yaml")
        for fmt in ("json", "yaml"):
            out, err = self.gateway.subsystem.list(**{"base_cmd_args": {"format": fmt}})
            LOG.info("subsystem list --format %s:\n%s%s", fmt, out or "", err or "")
        opted = self._nqn("spec-opt-in-at-create")
        default = self._nqn("opt-in-existing-then-out")
        self._assert_state(
            opted, opted_in=True, masks=self.mask_b, context="final cnode1"
        )
        self._assert_listeners(
            opted,
            self._expected_ips(self.mask_b),
            manual=False,
            context="final cnode1 listeners",
        )
        self._assert_state(default, opted_in=False, masks=[], context="final cnode2")
        self._assert_listeners(default, [], context="final cnode2 listeners")
        self._assert_subsystem_mask_unchanged("final")
        self._assert_manual_unchanged("final")

    def _reapply(self, masks):
        """Reapply the orch spec and wait until the new masks are published."""
        LOG.info("Reapply listener_network_masks=%s", list(masks))
        self.nvme_service.apply_nvmeof_spec(
            {"listener_network_masks": list(masks)},
            redeploy=True,
            wait_sec=60,
        )
        self.nvme_service.init_gateways()
        self.nvme_service.wait_for_gateways_ready()
        self.spec_masks = list(masks)
        self._assert_published_masks(masks)

    def _redeploy(self, gw_node_ids):
        LOG.info("Redeploying NVMeoF gateways on %s", gw_node_ids)
        self.nvme_service.config["gw_nodes"] = list(gw_node_ids)
        self.config["gw_nodes"] = list(gw_node_ids)
        self.nvme_service.gw_nodes = get_nodes_by_ids(self.ceph_cluster, gw_node_ids)
        self.nvme_service.deploy()
        self.nvme_service.gateways = []
        self.nvme_service.init_gateways()
        self.nvme_service.wait_for_gateways_ready()

    def _set_secondary(self, state):
        if state == "down":
            # Mark first so a partial failure still restores addresses in close().
            self.secondary_down = True
        secondary_iface_state_on_gateways(
            self.nvme_service.gateways, self.networks, state=state
        )
        self.secondary_down = state == "down"

    def _refresh(self, nqn, context):
        LOG.info("refresh_network %s (%s) on each gateway", nqn, context)
        for gw in self.nvme_service.gateways:
            result = refresh_gateway_network(gw, nqn)
            status = result.get("status", 0)
            if status not in (0, "0", None):
                raise RuntimeError(
                    f"refresh_network failed on {gw.node.hostname} for {nqn} "
                    f"({context}): {result}"
                )

    def _add_subsystem(self, sub, use_group_masks=False, network_mask=None):
        if use_group_masks and network_mask:
            # Sent together on purpose for the mutual-exclusion negative test.
            LOG.info(
                "Adding %s with both network-mask and spec opt-in (expect reject)",
                sub["nqn"],
            )
        args = {
            "subsystem": sub["nqn"],
            "max-namespaces": sub.get("max_ns", 32),
            "no-group-append": True,
            "port": int(sub.get("listener_port", self.port)),
        }
        if sub.get("serial") is not None:
            args["serial-number"] = sub["serial"]
        if network_mask:
            args["network-mask"] = network_mask
        if use_group_masks:
            args["use-group-config-network-masks"] = True
        return self.gateway.subsystem.add(**{"args": args})

    def _set_group(self, nqn, enabled):
        LOG.info(
            "set_use_group_config_network_masks %s on %s",
            "true" if enabled else "false",
            nqn,
        )
        out, err = self.gateway.subsystem.set_use_group_config_network_masks(
            **{
                "args": {
                    "subsystem": nqn,
                    "use-group-config-network-masks": "true" if enabled else "false",
                }
            }
        )
        text = f"{out or ''}\n{err or ''}".strip()
        if text:
            LOG.info("set_use_group_config_network_masks %s: %s", nqn, text)
            low = text.lower()
            if "fail" in low and "success" not in low:
                raise RuntimeError(
                    f"set_use_group_config_network_masks on {nqn} failed: {text}"
                )
        return out, err

    def _delete_if_present(self, nqn):
        try:
            subs = self._load_subsystems("json")
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Could not list subsystems before deleting %s: %s", nqn, exc)
            return
        names = {sub.get("nqn") or sub.get("subnqn") for sub in subs}
        if nqn not in names:
            return
        try:
            self.gateway.subsystem.delete(**{"args": {"subsystem": nqn, "force": True}})
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Failed to delete probe subsystem %s: %s", nqn, exc)

    def _expect_rejected(self, fn, context, matcher):
        try:
            out, err = fn()
        except CommandFailed as exc:
            text = str(exc)
            if _is_infra_failure(text):
                raise
            if _is_unknown_cli(text):
                raise RuntimeError(
                    f"{context}: nvmeof CLI does not recognize the spec "
                    f"network-mask command or flag. {text}"
                ) from exc
            matcher(text, context)
            LOG.info("%s rejected as expected", context)
            return
        text = f"{out or ''}\n{err or ''}"
        if _looks_like_cli_error(text):
            if _is_unknown_cli(text):
                raise RuntimeError(
                    f"{context}: nvmeof CLI does not recognize the spec "
                    f"network-mask command or flag. {text}"
                )
            matcher(text, context)
            LOG.info("%s rejected as expected", context)
            return
        raise RuntimeError(f"{context} succeeded unexpectedly: {text}")

    def _load_subsystems(self, fmt):
        try:
            out, _ = self.gateway.subsystem.list(**{"base_cmd_args": {"format": fmt}})
        except CommandFailed as exc:
            if "no subsystem" in str(exc).lower():
                return []
            raise
        if not out or not str(out).strip():
            return []
        data = json.loads(out) if fmt == "json" else yaml.safe_load(out)
        if not data:
            return []
        if isinstance(data, dict):
            subs = data.get("subsystems", data.get("subsystem", []))
            if isinstance(subs, dict):
                return [subs]
            return list(subs or [])
        if isinstance(data, list):
            return data
        raise RuntimeError(f"Unexpected subsystem list {fmt} payload: {data!r}")

    def _find_subsystem(self, fmt, nqn):
        for sub in self._load_subsystems(fmt):
            if (sub.get("nqn") or sub.get("subnqn")) == nqn:
                return sub
        raise RuntimeError(f"{nqn} not found in subsystem list --format {fmt}")

    def _assert_absent(self, nqn):
        names = {
            sub.get("nqn") or sub.get("subnqn") for sub in self._load_subsystems("json")
        }
        if nqn in names:
            raise RuntimeError(
                f"{nqn} exists after a create that should have been rejected"
            )

    def _flag(self, sub):
        for key in _GROUP_FLAG_NAMES:
            if key in sub:
                return _as_bool(sub[key])
        raise RuntimeError(
            f"subsystem record has no use_group_config_network_masks field: {sub}"
        )

    def _assert_state(self, nqn, opted_in, masks, context):
        expected = _normalize_masks(masks)
        for fmt in ("json", "yaml"):
            sub = self._find_subsystem(fmt, nqn)
            flag = self._flag(sub)
            if flag != opted_in:
                raise RuntimeError(
                    f"{context}: {fmt} use_group_config_network_masks={flag} "
                    f"on {nqn}, expected {opted_in}. record={sub}"
                )
            if "network_mask" in sub or "network-mask" in sub:
                got = _normalize_masks(sub.get("network_mask", sub.get("network-mask")))
            elif expected:
                raise RuntimeError(
                    f"{context}: {fmt} subsystem {nqn} has no network_mask. record={sub}"
                )
            else:
                got = []
            if got != expected:
                raise RuntimeError(
                    f"{context}: {fmt} network_mask={got} on {nqn}, "
                    f"expected {expected}. record={sub}"
                )
        LOG.info(
            "%s %s use_group_config_network_masks=%s network_mask=%s",
            context,
            nqn,
            opted_in,
            expected,
        )

    def _assert_table_hides_flag(self, nqn, masks):
        """The boolean is json/yaml only; the table still shows the effective mask."""
        text = self._table_output()
        for name in _GROUP_FLAG_NAMES:
            if name in text:
                raise RuntimeError(
                    "subsystem list table exposes "
                    f"{name}, which must stay out of the table:\n{text}"
                )
        if nqn not in text:
            raise RuntimeError(f"subsystem list table does not show {nqn}:\n{text}")
        for mask in _normalize_masks(masks):
            if mask not in text and mask.split("/")[0] not in text:
                raise RuntimeError(
                    f"subsystem list table does not show effective mask {mask} "
                    f"for {nqn}:\n{text}"
                )
        LOG.info("TC04 subsystem list table hides use_group_config_network_masks")

    def _table_output(self):
        errors = []
        for fmt in ("plain", "table", "pretty"):
            try:
                out, err = self.gateway.subsystem.list(
                    **{"base_cmd_args": {"format": fmt}}
                )
            except CommandFailed as exc:
                errors.append(f"{fmt}: {exc}")
                continue
            text = f"{out or ''}\n{err or ''}".strip()
            if text and not text.lstrip().startswith(("{", "[")):
                return text
            errors.append(f"{fmt}: output was not a table")
        try:
            out, err = self.gateway.subsystem.list()
        except CommandFailed as exc:
            errors.append(f"default: {exc}")
        else:
            text = f"{out or ''}\n{err or ''}".strip()
            if text and not text.lstrip().startswith(("{", "[")):
                return text
            errors.append("default: output was not a table")
        raise RuntimeError(
            "subsystem list did not return a table format. " + " | ".join(errors)
        )

    def _listeners(self, nqn):
        try:
            return list_listeners(self.gateway, nqn)
        except (json.JSONDecodeError, TypeError, KeyError, ValueError):
            return []

    def _listener_ips(self, nqn):
        return sorted(
            {
                listener.get("traddr")
                for listener in self._listeners(nqn)
                if listener.get("traddr")
            }
        )

    def _wait_listener_ips(self, nqn, ips, timeout):
        expected = sorted(ips)
        last = None
        deadline = time.time() + timeout
        while True:
            last = self._listener_ips(nqn)
            if last == expected:
                return self._listeners(nqn)
            if time.time() >= deadline:
                break
            time.sleep(5)
        raise RuntimeError(f"Listeners for {nqn} are {last}, expected {expected}")

    def _assert_listener_ips(self, nqn, ips, context, timeout=60):
        try:
            self._wait_listener_ips(nqn, ips, timeout=timeout)
        except RuntimeError as exc:
            raise RuntimeError(f"{context}: {exc}") from exc
        LOG.info("%s %s listeners=%s", context, nqn, sorted(ips))

    def _assert_listeners(self, nqn, ips, context, manual=None, timeout=90):
        listeners = self._wait_listener_ips(nqn, ips, timeout=timeout)
        if len(listeners) != len(list(ips)):
            raise RuntimeError(
                f"{context}: {nqn} has {len(listeners)} listeners for "
                f"{len(list(ips))} addresses: {listeners}"
            )
        if not ips:
            LOG.info("%s %s has no listeners", context, nqn)
            return
        for listener in listeners:
            got_manual = _listener_manual(listener)
            if manual is not None and got_manual != manual:
                raise RuntimeError(
                    f"{context}: listener manual={got_manual} on {nqn}, "
                    f"expected {manual}. listener={listener}"
                )
            port = listener.get("trsvcid", listener.get("port"))
            if str(port) != str(self.port):
                raise RuntimeError(
                    f"{context}: listener port {port} on {nqn}, expected {self.port}"
                )
            host = (
                listener.get("host_name")
                or listener.get("host-name")
                or listener.get("hostname")
                or ""
            )
            if not host:
                raise RuntimeError(
                    f"{context}: listener on {nqn} has no host: {listener}"
                )
            if not self._host_known(host):
                raise RuntimeError(
                    f"{context}: listener host {host} on {nqn} is not a gateway"
                )
        LOG.info(
            "%s %s listeners=%s manual=%s",
            context,
            nqn,
            sorted(ips),
            manual,
        )

    def _host_known(self, host):
        short = str(host).split(".")[0]
        names = set(self.networks["per_gateway"])
        names.update(gw.node.hostname for gw in self.nvme_service.gateways)
        return any(short == str(name).split(".")[0] for name in names)

    def _current_primary_ips(self):
        return sorted(
            {
                gw.node.ip_address
                for gw in self.nvme_service.gateways
                if getattr(gw.node, "ip_address", None)
            }
        )

    def _current_secondary_ips(self):
        live = {gw.node.hostname for gw in self.nvme_service.gateways}
        live_short = {name.split(".")[0] for name in live}
        ips = []
        for host, info in self.networks["per_gateway"].items():
            if host in live or str(host).split(".")[0] in live_short:
                ip = (info.get("secondary") or {}).get("ip")
                if ip:
                    ips.append(ip)
        return sorted(set(ips))

    def _expected_ips(self, masks):
        candidates = self._current_primary_ips() + self._current_secondary_ips()
        return _ips_covered_by(masks, candidates)

    def _dummy_mask(self):
        spec_cfg = self.config.get("spec_gw_netmask") or {}
        dummy = spec_cfg.get("dummy_network_mask", "192.0.2.0/24")
        network = ipaddress.ip_network(dummy, strict=False)
        for ip in self.networks["primary_ips"] + self.networks["secondary_ips"]:
            if ipaddress.ip_address(ip) in network:
                raise RuntimeError(
                    f"dummy_network_mask {network} covers gateway IP {ip}. "
                    "Pick a CIDR outside the gateway subnets."
                )
        if network.overlaps(ipaddress.ip_network(self.primary_mask, strict=False)):
            raise RuntimeError(
                f"dummy mask {network} overlaps primary {self.primary_mask}"
            )
        if network.overlaps(ipaddress.ip_network(self.secondary_mask, strict=False)):
            raise RuntimeError(
                f"dummy mask {network} overlaps secondary {self.secondary_mask}"
            )
        return str(network)

    def _assert_published_masks(self, masks):
        expected = _normalize_masks(masks)
        service = self._orch_service()
        orch_value = _orch_listener_masks(service)
        orch_got = _normalize_masks(orch_value) if orch_value is not None else []
        if orch_value is None and expected:
            raise RuntimeError(
                "ceph orch nvmeof spec has no listener_network_masks. "
                f"service={service}"
            )
        if orch_got != expected:
            raise RuntimeError(
                f"orch listener_network_masks={orch_got}, expected {expected}"
            )
        for gw in self.nvme_service.gateways:
            text = gw.read_ceph_nvmeof_conf()
            conf_value = _conf_listener_masks(text)
            conf_got = _normalize_masks(conf_value) if conf_value is not None else []
            if conf_value is None and expected:
                raise RuntimeError(
                    f"{gw.node.hostname} ceph-nvmeof.conf has no "
                    f"listener_network_masks in [gateway].\n{text[:2000]}"
                )
            if conf_got != expected:
                raise RuntimeError(
                    f"{gw.node.hostname} listener_network_masks={conf_got}, "
                    f"expected {expected}.\n{text[:2000]}"
                )
            LOG.info(
                "%s [gateway] listener_network_masks=%s",
                gw.node.hostname,
                conf_got,
            )
        LOG.info("Orch and gateway conf listener_network_masks=%s", expected)

    def _orch_service(self):
        orch = Orch(self.ceph_cluster, **{})
        out, _ = orch.shell(args=["ceph orch ls nvmeof --format json"])
        services = json.loads(out)
        group = self.nvme_service.group
        for service in services:
            name = service.get("service_name") or ""
            if "nvmeof" not in name:
                continue
            if group and group not in name:
                continue
            return service
        raise RuntimeError(
            f"No nvmeof orch service found for group {group}: {services}"
        )

    def _run_io(self, init_cfg, nqn):
        client = get_node_by_id(self.ceph_cluster, init_cfg["node"])
        initiator = NVMeInitiator(client)
        try:
            initiator.connect_targets(
                self.gateway,
                {
                    "nqn": nqn,
                    "listener_port": init_cfg.get("listener_port", self.port),
                },
            )
            out, err = client.exec_command(
                cmd="nvme list-subsys", sudo=True, check_ec=False
            )
            LOG.info(
                "[%s] nvme list-subsys:\n%s",
                client.hostname,
                (out or err or "").strip(),
            )
            paths = initiator.list_devices()
            if not paths:
                raise RuntimeError(f"No NVMe devices on {client.hostname} for {nqn}")
            io_args = init_cfg.get("io_args", {"size": "100M", "runtime": 30})
            with parallel() as pool:
                for path in paths:
                    pool.spawn(
                        run_fio,
                        **{
                            **io_args,
                            "device_name": path,
                            "client_node": client,
                            "long_running": True,
                            "cmd_timeout": "notimeout",
                        },
                    )
                for result in pool:
                    if isinstance(result, int) and result != 0:
                        raise RuntimeError(f"FIO failed with exit code {result}")
        finally:
            disconnect_initiators(None, node=client)


def run(ceph_cluster: Ceph, **kwargs) -> int:
    """Entry point for the spec gateway-netmask suite."""
    LOG.info("Starting NVMeoF spec listener_network_masks E2E")
    config = deepcopy(kwargs["config"])
    rbd_pools = initial_rbd_config(**kwargs)
    if not rbd_pools or not rbd_pools.get("rbd_reppool"):
        LOG.error("RBD pool setup failed (initial_rbd_config returned %s)", rbd_pools)
        return 1
    rbd_obj = rbd_pools["rbd_reppool"]
    custom_config = kwargs.get("test_data", {}).get("custom-config")
    check_and_set_nvme_cli_image(ceph_cluster, config=custom_config)

    test = None
    try:
        if config.get("cleanup-only"):
            nvme_service = NVMeService(config, ceph_cluster)
            nvme_service.init_gateways()
            return teardown(nvme_service, rbd_obj)
        test = SpecGatewayNetmaskE2E(ceph_cluster, config, rbd_obj)
        test.execute()
        return 0
    except Exception as exc:  # noqa: BLE001
        LOG.error("Spec gateway-netmask E2E failed: %s", exc)
        return 1
    finally:
        if test is not None:
            test.close()
        if config.get("cleanup") and not config.get("cleanup-only"):
            try:
                nvme_service = NVMeService(config, ceph_cluster)
                nvme_service.init_gateways()
                teardown(nvme_service, rbd_obj)
            except Exception as cleanup_exc:  # noqa: BLE001
                LOG.warning("Teardown warning: %s", cleanup_exc)


def _listener_manual(listener):
    """Return the listener manual flag. Spec auto-listeners are manual=false."""
    for key in ("manual", "is_manual", "Manual"):
        if key in listener:
            return _as_bool(listener[key])
    raise RuntimeError(f"Listener has no manual flag: {listener}")
