# =============================================================================
# crs-bug-finding-gemini-cli Docker Bake Configuration
# =============================================================================
#
# Builds the CRS base image with Gemini CLI and Python dependencies.
#
# Usage:
#   docker buildx bake prepare
#   docker buildx bake --push prepare   # Push to registry
# =============================================================================

variable "REGISTRY" {
  default = "ghcr.io/team-atlanta"
}

variable "VERSION" {
  default = "cli-0.9.0"
}

variable "GEMINI_CLI_VERSION" {
  default = "0.9.0"
}

function "tags" {
  params = [name]
  result = [
    "${REGISTRY}/${name}:${VERSION}",
    "${name}:${VERSION}"
  ]
}

# -----------------------------------------------------------------------------
# Groups
# -----------------------------------------------------------------------------

group "default" {
  targets = ["prepare"]
}

group "prepare" {
  targets = ["gemini-cli-bug-finding-base"]
}

# -----------------------------------------------------------------------------
# Base Image
# -----------------------------------------------------------------------------

target "gemini-cli-bug-finding-base" {
  context    = "."
  dockerfile = "oss-crs/base.Dockerfile"
  tags       = tags("gemini-cli-bug-finding-base")
  args = {
    GEMINI_CLI_VERSION = GEMINI_CLI_VERSION
  }
}
