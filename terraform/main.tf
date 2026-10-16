# =============================================================================
# Terraform: infrastructure scaffold
# =============================================================================
# This is a scaffold, not a working configuration: it declares no provider and
# no resource, and `terraform validate` passes because there is nothing to
# validate yet. It marks where provisioning belongs once the manual parts of the
# platform (VPS, DNS) should become code.
#
# Typical candidates:
#   - DNS records for the domains in services.yml
#   - VPS provisioning
#   - Firewall rules at the provider level (in addition to ufw_rules in Ansible)

terraform {
  required_version = ">= 1.5"
  required_providers {
    # Example providers (uncomment as needed):
    # docker = {
    #   source  = "kreuzwerker/docker"
    #   version = "~> 3.0"
    # }
    # cloudflare = {
    #   source  = "cloudflare/cloudflare"
    #   version = "~> 4.0"
    # }
  }
}

# --- Provider configuration (examples) ---
# provider "docker" {
#   host = "unix:///var/run/docker.sock"
# }
#
# provider "cloudflare" {
#   api_token = var.cloudflare_api_token
# }
