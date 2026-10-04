resource "aws_security_group" "web" {
  name = "web"

  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"] # EXPECT:TF-006
  }

  ingress {
    from_port        = 22
    to_port          = 22
    protocol         = "tcp"
    ipv6_cidr_blocks = ["::/0"] # EXPECT:TF-001
  }
}

resource "aws_vpc_security_group_ingress_rule" "rdp_open" {
  security_group_id = aws_security_group.web.id
  cidr_ipv4         = "0.0.0.0/0" # EXPECT:TF-002
  from_port         = 3389
  to_port           = 3389
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "https_open" {
  security_group_id = aws_security_group.web.id
  cidr_ipv4         = "0.0.0.0/0" # EXPECT:TF-006
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

resource "aws_security_group" "everything" {
  name = "everything"

  ingress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"] # EXPECT:TF-001
  }

  ingress {
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"] # EXPECT:TF-001
  }
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

resource "aws_db_instance" "main" {
  identifier          = "main"
  publicly_accessible = true # EXPECT:TF-007
  storage_encrypted   = false # EXPECT:TF-008
}

resource "aws_ebs_volume" "scratch" {
  availability_zone = "eu-west-1a"
  encrypted         = false # EXPECT:TF-008
}

resource "aws_eks_cluster" "main" {
  name = "main"

  vpc_config {
    endpoint_public_access = true
    public_access_cidrs    = ["203.0.113.0/24", "0.0.0.0/0"] # EXPECT:TF-009
  }
}

resource "aws_instance" "web" {
  ami = "ami-0abcdef1234567890"

  metadata_options {
    http_tokens = "optional" # EXPECT:TF-010
  }
}

resource "aws_ecr_repository" "app" {
  name                 = "app"
  image_tag_mutability = "MUTABLE" # EXPECT:TF-011
}
