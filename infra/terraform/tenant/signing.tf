# The tenant signs its own images with this profile, and the image policy trusts it only for the
# tenant's prefix. Signer cancels a destroyed profile and never frees its name, so the name is
# generated, and it allows letters, digits, and underscores only.
resource "aws_signer_signing_profile" "this" {
  count = local.enabled ? 1 : 0

  name_prefix = "${replace(local.name, "-", "_")}_"
  platform_id = "Notation-OCI-SHA384-ECDSA"

  tags = local.tags
}
