"""Static boot-phase responses for the native agent.

The Options+ frontend issues a fixed burst of GET/SET/SUBSCRIBE requests at
startup before it will render. Each payload here is the minimal valid
proto-JSON (an ``Any`` with its ``@type`` plus only the fields the UI reads)
for the corresponding route, with functional defaults chosen for a native
Linux install: all app permissions granted, English UI, system theme, privacy
consent left DISABLED (nothing is sent anywhere), and the plugin endpoint
reported LOADED so the UI does not wait on a depot download.

Field names and message types are taken from the recovered protocol schema
(``re/proto``); the values are defaults, not copied vendor configuration.
Routes that the real agent answers with an error (so the UI falls back to a
built-in default) are listed in ERRORS with the exact Result.Code.
"""

import os
TYPE = "type.googleapis.com/"


def _bool(v=True):
    return {"value": v}


# GET/SET routes answered SUCCESS with a static payload. Keyed by path.
BOOT = {
    "/permissions": {
        "@type": TYPE + "logi.protocol.app_permissions.Permissions",
        "analytics": _bool(), "flow": _bool(), "sso": _bool(), "update": _bool(),
        "backlight": _bool(), "logivoice": _bool(), "dfu": _bool(),
        "aipromptbuilder": _bool(), "deviceRecommendation": _bool(),
        "smartactions": _bool(), "actionsRing": _bool(),
    },
    "/configuration": {
        "@type": TYPE + "logi.protocol.application.Configuration",
        "autolaunch": _bool(True), "language": {"value": "en-US"},
        "onboardingShown": _bool(True), "migrationShown": _bool(True),
        "seenCoachMarks": [], "brand": {"value": "Logitech"},
        "notificationsEnabled": _bool(True), "settingsLoadedFromBackup": _bool(False),
        "keyValue": [], "lowBatteryNotificationsEnabled": _bool(True),
        "lockNotificationsEnabled": _bool(True), "useSystemLanguage": _bool(True),
        "theme": {"value": "LIGHT_MODE"}, "useSystemTheme": _bool(True),
        "micMuteOverlayEnabled": _bool(True), "automaticBackupEnabled": _bool(False),
        "macrosOnboardingShown": _bool(True), "macrosFeatureEnabled": _bool(True),
    },
    "/system/info": {"@type": TYPE + "logi.protocol.api.system_info"},
    "/system/settings": {"@type": TYPE + "logi.protocol.system_settings.SystemSettings"},
    "/pipl/status": {
        "@type": TYPE + "logi.protocol.pipl.ConsentStatus", "status": "DISABLED",
        "details": {"eula": False, "piiProcess": False, "piiTransfer": False, "piiShare": False},
    },
    "/lps/endpoint/info": {"@type": TYPE + "logi.protocol.lps.Endpoint.Information", "state": "LOADED"},
    "/lps/enable_notification": {"@type": TYPE + "google.protobuf.BoolValue", "value": False},
    "/lps/status": {"@type": TYPE + "logi.protocol.lps.ServiceStatus", "status": 1, "uptime": 0, "version": ""},
    # per-device app profiles: none until app integration exists; the device
    # resource saga fails (and shows "Download failed") if these error
    "/applications/predefined": {"@type": TYPE + "logi.protocol.applications.Applications", "applications": []},
    "/applications/predefined/installed": {"@type": TYPE + "logi.protocol.applications.Applications",
                                           "applications": []},
    # Telemetry stays off; userPrompted=True skips the analytics opt-in page.
    "/crash_reporting/status": {"@type": TYPE + "logi.protocol.crash_reporting.Status",
                                "enabled": False, "userPrompted": True},
    "/scarif/status": {"@type": TYPE + "logi.protocol.scarif.Status", "enabled": False, "userPrompted": True},
    "/scarif/info": {"@type": TYPE + "logi.protocol.scarif.Info"},
    # OPL_UI_LOGGING=1 keeps the renderer's console logging on (debugging aid)
    "/logging/optionsplus_backend/status": {"@type": TYPE + "logi.protocol.logging.Status",
                                            "enabledByCfg": os.environ.get("OPL_UI_LOGGING") == "1"},
    "/mouse/global/swap": {"@type": TYPE + "logi.protocol.mouse.MouseButtonSwap", "state": "LEFT"},
    "/rightsight/status": {"@type": TYPE + "logi.protocol.right_sight.Status"},
    "/dfu/config/update": {"@type": TYPE + "logi.protocol.dfu.UpdateCheckConfig"},
    "/api/v1/integrations": {"@type": TYPE + "logi.protocol.integrations.Integrations"},
    "/integration/plugin/photoshop/uxp_plugin_exists": {"@type": TYPE + "google.protobuf.BoolValue", "value": False},
    "/options/devices/list": {"@type": TYPE + "logi.protocol.devices_support.OptionsDevices"},
    "/logioptions/info": {"@type": TYPE + "logi.protocol.logioptions.LogiOptionsInfo"},
    "/backups/device/list": {"@type": TYPE + "logi.protocol.settings_backup.BackupListResult"},
    "/star_rating/notification_pending": {"@type": TYPE + "google.protobuf.BoolValue", "value": False},
    "/device_recommendation_enabled": {"@type": TYPE + "logi.protocol.util.BoolValue", "value": False},
    "/accounts/cn_idaas_status": {"@type": TYPE + "logi.protocol.accounts.CnIdaasStatus"},
    "/accounts/email_subscription/is_opted_in": {"@type": TYPE + "google.protobuf.BoolValue", "value": False},
    "/devices/support/info": {"@type": TYPE + "logi.protocol.devices_support.Device.Support"},
    "/webcams/in_configuration_devices": {"@type": TYPE + "logi.protocol.devices.InConfigurationDevices"},
    "/rac": {"@type": TYPE + "logi.protocol.rap.RACContainer"},
    "/macros/device/predefined_ids": {"@type": TYPE + "logi.protocol.rap.Data"},
    "/macros/custom_categories/storage": {"@type": TYPE + "logi.protocol.macros_categories.CustomCategories"},
    "/macro_assignments/all": {"@type": TYPE + "logi.protocol.profiles_v2.MacroAssignmentValCollection"},
    "/macros/presets": {"@type": TYPE + "logi.protocol.rap.CardList"},
    "/macros/predefined": {"@type": TYPE + "logi.protocol.profiles_v2.MacroInfosCollection"},
    # Depot-backed resource catalogues. Empty but well-formed until a
    # Logitech-fetched depot is present (see depots.py).
    "/resources/list_depot_resources": {"@type": TYPE + "logi.protocol.devices.Resources", "resources": []},
    "/updates/status": {"@type": TYPE + "logi.protocol.updates.Status"},
    "/lps/service_state": {"@type": TYPE + "logi.protocol.lps.GetServiceStateResponse"},
}

# Routes the real agent rejects; the UI treats the code as "feature absent".
ERRORS = {
}
