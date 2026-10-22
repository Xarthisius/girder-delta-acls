"""End-to-end behaviour of ``PUT /{folder,collection}/:id/access`` with the plugin loaded."""

import json

import pytest
from girder import events
from girder.constants import ACCESS_FLAGS, registerAccessFlag
from girder.models.collection import Collection
from girder.models.folder import Folder
from girder.models.setting import Setting
from pytest_girder.assertions import assertStatus, assertStatusOk

from girder_delta_acls.constants import PROPAGATE_AFTER_EVENT, RESPONSE_HEADER, PluginSettings
from girder_delta_acls.lib import propagate

from .conftest import ADMIN, READ, WRITE, acl_json, put_access, reload, users_of

pytestmark = pytest.mark.plugin('delta_acls')


def add_carol_on_c(server, people, tree, **params):
    return put_access(
        server,
        people['alice'],
        'collection',
        tree['C'],
        acl_json(tree['C'], Collection, add=[(people['carol'], READ)]),
        public=False,
        recurse=True,
        **params,
    )


def test_motivating_scenario_delta_keeps_subfolder_grants(server, people, tree):
    bob, carol = people['bob']['_id'], people['carol']['_id']

    resp = add_carol_on_c(server, people, tree, recurseMode='delta')
    assertStatusOk(resp)

    assert users_of(tree['C'], Collection)[carol] == READ
    for name in ('A', 'A1', 'S', 'S1'):
        assert users_of(tree[name])[carol] == READ, name

    # Bob's WRITE, granted on S alone, survives.
    assert users_of(tree['S'])[bob] == WRITE
    # P is not administered by Alice, so it and everything below it are untouched.
    assert carol not in users_of(tree['P'])
    assert carol not in users_of(tree['P1'])

    assert json.loads(resp.headers[RESPONSE_HEADER]) == {
        'mode': 'delta',
        'changed': True,
        'folders': 4,
    }


def test_replace_mode_is_core_behaviour(server, people, tree):
    bob, carol = people['bob']['_id'], people['carol']['_id']

    resp = add_carol_on_c(server, people, tree, recurseMode='replace')
    assertStatusOk(resp)

    assert RESPONSE_HEADER not in resp.headers
    assert users_of(tree['S'])[carol] == READ
    assert bob not in users_of(tree['S'])  # lost, as in core
    assert carol not in users_of(tree['P'])  # core prunes too


@pytest.mark.parametrize(
    ('default', 'bobKeepsWrite'), [(None, False), ('replace', False), ('delta', True)]
)
def test_default_recurse_mode(server, people, tree, default, bobKeepsWrite):
    if default is not None:
        Setting().set(PluginSettings.DEFAULT_RECURSE_MODE, default)

    assertStatusOk(add_carol_on_c(server, people, tree))
    assert (people['bob']['_id'] in users_of(tree['S'])) is bobKeepsWrite


def test_invalid_recurse_mode(server, people, tree):
    resp = add_carol_on_c(server, people, tree, recurseMode='sideways')
    assertStatus(resp, 400)
    assert 'recurseMode' in resp.json['message']
    assert people['carol']['_id'] not in users_of(tree['C'], Collection)


def test_recurse_false_ignores_recurse_mode(server, people, tree):
    resp = add_carol_on_c(server, people, tree, recurseMode='delta')  # sanity: recurse=True
    assertStatusOk(resp)

    access = acl_json(tree['A'], add=[(people['dave'], WRITE)])
    resp = put_access(
        server, people['alice'], 'folder', tree['A'], access, recurse=False, recurseMode='delta'
    )
    assertStatusOk(resp)
    assert RESPONSE_HEADER not in resp.headers
    assert users_of(tree['A'])[people['dave']['_id']] == WRITE
    assert people['dave']['_id'] not in users_of(tree['A1'])


def test_remove_principal_only_removes_that_principal(server, people, tree):
    alice, bob, dave = people['alice'], people['bob'], people['dave']
    for name in ('C', 'A', 'A1', 'S', 'S1', 'P'):
        model = Collection if name == 'C' else Folder
        model().setUserAccess(reload(tree[name], model), dave, READ, save=True)

    resp = put_access(
        server,
        alice,
        'collection',
        tree['C'],
        acl_json(tree['C'], Collection, remove=[dave]),
        recurse=True,
        recurseMode='delta',
    )
    assertStatusOk(resp)

    for name in ('A', 'A1', 'S', 'S1'):
        assert dave['_id'] not in users_of(tree[name]), name
    assert users_of(tree['S'])[bob['_id']] == WRITE
    assert users_of(tree['P'])[dave['_id']] == READ  # pruned


def test_level_change_overwrites_and_adds(server, people, tree):
    """D1: a changed level overwrites existing entries and creates missing ones."""
    alice, bob = people['alice'], people['bob']
    Collection().setUserAccess(reload(tree['C'], Collection), bob, READ, save=True)

    resp = put_access(
        server,
        alice,
        'collection',
        tree['C'],
        acl_json(tree['C'], Collection, set_level={bob['_id']: ADMIN}),
        recurse=True,
        recurseMode='delta',
    )
    assertStatusOk(resp)
    for name in ('A', 'A1', 'S', 'S1'):
        assert users_of(tree[name])[bob['_id']] == ADMIN, name


def test_pruned_subtree_untouched_even_where_admin_again(server, people, tree):
    before = reload(tree['P1'])
    assertStatusOk(add_carol_on_c(server, people, tree, recurseMode='delta'))
    after = reload(tree['P1'])
    assert after['access'] == before['access']
    assert after['public'] == before['public']


def test_public_flip_propagates(server, people, tree):
    alice = people['alice']
    resp = put_access(
        server,
        alice,
        'collection',
        tree['C'],
        acl_json(tree['C'], Collection),
        public=True,
        recurse=True,
        recurseMode='delta',
    )
    assertStatusOk(resp)
    assert reload(tree['C'], Collection)['public'] is True
    for name in ('A', 'A1', 'S', 'S1'):
        assert reload(tree[name])['public'] is True, name
    assert reload(tree['P'])['public'] is False


def test_unchanged_public_leaves_subfolder_privacy_alone(server, people, tree):
    Folder().setPublic(reload(tree['S']), True, save=True)

    assertStatusOk(add_carol_on_c(server, people, tree, recurseMode='delta'))

    assert reload(tree['S'])['public'] is True
    assert reload(tree['A'])['public'] is False


def test_empty_delta_writes_nothing(server, people, tree):
    before = {name: reload(tree[name]) for name in ('A', 'A1', 'S', 'S1')}
    resp = put_access(
        server,
        people['alice'],
        'collection',
        tree['C'],
        acl_json(tree['C'], Collection),
        public=False,
        recurse=True,
        recurseMode='delta',
    )
    assertStatusOk(resp)
    assert json.loads(resp.headers[RESPONSE_HEADER]) == {
        'mode': 'delta',
        'changed': False,
        'folders': 0,
    }
    for name, doc in before.items():
        assert reload(doc) == doc, name


def test_folder_root(server, people, tree):
    carol = people['carol']['_id']
    resp = put_access(
        server,
        people['alice'],
        'folder',
        tree['S'],
        acl_json(tree['S'], add=[(people['carol'], WRITE)]),
        recurse=True,
        recurseMode='delta',
    )
    assertStatusOk(resp)
    assert users_of(tree['S'])[carol] == WRITE
    assert users_of(tree['S1'])[carol] == WRITE
    assert carol not in users_of(tree['A'])
    assert carol not in users_of(tree['C'], Collection)


def test_folder_under_user_home(server, people):
    alice, bob, carol = people['alice'], people['bob'], people['carol']
    home = Folder().createFolder(alice, 'Research', parentType='user', creator=alice)
    sub = Folder().createFolder(home, 'Shared', parentType='folder', creator=alice)
    sub = Folder().setUserAccess(sub, bob, WRITE, save=True)

    resp = put_access(
        server,
        alice,
        'folder',
        home,
        acl_json(home, add=[(carol, READ)]),
        recurse=True,
        recurseMode='delta',
    )
    assertStatusOk(resp)
    assert users_of(sub) == {alice['_id']: ADMIN, bob['_id']: WRITE, carol['_id']: READ}


def test_non_admin_owner_can_still_put_access(server, people, tree):
    """Regression: the .before listener must carry an access decorator (invariant 1)."""
    alice = people['alice']
    assert not alice['admin']

    resp = put_access(server, alice, 'folder', tree['A'], acl_json(tree['A']))
    assertStatusOk(resp)

    resp = put_access(
        server,
        alice,
        'folder',
        tree['A'],
        acl_json(tree['A']),
        recurse=True,
        recurseMode='replace',
    )
    assertStatusOk(resp)

    resp = put_access(
        server,
        alice,
        'collection',
        tree['C'],
        acl_json(tree['C'], Collection),
        recurse=True,
        recurseMode='delta',
    )
    assertStatusOk(resp)


def test_admin_required_on_target(server, people, tree):
    resp = put_access(
        server,
        people['alice'],
        'folder',
        tree['P'],
        acl_json(tree['P']),
        recurse=True,
        recurseMode='delta',
    )
    assertStatus(resp, 403)


def test_anonymous_rejected(server, tree):
    resp = put_access(server, None, 'folder', tree['A'], acl_json(tree['A']), recurse=True)
    assertStatus(resp, 401)


def test_response_shape_matches_core(server, people, tree):
    alice = people['alice']
    core = put_access(server, alice, 'folder', tree['A'], acl_json(tree['A']), recurse=False)
    assertStatusOk(core)

    ours = put_access(
        server,
        alice,
        'folder',
        tree['A'],
        acl_json(tree['A'], add=[(people['carol'], READ)]),
        recurse=True,
        recurseMode='delta',
    )
    assertStatusOk(ours)

    assert set(ours.json) == set(core.json)
    assert 'access' in ours.json
    assert {e['id'] for e in ours.json['access']['users']} == {
        str(alice['_id']),
        str(people['carol']['_id']),
    }


def test_progress_notifications(server, people, tree):
    assertStatusOk(add_carol_on_c(server, people, tree, recurseMode='delta', progress=True))


def test_admin_only_flags_not_pushed_by_non_admin(server, people, tree):
    registerAccessFlag('delta_test_admin', 'Admin only', admin=True)
    registerAccessFlag('delta_test_plain', 'Plain')
    try:
        alice, bob = people['alice'], people['bob']
        # A site admin gives Bob the admin-only flag on C and on A.
        for name, model in (('C', Collection), ('A', Folder)):
            model().setUserAccess(
                reload(tree[name], model),
                bob,
                READ,
                flags=['delta_test_admin'],
                save=True,
                force=True,
            )
        access = acl_json(tree['C'], Collection)
        for e in access['users']:
            if e['id'] == str(bob['_id']):
                e['level'] = WRITE
                e['flags'] = ['delta_test_admin', 'delta_test_plain']

        assertStatusOk(
            put_access(
                server, alice, 'collection', tree['C'], access, recurse=True, recurseMode='delta'
            )
        )

        def bobOn(name, model=Folder):
            return {e['id']: e for e in reload(tree[name], model)['access']['users']}[bob['_id']]

        # On the target this is core's behaviour, not ours: _validateFlags compares the
        # client's string ids against stored ObjectIds, never sees the flag as already
        # enabled, and drops it for a non-admin. A non-recursive save does the same.
        assert bobOn('C', Collection)['flags'] == ['delta_test_plain']
        # Descendants never have admin-only flags added or removed by a non-admin.
        assert bobOn('A')['level'] == WRITE
        assert sorted(bobOn('A')['flags']) == ['delta_test_admin', 'delta_test_plain']
        assert bobOn('S')['level'] == WRITE  # S already had Bob at WRITE
        assert bobOn('S1') == {'id': bob['_id'], 'level': WRITE, 'flags': ['delta_test_plain']}
    finally:
        ACCESS_FLAGS.pop('delta_test_admin', None)
        ACCESS_FLAGS.pop('delta_test_plain', None)


def test_propagate_event(server, people, tree):
    seen = []
    with events.bound(PROPAGATE_AFTER_EVENT, 'test', lambda e: seen.append(e.info)):
        assertStatusOk(add_carol_on_c(server, people, tree, recurseMode='delta'))

    (info,) = seen
    assert info['rootType'] == 'collection'
    assert info['root']['_id'] == tree['C']['_id']
    assert info['user']['_id'] == people['alice']['_id']
    assert set(info['folderIds']) == {tree[n]['_id'] for n in ('A', 'A1', 'S', 'S1')}
    assert info['delta']['users'][0]['id'] == str(people['carol']['_id'])


def test_concurrent_rename_is_not_lost(server, people, tree, monkeypatch):
    """A write landing between the level query and the bulk update survives it."""
    original = propagate._applyToLevel

    def renameFirst(folderIds, pipeline, user):
        if tree['S']['_id'] in folderIds:
            Folder().updateFolder(dict(reload(tree['S']), name='S renamed'))
        return original(folderIds, pipeline, user)

    monkeypatch.setattr(propagate, '_applyToLevel', renameFirst)
    assertStatusOk(add_carol_on_c(server, people, tree, recurseMode='delta'))

    s = reload(tree['S'])
    assert s['name'] == 'S renamed'
    assert {e['id'] for e in s['access']['users']} >= {people['carol']['_id'], people['bob']['_id']}
