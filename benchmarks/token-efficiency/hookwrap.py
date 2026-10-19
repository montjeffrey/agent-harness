"""Run an agent-harness Claude hook with an isolated harness home.

usage: python hookwrap.py <harness-home> <hook-script.py>

The harness keeps machine-level memory, its project registry, and work-session
markers under Path.home()/.agent-harness. Each benchmark run gets its own home
so runs cannot leak memory into each other or into the operator's real store.
Claude itself must keep the real home (auth, config), so the override is
applied here, in the hook's own process only.
"""

import os
import runpy
import sys

home, script = sys.argv[1], sys.argv[2]
os.environ["USERPROFILE"] = home
os.environ["HOME"] = home
sys.argv = [script]
sys.path.insert(0, os.path.dirname(script))
runpy.run_path(script, run_name="__main__")
