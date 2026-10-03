import pytest

from pr_guardian.redact import redact

# Built from pieces so no secret-shaped literal exists in the repo (push protection,
# and our own repo-wide scan, would flag it).
AWS = "AKIA" + "QWERTYUIOPASDFGH"
GH = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
GH_PAT = "github_pat_" + "11ABCDEFG0" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8" + "x9Y8z7W6v5U4"
ANTHROPIC = "sk-ant-" + "api03-AbCdEfGhIjKlMnOpQrStUv"
OPENAI_STYLE = "sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2"
SLACK = "xoxb-" + "1234567890-abcdefghij"
GOOGLE = "AIza" + "SyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q"
JWT = (
    "eyJ" + "hbGciOiJIUzI1NiJ9" + "." + "eyJ" + "zdWIiOiIxMjM0NTY3" + "." + "SflKxwRJSMeKKF2QT4fwpM"
)
PEM_BEGIN = "-----BEGIN RSA " + "PRIVATE KEY-----"
PEM_END = "-----END RSA " + "PRIVATE KEY-----"


@pytest.mark.parametrize(
    ("secret", "kind"),
    [
        (AWS, "aws-access-key"),
        (GH, "github-token"),
        (GH_PAT, "github-token"),
        (ANTHROPIC, "anthropic-key"),
        (OPENAI_STYLE, "api-key"),
        (SLACK, "slack-token"),
        (GOOGLE, "google-api-key"),
        (JWT, "jwt"),
    ],
)
def test_known_token_formats_are_removed(secret, kind):
    result = redact(f"value: {secret}\nnext line")
    assert secret not in result.text
    assert result.counts.get(kind) == 1, result.counts
    assert result.text.split("\n")[1] == "next line"


def test_line_count_is_always_preserved():
    text = f"a\n{PEM_BEGIN}\nMIIEvQIBADANBg\n{PEM_END}\nb\n\nc"
    assert len(redact(text).text.split("\n")) == len(text.split("\n"))


def test_private_key_block_is_masked_line_by_line():
    text = f"before\n{PEM_BEGIN}\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\nZm9vYmFy\n{PEM_END}\nafter"
    result = redact(text)
    lines = result.text.split("\n")
    assert lines[0] == "before" and lines[-1] == "after"
    assert all(line == "[REDACTED:private-key]" for line in lines[1:-1])
    assert "MIIE" not in result.text and result.counts == {"private-key": 1}


def test_truncated_private_key_block_is_masked_to_the_end():
    text = f"x\n{PEM_BEGIN}\nMIIEvQIBADANBg\nZm9vYmFy"
    assert redact(text).text.split("\n")[1:] == ["[REDACTED:private-key]"] * 3


def test_credential_assignments_keep_the_key_and_drop_the_value():
    text = 'db_password = "hunter2hunter2"\napi_key: abcd1234efgh5678\ntoken="zzzzzzzzzz"'
    out = redact(text).text
    assert "hunter2" not in out and "abcd1234" not in out and "zzzzzzzz" not in out
    assert out.startswith('db_password = "[REDACTED:credential]"')


@pytest.mark.parametrize(
    "line",
    [
        "password = var.db_password",
        "password: ${{ secrets.DB_PASSWORD }}",
        "token: {{ .Values.token }}",
        "secret = data.aws_secretsmanager_secret.x.arn",
        "api_key = local.key",
    ],
)
def test_references_to_secrets_are_not_redacted(line):
    # `var.x` is the SAFE pattern; masking it would only blind the reviewer.
    assert redact(line).text == line


def test_url_credentials_are_removed_but_host_stays():
    out = redact("dsn: postgres://admin:S3cr3tPass@db.internal:5432/app").text
    assert "S3cr3tPass" not in out and "admin" not in out
    assert "@db.internal:5432/app" in out


def test_auth_headers_keep_the_scheme_word():
    out = redact("Authorization: Bearer abcdefghijklmnop1234").text
    assert out == "Authorization: Bearer [REDACTED:auth-header]"


def test_pinned_shas_survive_but_other_long_hex_is_masked():
    sha1 = "0123456789abcdef0123456789abcdef01234567"
    sha256 = "0123456789abcdef" * 4
    assert redact(f"uses: org/x@{sha1}").text == f"uses: org/x@{sha1}"
    assert redact(f"image: x@sha256:{sha256}").text == f"image: x@sha256:{sha256}"
    odd_hex = "0123456789abcdef" * 2  # 32 hex chars: could be an md5-style secret
    assert odd_hex not in redact(f"k = {odd_hex}").text


def test_ordinary_long_identifiers_are_not_mistaken_for_secrets():
    text = "resource aws_vpc_security_group_ingress_rule_allow_https_from_vpn_only"
    assert redact(text).text == text


def test_high_entropy_blob_is_masked():
    blob = "Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MGFiY2RlZmdoaWprbG1ub3A"
    result = redact(f"data = {blob}")
    assert blob not in result.text and result.counts == {"high-entropy": 1}


def test_configured_secrets_are_removed_wherever_they_appear():
    key = "my-very-private-configured-value"
    out = redact(f"echo {key} and again {key}", (key,))
    assert key not in out.text and out.counts == {"configured-secret": 2}


def test_short_configured_values_are_ignored_to_avoid_shredding_words():
    assert redact("the cat sat", ("cat",)).text == "the cat sat"


def test_counts_never_contain_values_and_zero_kinds_are_omitted():
    result = redact(f"a = {AWS}")
    assert result.counts == {"aws-access-key": 1}
    assert result.total == 1 and AWS not in str(result)
