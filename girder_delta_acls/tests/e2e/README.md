# Browser end-to-end checks

The Python tests cover the server. These cover what only a real browser can show: that the
nested choices are injected into core's access dialog and toggle with the core radios, that
the live preview tracks edits made through core's own search widget, that the replace-mode
confirm appears *inside* the dialog without destroying it, and what the dialog actually
sends.

```bash
./run.sh                       # uses the repo's uv-managed .venv by default
./run.sh /path/to/venv
```

`run.sh` drops its database (`girder_delta_acls_ui`), starts Girder on port 8751, runs
`01-access-dialog.mjs`, and stops the server. 31 checks; screenshots land in `shots/`
(gitignored).

Not wired into `tox -e pytest` on purpose: it needs a built web client, a running server, a
droppable Mongo, and a Chrome.

## Requirements

- Chrome, `CHROME=/usr/bin/google-chrome` by default.
- playwright, resolved from `$PLAYWRIGHT`, else `~/node_modules/playwright`, else the ambient
  `node_modules`. It is **not** a dependency of this repo, and no `ms-playwright` browsers
  are needed: `PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 npm install playwright` anywhere, then point
  `$PLAYWRIGHT` at it.
- MongoDB on `localhost:27017`.
- A built web client (`cd ../../web_client && npm ci && npm run build`).

## What `01-access-dialog.mjs` covers

Fixture (over REST): a site admin, then Alice (non-admin, ADMIN on collection *Shared Study*),
folders `A`, `S`, `S/S1`, and Bob with WRITE on `S` only.

As Alice: the nested choices appear only for recursive saves; delta is the default; the
empty-delta note; adding Carol through the search widget updates the preview
(`+ Carol Tester (Read)`, 3 subfolders); replace mode warns about the one divergent
subfolder; saving in replace mode shows the inline confirm and sends nothing; cancelling
dismisses it; a delta save sends `recurse=true&recurseMode=delta`, Carol reaches `S1` and Bob
keeps WRITE on `S`; a confirmed replace sends `recurseMode=replace` and removes Bob; a
non-recursive save sends no mode; the folder dialog from the hierarchy widget gets the same
choices; with `replace_requires_site_admin` on, the replace option is disabled and explained.

As the site admin: the config page shows and saves both settings.

Contexts disable CSS transitions from the first paint, and modal opens wait on Bootstrap's
`shown.bs.modal` plus a frame-stability check, never on a sleep (see `helpers.mjs`).

## Known-noise filter

Errors that reproduce on a stock Girder page are filtered: the google_analytics bundle's
`reading 'History'` error (if that plugin is installed) and core's EventStream websocket 403
with no signed-in user. Anything thrown from the `delta-acls` bundle is never filtered.
