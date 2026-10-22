"""
Unit tests for ``lib/delta.py``.

``computeDelta`` and ``withoutAdminFlags`` are pure and run without a server. The
``toMongoPipeline`` tests execute the pipeline against real Mongo (mongomock does not support
pipeline updates) on a scratch collection, via pytest-girder's ``db`` fixture only, so they
need no plugin marker.
"""

import pytest
from bson import ObjectId
from girder.constants import ACCESS_FLAGS, AccessType, registerAccessFlag

from girder_delta_acls.lib.delta import AclDelta, aclSignature, computeDelta, toMongoPipeline

READ, WRITE, ADMIN = AccessType.READ, AccessType.WRITE, AccessType.ADMIN
ALICE, BOB, CAROL, DAVE = (ObjectId() for _ in range(4))
STAFF, LAB = ObjectId(), ObjectId()


def acl(users=(), groups=(), public=False, publicFlags=()):
    """Build an access document from ``(id, level[, flags])`` tuples."""

    def entries(spec):
        return [
            {'id': s[0], 'level': s[1], 'flags': list(s[2] if len(s) > 2 else ())} for s in spec
        ]

    return {
        'access': {'users': entries(users), 'groups': entries(groups)},
        'public': public,
        'publicFlags': list(publicFlags),
    }


@pytest.fixture
def flags():
    """A normal flag and an admin-only flag, removed again afterwards."""
    registerAccessFlag('delta_test_plain', 'Plain')
    registerAccessFlag('delta_test_admin', 'Admin only', admin=True)
    yield
    ACCESS_FLAGS.pop('delta_test_plain', None)
    ACCESS_FLAGS.pop('delta_test_admin', None)


def byId(changes):
    return {c.id: c for c in changes}


class TestComputeDelta:
    def test_identical_is_empty(self):
        doc = acl(users=[(ALICE, ADMIN)], groups=[(STAFF, READ)], public=True)
        delta = computeDelta(doc, doc)
        assert delta.isEmpty()
        assert toMongoPipeline(delta) == []

    def test_entry_order_is_irrelevant(self):
        old = acl(users=[(ALICE, ADMIN), (BOB, READ, ['a', 'b'])])
        new = acl(users=[(BOB, READ, ['b', 'a']), (ALICE, ADMIN)])
        assert computeDelta(old, new).isEmpty()

    def test_add(self):
        delta = computeDelta(
            acl(users=[(ALICE, ADMIN)]), acl(users=[(ALICE, ADMIN), (CAROL, READ, ['x'])])
        )
        (c,) = delta.users
        assert (c.id, c.oldLevel, c.newLevel) == (CAROL, None, READ)
        assert c.levelChanged and not c.removed
        assert c.flagsAdded == {'x'} and c.newFlags == {'x'}
        assert delta.groups == ()

    def test_remove(self):
        delta = computeDelta(acl(groups=[(STAFF, WRITE), (LAB, READ)]), acl(groups=[(LAB, READ)]))
        (c,) = delta.groups
        assert c.id == STAFF and c.removed and c.oldLevel == WRITE

    def test_level_change(self):
        delta = computeDelta(acl(users=[(BOB, WRITE)]), acl(users=[(BOB, ADMIN)]))
        (c,) = delta.users
        assert (c.oldLevel, c.newLevel) == (WRITE, ADMIN)
        assert c.levelChanged and not c.flagsChanged

    def test_flags_only(self):
        delta = computeDelta(
            acl(users=[(BOB, READ, ['a', 'b'])]), acl(users=[(BOB, READ, ['b', 'c'])])
        )
        (c,) = delta.users
        assert not c.levelChanged
        assert c.flagsAdded == {'c'} and c.flagsRemoved == {'a'}

    def test_public_flip(self):
        delta = computeDelta(acl(public=False), acl(public=True))
        assert delta.public == (False, True)
        assert not delta.users and not delta.groups

    def test_public_flags(self):
        delta = computeDelta(
            acl(public=True, publicFlags=['a']), acl(public=True, publicFlags=['b'])
        )
        assert delta.public is None
        assert delta.publicFlagsAdded == {'b'} and delta.publicFlagsRemoved == {'a'}

    def test_missing_fields_are_tolerated(self):
        delta = computeDelta({}, acl(users=[(ALICE, ADMIN)]))
        assert [c.id for c in delta.users] == [ALICE]

    def test_duplicate_entries_collapse(self):
        old = acl(users=[(BOB, READ, ['a']), (BOB, WRITE)])
        assert computeDelta(old, acl(users=[(BOB, WRITE, ['a'])])).isEmpty()

    def test_summary_is_json_able(self):
        delta = computeDelta(
            acl(users=[(BOB, WRITE)], publicFlags=['a']), acl(users=[(BOB, ADMIN)], public=True)
        )
        assert delta.summary() == {
            'users': [
                {'id': str(BOB), 'from': WRITE, 'to': ADMIN, 'flagsAdded': [], 'flagsRemoved': []}
            ],
            'groups': [],
            'public': {'from': False, 'to': True},
            'publicFlags': {'added': [], 'removed': ['a']},
        }


class TestAdminFlags:
    def test_admin_flags_stripped(self, flags):
        delta = computeDelta(
            acl(users=[(BOB, READ, ['delta_test_admin'])], publicFlags=['delta_test_admin']),
            acl(
                users=[(BOB, READ, ['delta_test_plain']), (CAROL, READ, ['delta_test_admin'])],
                publicFlags=['delta_test_plain'],
            ),
        )
        stripped = delta.withoutAdminFlags()
        changes = byId(stripped.users)

        assert changes[BOB].flagsAdded == {'delta_test_plain'}
        assert changes[BOB].flagsRemoved == frozenset()
        assert changes[CAROL].newFlags == frozenset()
        assert changes[CAROL].flagsAdded == frozenset()
        assert stripped.publicFlagsAdded == {'delta_test_plain'}
        assert stripped.publicFlagsRemoved == frozenset()

        # The exact delta still carries them, for the target and for site admins.
        assert byId(delta.users)[CAROL].newFlags == {'delta_test_admin'}

    def test_change_that_only_touched_admin_flags_disappears(self, flags):
        delta = computeDelta(
            acl(users=[(BOB, READ)]), acl(users=[(BOB, READ, ['delta_test_admin'])])
        )
        assert not delta.isEmpty()
        assert delta.withoutAdminFlags().isEmpty()

    def test_no_admin_flags_registered_is_identity(self):
        delta = computeDelta(acl(), acl(users=[(BOB, READ)]))
        assert delta.withoutAdminFlags() is delta


@pytest.fixture
def scratch(db):
    from girder.models import getDbConnection

    coll = getDbConnection().get_database()['delta_acls_scratch']
    coll.drop()
    yield coll
    coll.drop()


def apply(scratch, doc, delta):
    _id = scratch.insert_one(dict(doc)).inserted_id
    pipeline = toMongoPipeline(delta)
    if pipeline:
        scratch.update_one({'_id': _id}, pipeline)
    return scratch.find_one({'_id': _id})


def levels(doc, principalType='users'):
    return {e['id']: (e['level'], sorted(e['flags'])) for e in doc['access'][principalType]}


class TestPipeline:
    def test_reproduces_target(self, scratch):
        """Applied to the document it was computed from, the delta reproduces the new ACL."""
        old = acl(
            users=[(ALICE, ADMIN), (BOB, WRITE, ['a']), (DAVE, READ)],
            groups=[(STAFF, READ)],
            publicFlags=['p'],
        )
        new = acl(
            users=[(ALICE, ADMIN), (BOB, ADMIN, ['b']), (CAROL, READ, ['c'])],
            groups=[(LAB, WRITE)],
            public=True,
            publicFlags=['q'],
        )
        result = apply(scratch, old, computeDelta(old, new))
        assert aclSignature(result) == aclSignature(new)

    def test_untouched_principals_survive(self, scratch):
        """The motivating case: Bob's own WRITE on the subfolder outlives adding Carol."""
        delta = computeDelta(
            acl(users=[(ALICE, ADMIN)]), acl(users=[(ALICE, ADMIN), (CAROL, READ)])
        )
        sub = acl(users=[(ALICE, ADMIN), (BOB, WRITE)], groups=[(LAB, READ)])
        result = apply(scratch, sub, delta)
        assert levels(result) == {ALICE: (ADMIN, []), BOB: (WRITE, []), CAROL: (READ, [])}
        assert levels(result, 'groups') == {LAB: (READ, [])}
        assert result['public'] is False

    def test_set_overwrites_existing_level(self, scratch):
        """Decision D1: an explicit SET overwrites, including downgrades."""
        delta = computeDelta(acl(), acl(users=[(BOB, READ, ['x'])]))
        result = apply(scratch, acl(users=[(BOB, ADMIN, ['y'])]), delta)
        assert levels(result) == {BOB: (READ, ['x', 'y'])}

    def test_level_change_adds_missing_entry(self, scratch):
        delta = computeDelta(acl(users=[(BOB, READ)]), acl(users=[(BOB, WRITE, ['a'])]))
        result = apply(scratch, acl(users=[(ALICE, ADMIN)]), delta)
        assert levels(result) == {ALICE: (ADMIN, []), BOB: (WRITE, ['a'])}

    def test_flag_only_change_is_noop_without_entry(self, scratch):
        delta = computeDelta(acl(users=[(BOB, READ)]), acl(users=[(BOB, READ, ['a'])]))
        result = apply(scratch, acl(users=[(ALICE, ADMIN)]), delta)
        assert levels(result) == {ALICE: (ADMIN, [])}

    def test_flag_change_keeps_other_flags_and_level(self, scratch):
        delta = computeDelta(acl(users=[(BOB, READ, ['a'])]), acl(users=[(BOB, READ, ['b'])]))
        result = apply(scratch, acl(users=[(BOB, WRITE, ['a', 'z'])]), delta)
        assert levels(result) == {BOB: (WRITE, ['b', 'z'])}

    def test_remove(self, scratch):
        delta = computeDelta(acl(groups=[(STAFF, READ)]), acl())
        result = apply(scratch, acl(groups=[(STAFF, ADMIN), (LAB, READ)]), delta)
        assert levels(result, 'groups') == {LAB: (READ, [])}

    def test_remove_absent_is_noop(self, scratch):
        delta = computeDelta(acl(users=[(BOB, READ)]), acl())
        result = apply(scratch, acl(users=[(ALICE, ADMIN)]), delta)
        assert levels(result) == {ALICE: (ADMIN, [])}

    def test_public_untouched_unless_changed(self, scratch):
        delta = computeDelta(acl(), acl(users=[(BOB, READ)]))
        assert apply(scratch, acl(public=True), delta)['public'] is True

    def test_public_flip_and_flags(self, scratch):
        delta = computeDelta(
            acl(public=False, publicFlags=['a']), acl(public=True, publicFlags=['b'])
        )
        result = apply(scratch, acl(public=False, publicFlags=['a', 'z']), delta)
        assert result['public'] is True
        assert sorted(result['publicFlags']) == ['b', 'z']

    def test_document_without_access_field(self, scratch):
        delta = computeDelta(acl(), acl(users=[(BOB, READ)], groups=[(LAB, READ)]))
        _id = scratch.insert_one({'name': 'bare'}).inserted_id
        scratch.update_one({'_id': _id}, toMongoPipeline(delta))
        result = scratch.find_one({'_id': _id})
        assert levels(result) == {BOB: (READ, [])}
        assert levels(result, 'groups') == {LAB: (READ, [])}

    def test_extra_entry_fields_preserved(self, scratch):
        delta = computeDelta(acl(users=[(BOB, READ)]), acl(users=[(BOB, WRITE)]))
        doc = {'access': {'users': [{'id': BOB, 'level': READ, 'flags': [], 'x': 1}], 'groups': []}}
        result = apply(scratch, doc, delta)
        assert result['access']['users'] == [{'id': BOB, 'level': WRITE, 'flags': [], 'x': 1}]

    def test_flag_names_are_literal(self, scratch):
        """A flag spelled like a field path must not be evaluated as one."""
        delta = computeDelta(acl(), acl(users=[(BOB, READ, ['$name'])], publicFlags=['$name']))
        result = apply(scratch, {'name': 'oops', **acl(users=[(ALICE, ADMIN)])}, delta)
        assert levels(result)[BOB] == (READ, ['$name'])
        assert result['publicFlags'] == ['$name']

    def test_empty_delta_dataclass(self):
        assert AclDelta().isEmpty()
