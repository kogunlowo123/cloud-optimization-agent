"""Suggested provider CLI commands for a recommendation. They are text for a person to review. Nothing here is run."""

from __future__ import annotations

import shlex

from cloudopt.models import Recommendation, Resource

NOTICE = "# Review every command before running it. cloudopt never executes them."

_TEMPLATES: dict[tuple[str, str], list[str]] = {
    ("aws", "stop_vm"): ["aws ec2 stop-instances --instance-ids {id}"],
    ("aws", "resize_vm"): [
        "aws ec2 stop-instances --instance-ids {id}",
        "aws ec2 modify-instance-attribute --instance-id {id} --instance-type Value={to}",
        "aws ec2 start-instances --instance-ids {id}",
    ],
    ("aws", "delete_disk"): [
        "aws ec2 create-snapshot --volume-id {id} --description pre-delete-backup",
        "aws ec2 delete-volume --volume-id {id}",
    ],
    ("aws", "delete_snapshot"): ["aws ec2 delete-snapshot --snapshot-id {id}"],
    ("aws", "release_ip"): ["aws ec2 release-address --allocation-id {id}"],
    ("aws", "delete_lb"): ["aws elbv2 delete-load-balancer --load-balancer-arn {id}"],
    ("aws", "delete_nat"): ["aws ec2 delete-nat-gateway --nat-gateway-id {id}"],
    ("aws", "tier_bucket"): [
        "aws s3api put-bucket-lifecycle-configuration --bucket {name} --lifecycle-configuration file://lifecycle.json"
    ],
    ("azure", "stop_vm"): ["az vm deallocate --ids {id}"],
    ("azure", "resize_vm"): [
        "az vm deallocate --ids {id}",
        "az vm resize --ids {id} --size {to}",
        "az vm start --ids {id}",
    ],
    ("azure", "delete_disk"): [
        "az snapshot create --resource-group {region} --name {name}-final --source {id}",
        "az disk delete --ids {id} --yes",
    ],
    ("azure", "delete_snapshot"): ["az snapshot delete --ids {id}"],
    ("azure", "release_ip"): ["az network public-ip delete --ids {id}"],
    ("azure", "delete_lb"): ["az network lb delete --ids {id}"],
    ("azure", "delete_nat"): ["az network nat gateway delete --ids {id}"],
    ("azure", "tier_bucket"): [
        "az storage account management-policy create --account-name {name} --policy @policy.json"
    ],
    ("gcp", "stop_vm"): ["gcloud compute instances stop {name} --zone {region}"],
    ("gcp", "resize_vm"): [
        "gcloud compute instances stop {name} --zone {region}",
        "gcloud compute instances set-machine-type {name} --zone {region} --machine-type {to}",
        "gcloud compute instances start {name} --zone {region}",
    ],
    ("gcp", "delete_disk"): [
        "gcloud compute disks snapshot {name} --zone {region} --snapshot-names {name}-final",
        "gcloud compute disks delete {name} --zone {region}",
    ],
    ("gcp", "delete_snapshot"): ["gcloud compute snapshots delete {name}"],
    ("gcp", "release_ip"): ["gcloud compute addresses delete {name} --region {region}"],
    ("gcp", "delete_lb"): ["gcloud compute forwarding-rules delete {name} --region {region}"],
    ("gcp", "delete_nat"): [
        "gcloud compute routers nats delete {name} --router {name}-router --region {region}"
    ],
    ("gcp", "tier_bucket"): [
        "gcloud storage buckets update gs://{name} --lifecycle-file=lifecycle.json"
    ],
}


def _quote(value: str) -> str:
    """Shell-quote a value that came from inventory data. Control characters are removed first."""
    clean = "".join(ch for ch in value if ch.isprintable())
    return shlex.quote(clean)


def action_for(rec: Recommendation, resource: Resource) -> str:
    """The template key for a recommendation, or an empty string when there is none."""
    if rec.category == "rightsize":
        return "resize_vm"
    if rec.category == "storage_tier":
        return "tier_bucket"
    if rec.category == "waste":
        return {
            "vm": "stop_vm",
            "disk": "delete_disk",
            "snapshot": "delete_snapshot",
            "public_ip": "release_ip",
            "load_balancer": "delete_lb",
            "nat_gateway": "delete_nat",
        }.get(resource.type, "")
    return ""


def commands_for(rec: Recommendation, resource: Resource) -> list[str]:
    """Commands for the recommendation, with every inventory value shell-quoted."""
    key = action_for(rec, resource)
    templates = _TEMPLATES.get((resource.provider, key))
    if not templates:
        return []
    values = {
        "id": _quote(resource.id),
        "name": _quote(resource.name or resource.id),
        "region": _quote(resource.region),
        "to": _quote(str(rec.details.get("to", ""))),
    }
    return [NOTICE, *[t.format(**values) for t in templates]]
