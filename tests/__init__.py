import os

# The suite never reads or writes the real session cache; cache tests point this at a temp dir.
os.environ["ISSHONI_CACHE_DIR"] = ""
