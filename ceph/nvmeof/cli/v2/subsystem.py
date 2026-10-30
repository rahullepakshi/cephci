from ceph.nvmeof.cli.v2.base_cli import BaseCLI

from .common import substitute_keys

KEY_MAP = {
    # TODO: Temporary solution until https://tracker.ceph.com/issues/72636
    #       is fixed.
    "force": "force=true",
}

# Container CLI and the native ``ceph nvmeof`` examples spell this flag with
# hyphens. ``subsystem add`` takes it as a bare switch (default false).
# ``set_use_group_config_network_masks`` takes an explicit true/false.
_GROUP_NETWORK_MASK_KEYS = (
    "use-group-config-network-masks",
    "use_group_config_network_masks",
    "use-default-gw-group-network-mask",
    "use_default_gw_group_network_mask",
)


def _pop_group_network_mask_flag(args):
    """Remove and return the spec-mask opt-in flag, if the caller set one."""
    if not isinstance(args, dict):
        return None
    found = [key for key in _GROUP_NETWORK_MASK_KEYS if key in args]
    if not found:
        return None
    if len(found) > 1:
        raise ValueError(f"Duplicate group network-mask flags: {found}")
    return args.pop(found[0])


def _group_network_mask_enabled(value):
    """Return True/False for the spec-mask opt-in flag."""
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "1", "yes"):
        return True
    if text in ("false", "0", "no"):
        return False
    raise ValueError(
        f"use-group-config-network-masks must be true or false, got {value!r}"
    )


class Subsystem:

    def __init__(self, base: BaseCLI) -> None:
        self.base = base
        self.name = "subsystem"

    def add(self, **kwargs):
        """Add a subsystem.

        ``use-group-config-network-masks`` is emitted last, as a bare switch,
        so it cannot swallow the following ``--gw_group`` argument.
        """
        args = dict(kwargs.get("args") or {})
        base_cmd_args = dict(kwargs.get("base_cmd_args") or {})
        flag = _pop_group_network_mask_flag(args)
        if flag is not None and _group_network_mask_enabled(flag):
            base_cmd_args["use-group-config-network-masks"] = True
        elif flag is not None:
            args["use-group-config-network-masks"] = "false"
        kwargs["args"] = args
        if base_cmd_args:
            kwargs["base_cmd_args"] = base_cmd_args
        return self.base.run_nvme_cli(self.name, "add", **kwargs)

    def set_use_group_config_network_masks(self, **kwargs):
        """Opt a subsystem into or out of the gateway-group spec network masks.

        Native CLI::

            ceph nvmeof subsystem set_use_group_config_network_masks
                --nqn <nqn> --use-group-config-network-masks true|false
        """
        args = dict(kwargs.get("args") or {})
        flag = _pop_group_network_mask_flag(args)
        if flag is None:
            raise ValueError(
                "set_use_group_config_network_masks requires "
                "use-group-config-network-masks"
            )
        args["use-group-config-network-masks"] = (
            "true" if _group_network_mask_enabled(flag) else "false"
        )
        kwargs["args"] = args
        return self.base.run_nvme_cli(
            self.name, "set_use_group_config_network_masks", **kwargs
        )

    def add_network(self, **kwargs):
        """Add a network mask for auto-listeners on the subsystem."""
        return self.base.run_nvme_cli(self.name, "add_network", **kwargs)

    def change_key(self, **kwargs):
        """Change DHCHAP key for subsystem."""
        return self.base.run_nvme_cli(self.name, "change_key", **kwargs)

    @substitute_keys(KEY_MAP)
    def delete(self, **kwargs):
        return self.base.run_nvme_cli(self.name, "del", **kwargs)

    def del_key(self, **kwargs):
        """Delete DHCHAP key from subsystem."""
        return self.base.run_nvme_cli(self.name, "del_key", **kwargs)

    def del_network(self, **kwargs):
        """Remove a network mask from the subsystem."""
        return self.base.run_nvme_cli(self.name, "del_network", **kwargs)

    def get(self, **kwargs):
        """Get subsystem details."""
        return self.base.run_nvme_cli(self.name, "get", **kwargs)

    def list(self, **kwargs):
        return self.base.run_nvme_cli(self.name, "list", **kwargs)

    def add_kmip_server_endpoint(self, **kwargs):
        """Register a KMIP server endpoint on a subsystem.

        Maps to:
          ceph nvmeof subsystem add_kmip_server_endpoint <nqn>
              [server_name] [address] [port] [gw_group] [server_address] [traddr]
        """
        return self.base.run_nvme_cli(self.name, "add_kmip_server_endpoint", **kwargs)
