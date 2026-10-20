"""Calm defaults for routes that depend on Logitech's cloud or are not ported.

Every route the UI calls is implemented, stubbed here, or documented as
unsupported (see feature-parity.md section 9). A stub answers the quiet
"nothing here" value so the UI hides the feature instead of spinning or
raising an error; none of them reaches the network.

``lookup(verb, path, payload)`` returns ``(code, payload)`` or ``None`` when the
path is not a stub. A SET that the router does not handle and that is not listed
here is answered NOT_SUPPORTED (and logged) by the router. Payloads carry only ``@type`` and the fields that differ from the
proto3 defaults; ``defaults.fill`` adds the rest on the way out.
"""

import re

TYPE = "type.googleapis.com/"


def _msg(name, **fields):
    out = {"@type": TYPE + name}
    out.update(fields)
    return out


def _bool(value=False):
    return _msg("google.protobuf.BoolValue", value=value)


_NOT_READY = ("NOT_READY", None)
_ABSENT = ("NO_SUCH_PATH", None)

# GET stubs by exact path: (result code, payload)
_GET = {
    # activities are Windows "Logi profiles": none
    "/unified_profiles/activities": ("SUCCESS", _msg("logi.protocol.unified_profiles.ActivityDataMap")),
    # Logi Plugin Service: loaded, no plugins, no packages
    "/lps/plugins": ("SUCCESS", _msg("logi.protocol.lps.GetPluginsResponse")),
    "/lps/get_library_package_list": ("SUCCESS", _msg("logi.protocol.lps.GetLibraryPackageListResponse")),
    "/lps/overlay_settings": ("SUCCESS", _msg("logi.protocol.lps.GetOverlaySettings", enabled=False)),
    # offers: nothing to redeem
    "/offer/retrieve": ("SUCCESS", _msg("logi.protocol.offer_redemption.Offers")),
    "/api/offer/available": ("SUCCESS", _msg("logi.protocol.offer_redemption.Offers")),
    # accounts: nobody is logged in
    "/accounts/user_info": _NOT_READY,
    "/accounts/email_subscription": _NOT_READY,
    "/accounts/is_authenticated": ("SUCCESS", _bool(False)),
    "/accounts/config": ("SUCCESS", _msg("logi.protocol.accounts.Config")),
    # backups and migration: none
    "/backups/device/status_backup": ("SUCCESS", _msg("logi.protocol.settings_backup.BackupStatus")),
    "/backups/device/status_restore": ("SUCCESS", _msg("logi.protocol.settings_backup.RestoreStatus")),
    "/migration/reports": _NOT_READY,
    "/migration/migration_status": ("SUCCESS", _msg("logi.protocol.migration.ApplicationMigrationState")),
    # beta features and secure input: inert
    "/beta_testing/features": ("SUCCESS", _msg("logi.protocol.beta_testing.Features")),
    "/get_secure_input_app_name": _ABSENT,
}

_FLOW = re.compile(r"^/flow/[^/]+/([a-z_]+)$")
_FLOW_GET = {
    "config": _msg("logi.protocol.flow.Config", on=False),
    "device_location": _msg("logi.protocol.flow.DeviceLocation"),
    "device_peer_status": _msg("logi.protocol.flow.PeerStatus"),
}

# LogiVoice: every boolean switch reads off; the rest stays absent.
_LOGIVOICE_OFF = re.compile(r"^/logivoice/(voice_search_enabled|get_[a-z_]*enabled)$")

# SET stubs by exact path: answer success with a calm payload
_SET = {
    "/offer/revoke_all": _msg("logi.protocol.offer_redemption.RevokedOffers"),
}

# SETs the UI's boot sagas send without catching a failure (one rejected call cancels the
# whole saga tree, see FINDINGS.md). They configure Logitech services we do not run, so
# the UI's own payload is echoed back.
_SET_ECHO = {
    "/scarif/event", "/scarif/settings", "/system/settings", "/updates/depot/info",
    "/lps/start_library_package_update_check", "/lps/enable_notification",
    "/lps/enable_trace_events", "/lps/enabled_in_background",
    "/lps/set_actions_ring_scale_factor", "/lps/set_image_size",
    "/event_tracing/enable", "/event_tracing/optionsplus_backend/enable",
    "/integration_manager/settings/led_sdk_enabled", "/set_feature_flag",
}
_LOGIVOICE_SET = re.compile(r"^/logivoice/set_[a-z_]+$")


def lookup(verb, path, payload=None):
    if verb == "GET":
        if path in _GET:
            return _GET[path]
        m = _FLOW.match(path)
        if m and m.group(1) in _FLOW_GET:
            return "SUCCESS", _FLOW_GET[m.group(1)]
        if _LOGIVOICE_OFF.match(path):
            return "SUCCESS", _bool(False)
        return None
    if verb == "SET":
        if path in _SET:
            return "SUCCESS", _SET[path]
        if path in _SET_ECHO or _LOGIVOICE_SET.match(path):
            return "SUCCESS", payload
    return None
