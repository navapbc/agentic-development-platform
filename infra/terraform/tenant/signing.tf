locals {
  # Signer caps name_prefix at 38 letters, digits, and underscores. A prefix that fits keeps the
  # <name_prefix>_tenant_<tenant>_ form, and a longer one keeps the whole tenant name behind a
  # truncated name_prefix and a hash of it, so the prefix stays unique to the tenant.
  signing_profile_name_limit  = 38
  signing_profile_name_prefix = replace(var.name_prefix, "/[^0-9A-Za-z_]/", "_")
  signing_profile_full_prefix = "${local.signing_profile_name_prefix}_tenant_${var.tenant}_"
  signing_profile_hash        = substr(sha256(var.name_prefix), 0, 8)
  signing_profile_prefix = (
    length(local.signing_profile_full_prefix) <= local.signing_profile_name_limit
    ? local.signing_profile_full_prefix
    : "${substr(local.signing_profile_name_prefix, 0, local.signing_profile_name_limit - length(var.tenant) - 11)}_${local.signing_profile_hash}_${var.tenant}_"
  )
}

# The tenant signs its own images with this profile, and the image policy trusts it only for the
# tenant's prefix. Signer cancels a destroyed profile and never frees its name, so the name is
# generated.
resource "aws_signer_signing_profile" "this" {
  count = local.enabled ? 1 : 0

  name_prefix = local.signing_profile_prefix
  platform_id = "Notation-OCI-SHA384-ECDSA"

  tags = local.tags

  lifecycle {
    precondition {
      condition     = can(regex("^[0-9A-Za-z_]{0,38}$", local.signing_profile_prefix))
      error_message = "The signing profile name_prefix must be at most 38 letters, digits, and underscores."
    }
  }
}
