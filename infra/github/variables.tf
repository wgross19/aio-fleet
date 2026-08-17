variable "github_owner" {
  description = "GitHub owner or organization that owns the AIO repositories."
  type        = string
  default     = "wgross19"
}

variable "repositories" {
  description = "GitHub-owned repository state managed by the fleet."
  type = map(object({
    description                     = string
    visibility                      = optional(string, "public")
    topics                          = optional(list(string), [])
    homepage_url                    = optional(string, "https://aethereal.dev")
    has_issues                      = optional(bool, true)
    has_projects                    = optional(bool, false)
    has_wiki                        = optional(bool, false)
    has_discussions                 = optional(bool, true)
    has_downloads                   = optional(bool, true)
    delete_branch_on_merge          = optional(bool, true)
    allow_auto_merge                = optional(bool, false)
    allow_merge_commit              = optional(bool, true)
    allow_rebase_merge              = optional(bool, true)
    allow_squash_merge              = optional(bool, true)
    allow_update_branch             = optional(bool, true)
    vulnerability_alerts            = optional(bool, true)
    branch                          = optional(string, "main")
    required_checks                 = optional(list(string), [])
    required_check_app_id           = optional(number)
    required_check_app_ids          = optional(map(number), {})
    strict_required_checks          = optional(bool, true)
    enforce_admins                  = optional(bool, false)
    require_signed_commits          = optional(bool, true)
    force_push_bypassers            = optional(list(string), ["/wgross19"])
    required_linear_history         = optional(bool, false)
    require_conversation_resolution = optional(bool, true)
    required_approving_review_count = optional(number, 0)
    dismiss_stale_reviews           = optional(bool, true)
    require_code_owner_reviews      = optional(bool, false)
    require_last_push_approval      = optional(bool, false)
    actions_enabled                 = optional(bool, true)
    actions_allowed_actions         = optional(string, "selected")
    actions_github_owned_allowed    = optional(bool, true)
    actions_verified_allowed        = optional(bool, true)
    actions_sha_pinning_required    = optional(bool, true)
    actions_patterns_allowed = optional(list(string), [
      "wgross19/aio-fleet/.github/workflows/aio-*.yml@*",
      "peter-evans/create-pull-request@5f6978faf089d4d20b00c7766989d076bb2fc7f1",
    ])
    required_secrets   = optional(list(string), [])
    required_variables = optional(list(string), [])
  }))
}

variable "registry_publish_environment_reviewers" {
  description = "GitHub user IDs allowed to approve the protected registry-publish environment."
  type        = set(number)
  default     = [49853598]
}
