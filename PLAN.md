# girder-delta-acls: implementation plan

A Girder 5 plugin that changes recursive ACL edits on Collections and Folders so they
push down **only what changed** (the delta) instead of **copying the whole ACL** onto every
subfolder. The current "make everything match" behaviour stays available as a separate,
clearly labelled option.

Status: implemented (2026-10-22). See `CLAUDE.md` for current state and for where the code departs from this plan.

---

## 1. How core Girder does it today

Line numbers are from the checkout at `/home/xarth/codes/wholetale-ng/girder`. Some have
moved since that repo's `CLAUDE.md` was written, so search for the names again before you
rely on a line number.

| Layer | Location | What it does |
|---|---|---|
| REST | `girder/api/v1/folder.py:210-239` `updateFolderAccess` | `PUT /folder/:id/access`. Takes `access` (JSON), `publicFlags`, `public`, `recurse`, `progress`. Uses `modelParam(level=ADMIN)` and `@access.user(scope=DATA_OWN)`, wrapped in `@filtermodel(addFields={'access'})`. |
| REST | `girder/api/v1/collection.py:138-168` `updateCollectionAccess` | `PUT /collection/:id/access`, with the same parameters. |
| Model | `girder/models/folder.py:866-917` `Folder.setAccessList(recurse=…)` | Sets `public`/`publicFlags`, then calls `AccessControlledModel.setAccessList`, which **replaces `doc['access']` wholesale**. Then, for each **direct child folder the user has ADMIN on** (`findWithPermissions(level=ADMIN)`), it recurses with the **same full ACL**. |
| Model | `girder/models/collection.py:304-358` `Collection.setAccessList(recurse=…)` | Same thing, starting from the top-level folders. (It also forgets to pass `force` down. Minor core bug.) |
| Model | `girder/models/model_base.py:1124` `AccessControlledModel.setAccessList` | Normalises entries, runs `_validateFlags` on each one, assigns `doc['access']`, then `save()`. One full-document write and one `model.folder.save` event per folder. |
| Model | `model_base.py:1096` `_validateFlags` | Without site admin, an **admin-only flag** survives only if it is already enabled on *that document's* entry. Validation is therefore per document. |
| Client | `girder/web/src/views/widgets/AccessWidget.js:301-328` `saveAccessList` | Reads the *final* ACL from the dialog and decides `recurse` with `$('#g-apply-recursive').is(':checked')`. Calls `model.updateAccess({recurse, progress: true})`, then fires `g:accessListSaved({recurse})`. |
| Client | `girder/web/src/models/AccessControlledModel.js:22` `updateAccess` | Sends `PUT …/access` with `access`, `public`, `publicFlags` and any extra params. |
| Client | `girder/web/src/templates/widgets/accessEditorMixins.pug:45` `recurseOption` | Two radios: "only this X" (`#g-apply-nonrecursive`) and "Also set permissions on all subfolders to match" (`#g-apply-recursive`). |
| Client | Callers: `CollectionView.js:135`, `HierarchyWidget.js:1033` (folders), `SystemConfigurationView.js:149` (sets `hideRecurseOption`). | |

### What actually goes wrong

A user with read-only access to the parent and write access to a subfolder **cannot**
start a recursive edit at all: the endpoint needs ADMIN on the
target, and core already skips (prunes) any subfolder where the editor lacks ADMIN. The real
damage happens on subfolders where the editor **does** have ADMIN:

* Alice is ADMIN on collection C. Earlier she gave Bob WRITE on subfolder `C/S` only. Now she
  adds Carol READ on C with "apply to subfolders". Every subfolder's ACL becomes C's ACL, so
  **Bob loses WRITE on `C/S`** and anyone who had access only to `C/S` loses it too.
* A subfolder that was deliberately private (or public) gets its `public` flag and
  `publicFlags` flipped to match the parent's.
* The editor sees the *final* ACL and has no way to tell that the request will also delete
  entries on subfolders.

The plugin's goal: when the request is "apply to subfolders", change only the principals
(and public settings) **the editor actually changed on the target**. Leave every other
entry on every subfolder alone.

---

## 2. Delta semantics

The delta is computed **on the server**, by comparing the target's ACL just before the write
with its ACL just after `setAccessList` has normalised and flag-validated it. The client
keeps sending the full ACL exactly as it does now, so core payloads stay the same and the
change is backwards compatible.

```
old = target.access, target.public, target.publicFlags     # loaded by modelParam
new = same fields after core setAccessList on the target only (recurse=False)

for each principal p in (users ∪ groups) of old ∪ new:
    p only in new                 -> SET   p = (level, flags)
    p only in old                 -> REMOVE p
    p in both, level differs      -> SET   p.level = new.level
    p in both, flags differ       -> flagsAdded / flagsRemoved for p
public changed                    -> SET public
publicFlags changed               -> publicFlagsAdded / publicFlagsRemoved
```

How the delta is applied to each descendant the editor has ADMIN on:

| Op | Descendant has no entry for p | Descendant has an entry for p |
|---|---|---|
| SET level (new principal) | add `{id, level, flags}` | **overwrite level** (see decision D1) |
| SET level (changed principal) | add `{id, level, flags}` | overwrite level |
| REMOVE | no-op | remove the entry |
| flagsAdded / flagsRemoved | add the entry if part of a SET, otherwise no-op | union / difference on that entry's flags |
| public | set | set |
| publicFlags ± | union / difference | union / difference |

Principals the editor did not touch are **never** read or written on descendants.

**Admin-only flags.** `_validateFlags` is per document. To keep that guarantee, a
non-site-admin's delta drops admin-only flags from `flagsAdded` and SET payloads, and keeps
any admin-only flags the descendant entry already has. A site admin's delta passes all flags
through. This matches what core would allow document by document.

**Traversal.** Same rule as core (decision D2): descend from the target, update only folders
the user has ADMIN on, and **prune** at a folder without ADMIN.

**Empty delta.** If "apply changes to subfolders" is chosen but nothing changed, the
descendants are left untouched. The response and the UI should say "no changes to
propagate". That is correct behaviour, not a bug.

---

## 3. Server design

### 3.1 Intercept the core endpoint with a `.before` event

Bind `rest.put.folder/:id/access.before` and `rest.put.collection/:id/access.before`, in the
style of girder-virtual-resources. Add one new optional request parameter:

* `recurseMode`: one of `delta` or `replace`. It only matters when `recurse=true`.

Handler logic:

1. `recurse` is false or missing → return. Core runs unchanged.
2. `recurseMode` (or the site default, §3.4) is `replace` → return. Core runs unchanged, so
   the original behaviour is literally core's code.
3. `delta` → handle the request ourselves, then `preventDefault()` and `addResponse(doc)`.
   The target gets the same atomic delta pipeline as its descendants. Core's
   `setAccessList(save=False)` is used only to normalise and validate (§6.1).

This works because `autoDescribeRoute` **ignores unknown params** (`describe.py`, it only
iterates the declared ones). Clients that don't send `recurseMode` behave as they do today,
and so does the core endpoint when the plugin is disabled.

**Implementing step 3 without re-implementing core's parameter handling.** Write the delta
handler as a module-level function decorated exactly like core's:
`@access.user(scope=DATA_OWN)` → `@boundHandler` → `@filtermodel(model=…, addFields={'access'})`
→ `@autoDescribeRoute(Description(…).modelParam('id', level=ADMIN).jsonParam('access',…)…
.param('recurseMode', enum=['delta','replace']), hide=True)`. Call it from the `.before`
listener as `handler(id=event.info['id'], params=event.info['params'])`. That gives us the
following for free:

* ADMIN check and document load (`_loadModel`), and JSON and boolean coercion
* `filtermodel` serialisation of the response, so the shape matches core exactly
* consistent 400 and 403 errors

**Traps carried over from collection-review (do not drop these):**

* **The `.before` listener itself needs an `@access.user(scope=DATA_OWN)` decorator.**
  `handleRoute` passes `_defaultAccess` as the `pre` callback (`rest.py`), and a listener
  without `accessLevel` silently makes the endpoint **admin-only for the whole site**. Add a
  regression test: a non-admin owner can still `PUT /folder/:id/access` with the plugin
  loaded.
* `autoDescribeRoute` empties `params` in place. Read `recurse`/`recurseMode` from
  `event.info['params']` **before** calling the inner handler, and do not count on `.after`
  listeners seeing them.

**Alternative considered: replacing the route** (`removeRoute` + `route` with an extended
`Description`). It would document `recurseMode` in Swagger, but it breaks if any other
plugin also wraps these routes, and it takes core's `replace` code path out of our hands.
Event interception composes better. To make the param show up in Swagger, re-register the
docs with `docs.addRouteDocs(...)` using core's description plus the new param (optional,
low priority).

### 3.2 `lib/delta.py`: pure functions, the heart of the plugin

* `computeDelta(oldDoc, newDoc, user) -> AclDelta`. A dataclass holding `set_users`,
  `set_groups`, `remove_users`, `remove_groups`, per-principal `flags_added`/`flags_removed`,
  `public`, `public_flags_added`/`public_flags_removed`. No database access, so it is fully
  unit-testable.
* `AclDelta.isEmpty()` and `AclDelta.summary()` (a JSON-able form for the response and the
  preview).
* `toMongoPipeline(delta) -> list`: one **aggregation-pipeline update** (MongoDB ≥ 4.2; Girder requires 4.4) that
  applies the whole delta to a document in a single atomic write:
  * `access.users = $concatArrays[ $filter(existing, id ∉ touched_ids) → $map(apply flag ±
    to surviving entries), [new SET entries merged with the existing entry's admin-only
    flags] ]`
  * the same for `access.groups`
  * `public` only if it changed, `publicFlags = $setUnion/$setDifference`.

### 3.3 `lib/propagate.py`: applying the delta

```
propagateDelta(root, rootType, delta, user, progress):
    frontier = [root._id]                       # parentCollection = rootType for level 0
    while frontier:
        children = Folder.collection.find(
            {parentId: {$in: frontier}, parentCollection: <type>,
             **Folder().permissionClauses(user, ADMIN)},     # prune non-admin (D2)
            projection={_id: 1})
        ids = [c._id for c in children]
        Folder.collection.update_many({_id: {$in: ids}, **permissionClauses},
                                      toMongoPipeline(delta))
        progress.update(increment=len(ids))
        frontier = ids
    events.trigger('delta_acls.propagate.after',
                   {root, rootType, delta, folderIds, user})
```

* **Atomic per document, no lost updates.** Each folder gets one `update_many` pipeline
  write, instead of core's read-modify-write `save()` of the whole document (the race
  described in collection-review's invariant 8).
  The tree as a whole is **not** transactional; that would need a replica set plus
  `start_session`. Document this.
* Repeating `permissionClauses` in the update filter closes the gap between the permission
  check and the write.
* One query and one write per **tree level**, not per folder.
* **Events.** Bulk writes fire no `model.folder.save` events. Fire one
  `delta_acls.propagate.after` with the folder ids so other plugins can react (decision D3).
* Collection roots could use `{baseParentType:'collection', baseParentId}` as a single
  query, but that **cannot prune** at non-admin folders. Use the level walk for both kinds
  of root. Add `{parentId, parentCollection}` index checks if needed (core already indexes
  `parentId`).
* Keep core's `ProgressContext` pattern. Compute the total with
  `subtreeCount(level=ADMIN)` exactly as core's handler does.

### 3.4 Settings (`settings.py`)

| Key | Default | Meaning |
|---|---|---|
| `delta_acls.default_recurse_mode` | `replace` | Used when an API client sends `recurse=true` without `recurseMode`. `replace` keeps existing scripts and `girder_client` behaving the same. The web UI always sends the mode explicitly. (D4) |
| `delta_acls.replace_requires_site_admin` | `false` | When true, `recurseMode=replace` returns 403 for non-site-admins. This is for deployments that want to retire the overwrite behaviour without removing it. |

A small plugin config page (`views/ConfigView.js`, the standard pattern) edits both.

### 3.5 Preview endpoint (feeds the UI)

`POST /folder/:id/access/preview` and `POST /collection/:id/access/preview`. Registered with
`info['apiRoot'].folder.route(...)` and `@access.user(scope=DATA_OWN)`, ADMIN `modelParam`,
taking the same `access`/`public`/`publicFlags` params. It writes nothing. It returns:

```json
{
  "delta": {"users": [{"id","login","from":1,"to":2}], "groups": [...],
            "public": {"from": false, "to": true}, "publicFlags": {...}},
  "subfolders": {"total": 37, "admin": 34, "skipped": 3},
  "divergent": 5          // ADMIN subfolders whose ACL != the target's *current* ACL
}
```

`divergent` is the number that makes the risk concrete: it counts the folders that
`replace` would overwrite and that `delta` would leave alone apart from the changed
principals. For large trees, cap the walk (for example 10k folders) and return
`"truncated": true`.

### 3.6 Plugin layout (following girder-collection-review)

```
girder-delta-acls/
  pyproject.toml uv.lock tox.ini ruff.toml MANIFEST.in README.md CLAUDE.md LICENSE
  package.json                         # lint-only (eslint/pug-lint/stylelint), as in collection-review
  .github/workflows/build-test.yaml    # tests + lint + codecov (copied from girder-jsonforms)
  .github/workflows/release.yml        # uv build → PyPI trusted publishing → sigstore GH release
  girder_delta_acls/
    __init__.py                        # DeltaAclsPlugin.load(): bind events, routes, static content
    constants.py                       # PLUGIN_NAME, RecurseMode, PluginSettings
    settings.py                        # @validator/@default for §3.4
    rest.py                            # .before listeners, inner delta handler, preview routes
    lib/delta.py                       # computeDelta, AclDelta, toMongoPipeline (pure)
    lib/propagate.py                   # level-walk + bulk apply + event
    tests/ ...                         # §5
    web_client/ ...                    # §4
```

Entry point: `girder.plugin: delta_acls = girder_delta_acls:DeltaAclsPlugin`. Symlink it
into `girder/plugins/delta_acls` the way `collection_review` is linked.

---

## 4. UI design

### 4.1 Recursion choices in the access dialog

Keep core's two radios, so the core ids (`#g-apply-nonrecursive` / `#g-apply-recursive`) and
the `g:accessListSaved({recurse})` refresh logic in `CollectionView` / `HierarchyWidget` keep
working. Add a **nested choice** that only appears when the recursive radio is selected:

```
( ) Apply permissions only to this folder
(•) Also apply to subfolders
      (•) Apply only the changes I made                    [recommended]
          Adds, removes or changes just the users/groups you edited here.
          Other permissions on subfolders are kept.
      ( ) Replace subfolder permissions with this list
          ⚠ Every subfolder you administer will get exactly this access
            list. Permissions set individually on subfolders will be lost.
```

Below that, a live summary line from the preview endpoint, debounced and refreshed when the
radio changes or the dialog's entries change:

> Will apply to **34** subfolders (3 skipped: you are not an admin there).
> Changes: **+ Carol (Read)**, **Bob: Write → Admin**, **− Staff (group)**.

In replace mode, the summary turns into an `alert-warning`:

> **5 subfolders have their own permissions** that will be overwritten.

### 4.2 Confirmation

* Delta mode with an empty delta: show an inline note "No permission changes to apply to
  subfolders" and save the target only.
* Replace mode with `divergent > 0`: show a `girder.dialog.confirm` ("Overwrite permissions
  on 5 subfolders?") before submitting. collection-review verified that this dialog stacks
  correctly over the access modal.

### 4.3 Wiring (per `WEB_CLIENT_CONVENTIONS.md` §8: `wrap()`, one file per core view)

* `views/AccessWidget.js`:
  * `wrap(AccessWidget, 'render', …)`: after core renders (the widget renders
    asynchronously after `fetchAccess`, so hook the point where `.g-recursive-container`
    exists), inject the nested radios from `templates/recurseMode.pug`, unless
    `hideRecurseOption` is set. Toggle them on `change` of the core radios.
  * Add event handlers through `AccessWidget.prototype.events[...]` for the new radios and
    for preview refresh. Also listen to the add-entry, remove-entry and level-change events
    the widget already handles.
  * `wrap(AccessWidget, 'saveAccessList', …)`: read the nested radio and stash it as
    `this.model._deltaAclsRecurseMode`. Run the replace-mode confirm if needed, then call
    the original.
* `views/AccessControlledModel.js`:
  `wrap(girder.models.AccessControlledModel, 'updateAccess', …)`. If
  `params.recurse && this._deltaAclsRecurseMode`, add `recurseMode` and clear the stash.
  This avoids copying core's `saveAccessList` body into the plugin.
* `views/ConfigView.js` + route `plugins/delta_acls/config` for the settings.
* Build: a Vite UMD bundle → `web_client/dist/`, registered with
  `registerPluginStaticContent`. The `dist/` folder must exist before girder starts.

---

## 5. Testing

**Unit tests (`computeDelta`, `toMongoPipeline`)**, with no server:

* add, remove, level change, flag ±, public flip, publicFlags ±, empty delta
* admin-only flag stripped for non-admins and kept for site admins

**Server tests (pytest-girder, real Mongo; mongomock does not support pipeline updates)**:

1. **The motivating scenario**: Bob has WRITE on `C/S`. Alice adds Carol on C with
   `recurse=true&recurseMode=delta`. Bob keeps WRITE on `C/S`, and Carol is on every
   subfolder.
2. Same request with `recurseMode=replace` → Bob is removed. Core behaviour is preserved.
3. `recurse=true` and no `recurseMode` → follows `default_recurse_mode`.
4. Removing a principal on the target removes it from descendants and leaves others alone.
5. A subfolder without ADMIN for the editor is skipped and its subtree pruned (same as core).
6. Public flip propagates. Making the target public leaves a separately private subfolder
   untouched when `public` didn't change.
7. Collection root and folder root, plus folders under user homes.
8. **A non-admin owner can still PUT the access endpoint** with the plugin loaded (the
   `.before` access-decorator trap).
9. The response shape matches core (`filtermodel`, `access` included).
10. Preview returns correct counts, `divergent`, and writes nothing.
11. The `replace_requires_site_admin` setting is enforced.
12. The `delta_acls.propagate.after` event fires with the right folder ids.
13. Concurrency smoke test: a rename running at the same time as propagation does not lose
    either write.

Every test module needs its own `pytestmark = pytest.mark.plugin('delta_acls')`. A mark in
`conftest.py` is silently ignored.

**E2E**: a playwright script modelled on `collection-review/tests/e2e/`. Open the access
dialog on a collection, check that the nested radios appear and disappear, check the preview
text, check the replace confirm, save, and verify the subfolder ACLs through the API.

---

## 6. Decisions (resolved)

| # | Question | Decision |
|---|---|---|
| D1 | A delta SET on a principal the subfolder **already has at a different level**: overwrite, or only raise the level (never downgrade)? | **Overwrite.** The editor explicitly set that principal, and it is predictable. A conservative "max(level)" variant could become a third mode later if needed. |
| D2 | Traversal at a non-admin folder: prune its subtree, or keep going into deeper folders where the user is ADMIN again? | **Prune**, the same as core. |
| D3 | Bulk pipeline writes (no per-folder `model.folder.save` events) or a per-folder `save()` loop? | **Bulk + one `delta_acls.propagate.after` event.** Verified, see §6.1. |
| D4 | What should `recurse=true` without `recurseMode` mean from API clients? | **`replace`** (backwards compatible) by default, made configurable. The UI always sends the mode, defaulting to `delta`. |
| D5 | Should replace mode stay available to everyone? | **Yes** by default. The `replace_requires_site_admin` setting is the off-switch. |
| D6 | MongoDB ≥ 4.2 is required for pipeline updates. | **Satisfied.** Girder's minimum is MongoDB 4.4, which production runs. Use pipeline updates unconditionally, with no `$pull`/`$push` fallback. |

### 6.1 D3 verification (2026-10-22)

Scope: every plugin mounted by `deploy-dev/docker-stack.yml` (girder and local_worker), using
the source paths in `deploy-dev/.env.local`: virtual-resources, wholetale, sem_viewer,
sample-tracker, jsonforms, dashboards, flycut-studio and observations. Also gwvolman,
girderfs, and core Girder with its bundled plugins. The search covered `model.folder.*`,
`model.collection.*`, `rest.*/:id/access`, `setAccessList(` and `copyAccessPolicies(`.

* **No plugin hooks `model.collection.*` or the `PUT …/access` endpoints.**
* `model.folder.save` → jsonforms `coerce_metadata_dates`
  (`girder_jsonforms/__init__.py:508`). It only rewrites `meta` dates. ACL writes never touch
  `meta`, so skipping the event loses nothing.
* `model.folder.validate` → virtual-resources `_validateFolder`
  (`rest/virtual_folder.py:41`), and core's `virtual_folders` plugin. These check
  symlink/virtual-folder structure, not `access`/`public`. Bulk writes skip validation, but
  this plugin only writes ACL fields that `lib/delta.py` builds and validates, so nothing is
  lost.
* **Model-level ACL callers are unaffected.** The plugin only intercepts the REST endpoints,
  so `Folder().setAccessList(...)` keeps core's replace semantics when called directly from
  wholetale `models/tale.py:335`, jsonforms `models/deposition.py:1064` and
  `rest/form.py:422`, and `scripts/tale_acls_fix.py`. That is what we want: those callers
  sync ACLs on folders they manage programmatically.
* **collection-review** (`~/codes/wholetale-ng/girder-collection-review`, not deployed yet)
  binds no ACL or folder/collection *model* events. It hooks `rest.put.folder/:id.after`
  (rename) and `rest.put.resource/move.before/.after`, which are different routes from ours,
  so there is no listener conflict. It does write ACLs itself, through bulk `$push`/`$pull`
  of a reviewer entry `{id, level: READ, flags: []}` on the collection and every subfolder
  (`lib/provisioning.py:106-136`, `:194-203`). How the two interact:
  * **Delta mode leaves the reviewer alone** unless the owner edits that entry in the access
    dialog. Removing it there removes it from the subtree, which matches what the owner
    asked for.
  * **Replace mode has a race, and so does core today.** If an owner runs a recursive save
    while `closeReview` → `revokeEverywhere` is running, a stale read-modify-write `save()`
    can bring back the reviewer entry after the `$pull`. The user has been deleted by then,
    so the entry is a harmless dangling id, but it is untidy. Delta mode's per-document
    pipeline writes cannot cause this on descendants.
  * The target document itself is still written by core's `setAccessList` → `save()`, so
    the race is still possible there. **Refinement:** after validating through core, apply
    the same delta pipeline to the target as well, instead of `save()`. That makes every
    write atomic. Add this to §3.1 step 3.
* Interop test (optional, skipped unless `girder_collection_review` is importable): open a
  review, give a subfolder its own permissions, do a delta-mode recursive save on the
  collection, then check that the reviewer still reads everything and that closing the
  review leaves no entries behind.
* dashboards-precipitate: not relevant (commented out of the stack).

## 7. Implementation order

1. Scaffold the repo with **uv**, following collection-review:
   * PEP 621 metadata in `pyproject.toml`, with no `setup.py`. The `girder.plugin` entry point
     goes under `[project.entry-points."girder.plugin"]`.
   * Dev dependencies (pytest, pytest-girder, pytest-cov, pytest-xdist, ruff) go in a PEP 735
     `[dependency-groups] dev`. No `requirements-dev.txt`.
   * Commit `uv.lock`. Use `uv sync` for the local `.venv` and `uv build` for the sdist and
     wheel.
   * `tox.ini` declares `requires = tox-uv`, so the tox envs are built by uv. tox resolves
     fresh rather than from the lock, which lets CI catch breakage in upstream Girder.
   * Copy collection-review's ruff and JS lint setup.
   * To run against the local Girder checkout, use the girder venv
     (`…/girder/venv/bin/python -m pytest -p no:celery`), or
     `uv pip install -e . --no-deps` into it.
2. **GitHub Actions**: a near-verbatim copy of `girder-jsonforms/.github/workflows/`.
   * `build-test.yaml` ("Testing and Linting"). Runs on push to `main`, on PRs and on
     `workflow_dispatch`, on ubuntu-24.04 with Python 3.12, and with `mongo:4.4` and
     `redis:7` service containers. Steps: setup-python, setup-node 24, checkout, then
     `astral-sh/setup-uv` with its cache keyed on `pyproject.toml`, then
     `uv tool install tox --with tox-uv`. Then `npm ci && npm run build` in
     `girder_delta_acls/web_client`, then `tox -e lint -- girder_delta_acls`, then
     `tox -e pytest` with `GIRDER_MAX_CURSOR_TIMEOUT_MS=60000`, and finally codecov upload
     (`secrets.CODECOV_TOKEN`). Only addition: a root `npm ci && npm run lint` step for the
     eslint, pug-lint and stylelint config (as in collection-review), which jsonforms lacks.
   * `release.yml` ("Publish to PyPI"). Runs on every push.
     * **build**: build the web client, `uv build`, `uvx check-manifest -vvv`, and upload
       `dist/` as an artifact.
     * **publish-to-pypi**: tag pushes only, through
       `pypa/gh-action-pypi-publish@release/v1` with trusted publishing (`id-token: write`),
       using environment `pypi` with url `https://pypi.org/p/girder-delta-acls`.
     * **github-release**: sign with `sigstore/gh-action-sigstore-python`, then
       `gh release upload` to the tag's release.
   * Things to set up outside the repo:
     * a **pending trusted publisher** on PyPI for `girder-delta-acls` (workflow
       `release.yml`, environment `pypi`)
     * a `pypi` environment in the GitHub repo
     * a `CODECOV_TOKEN` secret
     * a GitHub remote, which this repo does not have yet
   * As in jsonforms, `gh release upload` expects the GitHub Release for the tag to already
     exist. Create it with `gh release create <tag>` before or with the tag push.
   * `check-manifest` will fail unless `MANIFEST.in` includes `web_client/dist` and excludes
     the web sources, the e2e tests and the lint files.
3. `lib/delta.py` and its unit tests.
4. `lib/propagate.py`, then the `.before` listeners and the inner handler, then server tests
   1–9 and 12–13.
5. Settings and the preview endpoint, then tests 10–11.
6. Web client: nested radios, then the `updateAccess` wrap, then the preview summary, then
   the replace confirm, then the config page.
7. E2E script, README, and a CLAUDE.md that records the invariants.
