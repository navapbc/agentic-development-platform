variable "directory" {
  description = "Directory outside the repository holding the platform's config files. Empty reads none. A tenant-only file or image_exceptions.yaml in it is refused, because each tenant's files belong in its config bundle and platform finding exceptions live in the tenant config bucket."
  type        = string
  default     = ""

  validation {
    condition = var.directory == "" || length(setintersection(
      fileset(var.directory, "*.yaml"),
      ["repositories.yaml", "checks.yaml", "gate_engine.yaml", "github.yaml", "jira.yaml", "profiles.yaml", "escalation.yaml", "tickets.yaml"],
    )) == 0
    error_message = "The platform config directory holds a tenant-only file. Each of repositories.yaml, checks.yaml, gate_engine.yaml, github.yaml, jira.yaml, profiles.yaml, escalation.yaml, and tickets.yaml belongs in the tenant's config bundle under tenants/<tenant>/ of the tenant config bucket. Remove it from the directory."
  }

  validation {
    condition     = var.directory == "" || !contains(fileset(var.directory, "*"), "image_exceptions.yaml")
    error_message = "The platform config directory holds image_exceptions.yaml. Platform finding exceptions belong in platform/image_exceptions.yaml in the tenant config bucket, added through the image-exception workflow, and tenant exceptions belong in each tenant's config bundle. Remove the file from the directory."
  }
}
