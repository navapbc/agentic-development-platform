terraform {
  required_version = ">= 1.11.0"

  backend "s3" {}

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.50, < 7.0"
    }
  }
}

provider "aws" {
  region = var.region
}
