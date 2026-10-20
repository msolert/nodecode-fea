"""A simulated Abaqus/CAE session (tests only): runs the kernel start-up script, loads the
plug-ins from ./abaqus_plugins like CAE does, then runs a timer event loop until terminated.
"""
import glob
import os
import sys
import time

import abaqusGui

args = sys.argv[1:]
script = next((a.split("=", 1)[1] for a in args if a.startswith("script=")), None)
if script is None and "script" in args[:-1]:  # Windows cmd splits "script=boot.py" at the "="
    script = args[args.index("script") + 1]
sys.path.insert(0, os.getcwd())
if script:
    exec(compile(open(script, "rb").read(), script, "exec"), abaqusGui.KERNEL)
for plugin in sorted(glob.glob(os.path.join("abaqus_plugins", "*", "*_plugin.py"))):
    namespace = {"__file__": os.path.abspath(plugin), "__name__": "plugin"}
    exec(compile(open(plugin, "rb").read(), plugin, "exec"), namespace)
print("fake CAE ready; messages: %s" % abaqusGui.MESSAGES)
sys.stdout.flush()
end = time.time() + float(os.environ.get("FAKE_CAE_SECONDS", "120"))
while time.time() < end:
    time.sleep(0.1)
    abaqusGui.run_timers()
