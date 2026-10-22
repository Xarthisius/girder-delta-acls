from pathlib import Path

from girder import events
from girder.plugin import GirderPlugin, registerPluginStaticContent

from . import rest, settings  # noqa: F401 -- settings imported for its decorators
from .constants import PLUGIN_NAME


class DeltaAclsPlugin(GirderPlugin):
    DISPLAY_NAME = 'Delta ACLs'

    def load(self, info):
        events.bind('rest.put.folder/:id/access.before', PLUGIN_NAME, rest.onFolderAccessBefore)
        events.bind(
            'rest.put.collection/:id/access.before', PLUGIN_NAME, rest.onCollectionAccessBefore
        )

        info['apiRoot'].folder.route('POST', (':id', 'access', 'preview'), rest.previewFolderAccess)
        info['apiRoot'].collection.route(
            'POST', (':id', 'access', 'preview'), rest.previewCollectionAccess
        )

        registerPluginStaticContent(
            plugin=PLUGIN_NAME,
            css=['/style.css'],
            js=['/girder-plugin-delta-acls.umd.cjs'],
            staticDir=Path(__file__).parent / 'web_client' / 'dist',
            tree=info['serverRoot'],
        )
