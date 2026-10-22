import ConfigView from './views/ConfigView';

const router = girder.router;
const events = girder.events;
const { exposePluginConfig } = girder.utilities.PluginUtils;

exposePluginConfig('delta_acls', 'plugins/delta_acls/config');

router.route('plugins/delta_acls/config', 'deltaAclsConfig', function () {
    events.trigger('g:navigateTo', ConfigView);
});
