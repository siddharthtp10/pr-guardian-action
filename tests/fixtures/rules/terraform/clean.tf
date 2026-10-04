resource "aws_security_group" "web" {
  name = "web"

  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["10.0.0.0/8"]
  }

  # Egress to anywhere is normal and must NOT be flagged as an ingress problem.
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.web.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

# ingress { cidr_blocks = ["0.0.0.0/0"] }   <- commented-out code is ignored

resource "aws_s3_bucket_acl" "logs" {
  bucket = aws_s3_bucket.logs.id
  acl    = "private"
}

resource "aws_s3_bucket_public_access_block" "logs" {
  bucket                  = aws_s3_bucket.logs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

data "aws_iam_policy_document" "scoped" {
  statement {
    actions   = ["s3:GetObject", "s3:ListBucket"]
    resources = ["*"]
  }
  statement {
    not_actions = ["iam:*"]
    resources   = ["*"]
  }
}

resource "aws_iam_policy" "json" {
  description = "Grants read access"
  policy = jsonencode({
    Statement = [{
      Effect = "Allow"
      Action = "s3:GetObject"
    }]
  })
}

# Public web ingress is not an admin/DB port, so only the low rule would apply, and
# restricted CIDRs never trigger anything. Neither is a finding here.
resource "aws_security_group" "internal" {
  name = "internal"

  ingress {
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["10.0.0.0/8"]
  }

  ingress {
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = ["10.20.0.0/16"]
  }
}

resource "aws_db_instance" "main" {
  identifier          = "main"
  publicly_accessible = false
  storage_encrypted   = true
}

resource "aws_ebs_volume" "scratch" {
  availability_zone = "eu-west-1a"
  encrypted         = true
}

resource "aws_eks_cluster" "main" {
  name = "main"

  vpc_config {
    endpoint_public_access = false
    public_access_cidrs    = ["203.0.113.0/24"]
  }
}

resource "aws_instance" "web" {
  ami = "ami-0abcdef1234567890"

  metadata_options {
    http_tokens = "required"
  }
}

resource "aws_ecr_repository" "app" {
  name                 = "app"
  image_tag_mutability = "IMMUTABLE"
}
