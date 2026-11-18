import os
import tempfile
import unittest
from unittest import mock

from tests.support import PRICES, PRICES_PATH, user_prices
from isshoni import prices
from isshoni.transcript import SessionError

# Fixture rates: opus 5/10/15(5m)/20(1h)/0.5; haiku 1/2/3/4/0.1, over 100 tokens 10/20/30/40/1.
OPUS = "claude-opus-5-5"
HAIKU = "claude-haiku-5-5"


def usage(i=0, o=0, cw=0, cr=0, h1=None):
    u = {"input_tokens": i, "output_tokens": o, "cache_creation_input_tokens": cw, "cache_read_input_tokens": cr}
    if h1 is not None:
        u["cache_creation"] = {"ephemeral_1h_input_tokens": h1, "ephemeral_5m_input_tokens": cw - h1}
    return u


class CostTests(unittest.TestCase):
    def test_default_file_prices_the_corpus_models(self):
        p = prices.load()
        for m, per_m in (("claude-fable-5-2", 8), (OPUS, 4), ("claude-sonnet-5-5", 2), (HAIKU, 0.1)):
            self.assertAlmostEqual(p.cost(m, usage(i=100000)), per_m / 10, msg=m)  # haiku: at the long-prompt limit
        self.assertAlmostEqual(p.cost(HAIKU, usage(i=100001)), 0.5 * 100001 / 1e6)
        self.assertEqual((p.currency, p.as_of), ("USD", "2026-11-14"))

    def test_default_file_all_five_classes(self):
        # 1000 in, 2000 out, 3000 cache write (1000 of them 1h), 4000 cache read; hand-computed from the spec table
        p, u = prices.load(), usage(i=1000, o=2000, cw=3000, cr=4000, h1=1000)
        for m, want in (("claude-fable-5-2", 0.1248), (OPUS, 0.0628), ("claude-sonnet-5-5", 0.0314), (HAIKU, 0.00159)):
            self.assertAlmostEqual(p.cost(m, u), want, msg=m)
        long_u = usage(i=100000, o=1000, cw=2000, cr=1, h1=1000)  # prompt 102,001: all five long-prompt rates
        self.assertAlmostEqual(p.cost(HAIKU, long_u), 0.05412505)

    def test_cost_divides_the_summed_products_once(self):
        one = dict.fromkeys(prices.RATES, 1)
        p = prices.Prices("p", {"currency": "USD", "as_of": "x", "source": "x", "models": {"m": one}})
        u = usage(i=225, o=49, cw=945, cr=17, h1=465)  # per-class division would give 0.0012360000000000001
        self.assertEqual(p.cost("m", u), 0.001236)
        self.assertAlmostEqual(sum(p.parts("m", u).values()), 0.001236)

    def test_plain(self):
        self.assertAlmostEqual(PRICES.cost(OPUS, usage(i=1000, o=100, cr=10)), (5000 + 1000 + 5) / 1e6)

    def test_cache_write_split_and_no_breakdown(self):
        self.assertAlmostEqual(PRICES.cost(OPUS, usage(cw=100, h1=40)), (60 * 15 + 40 * 20) / 1e6)
        self.assertAlmostEqual(PRICES.cost(OPUS, usage(cw=100)), 100 * 15 / 1e6)  # no breakdown: all 5m
        self.assertAlmostEqual(PRICES.cost(OPUS, usage(cw=100, h1=40) | {"cache_creation_input_tokens": 30}),
                               30 * 20 / 1e6)  # 1h never exceeds the total written

    def test_haiku_long_prompt_boundary(self):
        self.assertAlmostEqual(PRICES.cost(HAIKU, usage(i=60, cr=40)), (60 * 1 + 40 * 0.1) / 1e6)  # 100: standard
        self.assertAlmostEqual(PRICES.cost(HAIKU, usage(i=61, cr=40)), (61 * 10 + 40 * 1) / 1e6)  # 101: long

    def test_model_matching(self):
        self.assertEqual(PRICES.cost(OPUS + "-20260101", usage(i=1)), PRICES.cost(OPUS, usage(i=1)))
        for m in ("claude-opus-5", OPUS + "-x", OPUS + "-2026", "unknown", "claude-sonnet-5-5"):
            self.assertIsNone(PRICES.cost(m, usage(i=1)), m)


class LoadErrorTests(unittest.TestCase):
    GOOD = ('currency = "USD"\nas_of = "2026-01-01"\nsource = "s"\n[models.m]\n'
            'input = 1\noutput = 1\ncache_write_5m = 1\ncache_write_1h = 1\ncache_read = 1\n')

    def fails(self, text, *words):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "p.toml")
            with open(path, "w") as f:
                f.write(text)
            with self.assertRaises(SessionError) as cm:
                prices.load(path)
        self.assertIn(path, str(cm.exception))
        for w in words:
            self.assertIn(w, str(cm.exception))

    def test_good_file_loads(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "p.toml")
            with open(path, "w") as f:
                f.write(self.GOOD)
            self.assertEqual(prices.load(path).to_dict()["path"], path)

    def test_not_utf8(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "p.toml")
            with open(path, "wb") as f:
                f.write(b"currency = \xff\xfe")
            with self.assertRaises(SessionError) as cm:
                prices.load(path)
        self.assertIn(path, str(cm.exception))

    def test_missing_file(self):
        with self.assertRaises(SessionError) as cm:
            prices.load("/nonexistent/prices.toml")
        self.assertIn("/nonexistent/prices.toml", str(cm.exception))

    def test_invalid_toml(self):
        self.fails("currency = ", "invalid TOML")

    def test_header_fields(self):
        self.fails(self.GOOD.replace('currency = "USD"\n', ""), "currency")
        self.fails(self.GOOD.replace('as_of = "2026-01-01"', 'as_of = ""'), "as_of")

    def test_model_rates(self):
        self.fails(self.GOOD.replace("cache_read = 1\n", ""), "cache_read")
        self.fails(self.GOOD.replace("output = 1", "output = -1"), "output")
        self.fails(self.GOOD.replace("input = 1", 'input = "1"'), "input")

    def test_long_prompt(self):
        rates = self.GOOD.split("[models.m]\n")[1]
        self.fails(self.GOOD + "[models.m.long_prompt]\n" + rates, "long_prompt", "over")
        self.fails(self.GOOD + "[models.m.long_prompt]\nover = 5\n" + rates.replace("output = 1\n", ""),
                   "long_prompt", "output")


CUSTOM = "vendor/model-x"
RATES5 = "input = 1\noutput = 2\ncache_write_5m = 3\ncache_write_1h = 4\ncache_read = 5\n"


class UserFileTests(unittest.TestCase):
    def load(self, text):
        with user_prices(text):
            return prices.load()

    def fails(self, text, *needles):
        with user_prices(text) as path:
            with self.assertRaises(SessionError) as c:
                prices.load()
        for n in (path,) + needles:
            self.assertIn(n, str(c.exception))

    def test_default_path_follows_xdg_then_home(self):
        with user_prices() as path:
            self.assertEqual(path.split(os.sep)[-2:], ["isshoni", "prices.toml"])
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": ""}):
            self.assertEqual(prices.user_path(), os.path.expanduser("~/.config/isshoni/prices.toml"))

    def test_no_user_file_is_the_builtin_alone(self):
        with user_prices():
            p = prices.load()
        self.assertEqual((p.path, p.user), (prices.DEFAULT_PATH, None))
        self.assertNotIn("user_path", p.to_dict())

    def test_overlay_adds_and_replaces(self):
        p = self.load('[models."%s"]\n%s[models.%s]\n%s' % (CUSTOM, RATES5, OPUS, RATES5.replace("= 1", "= 7")))
        self.assertAlmostEqual(p.cost(CUSTOM, usage(i=1000000)), 1)
        self.assertAlmostEqual(p.cost(OPUS, usage(i=1000000)), 7)
        self.assertAlmostEqual(p.cost("claude-sonnet-5-5", usage(i=1000000)), 2)  # untouched built-in
        self.assertIsNone(p.cost("vendor/other", usage(i=1)))
        self.assertEqual((p.path, p.to_dict()["user_path"] == p.user), (prices.DEFAULT_PATH, True))
        self.assertEqual((p.currency, p.as_of), ("USD", "2026-11-14"))

    def test_empty_user_file_is_named_but_changes_nothing(self):
        with user_prices("") as path:
            self.assertEqual(prices.load().user, path)

    def test_prices_flag_reads_its_file_alone(self):
        with user_prices('[models."%s"]\n%s' % (CUSTOM, RATES5)):
            p = prices.load(PRICES_PATH)
        self.assertIsNone(p.cost(CUSTOM, usage(i=1)))
        self.assertIsNone(p.user)

    def test_like_takes_the_whole_table(self):
        p = self.load('[models."%s"]\nlike = "%s"\n[models.alias]\nlike = "%s"\n' % (CUSTOM, HAIKU, OPUS))
        for m in (CUSTOM, "alias"):
            self.assertEqual(p.models[m], p.models[HAIKU if m == CUSTOM else OPUS])
        self.assertAlmostEqual(p.cost(CUSTOM, usage(i=100001)), 0.5 * 100001 / 1e6)  # the built-in long_prompt came along
        self.assertAlmostEqual(p.cost(CUSTOM + "-20260101", usage(i=1)), 1e-7)  # a dated id still matches minus its date

    def test_like_follows_a_replaced_model(self):
        p = self.load('[models.alias]\nlike = "%s"\n[models.%s]\n%s' % (OPUS, OPUS, RATES5))
        self.assertEqual(p.models["alias"], p.models[OPUS])
        self.assertEqual(p.models["alias"]["cache_read"], 5)

    def test_like_refusals(self):
        self.fails('[models.a]\nlike = "nothing"\n', "[models.a]", "nothing")
        self.fails('[models.a]\nlike = "b"\n[models.b]\nlike = "%s"\n' % OPUS, "[models.a]", "'b'")  # chained
        self.fails('[models.a]\nlike = "a"\n', "[models.a]")
        self.fails('[models.a]\nlike = "%s"\ninput = 1\n' % OPUS, "[models.a]", "like")  # mixed with rates

    def test_invalid_entries_name_the_user_file(self):
        self.fails("[models.m]\ninput = 1\n", "[models.m]", "output")
        self.fails("[models.m]\n" + RATES5.replace("= 1", "= -1"), "[models.m]", "input")
        self.fails("[models.m]\n" + RATES5.replace("= 1", "= nan"), "[models.m]", "input")
        self.fails('currency = "EUR"\n' + "[models.m]\n" + RATES5, "currency")
        self.fails("[models.m\n", "invalid TOML")

    def test_like_is_valid_in_any_price_file(self):
        p = prices.Prices("p", {"currency": "USD", "as_of": "x", "source": "x",
                                "models": {"a": dict.fromkeys(prices.RATES, 1), "b": {"like": "a"}}})
        self.assertEqual(p.models["b"], p.models["a"])


class SetPriceTests(unittest.TestCase):
    def test_round_trip_with_rates_and_an_id_needing_quotes(self):
        weird = 'v"endor\\/m x.y'
        with user_prices() as path:
            prices.set_price(CUSTOM, dict(zip(prices.RATES, (1, 2.5, 3, 4, 0.5))))
            prices.set_price(weird, {"like": OPUS})
            p = prices.load()
            self.assertAlmostEqual(p.cost(CUSTOM, usage(i=1000000, o=1000000)), 3.5)
            self.assertEqual(p.models[weird], p.models[OPUS])
            with open(path) as f:
                self.assertIn("isshoni price", f.readline())
            self.assertEqual(os.listdir(os.path.dirname(path)), ["prices.toml"])

    def test_keeps_other_entries_and_long_prompt(self):
        with user_prices('[models."%s"]\n%s[models."%s".long_prompt]\nover = 9\n%s' % (CUSTOM, RATES5, CUSTOM, RATES5)):
            prices.set_price("other", {"like": CUSTOM})
            self.assertEqual(prices.load().models["other"]["long_prompt"]["over"], 9)

    def test_refusals_leave_the_file_untouched(self):
        text = "[models.a]\n" + RATES5
        with user_prices(text) as path:
            for model, table in (("m", {"like": "nothing"}), ("m", {"like": "a", "input": 1}), ("m\nx", {"like": "a"}),
                                 ("", {"like": "a"}), ("m", dict(zip(prices.RATES, (1, 1, 1, 1, -1)))),
                                 ("a", {"like": "a"})):
                with self.assertRaises(SessionError):
                    prices.set_price(model, table)
                with open(path) as f:
                    self.assertEqual(f.read(), text)
            self.assertEqual(os.listdir(os.path.dirname(path)), ["prices.toml"])

    def test_a_key_toml_cannot_hold_is_refused_and_the_file_kept(self):
        text = '[models."a\\u007fb"]\n' + RATES5
        with user_prices(text) as path:
            with self.assertRaises(SessionError):
                prices.set_price("m", {"like": OPUS})
            with open(path) as f:
                self.assertEqual(f.read(), text)
            self.assertEqual(os.listdir(os.path.dirname(path)), ["prices.toml"])

    def test_a_broken_user_file_is_not_overwritten(self):
        with user_prices("[models.a\n") as path:
            with self.assertRaises(SessionError):
                prices.set_price("m", {"like": OPUS})
            with open(path) as f:
                self.assertEqual(f.read(), "[models.a\n")


if __name__ == "__main__":
    unittest.main()
