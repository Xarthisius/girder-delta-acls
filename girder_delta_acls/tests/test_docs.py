"""Core's access routes document recurseMode in Swagger once the plugin is loaded."""

import pytest
from pytest_girder.assertions import assertStatusOk

from girder_delta_acls import rest

pytestmark = pytest.mark.plugin('delta_acls')

ROUTES = ('/folder/{id}/access', '/collection/{id}/access')


def _describe(server):
    resp = server.request('/describe')
    assertStatusOk(resp)
    return resp.json['paths']


def _params(op):
    return {p['name']: p for p in op['parameters']}


@pytest.mark.parametrize('path', ROUTES)
def test_core_access_route_documents_recurse_mode(server, path):
    op = _describe(server)[path]['put']
    params = _params(op)

    assert params['recurseMode']['enum'] == ['delta', 'replace']
    assert params['recurseMode']['required'] is False
    assert 'delta_acls.default_recurse_mode' in params['recurseMode']['description']
    assert 'recurseMode chooses' in params['recurse']['description']
    assert 'Girder-Delta-Acls' in op['description']
    assert 'replace_requires_site_admin' in op['responses']['403']['description']
    # Core's own 403 reason is kept, not replaced.
    assert 'denied' in op['responses']['403']['description']


@pytest.mark.parametrize('path', ROUTES)
def test_preview_routes_are_documented(server, path):
    assert 'post' in _describe(server)[path + '/preview']


def test_describing_again_changes_nothing(server):
    before = _describe(server)
    rest.describeCoreAccessRoutes()
    rest.describeCoreAccessRoutes()
    after = _describe(server)

    for path in ROUTES:
        assert after[path] == before[path]
        names = [p['name'] for p in after[path]['put']['parameters']]
        assert names.count('recurseMode') == 1
