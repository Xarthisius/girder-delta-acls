PLUGIN_NAME = 'delta_acls'

#: Fired once per delta-mode recursive save, after every write has landed. Bulk pipeline
#: writes fire no ``model.folder.save`` events, so this is the only notification other
#: plugins get. ``event.info`` carries ``root``, ``rootType``, ``delta`` (``AclDelta.summary()``),
#: ``folderIds`` (descendants written, root excluded) and ``user``.
PROPAGATE_AFTER_EVENT = 'delta_acls.propagate.after'

#: Response header set by a delta-mode recursive save. JSON: ``{"mode", "changed", "folders"}``.
RESPONSE_HEADER = 'Girder-Delta-Acls'

#: Preview stops walking the tree after this many folders and reports ``truncated``.
PREVIEW_FOLDER_LIMIT = 10000


class RecurseMode:
    DELTA = 'delta'
    REPLACE = 'replace'

    ALL = (DELTA, REPLACE)


class PluginSettings:
    DEFAULT_RECURSE_MODE = 'delta_acls.default_recurse_mode'
    REPLACE_REQUIRES_SITE_ADMIN = 'delta_acls.replace_requires_site_admin'
