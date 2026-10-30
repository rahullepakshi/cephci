from ceph.nvmeof.cli.v1.execute import ExecuteCommandMixin


class Subsystem:

    def __init__(self, base: ExecuteCommandMixin) -> None:
        self.base = base
        self.name = "subsystem"

    def add(self, **kwargs):
        return self.base.run_nvme_cli(self.name, "add", **kwargs)

    def set_use_group_config_network_masks(self, **kwargs):
        """Opt a subsystem into or out of spec listener_network_masks.

        Container CLI::

            subsystem set_use_group_config_network_masks -n <nqn>
                --use-group-config-network-masks true|false
        """
        return self.base.run_nvme_cli(
            self.name, "set_use_group_config_network_masks", **kwargs
        )

    def add_network(self, **kwargs):
        """Add a network mask for auto-listeners on the subsystem."""
        return self.base.run_nvme_cli(self.name, "add_network", **kwargs)

    def change_key(self, **kwargs):
        """Change DHCHAP key for subsystem."""
        return self.base.run_nvme_cli(self.name, "change_key", **kwargs)

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
