import atexit
import os
import shutil
import tempfile

# The suite never reads or writes the real session cache; cache tests point this at a temp dir.
os.environ["ISSHONI_CACHE_DIR"] = ""

# Nor the real factory home (settings.toml decides which model a `factory:<role>` agent should have).
_factory_home = tempfile.mkdtemp(prefix="factory-home-")
atexit.register(shutil.rmtree, _factory_home, True)
os.environ["FACTORY_HOME"] = _factory_home
