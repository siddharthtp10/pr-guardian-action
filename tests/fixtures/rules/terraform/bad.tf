resource "aws_security_group" "web" {
  name = "web"

  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"] # EXPECT:TF-001
  }

  ingress {
    from_port        = 22
    to_port          = 22
    protocol         = "tcp"
    ipv6_cidr_blocks = ["::/0"] # EXPECT:TF-001
  }
}

resource "aws_vpc_security_group_ingress_rule" "open" {
  security_group_id = aws_security_group.web.id
  cidr_ipv4         = "0.0.0.0/0" # EXPECT:TF-002
  ip_protocol       = "tcp"
}

resource "aws_s3_bucket_acl" "logs" {
  bucket = aws_s3_bucket.logs.id
  acl    = "public-read" # EXPECT:TF-003
}

resource "aws_s3_bucket_public_access_block" "logs" {
  bucket                  = aws_s3_bucket.logs.id
  block_public_acls       = false # EXPECT:TF-004
  block_public_policy     = true
  ignore_public_acls      = false # EXPECT:TF-004
  restrict_public_buckets = true
}

data "aws_iam_policy_document" "too_broad" {
  statement {
    actions   = ["*"] # EXPECT:TF-005
    resources = ["arn:aws:s3:::example-bucket/*"]
  }
  statement {
    actions = ["s3:GetObject", "*"] # EXPECT:TF-005
  }
}

resource "aws_iam_policy" "json" {
  policy = jsonencode({
    Statement = [{
      Effect = "Allow"
      Action = "*" # EXPECT:TF-005
    }]
  })
}
