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
| TF-001 | high | Terraform | `0.0.0.0/0` or `::/0` inside an `ingress` block (egress is not flagged) |
| TF-002 | high | Terraform | `aws_vpc_security_group_ingress_rule` open to the internet |
| TF-003 | high | Terraform | S3 `acl = "public-read"` / `"public-read-write"` |
| TF-004 | high | Terraform | S3 Block Public Access setting set to `false` |
| TF-005 | high | Terraform | IAM `actions`/`Action` containing `"*"` |
| SEC-001 | critical | any | AWS access key ID (`AKIA...`/`ASIA...`) |
| SEC-002 | critical | any | Private key header |
| SEC-003 | medium | any | Password/token/key-named field assigned a literal string (heuristic) |
| K8S-001 | medium | Kubernetes | Container with no `resources.limits` (only when the whole container block is in the diff) |
| K8S-002 | critical | Kubernetes | `privileged: true` |
| K8S-003 | high | Kubernetes | `runAsUser: 0` or `runAsNonRoot: false` |
| K8S-004 | medium | Kubernetes | `image` with `:latest` or no tag |
| DOCKER-001 | medium | Dockerfile | `FROM ...:latest` |
| DOCKER-002 | medium | Dockerfile | `USER root` / `USER 0` |
| GUARD-001 | medium | all | A line over 10,000 characters was not scanned (built in, not a policy rule) |

## Known blind spots (by design, to avoid false positives)

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
