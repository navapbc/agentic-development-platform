locals {
  files = var.directory == "" ? {} : {
    for name in fileset(var.directory, "*.yaml") : name => file("${var.directory}/${name}")
  }
}
