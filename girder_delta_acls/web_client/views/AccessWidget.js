import recurseModeTemplate from '../templates/recurseMode.pug';
import previewSummaryTemplate from '../templates/previewSummary.pug';
import '../stylesheets/deltaAcls.styl';

const $ = girder.$;
const _ = girder._;
const AccessWidget = girder.views.widgets.AccessWidget;
const { AccessType } = girder.constants;
const { restRequest } = girder.rest;
const { wrap } = girder.utilities.PluginUtils;

const DELTA = 'delta';
const REPLACE = 'replace';
const PREVIEW_DEBOUNCE_MS = 300;

const LEVEL_NAMES = {
    [AccessType.READ]: 'Read',
    [AccessType.WRITE]: 'Write',
    [AccessType.ADMIN]: 'Admin'
};

/**
 * Turn the preview's delta into short, human readable change descriptions.
 */
function describeChanges(delta) {
    const out = [];
    const describe = (change, suffix) => {
        const who = (change.name || change.login || change.id) + suffix;
        if (change.from === null) {
            out.push({ kind: 'add', text: `+ ${who} (${LEVEL_NAMES[change.to]})` });
        } else if (change.to === null) {
            out.push({ kind: 'remove', text: `− ${who}` });
        } else if (change.from !== change.to) {
            out.push({
                kind: 'change',
                text: `${who}: ${LEVEL_NAMES[change.from]} → ${LEVEL_NAMES[change.to]}`
            });
        } else {
            out.push({ kind: 'change', text: `${who}: flags changed` });
        }
    };

    _.each(delta.users, (change) => describe(change, ''));
    _.each(delta.groups, (change) => describe(change, ' (group)'));
    if (delta.public) {
        out.push({
            kind: 'change',
            text: delta.public.to ? 'Private → Public' : 'Public → Private'
        });
    }
    if (delta.publicFlags) {
        out.push({ kind: 'change', text: 'public access flags changed' });
    }
    return out;
}

_.extend(AccessWidget.prototype, {
    _deltaAclsEnabled: function () {
        return !this.hideRecurseOption && this.$('.g-delta-acls-mode').length > 0;
    },

    _deltaAclsRecursive: function () {
        return this.$('#g-apply-recursive').is(':checked');
    },

    _deltaAclsMode: function () {
        return this.$('input[name="g-delta-acls-mode"]:checked').val() || DELTA;
    },

    /** The request core's saveAccessList would send, read from the dialog as it is now. */
    _deltaAclsRequestData: function () {
        const publicFlags = _.uniq(_.map(
            this.$('.g-public-flag-checkbox:checked'), (el) => $(el).attr('flag')));
        return {
            access: JSON.stringify(this.getAccessList()),
            public: this.$('#g-access-public').is(':checked'),
            publicFlags: JSON.stringify(publicFlags)
        };
    },

    /** Called on any edit in the dialog: the old preview and any pending confirm are stale. */
    _deltaAclsChanged: function () {
        if (!this._deltaAclsEnabled()) {
            return;
        }
        this._deltaAclsConfirming = false;
        this._deltaAclsReplaceConfirmed = false;

        const recursive = this._deltaAclsRecursive();
        this.$('.g-delta-acls-mode').toggleClass('hide', !recursive);
        if (recursive) {
            this._deltaAclsSchedulePreview();
        }
    },

    _deltaAclsSchedulePreview: function () {
        if (!this._deltaAclsDebouncedPreview) {
            this._deltaAclsDebouncedPreview = _.debounce(
                () => this._deltaAclsFetchPreview(), PREVIEW_DEBOUNCE_MS);
        }
        this._deltaAclsRenderSummary('loading');
        this._deltaAclsDebouncedPreview();
    },

    /**
     * Fetch a fresh preview of the dialog as it is now. Resolves with it, or with null if
     * the request failed. A response superseded by a newer request is still returned to the
     * caller, but no longer drawn.
     */
    _deltaAclsFetchPreview: function () {
        const seq = (this._deltaAclsSeq || 0) + 1;
        this._deltaAclsSeq = seq;

        return restRequest({
            url: `${this.model.altUrl || this.model.resourceName}/${this.model.id}/access/preview`,
            method: 'POST',
            data: this._deltaAclsRequestData(),
            error: null
        }).then((preview) => {
            if (seq !== this._deltaAclsSeq) {
                return preview;
            }
            this._deltaAclsPreview = preview;
            this._deltaAclsApplyPermissions(preview);
            this._deltaAclsRenderSummary('ready');
            return preview;
        }, () => {
            if (seq === this._deltaAclsSeq) {
                this._deltaAclsPreview = null;
                this._deltaAclsRenderSummary('error');
            }
            return $.Deferred().resolve(null).promise();
        });
    },

    _deltaAclsApplyPermissions: function (preview) {
        const restricted = !preview.replaceAllowed;
        const replace = this.$('#g-delta-acls-mode-replace');
        replace.girderEnable(!restricted);
        this.$('.g-delta-acls-restricted').toggleClass('hide', !restricted);
        if (restricted && replace.is(':checked')) {
            this.$('#g-delta-acls-mode-delta').prop('checked', true);
        }
    },

    _deltaAclsRenderSummary: function (state) {
        const preview = this._deltaAclsPreview;
        if (state === 'ready' && !preview && !this._deltaAclsConfirming) {
            state = 'error';
        }
        this.$('.g-delta-acls-summary').html(previewSummaryTemplate({
            state,
            preview,
            mode: this._deltaAclsMode(),
            modelType: this.modelType,
            confirming: !!this._deltaAclsConfirming,
            changes: preview && state === 'ready' ? describeChanges(preview.delta) : []
        }));
    },

    _deltaAclsWatchEntries: function () {
        if (this._deltaAclsObserver) {
            this._deltaAclsObserver.disconnect();
        }
        // Entries are added asynchronously (after the user or group is fetched) and removed
        // by core's own handler, so watching the list catches both without wrapping them.
        const lists = this.$('#g-ac-list-users, #g-ac-list-groups').toArray();
        if (lists.length && window.MutationObserver) {
            this._deltaAclsObserver = new MutationObserver(() => this._deltaAclsChanged());
            _.each(lists, (el) => this._deltaAclsObserver.observe(el, { childList: true }));
        }
    }
});

wrap(AccessWidget, 'render', function (render) {
    render.call(this);

    const container = this.$('.g-recursive-container');
    if (this.hideRecurseOption || !container.length) {
        return this;
    }

    // Core's wording promised "set permissions on all subfolders to match", which is now
    // only one of two choices below it.
    const recursiveLabel = container.find('#g-apply-recursive').parent();
    recursiveLabel.contents().filter(function () {
        return this.nodeType === 3 && this.nodeValue.trim();
    }).replaceWith(' Also apply to subfolders');

    container.find('.g-delta-acls-mode').remove();
    container.append(recurseModeTemplate());

    this._deltaAclsConfirming = false;
    this._deltaAclsReplaceConfirmed = false;
    this._deltaAclsWatchEntries();
    this._deltaAclsChanged();
    return this;
});

wrap(AccessWidget, 'privacyChanged', function (privacyChanged) {
    privacyChanged.call(this);
    this._deltaAclsChanged();
});

wrap(AccessWidget, '_toggleAccessFlag', function (toggle, e) {
    toggle.call(this, e);
    this._deltaAclsChanged();
});

wrap(AccessWidget, '_togglePublicAccessFlag', function (toggle, e) {
    toggle.call(this, e);
    this._deltaAclsChanged();
});

wrap(AccessWidget, 'saveAccessList', function (saveAccessList) {
    if (!this._deltaAclsEnabled() || !this._deltaAclsRecursive()) {
        delete this.model._deltaAclsRecurseMode;
        return saveAccessList.call(this);
    }

    const mode = this._deltaAclsMode();
    if (mode === REPLACE && !this._deltaAclsReplaceConfirmed) {
        // Always confirm against a fresh preview: the debounced one may still be in flight.
        // If the preview fails, ask anyway rather than overwrite unannounced.
        this._deltaAclsFetchPreview().done((preview) => {
            if (!preview || preview.divergent > 0) {
                this._deltaAclsConfirming = true;
                this._deltaAclsRenderSummary('ready');
                this.$('button.g-save-access-list').girderEnable(true);
            } else {
                this._deltaAclsReplaceConfirmed = true;
                this.saveAccessList();
            }
        });
        return;
    }

    this._deltaAclsReplaceConfirmed = false;
    this.model._deltaAclsRecurseMode = mode;
    return saveAccessList.call(this);
});

_.extend(AccessWidget.prototype.events, {
    'change .g-recursive-container input[name="recurse"]': '_deltaAclsChanged',
    'change input[name="g-delta-acls-mode"]': '_deltaAclsChanged',
    'change .g-access-col-right>select': '_deltaAclsChanged',
    'click .g-delta-acls-confirm-replace': function () {
        this._deltaAclsConfirming = false;
        this._deltaAclsReplaceConfirmed = true;
        this.$('button.g-save-access-list').girderEnable(false);
        this.saveAccessList();
    },
    'click .g-delta-acls-cancel-replace': function () {
        this._deltaAclsConfirming = false;
        this._deltaAclsRenderSummary('ready');
    }
});
