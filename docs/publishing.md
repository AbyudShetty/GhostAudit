# Publish GhostAudit

Suggested GitHub repository name: **`ghostaudit`**.

Suggested description:

> Evidence-backed data deletion audits across databases, replicas, logs and backups. Built for HackerRank Orchestrate, October 2026.

The commands below are for you to run after reviewing the changes. They have not been executed by this cleanup.

## Prepare a clean commit

The original repository already tracks the supplied dataset, root transcript and prediction CSV. Remove those paths from the index while retaining every local file, then stage the project cleanup:

```sh
git rm -r --cached --ignore-unmatch dataset log.txt output.csv
git add .
git status --short
git diff --cached --stat
git diff --cached --check
```

Review the staged diff before committing. The dataset, transcript, generated reports and archives should be absent from the new source tree. This removes files from the next commit; it does not erase copies in earlier commits.

```sh
git commit -m "Present GhostAudit as a standalone deletion audit project"
```

## Rename the existing GitHub repository

Rename `Orchestrate-October26` to `ghostaudit` in GitHub's repository settings, then update your checkout and push:

```sh
git remote set-url origin https://github.com/AbyudShetty/ghostaudit.git
git push -u origin main
```

## Use a new GitHub repository

Alternatively, create an empty repository named `ghostaudit` under your account, then use the same remote and push commands above. Keep it empty when creating it: this checkout already has a README and Git history.

Neither route requires a force push. Renaming the local checkout folder is optional and does not affect the source paths.
