locals {
  aws_access_key = "@@AWS_KEY@@" # EXPECT:SEC-001
  db_password    = "hunter2hunter2" # EXPECT:SEC-003
  api_token      = 'abcd1234efgh5678' # EXPECT:SEC-003

  # Not secrets (each of these must stay silent):
  from_variable   = var.db_password
  interpolated    = "${var.db_password}"
  templated       = "{{ .Values.password }}"
  placeholder     = "changeme-please"
  password        = "short"
  secret_name     = "prod/db/credentials"
  doc_key         = "AKIAIOSFODNN7EXAMPLE"
  masked_password = "************"
  from_random     = random_password.db1.result
  from_module     = module.auth.token2
  from_data       = data.aws_ssm_parameter.pw2.value
}

# Secrets in comments still leak, so comments are scanned: @@PEM@@ EXPECT:SEC-002
