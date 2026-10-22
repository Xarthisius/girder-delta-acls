from girder.exceptions import ValidationException
from girder.utility import setting_utilities, toBool

from .constants import PluginSettings, RecurseMode


@setting_utilities.default(PluginSettings.DEFAULT_RECURSE_MODE)
def _defaultRecurseMode():
    # ``replace`` keeps existing scripts and girder_client calls that send ``recurse=true``
    # behaving exactly as before. The web UI always sends ``recurseMode`` explicitly.
    return RecurseMode.REPLACE


@setting_utilities.validator(PluginSettings.DEFAULT_RECURSE_MODE)
def _validateDefaultRecurseMode(doc):
    if doc['value'] not in RecurseMode.ALL:
        raise ValidationException(
            'Default recurse mode must be one of: %s.' % ', '.join(RecurseMode.ALL), 'value'
        )


@setting_utilities.default(PluginSettings.REPLACE_REQUIRES_SITE_ADMIN)
def _defaultReplaceRequiresSiteAdmin():
    return False


@setting_utilities.validator(PluginSettings.REPLACE_REQUIRES_SITE_ADMIN)
def _validateReplaceRequiresSiteAdmin(doc):
    if not isinstance(doc['value'], bool):
        if not isinstance(doc['value'], str) or doc['value'].lower() not in ('true', 'false'):
            raise ValidationException('This setting must be a boolean.', 'value')
        doc['value'] = toBool(doc['value'])
