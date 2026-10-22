import ConfigViewTemplate from '../templates/configView.pug';

const PluginConfigBreadcrumbWidget = girder.views.widgets.PluginConfigBreadcrumbWidget;
const View = girder.views.View;
const events = girder.events;
const { restRequest } = girder.rest;

const DEFAULT_MODE = 'delta_acls.default_recurse_mode';
const REPLACE_ADMIN_ONLY = 'delta_acls.replace_requires_site_admin';

const ConfigView = View.extend({
    events: {
        'submit #g-delta-acls-config-form': function (event) {
            event.preventDefault();
            this.$('#g-delta-acls-config-error').empty();

            this._saveSettings([{
                key: DEFAULT_MODE,
                value: this.$('#g-delta-acls-default-mode').val()
            }, {
                key: REPLACE_ADMIN_ONLY,
                value: this.$('#g-delta-acls-replace-admin-only').is(':checked')
            }]);
        }
    },

    initialize: function () {
        restRequest({
            method: 'GET',
            url: 'system/setting',
            data: {
                list: JSON.stringify([DEFAULT_MODE, REPLACE_ADMIN_ONLY])
            }
        }).done((resp) => {
            this.render();
            this.$('#g-delta-acls-default-mode').val(resp[DEFAULT_MODE]);
            this.$('#g-delta-acls-replace-admin-only').prop('checked', !!resp[REPLACE_ADMIN_ONLY]);
        });
    },

    render: function () {
        this.$el.html(ConfigViewTemplate());

        if (!this.breadcrumb) {
            this.breadcrumb = new PluginConfigBreadcrumbWidget({
                pluginName: 'Delta ACLs',
                el: this.$('.g-config-breadcrumb-container'),
                parentView: this
            }).render();
        }

        return this;
    },

    _saveSettings: function (settings) {
        restRequest({
            method: 'PUT',
            url: 'system/setting',
            data: {
                list: JSON.stringify(settings)
            },
            error: null
        }).done(() => {
            events.trigger('g:alert', {
                icon: 'ok',
                text: 'Settings saved.',
                type: 'success',
                timeout: 4000
            });
        }).fail((resp) => {
            this.$('#g-delta-acls-config-error').text(resp.responseJSON.message);
        });
    }
});

export default ConfigView;
