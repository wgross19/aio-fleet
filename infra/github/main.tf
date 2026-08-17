provider "github" {
  owner = var.github_owner
}

resource "github_repository" "aio" {
  #checkov:skip=CKV_GIT_1: Public repos are intentional for Community Apps and portfolio visibility.
  for_each = var.repositories

  name                   = each.key
  description            = each.value.description
  visibility             = each.value.visibility
  homepage_url           = each.value.homepage_url
  has_issues             = each.value.has_issues
  has_projects           = each.value.has_projects
  has_wiki               = each.value.has_wiki
  has_discussions        = each.value.has_discussions
  has_downloads          = each.value.has_downloads
  delete_branch_on_merge = each.value.delete_branch_on_merge
  allow_auto_merge       = each.value.allow_auto_merge
  allow_merge_commit     = each.value.allow_merge_commit
  allow_rebase_merge     = each.value.allow_rebase_merge
  allow_squash_merge     = each.value.allow_squash_merge
  allow_update_branch    = each.value.allow_update_branch
  topics                 = each.value.topics

  archive_on_destroy = false

  lifecycle {
    ignore_changes = [
      template,
    ]
  }
}

resource "github_repository_vulnerability_alerts" "aio" {
  for_each = var.repositories

  repository = github_repository.aio[each.key].name
  enabled    = each.value.vulnerability_alerts
}

resource "github_branch_protection" "main" {
  #checkov:skip=CKV_GIT_5: Required review counts are policy-managed per repository.
  #checkov:skip=CKV_GIT_6: Signed commit enforcement is policy-managed per repository.
  for_each = {
    for name, repo in var.repositories : name => repo
    if length(repo.required_checks) > 0
  }

  repository_id                   = github_repository.aio[each.key].node_id
  pattern                         = each.value.branch
  enforce_admins                  = each.value.enforce_admins
  require_signed_commits          = each.value.require_signed_commits
  force_push_bypassers            = each.value.force_push_bypassers
  required_linear_history         = each.value.required_linear_history
  require_conversation_resolution = each.value.require_conversation_resolution

  dynamic "required_status_checks" {
    for_each = (
      each.value.required_check_app_id == null && length(each.value.required_check_app_ids) == 0
    ) ? [each.value] : []
    content {
      strict   = required_status_checks.value.strict_required_checks
      contexts = required_status_checks.value.required_checks
    }
  }

  required_pull_request_reviews {
    dismiss_stale_reviews           = each.value.dismiss_stale_reviews
    require_code_owner_reviews      = each.value.require_code_owner_reviews
    require_last_push_approval      = each.value.require_last_push_approval
    required_approving_review_count = each.value.required_approving_review_count
  }
}

resource "github_repository_ruleset" "trusted_required_checks" {
  for_each = {
    for name, repo in var.repositories : name => repo
    if length(repo.required_checks) > 0 && (
      repo.required_check_app_id != null || length(repo.required_check_app_ids) > 0
    )
  }

  repository  = github_repository.aio[each.key].name
  name        = "Trusted required checks"
  target      = "branch"
  enforcement = "active"

  conditions {
    ref_name {
      include = ["~DEFAULT_BRANCH"]
      exclude = []
    }
  }

  rules {
    required_status_checks {
      strict_required_status_checks_policy = each.value.strict_required_checks

      dynamic "required_check" {
        for_each = each.value.required_checks
        content {
          context = required_check.value
          integration_id = lookup(
            each.value.required_check_app_ids,
            required_check.value,
            each.value.required_check_app_id,
          )
        }
      }
    }
  }
}

resource "github_actions_repository_permissions" "aio" {
  for_each = var.repositories

  repository           = github_repository.aio[each.key].name
  enabled              = each.value.actions_enabled
  allowed_actions      = each.value.actions_allowed_actions
  sha_pinning_required = each.value.actions_sha_pinning_required

  allowed_actions_config {
    github_owned_allowed = each.value.actions_github_owned_allowed
    verified_allowed     = each.value.actions_verified_allowed
    patterns_allowed     = each.value.actions_patterns_allowed
  }
}

resource "github_repository_environment" "registry_publish" {
  repository          = github_repository.aio["aio-fleet"].name
  environment         = "registry-publish"
  can_admins_bypass   = false
  prevent_self_review = true

  reviewers {
    users = var.registry_publish_environment_reviewers
  }

  deployment_branch_policy {
    protected_branches     = true
    custom_branch_policies = false
  }
}

output "declared_repository_secrets" {
  description = "Secret names declared by policy. Values are intentionally not managed by OpenTofu."
  value = {
    for name, repo in var.repositories : name => repo.required_secrets
    if length(repo.required_secrets) > 0
  }
}

output "declared_repository_variables" {
  description = "Variable names declared by policy. Values can be managed later if they are non-sensitive and stable."
  value = {
    for name, repo in var.repositories : name => repo.required_variables
    if length(repo.required_variables) > 0
  }
}
