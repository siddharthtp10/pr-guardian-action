# Examples

Four **intentionally insecure** files used by the demo. They exist so that
PR Guardian has something to find. Never copy them into real infrastructure.

| File | What it shows | Rules it should trigger |
|---|---|---|
| [`terraform/main.tf`](terraform/main.tf) | Security group open to the internet on ingress (and a harmless open *egress*, which must not be flagged), open ingress rule resource, public S3 ACL, Block Public Access switched off, `"*"` IAM actions, a hard-coded password | `TF-001` `TF-002` `TF-003` `TF-004` `TF-005` `SEC-003` |
| [`kubernetes/deployment.yaml`](kubernetes/deployment.yaml) | A container with no resource limits, `:latest` image, privileged, running as root | `K8S-001` `K8S-002` `K8S-003` `K8S-004` |
| [`workflows/pr-guardian-demo-bad-workflow.yml`](workflows/pr-guardian-demo-bad-workflow.yml) | Unpinned third-party and first-party actions, an attacker-controlled value expanded inside `run:` | `GHA-001` `GHA-002` `GHA-003` |
| [`docker/Dockerfile`](docker/Dockerfile) | `FROM ...:latest`, running as root | `DOCKER-001` `DOCKER-002` |

The workflow example is **inert**: it can only be started by hand
(`workflow_dispatch`) and its only job has `if: false`, so putting it on a demo
branch cannot run anything.

`SEC-001` (AWS key) and `SEC-002` (private key) are not shown here: a file
containing key-shaped text would be rejected by GitHub push protection. They are
covered by the test suite, which assembles such strings at run time.

## How the demo uses these files

[`manifest.tsv`](manifest.tsv) maps each file to where it lands on the demo
branch and lists the rules that must fire. The paths matter because PR Guardian
decides what a file is from its path:

- Terraform, Kubernetes and Dockerfile examples go under `demo-pr/`, which the
  repository's own review does **not** exclude, so the demo PR is reviewed.
- The workflow example must live under `.github/workflows/` to be recognised
  as a workflow, so it lands there.

`examples/` itself is excluded from the repository's own review (a repo should
not fail its own check on its demo inputs), and the tests assert that the files
above really trigger exactly the listed rules.

```bash
scripts/open-demo-pr.sh --dry-run
```
