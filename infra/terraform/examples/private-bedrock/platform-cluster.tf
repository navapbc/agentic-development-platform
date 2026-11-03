module "ecs_cluster" {
  source = "../../ecs-cluster"

  create_ecs_cluster          = var.create_ecs_cluster
  name_prefix                 = var.name_prefix
  deployer_passable_role_arns = var.ecs_deployer_passable_role_arns
  tags                        = var.tags
}
