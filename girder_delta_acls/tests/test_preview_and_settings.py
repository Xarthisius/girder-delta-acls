"""The preview endpoint and the plugin settings."""

import json

import pytest
from girder.exceptions import ValidationException
from girder.models.collection import Collection
from girder.models.folder import Folder
from girder.models.setting import Setting
from pytest_girder.assertions import assertStatus, assertStatusOk

from girder_delta_acls.constants import PluginSettings

from .conftest import READ, WRITE, acl_json, put_access, reload, users_of

pytestmark = pytest.mark.plugin('delta_acls')


def preview(server, user, kind, doc, access, public=None):
    params = {'access': json.dumps(access)}
    if public is not None:
        params['public'] = json.dumps(public)
    return server.request(
        '/%s/%s/access/preview' % (kind, doc['_id']), method='POST', user=user, params=params
    )


def test_preview_counts_and_writes_nothing(server, people, tree):
    alice, carol = people['alice'], people['carol']
    before = {n: reload(tree[n]) for n in ('A', 'A1', 'S', 'S1', 'P', 'P1')}

    resp = preview(
        server,
        alice,
        'collection',
        tree['C'],
        acl_json(tree['C'], Collection, add=[(carol, READ)]),
        public=False,
    )
    assertStatusOk(resp)

    assert resp.json['empty'] is False
    assert resp.json['delta']['users'] == [
        {
            'id': str(carol['_id']),
            'login': 'carol',
            'name': 'Carol Tester',
            'from': None,
            'to': READ,
            'flagsAdded': [],
            'flagsRemoved': [],
        }
    ]
    assert resp.json['delta']['public'] is None
    # A, A1, S, S1 administered; P skipped (and P1 below it never counted).
    assert resp.json['subfolders'] == {'total': 5, 'admin': 4, 'skipped': 1}
    # Only S carries Bob's WRITE (S1 was created before it was granted), which C does not.
    assert resp.json['divergent'] == 1
    assert resp.json['truncated'] is False
    assert resp.json['replaceAllowed'] is True
    assert resp.json['defaultRecurseMode'] == 'replace'

    assert carol['_id'] not in users_of(tree['C'], Collection)
    for name, doc in before.items():
        assert reload(doc) == doc, name


def test_preview_empty_and_divergent(server, people, tree):
    resp = preview(server, people['alice'], 'folder', tree['S'], acl_json(tree['S']))
    assertStatusOk(resp)
    assert resp.json['empty'] is True
    assert resp.json['subfolders'] == {'total': 1, 'admin': 1, 'skipped': 0}
    # S has Bob's WRITE and S1 does not, so replacing would change S1.
    assert resp.json['divergent'] == 1

    resp = preview(server, people['alice'], 'folder', tree['A'], acl_json(tree['A']))
    assert resp.json['divergent'] == 0


def test_preview_public_change(server, people, tree):
    resp = preview(server, people['alice'], 'folder', tree['A'], acl_json(tree['A']), public=True)
    assertStatusOk(resp)
    assert resp.json['delta']['public'] == {'from': False, 'to': True}


def test_preview_truncates(server, people, tree, monkeypatch):
    from girder_delta_acls.lib import propagate

    original = propagate.surveySubtree
    monkeypatch.setattr(
        'girder_delta_acls.rest.surveySubtree',
        lambda root, rootType, user: original(root, rootType, user, limit=2),
    )
    resp = preview(
        server, people['alice'], 'collection', tree['C'], acl_json(tree['C'], Collection)
    )
    assertStatusOk(resp)
    assert resp.json['truncated'] is True
    assert resp.json['subfolders']['total'] == 2


def test_preview_requires_admin(server, people, tree):
    resp = preview(server, people['alice'], 'folder', tree['P'], acl_json(tree['P']))
    assertStatus(resp, 403)
    resp = preview(server, None, 'folder', tree['A'], acl_json(tree['A']))
    assertStatus(resp, 401)


def test_replace_requires_site_admin(server, people, tree):
    Setting().set(PluginSettings.REPLACE_REQUIRES_SITE_ADMIN, True)
    alice, admin, carol = people['alice'], people['admin'], people['carol']

    def put(user, mode):
        return put_access(
            server,
            user,
            'collection',
            tree['C'],
            acl_json(tree['C'], Collection, add=[(carol, READ)]),
            recurse=True,
            recurseMode=mode,
        )

    resp = put(alice, 'replace')
    assertStatus(resp, 403)
    assert 'site administrators' in resp.json['message']
    assert carol['_id'] not in users_of(tree['C'], Collection)

    # The setting also reaches recurse=true without a mode, while the default is replace.
    resp = put_access(
        server, alice, 'collection', tree['C'], acl_json(tree['C'], Collection), recurse=True
    )
    assertStatus(resp, 403)

    # Non-recursive saves and delta mode are unaffected.
    assertStatusOk(
        put_access(server, alice, 'collection', tree['C'], acl_json(tree['C'], Collection))
    )
    assertStatusOk(put(alice, 'delta'))

    assertStatusOk(put(admin, 'replace'))

    resp = preview(server, alice, 'folder', tree['A'], acl_json(tree['A']))
    assert resp.json['replaceAllowed'] is False
    resp = preview(server, admin, 'folder', tree['A'], acl_json(tree['A']))
    assert resp.json['replaceAllowed'] is True


def test_settings_validation(db):
    with pytest.raises(ValidationException):
        Setting().set(PluginSettings.DEFAULT_RECURSE_MODE, 'sideways')
    Setting().set(PluginSettings.DEFAULT_RECURSE_MODE, 'delta')
    assert Setting().get(PluginSettings.DEFAULT_RECURSE_MODE) == 'delta'

    with pytest.raises(ValidationException):
        Setting().set(PluginSettings.REPLACE_REQUIRES_SITE_ADMIN, 'maybe')
    Setting().set(PluginSettings.REPLACE_REQUIRES_SITE_ADMIN, 'true')
    assert Setting().get(PluginSettings.REPLACE_REQUIRES_SITE_ADMIN) is True


def test_settings_endpoint_as_admin(server, admin):
    resp = server.request(
        '/system/setting',
        method='PUT',
        user=admin,
        params={'key': PluginSettings.DEFAULT_RECURSE_MODE, 'value': 'delta'},
    )
    assertStatusOk(resp)
    resp = server.request(
        '/system/setting',
        method='PUT',
        user=admin,
        params={'key': PluginSettings.DEFAULT_RECURSE_MODE, 'value': 'nope'},
    )
    assertStatus(resp, 400)


def test_folder_write_level_irrelevant_to_preview_counts(server, people, tree):
    """WRITE is not enough: only ADMIN subfolders are counted as reachable."""
    alice = people['alice']
    Folder().setAccessList(
        reload(tree['A1']), {'users': [{'id': alice['_id'], 'level': WRITE}]}, save=True
    )
    resp = preview(server, alice, 'folder', tree['A'], acl_json(tree['A']))
    assert resp.json['subfolders'] == {'total': 1, 'admin': 0, 'skipped': 1}


def test_preview_does_not_name_principals_the_editor_cannot_see(server, people, tree):
    """Posting arbitrary ids must not reveal private users' logins or private group names."""
    from girder.models.group import Group
    from girder.models.user import User

    alice, admin, dave = people['alice'], people['admin'], people['dave']
    User().setPublic(dave, False, save=True)
    group = Group().createGroup('Hidden Lab', admin, public=False)

    access = acl_json(tree['A'], add=[(dave, READ)])
    access['groups'].append({'id': str(group['_id']), 'level': READ, 'flags': []})

    def named(user):
        resp = preview(server, user, 'folder', tree['A'], access)
        assertStatusOk(resp)
        return resp.json['delta']['users'][0], resp.json['delta']['groups'][0]

    u, g = named(alice)
    assert (u['id'], u['to'], u['login'], u['name']) == (str(dave['_id']), READ, None, None)
    assert (g['id'], g['name']) == (str(group['_id']), None)

    u, g = named(admin)
    assert (u['login'], g['name']) == ('david', 'Hidden Lab')

    # Once they are on the ACL, core's GET .../access shows them to Alice anyway.
    Folder().setUserAccess(reload(tree['A']), dave, READ, save=True)
    resp = preview(server, alice, 'folder', tree['A'], acl_json(tree['A'], remove=[dave]))
    assert resp.json['delta']['users'][0]['login'] == 'david'
