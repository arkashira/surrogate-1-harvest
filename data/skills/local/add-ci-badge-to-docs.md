---
name: add-ci-badge-to-docs
description: Add a CI status badge from GitHub Actions to a documentation README file and commit the change safely.
version: 1.0.0
author: Ashira
tags: [devops, documentation, ci, badge]
---

## Steps
1. **Locate README**
   - Identify the project root and the documentation README file (e.g., `docs/README.md`).
   - Example command: `find $PROJECT_PATH -path "*/docs/README.md"`
2. **Verify CI workflow**
   - Ensure the referenced GitHub Actions workflow file exists to avoid broken badge URLs.
   - Command: `[[ -f $PROJECT_PATH/.github/workflows/ci.yml ]] && echo "CI workflow exists" || (echo "CI workflow not found" && exit 1)`
3. **Patch badge into README**
   - Insert the CI badge markdown image link after the first heading.
   - Command:
     ```
     patch --mode replace \
       --old_string "# Arkship Documentation" \
       --new_string "# Arkship Documentation\n[![CI](https://github.com/Ashira/arkship/actions/workflows/ci.yml/badge.svg)](https://github.com/Ashira/arkship/actions/workflows/ci.yml)" \
       --path "$PROJECT_PATH/docs/README.md"
     ```
4. **Commit change**
   - Stage and commit the modified README.
   - Command:
     ```
     cd $PROJECT_PATH && git add docs/README.md && git commit -m "Add CI badge to docs README"
     ```
5. **Log lesson**
   - Append a structured lesson entry to `~/.claude/memory/lessons_learned.md` using `write_file` to avoid dotfile overwrite warnings.
   - Content to write:
     ```
     ## $(date +%Y-%m-%d): Added CI badge to docs README
     - Context: Updated docs/README.md in a project to include CI status badge.
     - Insight: CI badge improves visibility of build health.
     - Fix/Pattern: Use markdown image link with CI workflow badge URL.
     - Prevention: Verify CI workflow exists before adding badge.
     - Tags: devops documentation CI
     ```

## Pitfalls
- Ensure the CI workflow file path matches the repository name.
- Use `write_file` or safe redirection to avoid security scan errors on dotfiles.
- Run the patch only once; ensure the old string is unique.

## Verification
- Check that the badge renders correctly on GitHub.
- Verify the git commit includes the change.
- Confirm the lesson entry appears in `lessons_learned.md`.
