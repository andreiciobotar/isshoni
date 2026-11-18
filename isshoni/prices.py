"""The price file and the cost of one message: the one owner of prices."""
from __future__ import annotations

import json
import math
import os
import re
import tempfile
import tomllib

from .transcript import SessionError, _int, usage_from

DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prices.toml")
RATES = ("input", "output", "cache_write_5m", "cache_write_1h", "cache_read")
_DATED = re.compile(r"-\d{8}$")
USER_HEADER = "# Written by `isshoni price`, which rewrites this whole file: comments added by hand are not kept.\n"


def user_path():
    """The per-user overlay: $XDG_CONFIG_HOME/isshoni/prices.toml, else ~/.config/isshoni/prices.toml."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "isshoni", "prices.toml")


def _rates(table, where, path, extra=()):
    if not isinstance(table, dict):
        raise SessionError("prices file %s: %s must be a table" % (path, where))
    for k in RATES + extra:
        v = table.get(k)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
            raise SessionError("prices file %s: %s needs %r as a non-negative number" % (path, where, k))


def _resolve(models, where):
    """The tables with every `like = "<id>"` replaced by the whole table of that id; invalid entries raise."""
    out = {}
    for m, t in models.items():
        if isinstance(t, dict) and "like" in t:
            continue
        _rates(t, "[models.%s]" % m, where)
        if "long_prompt" in t:
            _rates(t["long_prompt"], "[models.%s.long_prompt]" % m, where, extra=("over",))
        out[m] = t
    for m, t in models.items():
        if m in out:
            continue
        target = t["like"]
        if set(t) != {"like"} or not isinstance(target, str):
            raise SessionError('prices file %s: [models.%s] is either the five rates or only like = "<id>"' % (where, m))
        if target not in out:
            raise SessionError("prices file %s: [models.%s] like %r names no model with rates" % (where, m, target))
        out[m] = out[target]
    return out


class Prices:
    def __init__(self, path, data, user=None):
        """`user` is the overlay file `data` already holds, so errors name it; `path` stays the base file."""
        self.path, self.user = path, user
        where = user or path
        for k in ("currency", "as_of", "source"):
            v = data.get(k)
            if not isinstance(v, str) or not v:
                raise SessionError("prices file %s: %r must be a non-empty string" % (path, k))
            setattr(self, k, v)
        models = data.get("models")
        if not isinstance(models, dict) or not models:
            raise SessionError("prices file %s: no [models.<id>] tables" % path)
        self.models = _resolve(models, where)

    def to_dict(self):
        d = {"currency": self.currency, "as_of": self.as_of, "source": self.source, "path": self.path}
        if self.user:
            d["user_path"] = self.user
        return d

    def _products(self, model, raw_usage):
        """Tokens times rate per class in the order the cost adds them, or None when the model has no price."""
        t = self.models.get(model) or self.models.get(_DATED.sub("", model))
        if t is None:
            return None
        u = usage_from(raw_usage)
        long_prompt = t.get("long_prompt")
        if long_prompt and u.input + u.cache_write + u.cache_read > long_prompt["over"]:
            t = long_prompt
        write_5m, write_1h = write_tiers(raw_usage)
        n = {"input": u.input, "output": u.output, "cache_read": u.cache_read,
             "cache_write_5m": write_5m, "cache_write_1h": write_1h}
        return {k: v * t[k] for k, v in n.items()}

    def parts(self, model, raw_usage):
        """Cost of one message by token class, or None when the model has no price."""
        p = self._products(model, raw_usage)
        return None if p is None else {k: p[k] / 1e6 for k in RATES}

    def cost(self, model, raw_usage):
        """Cost of one message in the file's currency, or None when the model has no price."""
        p = self._products(model, raw_usage)
        return None if p is None else sum(p.values()) / 1e6


def write_tiers(raw_usage):
    """Cache-write tokens of a message as (five-minute, one-hour)."""
    u = usage_from(raw_usage)
    tiers = raw_usage.get("cache_creation") if isinstance(raw_usage, dict) else None
    write_1h = min(_int(tiers.get("ephemeral_1h_input_tokens")) if isinstance(tiers, dict) else 0, u.cache_write)
    return u.cache_write - write_1h, write_1h


def _toml(path):
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except OSError as e:
        raise SessionError("prices file %s: %s" % (path, e.strerror or e))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise SessionError("prices file %s: invalid TOML (%s)" % (path, e))


def _user_models(path):
    """The [models] tables of the user file, {} when it does not exist; anything else in it is an error."""
    if not os.path.exists(path):
        return {}
    data = _toml(path)
    extra = sorted(set(data) - {"models"})
    if extra:
        raise SessionError("prices file %s: only [models.<id>] tables are allowed here, not %s" % (path, ", ".join(extra)))
    models = data.get("models", {})
    if not isinstance(models, dict):
        raise SessionError("prices file %s: 'models' must hold [models.<id>] tables" % path)
    return models


def _overlay(user, models):
    """The built-in file with the user's tables over it, validated as a whole (errors name the user file)."""
    data = _toml(DEFAULT_PATH)
    data["models"] = {**data["models"], **models}
    return Prices(DEFAULT_PATH, data, user)


def load(path=None):
    """The prices of `--prices PATH` alone, else the built-in file under the user's file when it exists."""
    if path:
        path = os.path.expanduser(path)
        return Prices(path, _toml(path))
    user = user_path()
    if not os.path.exists(user):
        return Prices(DEFAULT_PATH, _toml(DEFAULT_PATH))
    return _overlay(user, _user_models(user))


def _dump(models):
    lines = [USER_HEADER]
    for m, t in models.items():
        key = json.dumps(m, ensure_ascii=False)
        lines.append("\n[models.%s]\n" % key)
        if "like" in t:
            lines.append("like = %s\n" % json.dumps(t["like"], ensure_ascii=False))
            continue
        lines += ["%s = %s\n" % (k, repr(t[k])) for k in RATES]
        if "long_prompt" in t:
            lines.append("\n[models.%s.long_prompt]\n" % key)
            lines += ["%s = %s\n" % (k, repr(t["long_prompt"][k])) for k in ("over",) + RATES]
    return "".join(lines)


def set_price(model, table):
    """Set one table of the user file (rates, or {"like": id}), validate the merged prices, write the file; the new Prices."""
    if not model or any(ord(c) < 32 or ord(c) == 127 for c in model):
        raise SessionError("model id %r is empty or holds a control character" % model)
    user = user_path()
    models = {**_user_models(user), model: table}
    prices = _overlay(user, models)
    os.makedirs(os.path.dirname(user), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(user), suffix=".tmp")
    try:
        text = _dump(models)
        try:
            written = tomllib.loads(text)["models"]
        except tomllib.TOMLDecodeError:  # a key json.dumps leaves raw (DEL) is invalid TOML
            written = None
        if written != models:
            raise SessionError("the user file cannot hold these model ids %s as TOML" % sorted(models))
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, user)
    except BaseException:
        os.unlink(tmp)
        raise
    return prices
