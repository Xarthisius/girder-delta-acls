const events = girder.events;
const AccessControlledModel = girder.models.AccessControlledModel;
const { wrap } = girder.utilities.PluginUtils;

const RESPONSE_HEADER = 'Girder-Delta-Acls';

/**
 * Forward the recursion mode chosen in the access dialog.
 *
 * AccessWidget.saveAccessList stashes the mode on the model just before it calls
 * updateAccess, so core's saveAccessList body does not have to be copied here. The stash is
 * consumed on every call, so a later programmatic updateAccess never inherits it.
 */
wrap(AccessControlledModel, 'updateAccess', function (updateAccess, params) {
    const mode = this._deltaAclsRecurseMode;
    delete this._deltaAclsRecurseMode;

    if (!(params && params.recurse && mode)) {
        return updateAccess.call(this, params);
    }

    return updateAccess.call(this, Object.assign({}, params, { recurseMode: mode }))
        .done((resp, status, xhr) => {
            let result;
            try {
                result = JSON.parse(xhr.getResponseHeader(RESPONSE_HEADER));
            } catch (e) {
                return;
            }
            if (!result || result.mode !== 'delta') {
                return;
            }

            let text;
            if (!result.changed) {
                text = 'No permission changes to apply to subfolders.';
            } else if (result.folders) {
                text = `Permission changes applied to ${result.folders} ` +
                    (result.folders === 1 ? 'subfolder.' : 'subfolders.');
            } else {
                text = 'Permissions saved. No subfolders needed changing.';
            }
            events.trigger('g:alert', { icon: 'ok', text, type: 'success', timeout: 4000 });
        });
});
