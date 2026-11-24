output "files" {
  description = "Config file name to YAML content, which the platform tasks and pods write to PLATFORM_ENV_CONFIG_DIR at start."
  value       = local.files
}
