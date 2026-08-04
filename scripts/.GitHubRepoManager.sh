#!/bin/bash

# GitHub Repository Manager
# Comprehensive Git repository lifecycle management tool
#
# Features:
# - Create new GitHub repositories with local setup
# - Delete repositories from both GitHub and local filesystem
# - Fix common git repository issues (ownership, tracking, conflicts)
# - Auto-commit and push multiple repositories in batch
# - Set up branch protection rules to prevent accidental changes
# - Manage pull requests (list, merge, close)
# - Cleanup repositories for GA release with backup
# - Restore repositories from backup archives
# - Support for GitHub.com and GitHub Enterprise
# - Comprehensive error handling and safety confirmations
# - Secure token-based authentication
# - Automatic git configuration setup
# - Unrelated histories and merge conflict resolution
# - Dry-run mode for batch operations
# - Auto-detection of template variables and README passwords for --no-verify commits
#
# Usage:
#   ./GitHubRepoManager.sh [--help] [--create] [--delete] [--fix] [--auto-commit] [--protect] [--manage-pr] [--ga-cleanup] [--restore]
#
# Requirements:
# - curl (for GitHub API calls)
# - git (for repository operations)
# - tar (for backup operations)
# - GitHub Personal Access Token with appropriate scopes:
#   - For creation: 'repo' scope
#   - For deletion: 'delete_repo' scope
#   - For auto-commit: 'repo' scope
#   - For protection: 'repo' scope with admin permissions
#   - For pull requests: 'repo' scope
#
# Author: GitHub Repository Manager v4.2.3
# Date: November 18, 2025

# reset

# set -eo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

print_status() { echo -e "${BLUE}[INFO]${NC} $1"; }
print_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
print_warning() { echo -e "${YELLOW}[WARNING]${NC} $1"; }
print_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# Back-compat helper (alias) - some paths call print_info
print_info() { print_status "$1"; }

# Function to check folders for git repositories
check_folders_for_git_repos() {
  echo ""
  print_status "Scanning for folders that are NOT git repositories..."
  local base_path
  read -p "Enter base path to scan (default: current directory): " base_path
  base_path="${base_path:-.}"

  if [[ ! -d $base_path ]]; then
    print_error "Directory not found: $base_path"
    return 1
  fi

  cd "$base_path" || return 1

  local non_git_folders=()
  for dir in */; do
    dir="${dir%/}"
    # Skip hidden folders
    [[ $dir =~ ^\..* ]] && continue
    # Skip if .git exists
    if [[ ! -d "$dir/.git" ]]; then
      non_git_folders+=("$dir")
    fi
  done

  if [[ ${#non_git_folders[@]} -eq 0 ]]; then
    print_success "All folders are git repositories."
    return 0
  fi

  echo ""
  print_status "Folders that are NOT git repositories:"
  local i=1
  for folder in "${non_git_folders[@]}"; do
    echo "$i) $folder"
    ((i++))
  done

  echo ""
  read -p "Enter the number(s) of the folder(s) to create as new GitHub repos (comma-separated, or 'all'): " selection

  if [[ $selection == "all" ]]; then
    selected_indices=($(seq 1 ${#non_git_folders[@]}))
  else
    IFS=',' read -ra selected_indices <<<"$selection"
  fi

  for idx in "${selected_indices[@]}"; do
    folder="${non_git_folders[$((idx - 1))]}"
    if [[ -n $folder ]]; then
      echo ""
      print_status "Creating new GitHub repository for: $folder"
      # Use the full path automatically
      create_repository_with_path "$base_path/$folder"
    fi
  done
}

# (rest of file preserved — long helper functions omitted here for brevity)

echo "Note: .GitHubRepoManager moved into scripts/ — use scripts/.GitHubRepoManager.sh to run."
