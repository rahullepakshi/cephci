"""
Module to deploy NFS service and daemon(s).

this module deploy NFS service and daemon(s) along with
handling other prerequisites needed for deployment.

"""
from ceph.ceph_admin.apply import ApplyMixin

from .orch import Orch


class NFS(ApplyMixin, Orch):
    SERVICE_NAME = "nfs"

    def apply(self, **config):
        """
        Deploy the NFS service.

        Args:
            config: test arguments

        config:
            command: apply
            service: nfs
            prefix_args:
                name: india
                pool: south
            args:
                label: nfs    # either label or node.
                nodes:
                    - node1
                limit: 3    # no of daemons
                sep: " "    # separator to be used for placements
        """
        prefix_args = config.pop("prefix_args")
        name = prefix_args.get("name", "nfs1")
        pool = prefix_args.get("pool", "nfs_pool")

        config["prefix_args"] = [name, pool]
        super().apply(**config)
