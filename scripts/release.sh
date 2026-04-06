#!/usr/bin/env bash
# Usage: ./scripts/release.sh 0.2.0
# Bumps version in pyproject.toml, commits (if changed), tags, and pushes —
# triggering the GitHub Actions publish workflow.

set -euo pipefail

VERSION="${1:-}"

if [ -z "$VERSION" ]; then
  echo "Usage: $0 <version>   e.g. $0 0.2.0"
  exit 1
fi

TAG="v${VERSION}"

echo "Releasing abrasio-mcp ${TAG}..."

# Bump version in pyproject.toml
sed -i "s/^version = \".*\"/version = \"${VERSION}\"/" pyproject.toml

# Only commit if pyproject.toml actually changed (version was already correct = no commit needed)
if ! git diff --quiet pyproject.toml; then
  git add pyproject.toml
  git commit -m "chore: release ${TAG}"
else
  echo "Version already ${VERSION} in pyproject.toml — skipping version commit."
fi

# Fail early if this tag already exists
if git rev-parse "${TAG}" >/dev/null 2>&1; then
  echo "Error: tag ${TAG} already exists locally. Delete it first: git tag -d ${TAG}"
  exit 1
fi

# Tag and push
git tag "${TAG}"
git push origin HEAD
git push origin "${TAG}"

echo "Pushed ${TAG} — GitHub Actions will build and publish to PyPI."
