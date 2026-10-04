# Rule reference

Rules live in [`src/pr_guardian/policies/default.yaml`](../src/pr_guardian/policies/default.yaml).
Severity reflects **impact x confidence**: exact patterns (a private key header)
are `critical`; heuristics that can have harmless matches are `medium`.
Only added lines are ever flagged.

| ID | Severity | Applies to | Flags |
|---|---|---|---|
| GHA-001 | high | GitHub Actions | Third-party action not pinned to a full commit SHA |
| GHA-002 | medium | GitHub Actions | `actions/*` / `github/*` action not pinned to a full commit SHA |
| GHA-003 | high | GitHub Actions | Attacker-controllable expression (PR title, branch, comment...) expanded inside `run:`/`script:` |
| GHA-004 | high | GitHub Actions | `permissions: write-all` |
| TF-001 | high | Terraform | `0.0.0.0/0` or `::/0` inside an `ingress` block **on an administrative or database port, or on all ports/protocols** (egress is not flagged) |
| TF-002 | high | Terraform | The same, for `aws_vpc_security_group_ingress_rule` |
| TF-003 | high | Terraform | S3 `acl = "public-read"` / `"public-read-write"` |
| TF-004 | high | Terraform | S3 Block Public Access setting set to `false` |
| TF-005 | high | Terraform | IAM `actions`/`Action` containing `"*"` |
| TF-006 | low | Terraform | Ingress open to the internet on any **other** port (normal for public web services; confirm it is intended) |
| TF-007 | high | Terraform | `publicly_accessible = true` (RDS and similar) |
| TF-008 | medium | Terraform | `storage_encrypted = false` / `encrypted = false` |
| TF-009 | medium | Terraform | EKS `public_access_cidrs` containing `0.0.0.0/0` (single-line list) |
| TF-010 | medium | Terraform | Instance metadata v1 allowed (`http_tokens = "optional"`) |
| TF-011 | low | Terraform | Container repository with `image_tag_mutability = "MUTABLE"` |
| SEC-001 | critical | any | AWS access key ID (`AKIA...`/`ASIA...`) |
| SEC-002 | critical | any | Private key header |
| SEC-003 | medium | any | Password/token/key-named field assigned a literal value, **quoted or unquoted** (heuristic) |
| K8S-001 | medium | Kubernetes | Container with no `resources.limits` (only when the whole container block is in the diff) |
| K8S-002 | critical | Kubernetes | `privileged: true` |
| K8S-003 | high | Kubernetes | `runAsUser: 0` or `runAsNonRoot: false` |
| K8S-004 | medium | Kubernetes | `image` with `:latest` or no tag |
| K8S-005 | high | Kubernetes | `hostNetwork` / `hostPID` / `hostIPC: true` |
| K8S-006 | medium | Kubernetes | `allowPrivilegeEscalation: true` |
| DOCKER-001 | medium | Dockerfile | `FROM ...:latest` |
| DOCKER-002 | medium | Dockerfile | `USER root` / `USER 0` |
| DOCKER-003 | medium | Dockerfile | A downloaded script piped into a shell (`curl ... \| sh`) |
| GUARD-001 | medium | all | A line over 10,000 characters was not scanned (built in, not a policy rule) |

### How the severities were chosen

Severity is **impact x confidence**, and only `high` and `critical` fail the
default check (`fail-on: high`). So a rule gets `high` only when a match is
almost always a real problem, `medium` for heuristics or context-dependent
findings, and `low` for "worth a glance". For example, an ingress open to the
internet is `high` on SSH, RDP, database ports or all ports, but only `low` on
443, because a public load balancer legitimately looks exactly like that.

## Known blind spots (by design, to avoid false positives)

- **Sibling-line rules need the whole block.** TF-001, TF-002 and TF-006 look at
  the other lines of the same `ingress { ... }` or resource block (the port,
  the protocol). If the closing line of that block is not in the diff, they stay
  silent rather than guess, and a port range that merely *contains* an
  administrative port (for example 0-1024) is not detected.
- Multi-line lists are not inspected (TF-009 sees only the single-line form).
- SEC-003's unquoted form requires a digit in the value, to keep prose quiet; an
  all-letters unquoted password is missed.

- **The diff is all we see.** A rule that needs context outside the changed
  hunks stays silent rather than guessing (for example an `ingress {` opener or
  a container's closing line that is not in the diff).
- Dockerfiles: an *absent* `USER` instruction and untagged `FROM` lines are not
  flagged (the file as a whole is not visible, and `FROM stage-name` is valid).
- Terraform: service-level wildcards (`s3:*`), `Resource: "*"`, multi-line
  action lists, and `aws_security_group_rule` with `type = "ingress"` are not
  covered. Kubernetes: pod-level `securityContext` inheritance is not modelled.
- Kubernetes detection is path-based, so Helm `values.yaml` with a separate
  `tag:` can trigger K8S-004 (documented in the rule message).
- Regex rules read indentation, so they assume formatted code (`terraform fmt`).
