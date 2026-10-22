"""
Pure ACL diffing and the Mongo update that applies a diff.

Nothing in here touches the database. ``computeDelta`` compares two versions of one
document's ACL; ``toMongoPipeline`` turns the result into a single aggregation-pipeline
update that applies it to *any* document atomically, leaving every principal the delta does
not mention exactly as it was.

Semantics, per principal ``p`` the editor touched on the target:

=================  ==============================  ==========================================
Change             Descendant has no entry for p   Descendant has an entry for p
=================  ==============================  ==========================================
added              add ``{id, level, flags}``      overwrite level, union the new flags
level changed      add ``{id, level, flags}``      overwrite level, apply flag +/-
only flags changed no-op                           apply flag +/-
removed            no-op                           remove the entry
=================  ==============================  ==========================================

``public`` is set only if it changed; ``publicFlags`` gets the flag +/- applied.
"""

import dataclasses

from girder.constants import ACCESS_FLAGS

_PRINCIPAL_TYPES = ('users', 'groups')


@dataclasses.dataclass(frozen=True)
class PrincipalChange:
    """
    What happened to one user or group entry on the target.

    ``oldLevel`` is None when the principal is new on the target; ``newLevel`` is None when
    it was removed. ``newFlags`` is the principal's complete flag set after the edit, used
    when a descendant has no entry yet.
    """

    id: object
    oldLevel: int | None
    newLevel: int | None
    flagsAdded: frozenset = frozenset()
    flagsRemoved: frozenset = frozenset()
    newFlags: frozenset = frozenset()

    @property
    def removed(self):
        return self.newLevel is None

    @property
    def levelChanged(self):
        """True for additions and level changes: the entry is created where missing."""
        return self.newLevel is not None and self.newLevel != self.oldLevel

    @property
    def flagsChanged(self):
        return bool(self.flagsAdded or self.flagsRemoved)

    def isNoop(self):
        return not (self.removed or self.levelChanged or self.flagsChanged)

    def withoutFlags(self, flags):
        return dataclasses.replace(
            self,
            flagsAdded=self.flagsAdded - flags,
            flagsRemoved=self.flagsRemoved - flags,
            newFlags=self.newFlags - flags,
        )

    def summary(self):
        return {
            'id': str(self.id),
            'from': self.oldLevel,
            'to': self.newLevel,
            'flagsAdded': sorted(self.flagsAdded),
            'flagsRemoved': sorted(self.flagsRemoved),
        }


@dataclasses.dataclass(frozen=True)
class AclDelta:
    users: tuple = ()
    groups: tuple = ()
    #: ``(old, new)`` when ``public`` changed, else None.
    public: tuple | None = None
    publicFlagsAdded: frozenset = frozenset()
    publicFlagsRemoved: frozenset = frozenset()

    def isEmpty(self):
        return not (
            self.users
            or self.groups
            or self.public is not None
            or self.publicFlagsAdded
            or self.publicFlagsRemoved
        )

    def withoutAdminFlags(self):
        """
        The delta a non-site-admin may push to *other* documents.

        Core's ``_validateFlags`` only lets a non-admin keep an admin-only flag on an entry
        that already has it on that same document. Stripping admin-only flags from every
        add, remove and new-entry payload preserves that guarantee document by document:
        such flags are never granted, and any a descendant already carries are left alone.
        """
        adminFlags = frozenset(k for k, v in ACCESS_FLAGS.items() if v['admin'])
        if not adminFlags:
            return self

        def strip(changes):
            stripped = (c.withoutFlags(adminFlags) for c in changes)
            return tuple(c for c in stripped if not c.isNoop())

        return dataclasses.replace(
            self,
            users=strip(self.users),
            groups=strip(self.groups),
            publicFlagsAdded=self.publicFlagsAdded - adminFlags,
            publicFlagsRemoved=self.publicFlagsRemoved - adminFlags,
        )

    def summary(self):
        """A JSON-able description of the delta."""
        flags = None
        if self.publicFlagsAdded or self.publicFlagsRemoved:
            flags = {
                'added': sorted(self.publicFlagsAdded),
                'removed': sorted(self.publicFlagsRemoved),
            }

        return {
            'users': [c.summary() for c in self.users],
            'groups': [c.summary() for c in self.groups],
            'public': (
                None if self.public is None else {'from': self.public[0], 'to': self.public[1]}
            ),
            'publicFlags': flags,
        }


def _entries(doc, principalType):
    """
    Map principal id to ``(level, flags)`` for one side of a document's ACL.

    Core never deduplicates entries, so a principal can appear twice. Collapse duplicates
    to the highest level and the union of flags, which is what ``getAccessLevel`` and
    ``hasAccessFlags`` effectively see.
    """
    out = {}
    for entry in (doc.get('access') or {}).get(principalType) or ():
        level, flags = entry['level'], frozenset(entry.get('flags') or ())
        if entry['id'] in out:
            prevLevel, prevFlags = out[entry['id']]
            level, flags = max(level, prevLevel), flags | prevFlags
        out[entry['id']] = (level, flags)
    return out


def _diffPrincipals(old, new):
    changes = []
    # Keep a stable order: old entries in their order, then newly added ones.
    for pid in list(old) + [p for p in new if p not in old]:
        oldLevel, oldFlags = old.get(pid, (None, frozenset()))
        newLevel, newFlags = new.get(pid, (None, frozenset()))

        if newLevel is None:
            change = PrincipalChange(pid, oldLevel, None, flagsRemoved=oldFlags)
        else:
            change = PrincipalChange(
                pid,
                oldLevel,
                newLevel,
                flagsAdded=newFlags - oldFlags,
                flagsRemoved=oldFlags - newFlags,
                newFlags=newFlags,
            )

        if not change.isNoop():
            changes.append(change)

    return tuple(changes)


def computeDelta(oldDoc, newDoc):
    """
    Compare the ACL of one document before and after an edit.

    ``newDoc`` should be the result of core's ``setAccessList(save=False)`` on a copy of
    ``oldDoc``, so it has already been normalised and flag-validated for the editor. The
    returned delta is exact: applied to ``oldDoc`` it reproduces ``newDoc``'s ACL. Use
    ``AclDelta.withoutAdminFlags()`` before applying it anywhere else as a non-admin.
    """
    users = _diffPrincipals(_entries(oldDoc, 'users'), _entries(newDoc, 'users'))
    groups = _diffPrincipals(_entries(oldDoc, 'groups'), _entries(newDoc, 'groups'))

    oldPublic, newPublic = bool(oldDoc.get('public')), bool(newDoc.get('public'))
    oldFlags = frozenset(oldDoc.get('publicFlags') or ())
    newFlags = frozenset(newDoc.get('publicFlags') or ())

    return AclDelta(
        users=users,
        groups=groups,
        public=(oldPublic, newPublic) if oldPublic != newPublic else None,
        publicFlagsAdded=newFlags - oldFlags,
        publicFlagsRemoved=oldFlags - newFlags,
    )


def aclSignature(doc):
    """
    A hashable, order-insensitive form of a document's whole access state, for checking
    whether two documents have the same permissions.
    """
    return (
        frozenset(_entries(doc, 'users').items()),
        frozenset(_entries(doc, 'groups').items()),
        bool(doc.get('public')),
        frozenset(doc.get('publicFlags') or ()),
    )


def _applyFlags(current, added, removed):
    if not (added or removed):
        return current
    expr = current
    if added:
        expr = {'$setUnion': [expr, {'$literal': sorted(added)}]}
    if removed:
        expr = {'$setDifference': [expr, {'$literal': sorted(removed)}]}
    return expr


def _principalsExpr(principalType, changes):
    """The new value of ``access.<principalType>`` as an aggregation expression."""
    path = '$access.' + principalType
    current = {'$ifNull': [path, []]}

    removedIds = [c.id for c in changes if c.removed]
    if removedIds:
        current = {
            '$filter': {
                'input': current,
                'as': 'e',
                'cond': {'$not': [{'$in': ['$$e.id', removedIds]}]},
            }
        }

    branches = [
        {
            'case': {'$eq': ['$$e.id', c.id]},
            'then': {
                '$mergeObjects': [
                    '$$e',
                    {
                        'level': c.newLevel if c.levelChanged else '$$e.level',
                        'flags': _applyFlags(
                            {'$ifNull': ['$$e.flags', []]}, c.flagsAdded, c.flagsRemoved
                        ),
                    },
                ]
            },
        }
        for c in changes
        if not c.removed
    ]
    if branches:
        current = {
            '$map': {
                'input': current,
                'as': 'e',
                'in': {'$switch': {'branches': branches, 'default': '$$e'}},
            }
        }

    # Additions and level changes create the entry where the document has none. These read
    # the *input* document (all expressions in one $set stage do), so they cannot see the
    # result of the $map above, and do not need to.
    existingIds = {'$ifNull': [path + '.id', []]}
    additions = [
        {
            '$cond': [
                {'$in': [c.id, existingIds]},
                [],
                [{'id': c.id, 'level': c.newLevel, 'flags': {'$literal': sorted(c.newFlags)}}],
            ]
        }
        for c in changes
        if c.levelChanged
    ]
    if additions:
        current = {'$concatArrays': [current, *additions]}

    return current


def toMongoPipeline(delta):
    """
    An aggregation-pipeline update (MongoDB >= 4.2) applying ``delta`` to a document in one
    atomic write. Returns ``[]`` for an empty delta.
    """
    fields = {}
    for principalType in _PRINCIPAL_TYPES:
        changes = getattr(delta, principalType)
        if changes:
            fields['access.' + principalType] = _principalsExpr(principalType, changes)

    if delta.public is not None:
        fields['public'] = {'$literal': delta.public[1]}

    if delta.publicFlagsAdded or delta.publicFlagsRemoved:
        fields['publicFlags'] = _applyFlags(
            {'$ifNull': ['$publicFlags', []]}, delta.publicFlagsAdded, delta.publicFlagsRemoved
        )

    return [{'$set': fields}] if fields else []
