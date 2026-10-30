terraform {
  required_version = ">= 1.11.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.50, < 7.0"
    }
    random = {
      source  = "hashicorp/random"
      version = ">= 3.7, < 4.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = ">= 2.7, < 3.0"
    }
  }
}
