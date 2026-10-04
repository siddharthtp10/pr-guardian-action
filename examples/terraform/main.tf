# INTENTIONALLY INSECURE - a demo input for PR Guardian. Never copy this.
# Each block below trips one or more rules; see examples/README.md.

resource "aws_security_group" "web" {
  name        = "web"
  description = "Web tier"

  # Public HTTPS is common for a load balancer: only a LOW finding (TF-006).
  ingress {
    description = "HTTPS from anywhere"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # SSH from anywhere is an administrative port: HIGH (TF-001).
  ingress {
    description = "SSH from anywhere"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # Egress to anywhere is normal and is NOT flagged.
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_vpc_security_group_ingress_rule" "ssh_open" {
  security_group_id = aws_security_group.web.id
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 22
  to_port           = 22
  ip_protocol       = "tcp"
}

resource "aws_s3_bucket_acl" "assets" {
  bucket = aws_s3_bucket.assets.id
  acl    = "public-read"
}

resource "aws_s3_bucket_public_access_block" "assets" {
  bucket                  = aws_s3_bucket.assets.id
  block_public_acls       = false
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

data "aws_iam_policy_document" "deploy" {
  statement {
    actions   = ["*"]
    resources = ["*"]
  }
}

locals {
  db_password = "hunter2hunter2"
}

resource "aws_db_instance" "main" {
  identifier          = "main"
  engine              = "postgres"
  publicly_accessible = true
  storage_encrypted   = false
}

resource "aws_eks_cluster" "main" {
  name     = "main"
  role_arn = aws_iam_role.eks.arn

  vpc_config {
    subnet_ids          = var.subnet_ids
    public_access_cidrs = ["0.0.0.0/0"]
  }
}

resource "aws_instance" "web" {
  ami           = "ami-0abcdef1234567890"
  instance_type = "t3.micro"

  metadata_options {
    http_tokens = "optional"
  }
}

resource "aws_ecr_repository" "app" {
  name                 = "app"
  image_tag_mutability = "MUTABLE"
}
