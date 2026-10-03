import pytest

from pr_guardian.filetypes import FileKind, classify, glob_match

K = FileKind


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        ("infra/main.tf", K.TERRAFORM),
        ("infra/prod.tfvars", K.TERRAFORM),
        ("MAIN.TF", K.TERRAFORM),
        ("Dockerfile", K.DOCKERFILE),
        ("services/api/Dockerfile.prod", K.DOCKERFILE),
        ("build/app.dockerfile", K.DOCKERFILE),
        (".github/workflows/ci.yml", K.GITHUB_ACTIONS),
        (".github/workflows/deploy.yaml", K.GITHUB_ACTIONS),
        (".github/actions/setup/action.yml", K.GITHUB_ACTIONS),
        ("action.yml", K.GITHUB_ACTIONS),
        ("k8s/deployment.yaml", K.KUBERNETES),
        ("charts/app/templates/svc.yml", K.KUBERNETES),
    ],
)
def test_supported_files(path, kind):
    assert classify(path) is kind


@pytest.mark.parametrize(
    "path",
    [
        "README.md",
        "src/app.py",
        "Dockerfile.md",  # docs about a Dockerfile, not a Dockerfile
        ".github/dependabot.yml",
        ".github/ISSUE_TEMPLATE/bug.yml",
        ".github/workflows/nested/ci.yml",  # GitHub does not run nested workflows
        "docker-compose.yml",
        ".pre-commit-config.yaml",
        "package.json",
        "terraform.tfstate",
    ],
)
def test_unsupported_files(path):
    assert classify(path) is None


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("*.tf", "infra/deep/main.tf", True),  # no slash -> matches basename anywhere
        ("*.tf", "infra/main.tfvars", False),
        ("infra/*.tf", "infra/main.tf", True),
        ("infra/*.tf", "infra/sub/main.tf", False),  # * does not cross directories
        ("infra/**/*.tf", "infra/a/b/main.tf", True),
        ("infra/**/*.tf", "infra/main.tf", True),  # ** may match zero directories
        ("infra/**", "infra/a/b.txt", True),
        ("k8s/?.yaml", "k8s/a.yaml", True),
        ("k8s/?.yaml", "k8s/ab.yaml", False),
        ("a.b", "axb", False),  # dots are literal, not regex wildcards
    ],
)
def test_glob_match(pattern, path, expected):
    assert glob_match(pattern, path) is expected
