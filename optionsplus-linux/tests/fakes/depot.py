"""Builds a synthetic Logitech-shaped depot tree containing only invented values."""

import json
import os

DEPOT = "test_mouse_x1"
MODEL_ID = "2test-x1"
SLOT_PREFIX = "046d_4242"
DISPLAY_NAME = "Test Mouse X1"
THUMB_BYTES = b"not-a-real-image"


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb" if isinstance(data, bytes) else "w") as fh:
        fh.write(data)


def build_depot(root):
    """Create the tree under `root`; returns it (the OPL_DEPOT_DIR)."""
    main = os.path.join(root, "logioptionsplus")
    _write(os.path.join(main, "data", "devices", "devices_initial.json"), json.dumps({"devices": [{
        "modelId": MODEL_ID, "depot": DEPOT, "type": "MOUSE", "displayName": DISPLAY_NAME,
        "extendedDisplayName": DISPLAY_NAME + " Wireless", "slotPrefix": SLOT_PREFIX,
        "thumbnail": "pipeline://%s/thumb.png" % DEPOT,
        "modes": [{"interfaces": [{"id": "046d_4242"}]}],
    }]}))
    _write(os.path.join(main, "data", "strings", "en-US.yaml"),
           'HELLO_KEY: "Hello"\nBYE_KEY: Bye\n')
    dev = os.path.join(root, DEPOT)
    _write(os.path.join(dev, "thumb.png"), THUMB_BYTES)
    _write(os.path.join(dev, "layout.json"), json.dumps({"buttons": []}))
    _write(os.path.join(dev, "manifest.json"), json.dumps({"devices": [{
        "modelId": MODEL_ID, "resources": [
            {"key": "thumbnail", "src": "thumb.png"},
            {"key": "layout", "src": "layout.json"}]}]}))
    _write(os.path.join(dev, "default_configurations.json"), json.dumps({"defaultConfigurations": [{
        "settings": {"control_id_mappings": {"82": {"id": "card-back"}, "83": {"id": "card-forward"}},
                     "mouseThumbWheelAction": {"id": "card-wheel"},
                     "mouseSettings": {"pointerSpeed": {"active": {"value": 0.5}}}}}]}))
    return root
