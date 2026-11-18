import contextlib
import os
import shutil
import sys
import tempfile
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from isshoni import metrics, prices, transcript  # noqa: E402

PRICES_PATH = os.path.join(HERE, "fixtures", "prices.toml")
PRICES = prices.load(PRICES_PATH)
PROJECTS = os.path.join(HERE, "fixtures", "projects")
AGENTS = os.path.join(HERE, "fixtures", "agents")
SESSION = os.path.join(PROJECTS, "-fixture-proj", "fixture-0001.jsonl")


def load(threshold=0.5):
    s = transcript.load_session(SESSION, PRICES)
    metrics.analyze(s, AGENTS, threshold)
    return s


def by_desc(s, desc):
    return next(d for d in s.dispatches if d.description == desc)


@contextlib.contextmanager
def user_prices(text=None):
    """An empty config home for the block; yields the path of the user price file, written with `text` when given."""
    home = os.path.realpath(tempfile.mkdtemp())
    try:
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": home}):
            path = prices.user_path()
            if text is not None:
                os.makedirs(os.path.dirname(path))
                with open(path, "w") as f:
                    f.write(text)
            yield path
    finally:
        shutil.rmtree(home, True)
