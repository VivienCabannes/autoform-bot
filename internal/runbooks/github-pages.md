# GitHub Pages dashboard publication

GitHub Pages is an optional, read-only snapshot. The loopback dashboard remains
the operational surface for live agents, queues, backend selection, cancellation,
and human verdict entry.

## Publication contract

The Pages artifact may contain only:

- graph structure;
- theorem content from committed `informal_content` files;
- proof status;
- review verdicts and rubric scores;
- committed kernel evidence.

It must not contain agent activity, task queues, dispatcher logs, provider or
backend configuration, credentials, review notes or reviewer identities, local
filesystem paths, or uncommitted state.

## Inspect before asking

From the target Git repository:

```bash
uv run --directory "<AUTOFORM_PLUGIN_ROOT>" python \
  "<AUTOFORM_PLUGIN_ROOT>/scripts/configure_github_pages.py" \
  --repo-root "$PROJECT_DIR" inspect
```

Show the complete JSON result to the user. Stop when `visibility` is `unclear`.
For `private` or `internal`, verify Pages availability and access control against
the user's GitHub plan or enterprise policy. Repository privacy alone does not
prove the deployed Pages site has the intended audience.

## Install after approval

After explicit approval to add the two configuration files:

```bash
uv run --directory "<AUTOFORM_PLUGIN_ROOT>" python \
  "<AUTOFORM_PLUGIN_ROOT>/scripts/configure_github_pages.py" \
  --repo-root "$PROJECT_DIR" install \
  --graph <GRAPH_PATH_RELATIVE_TO_REPOSITORY> \
  --site .autoform/site \
  --approve-publication
```

The generated workflow pins every GitHub action and the Autoform exporter to
full commit hashes. It checks out the project, exports the site, uploads only the
site directory, and deploys with the official Pages actions.

The workflow publishes an informal blueprint only when a built
`blueprint/web/index.html` and its assets are already committed. Build and review
that directory before committing it. Deployment never installs TeX or floating
Python packages; when the committed build is absent, it publishes the dashboard
without a blueprint.

Do not create a GitHub repository, commit or push files, enable Pages, or open a
browser under the approval above. Each is a separate outward-facing action and
requires explicit approval at that point.

After separate approval to enable Pages, inspect the current setting before
changing it:

```bash
gh api "repos/<OWNER>/<REPOSITORY>/pages"
```

If Pages is not configured, enable workflow builds:

```bash
gh api --method POST "repos/<OWNER>/<REPOSITORY>/pages" -f build_type=workflow
```

Do not open the deployed URL without another explicit approval.

## Durable update boundary

The exporter refuses publication when `graph.json`, `informal_content/`,
`kernel/`, or `review_status.json` is dirty. Operational files are neither read
nor placed in the artifact. A dashboard update therefore follows this sequence:

1. commit durable graph/content/review/kernel changes;
2. push the commit;
3. let the generated workflow export and deploy the snapshot.

The static artifact contains all local CSS, JavaScript, node pages, cluster
pages, and JSON data it needs. It does not depend on the local plugin cache or a
Python server after export.
