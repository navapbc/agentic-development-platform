resource "aws_iam_role" "reader" {
  count = local.create_roles ? 1 : 0

  name                 = "${var.name_prefix}-rebar-tickets-reader"
  description          = "Fetches the rebar ticket store."
  path                 = var.iam_path
  permissions_boundary = var.permissions_boundary_arn == "" ? null : var.permissions_boundary_arn
  assume_role_policy   = local.assume_policy

  tags = var.tags
}

resource "aws_iam_role" "writer" {
  count = local.create_roles ? 1 : 0

  name                 = "${var.name_prefix}-rebar-tickets-writer"
  description          = "Pushes the rebar ticket store."
  path                 = var.iam_path
  permissions_boundary = var.permissions_boundary_arn == "" ? null : var.permissions_boundary_arn
  assume_role_policy   = local.assume_policy

  tags = var.tags
}

resource "aws_iam_role_policy" "reader" {
  count = local.create_roles ? 1 : 0

  name   = "rebar-tickets-read"
  role   = aws_iam_role.reader[0].id
  policy = local.reader_policy
}

resource "aws_iam_role_policy" "writer" {
  count = local.create_roles ? 1 : 0

  name   = "rebar-tickets-write"
  role   = aws_iam_role.writer[0].id
  policy = local.writer_policy
}
