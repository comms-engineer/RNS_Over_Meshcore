# RNS_Over_Meshtastic

IGNORE THIS FOR NOW - IT'S ACTIVELY IN WORK AND NON-FUNCTIONAL

Interface for RNS using MeshCore as the underlying networking layer to utilize existing MeshCore networks.

- This is a fork of Land and Air's RNS_Over_Meshtastic project (https://github.com/landandair/RNS_Over_Meshtastic)
- No idea what speeds will be possible with this
- Need to look into options for defining multiple cooperative MeshCore endpoints also running this RNS interface
- This would allow RNS users to ride existing MeshCore buildouts, such as PugetMesh
- At some point when this is functional, I'll look into adding BLE connectivity back in.

## Usage
- Install MeshCore Python Library (pip install meshcore) (use --break-system-packages for RPi if you don't want to fuss with venv)
- Add the file [MeshCore_Interface.py](Interface%2FMeshCore_Interface.py) to your interfaces folder for reticulum
- Modify the node config file and add the following
```
 [[MeshCore Interface]]
  type = MeshCore_Interface
  enabled = true
  mode = gateway
  port = /dev/[path to device]  # Optional: Meshtastic serial device port
  speed = 115200
```

- Radio settings should be able to be defined in the config entry, but I haven't tried it.
```
