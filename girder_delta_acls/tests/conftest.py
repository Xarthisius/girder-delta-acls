"""Shared fixtures and helpers for the delta-acls server tests.

Girder's own fixtures (``db``, ``server``, ``admin``, ``user``) come from pytest-girder.

Each server test module declares ``pytestmark = pytest.mark.plugin('delta_acls')`` itself.
That marker cannot live here: pytest only honors module- and class-level ``pytestmark``, so
one in a conftest is silently ignored and the tests would run without the plugin loaded.
"""

import json

import pytest
from girder.constants import AccessType
from girder.models.collection import Collection
from girder.models.folder import Folder
from girder.models.user import User

READ, WRITE, ADMIN = AccessType.READ, AccessType.WRITE, AccessType.ADMIN


def make_user(login):
    return User().createUser(login, 'password', login.title(), 'Tester', '%s@example.com' % login)


def reload(doc, model=Folder):
    return model().load(doc['_id'], force=True)


def users_of(doc, model=Folder):
    """``{userId: level}`` for a document, freshly loaded."""
    return {e['id']: e['level'] for e in reload(doc, model)['access']['users']}


def groups_of(doc, model=Folder):
    return {e['id']: e['level'] for e in reload(doc, model)['access']['groups']}


def acl_json(doc, model=Folder, add=(), remove=(), set_level=None):
    """
    The current ACL of ``doc`` in the shape the web client sends, edited.

    ``add`` is ``[(user, level), ...]``; ``remove`` is a list of users; ``set_level`` maps a
    user to a new level.
    """
    doc = reload(doc, model)
    users = [
        {'id': str(e['id']), 'level': e['level'], 'flags': e.get('flags', [])}
        for e in doc['access']['users']
        if e['id'] not in {u['_id'] for u in remove}
    ]
    for u, level in (set_level or {}).items():
        for e in users:
            if e['id'] == str(u):
                e['level'] = level
    users += [{'id': str(u['_id']), 'level': level, 'flags': []} for u, level in add]
    groups = [
        {'id': str(e['id']), 'level': e['level'], 'flags': e.get('flags', [])}
        for e in doc['access']['groups']
    ]
    return {'users': users, 'groups': groups}


def put_access(server, user, kind, doc, access, public=None, **params):
    """``PUT /<kind>/:id/access``. Extra kwargs become query params (``recurse``, ...)."""
    request = {'access': json.dumps(access)}
    if public is not None:
        request['public'] = json.dumps(public)
    for key, value in params.items():
        request[key] = json.dumps(value) if isinstance(value, bool) else value
    return server.request(
        '/%s/%s/access' % (kind, doc['_id']), method='PUT', user=user, params=request
    )


@pytest.fixture
def people(db, admin, user):
    """``alice`` is the non-admin editor (pytest-girder's ``user``); ``admin`` is site admin."""
    yield {
        'admin': admin,
        'alice': user,
        'bob': make_user('bobby'),
        'carol': make_user('carol'),
        'dave': make_user('david'),
    }


@pytest.fixture
def tree(people):
    """
    A private collection ``C`` that Alice administers::

        C
        ├── A           Alice ADMIN (inherited)
        │   └── A1
        ├── S           Alice ADMIN, plus Bob WRITE set on S alone
        │   └── S1
        └── P           Bob ADMIN, Alice READ only  -> pruned
            └── P1      Alice ADMIN again, but below P so never reached
    """
    alice, bob = people['alice'], people['bob']
    folders = Folder()

    coll = Collection().createCollection('C', creator=alice, public=False)
    a = folders.createFolder(coll, 'A', parentType='collection', creator=alice)
    a1 = folders.createFolder(a, 'A1', parentType='folder', creator=alice)
    s = folders.createFolder(coll, 'S', parentType='collection', creator=alice)
    s1 = folders.createFolder(s, 'S1', parentType='folder', creator=alice)
    p = folders.createFolder(coll, 'P', parentType='collection', creator=alice)
    p1 = folders.createFolder(p, 'P1', parentType='folder', creator=alice)

    s = folders.setUserAccess(s, bob, WRITE, save=True)
    p = folders.setAccessList(
        p,
        {'users': [{'id': bob['_id'], 'level': ADMIN}, {'id': alice['_id'], 'level': READ}]},
        save=True,
    )

    yield {'C': coll, 'A': a, 'A1': a1, 'S': s, 'S1': s1, 'P': p, 'P1': p1}
