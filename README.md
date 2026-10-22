# girder-delta-acls

A Girder 5 plugin that changes what "apply to subfolders" means when you edit the access
list of a collection or folder.

Core Girder copies the **whole** access list onto every subfolder you administer. Anything
set individually on a subfolder is lost: if Bob had WRITE on one subfolder only and you add
Carol to the collection, Bob loses his WRITE. This plugin pushes down **only what you
changed** — the users and groups you added, removed or edited, plus the public setting if
you changed it — and leaves everything else on each subfolder alone.

The old "make everything match" behaviour is still there, as a separate, clearly labelled
choice.

## In the access dialog

When **Also apply to subfolders** is selected, two choices appear:

- **Apply only the changes I made** (the default). A live summary shows how many subfolders
  will be updated, how many are skipped because you are not an admin there, and exactly
  which changes will be pushed (`+ Carol (Read)`, `Bob: Write → Admin`, `− Staff (group)`).
- **Replace subfolder permissions with this list** — core's behaviour. The summary warns how
  many subfolders have permissions of their own that will be overwritten, and saving asks for
  confirmation when any do.

## Semantics

For each user or group you changed on the target, on each subfolder you administer:

| Change on the target | Subfolder has no entry | Subfolder has an entry |
|---|---|---|
| added | entry added | level overwritten, flags added |
| level changed | entry added | level overwritten, flags +/- applied |
| only flags changed | nothing | flags +/- applied |
| removed | nothing | entry removed |

`public` is set on subfolders only if you changed it; public flags get the same +/- treatment.
Traversal is the same as core's: a subfolder you do not administer is skipped **together with
everything below it**. Admin-only access flags are never added to or removed from subfolders
by a non-site-admin.

Each subfolder is updated with one atomic MongoDB write, so concurrent changes (a rename,
another plugin's ACL update) are never lost. The tree as a whole is not transactional.

## API

`PUT /folder/:id/access` and `PUT /collection/:id/access` take one extra parameter:

| Parameter | Values | Meaning |
|---|---|---|
| `recurseMode` | `delta`, `replace` | Only used with `recurse=true`. Omitted → the `delta_acls.default_recurse_mode` setting. |

The parameter is documented on core's own routes in the API docs (Swagger). Responses are
identical to core's. A delta-mode save also sets a `Girder-Delta-Acls` header,
e.g. `{"mode": "delta", "changed": true, "folders": 34}`.

`POST /folder/:id/access/preview` and `POST /collection/:id/access/preview` take the same
`access` / `public` / `publicFlags` as the save, write nothing, and return the delta, the
subfolder counts and the number of subfolders a replace would overwrite (`divergent`).

When a delta-mode save writes anything, the plugin fires a `delta_acls.propagate.after` event
with `root`, `rootType`, `delta`, `folderIds` and `user`. The bulk writes fire no
`model.folder.save` events.

Code calling `Folder().setAccessList(...)` directly is unaffected; only the REST endpoints are
intercepted.

## Settings

| Key | Default | Meaning |
|---|---|---|
| `delta_acls.default_recurse_mode` | `replace` | What `recurse=true` without `recurseMode` means. `replace` keeps existing scripts and `girder_client` behaving as before. The web UI always sends the mode. |
| `delta_acls.replace_requires_site_admin` | `false` | When true, only site admins may use `replace`; everyone else gets 403 and the option is disabled in the dialog. |

Both can be edited on the plugin's configuration page (**Admin console → Plugins → Delta ACLs**).

## Requirements

Girder ≥ 5.0.6 and MongoDB ≥ 4.2 (aggregation-pipeline updates; Girder itself requires 4.4).

## Development

```bash
uv sync                                           # .venv with the plugin and dev tools
cd girder_delta_acls/web_client && npm ci && npm run build && cd -   # needed before Girder starts
tox -e lint && tox -e pytest                      # ruff; pytest against a released Girder
npm ci && npm run lint                            # eslint, pug-lint, stylelint
girder_delta_acls/tests/e2e/run.sh                # browser checks, see tests/e2e/README.md
```

MongoDB must be running on `localhost:27017`.
