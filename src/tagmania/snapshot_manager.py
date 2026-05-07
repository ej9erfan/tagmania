"""AWS Cluster Snapshot Management CLI.

This module provides the main CLI interface for creating, restoring, deleting,
and listing EBS snapshots for EC2 clusters. It supports both full cluster
operations and targeted operations using regex patterns to match specific instances.

The tool relies on EC2 instances having "Cluster" tags to identify cluster membership
and "Name" tags for targeted operations. All operations include safety features
with confirmation prompts to prevent accidental data loss.

Features:
    - Create named snapshots of entire clusters
    - Restore clusters from snapshots with volume replacement
    - Targeted restore using regex patterns on instance names
    - List and delete existing snapshots
    - Safety confirmations for all destructive operations

Usage:
    The module is typically invoked via the cluster-snap CLI command:

    ```bash
    # Create a backup
    cluster-snap --backup --name daily-backup production

    # Restore entire cluster
    cluster-snap --restore --name daily-backup production

    # Targeted restore (only web servers)
    cluster-snap --restore --target ".*-web-.*" production

    # List snapshots
    cluster-snap --list production

    # Delete snapshots
    cluster-snap --delete --name daily-backup production
    ```

Warning:
    Restore operations permanently delete existing EBS volumes and replace them
    with volumes created from snapshots. This operation cannot be undone.
    Always confirm you have the correct backup before proceeding.
"""

import argparse
import re

from tagmania.iac_tools.clusterset import ClusterSet


def main():
    """Main entry point for the cluster snapshot management CLI.

    Parses command line arguments and executes the appropriate snapshot operation
    (backup, restore, list, or delete) on the specified cluster.

    The function handles all user interactions including confirmation prompts
    for destructive operations and provides detailed feedback on operation progress.

    Raises:
        SystemExit: On invalid command line arguments or user cancellation.
        ValueError: On invalid regex patterns for targeted operations.
        AWSError: On AWS API failures during snapshot operations.
    """
    parser = argparse.ArgumentParser(
        description="AWS cluster snapshot backup and restore tool.",
        epilog="""
            This tool relies on the "Cluster" and "Owner" tags on instances and
            volumes. IAC automation puts this in place. This tool
            creates multiple sets of labeled snapshots and creates volumes from
            them when a restore operation is performed.""",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "-b",
        "--backup",
        action="store_const",
        dest="backup",
        const=True,
        default=False,
        help="Create snapshots for cluster CLUSTER.",
    )
    group.add_argument(
        "-D",
        "--delete",
        action="store_const",
        dest="delete",
        const=True,
        default=False,
        help="Delete snapshots from cluster CLUSTER.",
    )
    group.add_argument(
        "-r",
        "--restore",
        action="store_const",
        dest="restore",
        const=True,
        default=False,
        help="Restore cluster CLUSTER from snapshots.",
    )
    group.add_argument(
        "-l",
        "--list",
        action="store_const",
        dest="list",
        const=True,
        default=False,
        help="List snapshots with a given label or all if none specified.",
    )
    parser.add_argument("-n", "--name", default=None, help="Name to use for the snapshots.")
    parser.add_argument(
        "-t",
        "--target",
        type=str,
        default=None,
        help="Regex pattern to match instance Name tags for targeted restore.",
    )
    parser.add_argument(
        "-d",
        "--divisor",
        type=int,
        default=1,
        help="Divide the total instances into this many batches to avoid AWS rate limits.",
    )
    parser.add_argument(
        "cluster",
        help="""
            the name CLUSTER of the cluster in question. This can be found by
            looking at any node in the AWS console and looking for the "Cluster"
            tag.""",
    )
    parser.add_argument("--profile", "-p", help="the AWS profile to use", default=None)
    args = parser.parse_args()

    cluster = ClusterSet(args.cluster, profile=args.profile)

    if args.backup:
        snapshot_name = "default" if args.name is None else args.name
        confirm = input(f"Create backup of {args.cluster} named '{snapshot_name}'? [no] ")
        if confirm == "yes":
            print("Making backup.")
            instances = cluster.get_instances()
            if len(instances) == 0:
                print("No instances found. Operation aborted.")
            else:
                # Stop the cluster (not clean)
                cluster.stop_instances()
                cluster.create_snapshots(snapshot_name)
                # Start cluster
                # cluster.start_instances()
                print("Operation completed successfully!")
        else:
            print("Operation aborted.")

    if args.delete:
        if args.name is None:
            snapshot_name = "*"
            confirm_string = f"Delete all backups for {args.cluster}"
        else:
            snapshot_name = args.name
            confirm_string = f"Delete backup of {args.cluster} named '{snapshot_name}'"
        confirm = input(f"{confirm_string}? [no] ")
        if confirm == "yes":
            print("Deleting snapshots.")
            snapshots = cluster.get_snapshots(snapshot_name)
            if len(snapshots) == 0:
                print("No snapshots found. Operation aborted.")
            else:
                cluster.delete_snapshots(snapshot_name)
        else:
            print("Operation aborted.")

    if args.restore:
        snapshot_name = "default" if args.name is None else args.name

        instances_to_restore = []

        # Identify target instances
        if args.target:
            try:
                re.compile(args.target)  # Validate regex
                all_instances = cluster.get_instances()
                instances_to_restore = cluster._filter_instances_by_name_regex(
                    all_instances, args.target
                )
            except re.error as e:
                print(f"Invalid regex pattern '{args.target}': {e}")
                return
        else:
            # For a full restore, we just get everyone
            instances_to_restore = cluster.get_instances()

        if not instances_to_restore:
            if args.target:
                print(f"No instances found matching pattern '{args.target}'. Operation aborted.")
            else:
                print("No instances found to restore. Operation aborted.")
            return

        # Setup Batching
        total_count = len(instances_to_restore)
        num_batches = max(1, min(args.divisor, total_count))
        batch_size = (total_count + num_batches - 1) // num_batches

        print("\n--- Restore Plan ---")
        print(f"Target Cluster: {args.cluster}")
        print(f"Snapshot Name:  {snapshot_name}")
        print(f"Total Instances: {total_count}")
        print(f"Batching:       {num_batches} batches of ~{batch_size} instances")

        confirm = input("\nProceed with restore? [no] ")
        if confirm.lower() == "yes":
            # Stop the instances involved
            print("Stopping instances...")
            cluster.stop_instances()  # It's safer to stop the whole cluster set once

            # Process the Batches
            for i in range(0, total_count, batch_size):
                batch = instances_to_restore[i : i + batch_size]

                # --- NEW LOGIC START ---
                # Get the "Name" tag value for each instance in the batch
                batch_names = []
                for ins in batch:
                    name_value = next(
                        (tag["Value"] for tag in ins.tags if tag["Key"] == "Name"), None
                    )
                    if name_value:
                        batch_names.append(name_value)

                # Join the Names into a regex pattern
                batch_pattern = "|".join(batch_names)
                # --- NEW LOGIC END ---

                print(
                    f"\n[Batch {i // batch_size + 1}/{num_batches}] Processing {len(batch)} instances..."
                )
                print(f"Pattern: {batch_pattern}")  # Debug to see the names

                cluster.detach_volumes_targeted(batch_pattern)
                cluster.delete_volumes_targeted(batch_pattern)
                cluster.create_volumes_targeted(snapshot_name, batch_pattern)
                cluster.attach_volumes_targeted(snapshot_name, batch_pattern)

                # Wait for the AWS hydration rate to settle before the next batch
                if i + batch_size < total_count:
                    print("Waiting 60s for AWS throughput limits to clear...")
                    import time

                    time.sleep(60)

            print("\nRestore operation completed successfully!")
            return

        else:
            print("Operation aborted.")
            return

    if args.list:
        if args.name is None:
            print(f"Listing all snapshots associated with {args.cluster}.")
            snapshots = cluster.get_snapshots("*")

            # Getting a dictionary of all snapshots associated with the cluster provided grouped by label
            label_list = []
            snapshot_dict: dict[str, list[str]] = {}
            for snapshot in snapshots:
                for tag in snapshot.tags:
                    if "Label" in tag["Key"] and tag["Value"] not in label_list:
                        label_list.append(tag["Value"])
                        snapshot_dict[tag["Value"]] = []
                        snapshot_dict[tag["Value"]].append(snapshot.id)
                    elif "Label" in tag["Key"]:
                        snapshot_dict[tag["Value"]].append(snapshot.id)

            # Printing snapshots sorted by Label name
            label_list.sort()
            for label in label_list:
                print("\nLabel: " + label)
                for snapshot_id in snapshot_dict[label]:
                    print(snapshot_id)
        else:
            print(f"Listing snapshots labeled '{args.name}' for {args.cluster}.")
            snapshots = cluster.get_snapshots(args.name)
            for snapshot in snapshots:
                print(snapshot.id)


if __name__ == "__main__":
    main()
