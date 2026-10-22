"""
Applying an ``AclDelta`` to a target and its subtree.

Every write is one aggregation-pipeline update, so each document is changed atomically and
nothing written concurrently (a rename, another plugin's ``$push``) is lost. Core instead
loads, mutates and ``save()``s whole documents. The tree as a whole is **not**
transactional: that would need a replica set and ``start_session``.
"""

import pymongo
from girder import events
from girder.constants import AccessType
from girder.models.folder import Folder
from girder.utility.progress import noProgress

from ..constants import PREVIEW_FOLDER_LIMIT, PROPAGATE_AFTER_EVENT
from .delta import aclSignature, toMongoPipeline


def _withPermissions(query, user, level=AccessType.ADMIN):
    clauses = Folder().permissionClauses(user, level)
    return {'$and': [query, clauses]} if clauses else query


def applyToDocument(model, doc, delta):
    """
    Apply ``delta`` to ``doc`` itself and return the document as stored afterwards.

    The caller has already checked ADMIN on ``doc``. Like the descendant writes this fires
    no ``model.<name>.save`` event.
    """
    pipeline = toMongoPipeline(delta)
    if not pipeline:
        return doc

    return model.collection.find_one_and_update(
        {'_id': doc['_id']}, pipeline, return_document=pymongo.ReturnDocument.AFTER
    )


def _childQuery(frontier, parentType):
    return {'parentId': {'$in': frontier}, 'parentCollection': parentType}


def _applyToLevel(folderIds, pipeline, user):
    """One write for a whole tree level. Re-checks ADMIN to close the check-then-write gap."""
    Folder().collection.update_many(_withPermissions({'_id': {'$in': folderIds}}, user), pipeline)


def propagateDelta(root, rootType, delta, user, progress=noProgress):
    """
    Apply ``delta`` to every folder below ``root`` that ``user`` administers.

    Walks the tree one level at a time and prunes at any folder without ADMIN, exactly as
    core's recursive ``setAccessList`` does: nothing beneath such a folder is touched, even
    where the user is an admin again further down. ``root`` itself is not written here.

    ``rootType`` is ``'folder'`` or ``'collection'``. Returns the ids of the folders written.
    """
    pipeline = toMongoPipeline(delta)
    folderIds = []

    if pipeline:
        frontier, parentType = [root['_id']], rootType
        while frontier:
            ids = [
                f['_id']
                for f in Folder().collection.find(
                    _withPermissions(_childQuery(frontier, parentType), user),
                    projection={'_id': True},
                )
            ]
            if not ids:
                break

            _applyToLevel(ids, pipeline, user)
            folderIds.extend(ids)
            progress.update(increment=len(ids), message='Updated %d subfolders' % len(folderIds))
            frontier, parentType = ids, 'folder'

    events.trigger(
        PROPAGATE_AFTER_EVENT,
        {
            'root': root,
            'rootType': rootType,
            'delta': delta.summary(),
            'folderIds': folderIds,
            'user': user,
        },
    )

    return folderIds


def surveySubtree(root, rootType, user, limit=PREVIEW_FOLDER_LIMIT):
    """
    Count what a recursive save on ``root`` would reach. Writes nothing.

    Returns ``{'admin', 'skipped', 'total', 'divergent', 'truncated'}``: ``admin`` folders
    would be written; ``skipped`` are the non-admin folders where the walk is pruned (their
    contents are not counted); ``divergent`` counts admin folders whose access differs from
    ``root``'s current access, i.e. the folders whose own permissions a replace-mode save
    would overwrite.
    """
    folderModel = Folder()
    rootSignature = aclSignature(root)
    admin = skipped = divergent = 0
    truncated = False

    frontier, parentType = [root['_id']], rootType
    while frontier and not truncated:
        nextFrontier = []
        for folder in folderModel.collection.find(
            _childQuery(frontier, parentType),
            projection={'_id': True, 'access': True, 'public': True, 'publicFlags': True},
        ):
            if admin + skipped >= limit:
                truncated = True
                break

            if not folderModel.hasAccess(folder, user, AccessType.ADMIN):
                skipped += 1
                continue

            admin += 1
            nextFrontier.append(folder['_id'])
            if aclSignature(folder) != rootSignature:
                divergent += 1

        frontier, parentType = nextFrontier, 'folder'

    return {
        'admin': admin,
        'skipped': skipped,
        'total': admin + skipped,
        'divergent': divergent,
        'truncated': truncated,
    }
