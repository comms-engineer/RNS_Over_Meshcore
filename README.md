# RNS_Over_Meshtastic

This interface is now minimally functional. More to follow.

- To Do:
  Disable broadcast sending - don't want to flood a meshcore network if you don't know who your endpoint is
  User instructions
  <stuff I can't think of right now>

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
  port = /dev/[path to device]
  speed = 115200
```

Yes, I absolutely had help from ChatGPT and Copilot on this. I'm not a software person, I'm just dumb enough to think I can beat my head against something until it works. PLEASE feel free to offer improvements and corrections.
