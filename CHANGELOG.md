# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses semantic versioning.

## [Unreleased]

### Added

- Container `HEALTHCHECK` that runs `cloudopt catalog`.

## [0.1.0]

### Added

- JSON Lines inventory and billing loaders with strict validation and limits.
- Waste analyzer for idle instances and databases, unattached disks, orphaned snapshots, unassociated addresses,
  unused load balancers and NAT gateways.
- Rightsizing across sizes and generations, non-production schedules and storage tiering.
- Planner that orders and de-duplicates recommendations, prices each against the remaining cost, blocks protected
  resources and sizes commitments on what is left.
- Cost anomaly detection with attribution, and allocation by tag.
- Suggested AWS, Azure and GCP commands, shell-quoted and never executed.
- Markdown, JSON and CSV reports, an optional grounded model summary, a deterministic simulator, a
  command-line interface, Docker image and CI workflows.
