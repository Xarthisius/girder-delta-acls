# CLAUDE.md — girder-delta-acls

Handoff notes for this Girder 5 plugin. The design and the reasoning behind it are in
`PLAN.md`. Repo-wide Girder architecture is in the girder checkout's own `CLAUDE.md`
(`/home/xarth/codes/wholetale-ng/girder/CLAUDE.md`). Packaging, lint and CI follow
`/home/xarth/codes/wholetale-ng/girder-collection-review` almost verbatim. Its `CLAUDE.md`
explains those choices, and they are not repeated here.

## Status (2026-10-22)

| Area | State |
|---|---|
| `lib/delta.py`, `lib/propagate.py`, REST interception, preview, settings, Swagger docs | **Done.** 66 tests. |
| Web client (nested radios, live preview, replace confirm, config page) | **Done.** 31 browser checks (`tests/e2e/run.sh`), run in CI by the **Browser end-to-end** job. |
| `tox -e lint`, `tox -e pytest`, `npm run lint`, `uv build`, `check-manifest` | All green. |
| CI | **Testing and Linting** (`build (3.12)` + **Browser end-to-end**) and **Publish to PyPI** green on `main`. |
| Release | **v1.0.0** released 2026-10-22: on [PyPI](https://pypi.org/p/girder-delta-acls), and as a GitHub release with the wheel, sdist and sigstore bundles. See "Releasing". |

## Commands

```bash
uv sync                                   # .venv: plugin (editable) + dev group, girder from PyPI
.venv/bin/python -m pytest -q             # fast loop; needs MongoDB on localhost:27017
env -u FORCE_COLOR tox -e lint            # ruff check
env -u FORCE_COLOR tox -e pytest          # fresh resolve, 4-way xdist, coverage.xml
env -u FORCE_COLOR tox -e format          # ruff format + fix; the tree is kept format-clean
npm ci && npm run lint                    # eslint + pug-lint + stylelint (root package.json is lint-only)
cd girder_delta_acls/web_client && npm ci && npm run build   # REQUIRED before Girder starts
(cd girder_delta_acls/tests/e2e && npm ci) # playwright for the browser checks
girder_delta_acls/tests/e2e/run.sh        # browser checks; see tests/e2e/README.md
```

`registerPluginStaticContent` md5s the files in `web_client/dist/` at plugin load, so a
missing `dist/` breaks **server tests too**, not just the UI. Build it first.

The local deploy-dev deployment can leave `web_client/dist/` and
`web_client/node_modules/.vite-temp` owned by root, after which `npm run build` fails with
EACCES. Fix: `sudo chown -R $USER girder_delta_acls/web_client/{dist,node_modules}`.

On this machine MongoDB was not running. Tests were run against a throwaway
`docker run -d --rm --name delta-acls-mongo -p 27017:27017 mongo:4.4`. mongomock does not
work: it has no pipeline updates.

## CI and releasing

`.github/workflows/build-test.yaml` (push to `main`, PRs) has two jobs:

- `build (3.12)`: JS lint, ruff, `tox -e pytest`, Codecov upload. The upload step has
  `fail_ci_if_error: true`, and `codecov.yml` sets `after_n_builds: 1`. Without that,
  Codecov posted green "No report found" statuses while the test job was still queued.
- **Browser end-to-end**: `uv sync --locked`, builds the web client, installs playwright's
  Chromium (`--with-deps`), runs `tests/e2e/run.sh` with `CHROME=""`, and always uploads
  `shots/` and `girder.log` as the `browser-verification` artifact.

`.github/workflows/release.yml` builds on every push and publishes only on a tag, through
PyPI trusted publishing (GitHub environment `pypi`). It then signs with sigstore and
attaches everything to the GitHub release. That last step tries `gh release create` and
falls back to `gh release upload --clobber`, so either order works (ported from
girder-flycut-studio `8512d70`).

To release:

1. Bump `version` in `pyproject.toml` (hardcoded, no setuptools-scm), commit, push, and
   wait for CI to go green.
2. Publish a release for tag `vX.Y.Z` from the GitHub UI, or run
   `gh release create vX.Y.Z --target main --generate-notes`.
3. Check that the PyPI wheel contains `girder_delta_acls/web_client/dist/*.umd.cjs` and
   `style.css`. Girder will not load the plugin without them.

## Layout

```
girder_delta_acls/
  __init__.py          # DeltaAclsPlugin.load(): binds the two .before listeners, adds the
                       # two preview routes, registers static content
  constants.py         # PLUGIN_NAME, RecurseMode, PluginSettings, event + header names
  settings.py          # validators/defaults for the two settings
  rest.py              # listeners, hidden delta handlers, preview handlers,
                       # describeCoreAccessRoutes (Swagger docs on core's routes)
  lib/delta.py         # PURE: computeDelta, AclDelta, toMongoPipeline, aclSignature
  lib/propagate.py     # applyToDocument, propagateDelta (level walk), surveySubtree (preview)
  tests/
    test_delta.py                  # unit tests; pipeline tests run on real Mongo via `db`
    test_recursive_access.py       # PUT .../access end to end (plan §5 tests 1-9, 12, 13)
    test_preview_and_settings.py   # preview, settings, replace_requires_site_admin (10, 11)
    test_docs.py                   # recurseMode documented on core's routes in /describe
    e2e/                           # browser checks; NOT part of tox -e pytest
  web_client/
    main.js routes.js
    views/AccessWidget.js          # wraps render/saveAccessList/privacyChanged/flag toggles
    views/AccessControlledModel.js # wraps updateAccess to add recurseMode
    views/ConfigView.js
    templates/{recurseMode,previewSummary,configView}.pug  stylesheets/deltaAcls.styl
```

## How a request flows

1. Core matches `PUT /folder/:id/access` and fires `rest.put.folder/:id/access.before`.
2. `rest._makeAccessListener` reads `recurse` / `recurseMode` from `event.info['params']`.
   If there is no recursion, or the mode is `replace`, it returns and **core's own handler
   runs, unchanged**. That makes `replace` literally core's code, apart from the optional
   `replace_requires_site_admin` 403.
3. Delta mode: it calls `updateFolderAccessDelta(id=..., params=dict(params))`. That handler
   is decorated exactly like core's (`access.user` → `boundHandler` → `filtermodel` →
   `autoDescribeRoute(hide=True)`), so the ADMIN check, coercion, errors and response
   filtering are core's. The listener then `preventDefault()`s with the result.
4. `_deltaUpdate`: core's `setAccessList(save=False, recurse=False)` on a **copy** normalises
   and validates the requested ACL. `computeDelta(old, new)` gives the exact delta.
   `applyToDocument` writes it to the target with one pipeline update.
   `propagateDelta` writes `delta.withoutAdminFlags()` (or the exact delta for a site admin)
   to the subtree, one `update_many` per tree level.

## Invariants — do not "simplify" these

1. **The `.before` listeners must keep `@access.user(scope=DATA_OWN)`.** `handleRoute`
   passes `Resource._defaultAccess` as `pre` for every `rest.*.before` listener, and that
   calls `requireAdmin` on any listener without an `accessLevel` attribute. Without the
   decorator, binding alone makes `PUT .../access` admin-only site-wide.
   `test_non_admin_owner_can_still_put_access` guards this.
2. **The listener passes `dict(params)`, not `params`.** `autoDescribeRoute` pops params out
   of the dict it is given. With a copy, `.after` listeners still see them.
3. **Every write is an aggregation-pipeline update, never `save()`.** That includes the
   target (PLAN §6.1 refinement). A read-modify-write `save()` loses concurrent updates.
   `test_concurrent_rename_is_not_lost` injects a rename between the level query and the
   write.
4. **`update_many` repeats the ADMIN permission clause** in its filter. This closes the gap
   between the child query and the write.
5. **Pruning is by construction.** Only folders returned by the ADMIN-filtered query join the
   next frontier. Do not swap the walk for a single `baseParentId` query. It is cheaper, but
   it cannot prune (decision D2).
6. **Flag names go through `$literal`** in the pipeline. A flag spelled like `$name` would
   otherwise be read as a field path (`test_flag_names_are_literal`).
7. **`computeDelta` takes the doc *after* core validation**, not the raw request. Feeding it
   the request would skip `_validateFlags` and the level checks.
8. **`describeCoreAccessRoutes` must also patch `docs.routes`.** Core registers each
   route's Swagger operation before plugins load, as a *shallow copy* of
   `description.asDict()`. Params (shared list) and error responses (shared dict) added
   later show up; notes do not, because `description` becomes a new key. The registry keys
   methods as `'PUT'`, and `/describe` lowercases them. `test_docs.py` catches a regression
   only when the folder test runs first in a fresh process. Later in the same process an
   earlier load has already mutated the class-level description before core re-registers.
9. **The preview must not name principals the editor cannot see.** `_principalSummary`
   loads with `force=True`, then names a principal the request *adds* only if
   `hasAccess(READ)` allows it. Without that check, anyone administering any folder
   could post arbitrary ids and read back private users' logins and private groups'
   names. Principals already on the ACL are named, because core's GET .../access shows
   them anyway. Guarded by `test_preview_does_not_name_principals_the_editor_cannot_see`.
10. **Every server test module declares `pytestmark = pytest.mark.plugin('delta_acls')`.**
   A mark in `conftest.py` is silently ignored. `test_delta.py` has none on purpose: it
   only needs the `db` fixture.

## Where the implementation departs from PLAN.md

- **The replace confirm is inline, not `girder.dialog.confirm`.** `dialog.confirm` renders
  into `#g-dialog-container`, which is the element the access modal itself lives in. It
  would destroy the dialog, and with it the DOM that core's `saveAccessList` reads the ACL
  from. Instead, Save in replace mode fetches a fresh preview. If `divergent > 0`, or if the
  preview fails, a red confirm box appears in the summary area with Overwrite and Cancel,
  and nothing is sent until Overwrite. collection-review's "confirm stacks fine" finding
  does not transfer: its manage widget does not live in `#g-dialog-container`.
- **`computeDelta(oldDoc, newDoc)` has no `user` argument.** It returns the exact delta, and
  `AclDelta.withoutAdminFlags()` makes the descendant version. The target needs the exact
  delta, or applying it there would not reproduce what core validated.
- **For non-admins, admin-only flags are also removed from `flagsRemoved`** for descendants,
  not only from adds. A non-admin therefore never changes admin-only flags below the target.
  This is the plan's "keeps any admin-only flags the descendant entry already has".
- **"No changes to propagate" is signalled by a response header** (`Girder-Delta-Acls`,
  `{"mode","changed","folders"}`), so the body stays identical to core's. The web client
  reads it to show a success alert.
- **The preview also returns `replaceAllowed` and `defaultRecurseMode`.** The dialog uses
  `replaceAllowed` to disable the replace radio, so no extra settings endpoint is needed.
- **The core recursive radio is relabelled** "Also apply to subfolders". Core's text
  ("set permissions on all subfolders to match") now describes only one of the two choices.

## Core quirk found along the way

`AccessControlledModel._validateFlags` keeps an admin-only flag for a non-admin only if
`_isFlagEnabled` finds it already set on that entry. But `_hasUserAccessFlag` compares the
stored `ObjectId` with the **string** id from the client's JSON, so the check never
matches. A non-admin saving through REST therefore always drops admin-only flags from the
target, recursive or not. The plugin keeps that core behaviour on the target. On
descendants the stripped delta leaves such flags untouched.
`test_admin_only_flags_not_pushed_by_non_admin` pins both halves. The other core bug noted in
PLAN §1 (`Collection.setAccessList` not passing `force` down) does not affect this plugin.

## Not done (all optional or outside this repo)

- The collection-review interop test (PLAN §6.1, optional).
- A symlink into `girder/plugins/` (outside this repo). `uv sync` / `pip install -e .` is
  enough for Girder to load the plugin through the entry point.
- Web-client unit tests (jasmine). The e2e script covers the flows.
- In delta mode, a failing save does not fire core's `rest.put.*/access.failed` event,
  because the exception is raised inside the `.before` listener. Nothing in the deployed
  stack listens to it.
- The dialog's `MutationObserver` is not disconnected when the AccessWidget is destroyed.
  It only keeps a reference to detached DOM.
