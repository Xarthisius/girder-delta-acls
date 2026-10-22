"""
REST side of the plugin.

``PUT /folder/:id/access`` and ``PUT /collection/:id/access`` stay core's endpoints. A
``.before`` listener on each inspects ``recurse`` / ``recurseMode`` and either lets core run
(replace mode, or no recursion) or answers the request itself in delta mode. The delta
handlers below are declared exactly like core's, so ``autoDescribeRoute`` /
``filtermodel`` give the same parameter coercion, ADMIN check, errors and response shape.
"""

import copy
import json

from girder.api import access, docs
from girder.api.describe import Description, autoDescribeRoute
from girder.api.rest import (
    RestException,
    boundHandler,
    filtermodel,
    getCurrentUser,
    requireAdmin,
    setResponseHeader,
)
from girder.api.v1.collection import Collection as CollectionResource
from girder.api.v1.folder import Folder as FolderResource
from girder.constants import AccessType, TokenScope
from girder.models.collection import Collection
from girder.models.folder import Folder
from girder.models.group import Group
from girder.models.setting import Setting
from girder.models.user import User
from girder.utility import toBool
from girder.utility.progress import ProgressContext

from .constants import RESPONSE_HEADER, PluginSettings, RecurseMode
from .lib.delta import computeDelta
from .lib.propagate import applyToDocument, propagateDelta, surveySubtree


_RECURSE_MODE_DOC = (
    'How recurse=true treats subfolders. "delta" applies only the entries that changed on '
    'this resource to the subfolders you administer, and leaves every other entry on them '
    'alone. "replace" overwrites their access lists with this one (core Girder behaviour). '
    'If omitted, the delta_acls.default_recurse_mode setting decides. Ignored unless '
    'recurse is true.'
)

_RECURSE_DOC = 'With the delta_acls plugin, recurseMode chooses how this is done.'

_REPLACE_RESTRICTED_DOC = (
    'recurseMode=replace was requested by a non-site-admin while '
    'delta_acls.replace_requires_site_admin is enabled.'
)

_ACCESS_NOTES = (
    'The delta_acls plugin changes what recurse=true does when recurseMode=delta: only the '
    'users, groups and public settings that changed on this %s are applied to its '
    'subfolders, and every other entry on them is kept. Subfolders you do not administer '
    'are skipped together with everything below them, in both modes. A delta-mode save '
    'sets a Girder-Delta-Acls response header, JSON {"mode", "changed", "folders"}. Use '
    'POST /%s/{id}/access/preview to see the effect before saving.'
)


def describeCoreAccessRoutes():
    """
    Document ``recurseMode`` on core's own ``PUT /{folder,collection}/:id/access``.

    The delta handlers are hidden from Swagger and only reached through the .before
    listeners, so core's route descriptions are where API users look. They are extended in
    place, as girder-jsonforms does, and the notes are also copied into the Swagger
    operation core has already registered (see below). ``param`` and ``errorResponse``
    merge into an existing entry and skip text that is already there, so calling this
    again (pytest-girder loads the plugin once per test) changes nothing.

    Adding the param also makes core's autoDescribeRoute validate it against the enum. That
    is harmless: core's handler takes no ``recurseMode`` argument, so the value is dropped,
    and the .before listener has already rejected bad values with the same 400.
    """
    for handler, typeName in (
        (FolderResource.updateFolderAccess, 'folder'),
        (CollectionResource.updateCollectionAccess, 'collection'),
    ):
        description = handler.description
        description.param(
            'recurse', _RECURSE_DOC, dataType='boolean', default=False, required=False
        )
        description.param(
            'recurseMode', _RECURSE_MODE_DOC, enum=list(RecurseMode.ALL), required=False
        )
        description.errorResponse(_REPLACE_RESTRICTED_DOC, 403)

        notes = _ACCESS_NOTES % (typeName, typeName)
        if notes not in (description._notes or ''):
            description.notes('\n\n'.join(filter(None, (description._notes, notes))))

        # Core registered this route's Swagger operation before plugins load, as a shallow
        # copy of description.asDict(). The params list and responses dict are shared with
        # it, so the additions above already show; the notes are not, so copy them over.
        # Core registers it under 'PUT'; /describe lowercases method names on output.
        methods = docs.routes.get(typeName, {}).get('/%s/{id}/access' % typeName, {})
        for method, operation in methods.items():
            if method.upper() == 'PUT':
                operation['description'] = description._notes


def _accessDescription(description, model, typeName):
    """Core's parameter list for ``PUT /<type>/:id/access``, plus ``recurseMode``."""
    return (
        Description(description)
        .modelParam('id', model=model, level=AccessType.ADMIN)
        .jsonParam('access', 'The JSON-encoded access control list.', requireObject=True)
        .jsonParam(
            'publicFlags', 'JSON list of public access flags.', requireArray=True, required=False
        )
        .param(
            'public',
            'Whether the %s should be publicly visible.' % typeName,
            dataType='boolean',
            required=False,
        )
    )


def _withRecurseParams(description):
    return (
        description.param(
            'recurse',
            'Whether the policies should be applied to all subfolders as well.',
            dataType='boolean',
            default=False,
            required=False,
        )
        .param(
            'progress',
            'If recurse is set to True, this controls whether progress notifications will be sent.',
            dataType='boolean',
            default=False,
            required=False,
        )
        .param('recurseMode', _RECURSE_MODE_DOC, enum=list(RecurseMode.ALL), required=False)
        .errorResponse('ID was invalid.')
        .errorResponse('Admin access was denied.', 403)
    )


def _deltaUpdate(model, rootType, doc, access, public, publicFlags, progress):
    user = getCurrentUser()

    # Core's setAccessList is used only to normalise and flag-validate the requested ACL
    # against this document, exactly as a non-recursive save would. Nothing is saved.
    newDoc = model.setAccessList(
        copy.deepcopy(doc),
        access,
        save=False,
        recurse=False,
        user=user,
        setPublic=public,
        publicFlags=publicFlags,
    )
    delta = computeDelta(doc, newDoc)

    folderIds = []
    with ProgressContext(
        progress, user=user, title='Updating permissions', message='Calculating progress...'
    ) as ctx:
        if progress and not delta.isEmpty():
            ctx.update(
                total=model.subtreeCount(doc, includeItems=False, user=user, level=AccessType.ADMIN)
            )

        if not delta.isEmpty():
            ctx.update(increment=1, message='Updating ' + doc['name'])
            doc = applyToDocument(model, doc, delta)

            descendantDelta = delta if user['admin'] else delta.withoutAdminFlags()
            folderIds = propagateDelta(doc, rootType, descendantDelta, user, progress=ctx)

    setResponseHeader(
        RESPONSE_HEADER,
        json.dumps(
            {'mode': RecurseMode.DELTA, 'changed': not delta.isEmpty(), 'folders': len(folderIds)}
        ),
    )
    return doc


@access.user(scope=TokenScope.DATA_OWN)
@boundHandler
@filtermodel(model=Folder, addFields={'access'})
@autoDescribeRoute(
    _withRecurseParams(
        _accessDescription('Delta-mode recursive folder access update.', Folder, 'folder')
    ),
    hide=True,
)
def updateFolderAccessDelta(
    self, folder, access, publicFlags, public, recurse, progress, recurseMode
):
    return _deltaUpdate(Folder(), 'folder', folder, access, public, publicFlags, progress)


@access.user(scope=TokenScope.DATA_OWN)
@boundHandler
@filtermodel(model=Collection, addFields={'access'})
@autoDescribeRoute(
    _withRecurseParams(
        _accessDescription(
            'Delta-mode recursive collection access update.', Collection, 'collection'
        )
    ),
    hide=True,
)
def updateCollectionAccessDelta(
    self, collection, access, publicFlags, public, recurse, progress, recurseMode
):
    return _deltaUpdate(
        Collection(), 'collection', collection, access, public, publicFlags, progress
    )


def resolveRecurseMode(params):
    mode = params.get('recurseMode')
    if mode in (None, ''):
        return Setting().get(PluginSettings.DEFAULT_RECURSE_MODE)
    if mode not in RecurseMode.ALL:
        raise RestException(
            'Invalid value for recurseMode: "%s". Allowed values: %s.'
            % (mode, ', '.join(RecurseMode.ALL))
        )
    return mode


def _isTrue(value):
    return value if isinstance(value, bool) else toBool(value or '')


def _makeAccessListener(deltaHandler):
    # The access decorator is load bearing. handleRoute passes Resource._defaultAccess as
    # the ``pre`` callback for every rest.*.before listener, and it calls requireAdmin on
    # any listener lacking an ``accessLevel`` attribute: without this, merely binding the
    # listener would make the access endpoint admin-only for the whole site. DATA_OWN is
    # what core's endpoint declares, so nothing is lowered.
    @access.user(scope=TokenScope.DATA_OWN)
    def listener(event):
        # Read everything here, before calling into autoDescribeRoute: it consumes the
        # params dict in place. A copy is passed so .after listeners still see them.
        params = event.info.get('params') or {}
        if not _isTrue(params.get('recurse')):
            return

        if resolveRecurseMode(params) == RecurseMode.REPLACE:
            if Setting().get(PluginSettings.REPLACE_REQUIRES_SITE_ADMIN):
                requireAdmin(
                    getCurrentUser(),
                    'Replacing subfolder permissions is restricted to site administrators.',
                )
            return  # core's own handler runs, unchanged

        result = deltaHandler(id=event.info['id'], params=dict(params))
        event.preventDefault().addResponse(result)

    return listener


onFolderAccessBefore = _makeAccessListener(updateFolderAccessDelta)
onCollectionAccessBefore = _makeAccessListener(updateCollectionAccessDelta)


def _principalSummary(change, principalType):
    out = change.summary()
    if principalType == 'users':
        doc = User().load(change.id, force=True, fields=['login', 'firstName', 'lastName'])
        out['login'] = doc and doc['login']
        out['name'] = doc and ('%s %s' % (doc['firstName'], doc['lastName'])).strip()
    else:
        doc = Group().load(change.id, force=True, fields=['name'])
        out['name'] = doc and doc['name']
    return out


def _preview(model, rootType, doc, access, public, publicFlags):
    user = getCurrentUser()
    newDoc = model.setAccessList(
        copy.deepcopy(doc),
        access,
        save=False,
        recurse=False,
        user=user,
        setPublic=public,
        publicFlags=publicFlags,
    )
    delta = computeDelta(doc, newDoc)
    summary = delta.summary()
    for principalType in ('users', 'groups'):
        summary[principalType] = [
            _principalSummary(c, principalType) for c in getattr(delta, principalType)
        ]

    survey = surveySubtree(doc, rootType, user)

    return {
        'delta': summary,
        'empty': delta.isEmpty(),
        'subfolders': {k: survey[k] for k in ('total', 'admin', 'skipped')},
        'divergent': survey['divergent'],
        'truncated': survey['truncated'],
        'defaultRecurseMode': Setting().get(PluginSettings.DEFAULT_RECURSE_MODE),
        'replaceAllowed': bool(
            user['admin'] or not Setting().get(PluginSettings.REPLACE_REQUIRES_SITE_ADMIN)
        ),
    }


_PREVIEW_NOTES = (
    'Writes nothing. Returns the changes a delta-mode recursive save would push to '
    'subfolders, how many subfolders it would reach, and how many of those have permissions '
    'of their own that a replace-mode save would overwrite ("divergent").'
)


@access.user(scope=TokenScope.DATA_OWN)
@boundHandler
@autoDescribeRoute(
    _accessDescription('Preview a recursive folder access update.', Folder, 'folder')
    .notes(_PREVIEW_NOTES)
    .errorResponse('ID was invalid.')
    .errorResponse('Admin access was denied for the folder.', 403)
)
def previewFolderAccess(self, folder, access, publicFlags, public):
    return _preview(Folder(), 'folder', folder, access, public, publicFlags)


@access.user(scope=TokenScope.DATA_OWN)
@boundHandler
@autoDescribeRoute(
    _accessDescription('Preview a recursive collection access update.', Collection, 'collection')
    .notes(_PREVIEW_NOTES)
    .errorResponse('ID was invalid.')
    .errorResponse('Admin access was denied for the collection.', 403)
)
def previewCollectionAccess(self, collection, access, publicFlags, public):
    return _preview(Collection(), 'collection', collection, access, public, publicFlags)
