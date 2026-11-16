import base64
import html
import io
import json
import os
import re
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from test_factory import ROOT, FakeRun, factory, repo_answers

import factory_setup

real_run = factory.run
TOKEN = "ghs_THROWAWAY_TOKEN"
INSTALL_URL = "https://github.com/settings/installations/7654321"
APP = {"id": 123456, "slug": "factory-octocat", "client_id": "Iv23abc", "html_url": "https://github.com/apps/factory-octocat",
       "owner": {"login": "octocat", "type": "User"}, "webhook_secret": "whsec-1", "client_secret": "csec-1"}
INSTALLATION = {"id": 7654321, "html_url": INSTALL_URL, "account": {"login": "Octocat"}, "repository_selection": "selected",
                "permissions": {"contents": "write", "pull_requests": "write", "checks": "read", "statuses": "read",
                                "actions": "write", "metadata": "read"}}
BOT = "factory-octocat%5Bbot%5D"


def openssl(*args):
    subprocess.run(["openssl", *args], check=True, capture_output=True)


def unb64(part):
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


class FakeGitHub:
    """Stands in for factory.github_api: routes are (method, path without query) -> (status, body) or a function of the call."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, method, path, bearer=None, body=None):
        self.calls.append((method, path, bearer))
        answer = self.routes.get((method, path.partition("?")[0]), (404, {"message": "unrouted"}))
        return answer(path) if callable(answer) else answer

    def called(self, method, path):
        return any(c[:2] == (method, path) for c in self.calls)


def routes(pem, over=None):
    return {("GET", "/users/octocat"): (200, {"type": "User"}),
            ("POST", "/app-manifests/THECODE/conversions"): (201, {**APP, "pem": pem}),
            ("GET", "/app"): (200, {"slug": "factory-octocat", "html_url": APP["html_url"]}),
            ("GET", "/app/installations"): (200, [INSTALLATION]),
            ("POST", "/app/installations/7654321/access_tokens"): (201, {"token": TOKEN, "expires_at": "2030-01-01T00:00:00Z"}),
            ("GET", "/installation/repositories"): (200, {"total_count": 2, "repositories": [
                {"full_name": "octocat/one"}, {"full_name": "octocat/two"}]}),
            ("GET", f"/users/{BOT}"): (200, {"id": 99999999}), **(over or {})}


class Wizard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.keys.cleanup)
        cls.key = Path(cls.keys.name, "key.pem")
        openssl("genrsa", "-out", str(cls.key), "2048")
        cls.pem = cls.key.read_text()

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        patcher = mock.patch.dict(os.environ, {"HOME": tmp.name, "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1",
                                                "DISPLAY": ":0"})
        patcher.start()
        self.addCleanup(patcher.stop)
        store = self.home / ".claude" / "factory"
        for name, value in (("STORE", store), ("SETTINGS_PATH", store / "settings.toml"), ("IDENTITY", store / "identity")):
            patch = mock.patch.object(factory, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.identity = store / "identity"
        self.opened = []
        self.forms = []
        self.gh = None

    def stored(self, **over):
        """An identity written by an earlier run: the real key and an app.json as step 2e leaves it."""
        self.identity.mkdir(parents=True, mode=0o700)
        (self.identity / "key.pem").write_text(self.pem)
        os.chmod(self.identity / "key.pem", 0o600)
        data = {"app_id": 123456, "slug": "factory-octocat", "client_id": "Iv23abc", "html_url": APP["html_url"],
                "owner": "octocat", "owner_type": "User", **over}
        (self.identity / "app.json").write_text(json.dumps(data))

    def browser(self, url):
        """The user's browser: loads the form, and confirms on GitHub, which redirects to /callback (once with a bad state)."""
        self.opened.append(url)
        if not url.endswith("/"):
            return
        page = urllib.request.urlopen(url, timeout=5).read().decode()
        action = html.unescape(re.search(r'action="([^"]*)"', page).group(1))
        manifest = json.loads(html.unescape(re.search(r'name=manifest value="([^"]*)"', page).group(1)))
        self.forms.append((action, manifest))
        state = urllib.parse.parse_qs(urllib.parse.urlparse(action).query)["state"][0]
        for query in ("code=X&state=wrong", "code=X", f"state={state}"):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(f"{url}callback?{query}", timeout=5)
            self.assertEqual(caught.exception.code, 400)
        urllib.request.urlopen(f"{url}callback?code=THECODE&state={state}", timeout=5)

    def wizard(self, *args, github=None, browser=None, wait=600, login=(0, "octocat\n", "")):
        """The App step of the wizard, answering every question yes; `login` is what `gh api user` says."""
        self.gh = FakeGitHub(github if github is not None else routes(self.pem))
        out, err = io.StringIO(), io.StringIO()
        fake_gh = FakeRun({("gh", "api", "user"): login})
        with mock.patch.object(factory, "github_api", self.gh), \
                mock.patch.object(factory, "run", lambda argv, **kw: fake_gh(argv, **kw) if argv[0] == "gh" else real_run(argv, **kw)), \
                mock.patch.object(factory.webbrowser, "open", browser or self.browser), \
                mock.patch.object(factory, "WIZARD_WAIT", wait), mock.patch.object(factory, "INSTALL_POLL", 0), \
                redirect_stdout(out), redirect_stderr(err):
            try:
                factory_setup.setup_app(factory.parse_args(["wizard", "--yes", *args]), None)
                rc = 0
            except factory.Fail as e:
                print(f"factory: {e}", file=sys.stderr)
                rc = e.code
        self.out, self.err = out.getvalue(), err.getvalue()
        return rc

    def files(self):
        return sorted(p.name for p in self.identity.iterdir()) if self.identity.exists() else []

    def app_json(self):
        return json.loads((self.identity / "app.json").read_text())

    def assertQuiet(self):
        for text in (self.out, self.err):
            self.assertNotIn(TOKEN, text)
            self.assertNotIn("PRIVATE KEY", text)
            self.assertNotIn(self.pem.splitlines()[1], text)


class JwtTests(Wizard):
    def test_signed_with_the_stored_key(self):
        self.stored()
        jwt = factory.app_jwt(factory.load_identity())
        self.assertNotIn("=", jwt)
        head, body, sig = jwt.split(".")
        self.assertEqual(json.loads(unb64(head)), {"alg": "RS256", "typ": "JWT"})
        claims = json.loads(unb64(body))
        self.assertEqual(claims["iss"], "Iv23abc")
        self.assertEqual(claims["exp"] - claims["iat"], 600)
        pub, signature, data = self.home / "pub.pem", self.home / "sig", self.home / "data"
        openssl("rsa", "-in", str(self.key), "-pubout", "-out", str(pub))
        signature.write_bytes(unb64(sig))
        data.write_text(f"{head}.{body}")
        openssl("dgst", "-sha256", "-verify", str(pub), "-signature", str(signature), str(data))

    def test_openssl_failing_is_named(self):
        self.stored()
        with mock.patch.object(factory, "run", return_value=(127, "", "no such file")):
            with self.assertRaisesRegex(factory.Fail, "openssl"):
                factory.app_jwt(factory.load_identity())


class TokenTests(Wizard):
    def test_returns_token_and_expiry(self):
        self.stored(installation_id=7654321)
        gh = FakeGitHub(routes(self.pem))
        with mock.patch.object(factory, "github_api", gh):
            self.assertEqual(factory.installation_token(factory.load_identity()), (TOKEN, "2030-01-01T00:00:00Z"))
        self.assertTrue(gh.calls[0][2])

    def test_a_refusal_is_named(self):
        self.stored(installation_id=7654321)
        gh = FakeGitHub(routes(self.pem, {("POST", "/app/installations/7654321/access_tokens"): (403, {"message": "nope"})}))
        with mock.patch.object(factory, "github_api", gh):
            with self.assertRaisesRegex(factory.Fail, "HTTP 403: nope"):
                factory.installation_token(factory.load_identity())


class FirstRunTests(Wizard):
    def expected_manifest(self, owner):
        return {"name": f"isshoni-lander-{owner}", "url": f"https://github.com/{owner}", "public": False,
                "redirect_url": self.opened[0] + "callback",
                "hook_attributes": {"url": f"https://github.com/{owner}", "active": False},
                "default_events": [],
                "default_permissions": {"contents": "write", "pull_requests": "write", "checks": "read",
                                       "statuses": "read", "actions": "write"}}

    def test_user_account_end_to_end(self):
        self.assertEqual(self.wizard("--owner", "octocat"), 0, self.err)
        action, manifest = self.forms[0]
        self.assertRegex(action, r"^https://github\.com/settings/apps/new\?state=[\w-]+$")
        self.assertEqual(manifest, self.expected_manifest("octocat"))
        self.assertRegex(self.opened[0], r"^http://127\.0\.0\.1:\d+/$")
        self.assertEqual(self.files(), ["app.json", "key.pem"])
        self.assertEqual(stat.S_IMODE(self.identity.stat().st_mode), 0o700)
        for name in self.files():
            self.assertEqual(stat.S_IMODE((self.identity / name).stat().st_mode), 0o600)
        self.assertEqual((self.identity / "key.pem").read_text(), self.pem)
        self.assertEqual(self.app_json(), {
            "app_id": 123456, "slug": "factory-octocat", "client_id": "Iv23abc", "html_url": APP["html_url"],
            "owner": "octocat", "owner_type": "User", "installation_id": 7654321, "installation_url": INSTALL_URL, "repository_selection": "selected",
            "bot_login": "factory-octocat[bot]", "bot_id": 99999999,
            "bot_email": "99999999+factory-octocat[bot]@users.noreply.github.com"})
        for line in ("octocat/one", "octocat/two", "factory-octocat[bot]", str(factory.IDENTITY),
                     "To remove it: `factory uninstall`"):
            self.assertIn(line, self.out)
        self.assertNotIn("advanced", self.out)
        self.assertQuiet()
        self.assertNotIn("whsec-1", (self.identity / "app.json").read_text() + self.out)
        self.assertNotIn("csec-1", (self.identity / "app.json").read_text() + self.out)

    def test_organization_account(self):
        github = routes(self.pem, {("GET", "/users/octocat"): (200, {"type": "Organization"})})
        github[("POST", "/app-manifests/THECODE/conversions")] = (201, {**APP, "pem": self.pem,
                                                                        "owner": {"login": "octocat", "type": "Organization"}})
        self.assertEqual(self.wizard("--owner", "octocat", github=github), 0, self.err)
        self.assertRegex(self.forms[0][0], r"^https://github\.com/organizations/octocat/settings/apps/new\?state=")
        self.assertEqual(self.app_json()["owner_type"], "Organization")
        self.assertEqual(factory.delete_url(self.app_json()), "https://github.com/organizations/octocat/settings/apps/factory-octocat/advanced")

    def test_name_is_cut_to_34_characters(self):
        owner = "o" * 40
        self.wizard("--owner", owner, github={("GET", f"/users/{owner}"): (200, {"type": "User"})})
        self.assertEqual(self.forms[0][1]["name"], ("isshoni-lander-" + owner)[:34])

    def test_name_flag_overrides_the_default(self):
        self.wizard("--owner", "octocat", "--name", "my-factory")
        self.assertEqual(self.forms[0][1]["name"], "my-factory")
        self.assertIn("named my-factory", self.out)

    def test_a_name_of_34_characters_is_accepted(self):
        self.assertEqual(self.wizard("--owner", "octocat", "--name", "n" * 34), 0, self.err)

    def test_name_with_a_stored_identity_is_refused(self):
        self.stored()
        self.assertEqual(self.wizard("--name", "other"), 1)
        self.assertIn("does not rename", self.err)
        self.assertIn("GitHub settings page", self.err)
        self.assertEqual(self.gh.calls, [])
        self.assertEqual(self.opened, [])

    def test_no_answer_stores_nothing(self):
        rc = self.wizard("--owner", "octocat", browser=lambda url: None, wait=0)
        self.assertEqual(rc, 1)
        self.assertIn("nothing was stored here", self.err)
        self.assertIn("delete it at https://github.com/settings/apps before running again", self.err)
        self.assertEqual(self.files(), [])
        self.assertFalse(self.gh.called("POST", "/app-manifests/THECODE/conversions"))

    def idle_socket(self, url):
        """A browser's preconnect: a connection that sends nothing."""
        idle = socket.create_connection(("127.0.0.1", int(url.rsplit(":", 1)[1].rstrip("/"))))
        self.addCleanup(idle.close)

    def test_an_idle_connection_does_not_block_the_flow(self):
        def browser(url):
            self.idle_socket(url)
            self.browser(url)
        self.assertEqual(self.wizard("--owner", "octocat", browser=browser), 0, self.err)

    def test_an_idle_connection_does_not_block_the_timeout(self):
        start = time.monotonic()
        self.assertEqual(self.wizard("--owner", "octocat", browser=self.idle_socket, wait=1), 1)
        self.assertLess(time.monotonic() - start, 5)
        self.assertIn("nothing was stored here", self.err)

    def test_failed_exchange_says_the_app_may_exist(self):
        github = routes(self.pem, {("POST", "/app-manifests/THECODE/conversions"): (0, {"message": "offline"})})
        self.assertEqual(self.wizard("--owner", "octocat", github=github), 1)
        self.assertIn("network error: offline", self.err)
        self.assertIn("If you confirmed on GitHub, the App exists: delete it at https://github.com/settings/apps", self.err)
        self.assertEqual(self.files(), [])
        github[("GET", "/users/octocat")] = (200, {"type": "Organization"})
        github[("POST", "/app-manifests/THECODE/conversions")] = (422, {"message": "expired"})
        self.assertEqual(self.wizard("--owner", "octocat", github=github), 1)
        self.assertIn("https://github.com/organizations/octocat/settings/apps before", self.err)

    def test_key_without_app_json_stops_before_any_call(self):
        self.stored()
        (self.identity / "app.json").unlink()
        self.assertEqual(self.wizard("--owner", "octocat"), 1)
        self.assertIn("rm -r", self.err)
        self.assertEqual(self.gh.calls, [])
        self.assertEqual(self.opened, [])
        self.assertEqual((self.identity / "key.pem").read_text(), self.pem)

    def test_owner_lookup_failing_is_reported(self):
        rc = self.wizard("--owner", "octocat", github={("GET", "/users/octocat"): (0, {"message": "offline"})})
        self.assertEqual(rc, 1)
        self.assertIn("network error: offline", self.err)
        self.assertEqual(self.opened, [])

    def test_answer_without_the_slug_says_so(self):
        gone = {k: v for k, v in APP.items() if k != "slug"}
        github = routes(self.pem, {("POST", "/app-manifests/THECODE/conversions"): (201, {**gone, "pem": self.pem})})
        self.assertEqual(self.wizard("--owner", "octocat", github=github), 1)
        self.assertIn("'slug'", self.err)
        self.assertEqual(self.files(), [])


class OwnerTests(Wizard):
    def owner(self, url):
        git = repo_answers([], (), {("git", "remote", "get-url"): (0, url + "\n", "")})
        with mock.patch.object(factory, "run", FakeRun(git)):
            return factory.remote_repo()

    def test_forms(self):
        for url in ("https://github.com/octocat/repo.git", "git@github.com:octocat/repo.git", "ssh://git@github.com/octocat/repo",
                    "https://github.com/octocat/repo/", "https://github.com/octocat/repo",
                    "https://user@github.com/octocat/repo.git", "https://GitHub.com/octocat/repo"):
            self.assertEqual(self.owner(url), ("octocat", "repo"), url)
        self.assertEqual(self.owner("git@github.com:octocat/my.dotted-repo.git"), ("octocat", "my.dotted-repo"))

    def test_only_github_dot_com_is_a_host(self):
        for url in ("https://notgithub.com/octocat/repo", "https://evil.io/github.com/octocat/repo",
                    "https://gitlab.com/x/github.com/octocat/repo", "https://github.com.evil.io/octocat/repo",
                    "git@notgithub.com:octocat/repo.git", "ssh://git@gitlab.com/github.com/octocat/repo",
                    "ssh://git@github.com:2222/octocat/repo", "https://user:pw@evil.io@github.com.evil.io/octocat/repo"):
            self.assertIsNone(self.owner(url), url)

    def test_none(self):
        self.assertIsNone(self.owner("https://example.com/octocat/repo"))
        self.assertIsNone(self.owner("https://github.com/octocat"))
        with mock.patch.object(factory, "run", FakeRun({})):
            self.assertIsNone(factory.remote_repo())

    def test_no_login_asks_for_the_flag(self):
        self.assertEqual(self.wizard(login=(1, "", "not logged in")), 1)
        self.assertIn("--owner", self.err)
        self.assertEqual(self.opened, [])

    def test_your_login_is_the_owner_without_the_flag(self):
        self.assertEqual(self.wizard(), 0, self.err)
        self.assertEqual(self.app_json()["owner"], "octocat")


class RerunTests(Wizard):
    def test_installed_identity_is_verified_and_no_app_is_created(self):
        self.stored()
        self.assertEqual(self.wizard(), 0, self.err)
        self.assertFalse(self.gh.called("POST", "/app-manifests/THECODE/conversions"))
        self.assertEqual(self.opened, [])
        self.assertEqual(self.app_json()["installation_id"], 7654321)
        self.assertIn("octocat/two", self.out)
        self.assertQuiet()

    def test_a_renamed_app_is_verified_and_recorded(self):
        self.stored()
        cache = self.identity / "token-one.json"
        cache.write_text('{"token": "t"}')
        new = "https://github.com/apps/isshoni-lander"
        github = routes(self.pem, {("GET", "/app"): (200, {"slug": "isshoni-lander", "html_url": new}),
                                   ("GET", "/users/isshoni-lander%5Bbot%5D"): (200, {"id": 99999999}),
                                   ("GET", f"/users/{BOT}"): (404, {})})
        self.assertEqual(self.wizard(github=github), 0, self.err)
        self.assertEqual(self.app_json(), {
            "app_id": 123456, "slug": "isshoni-lander", "client_id": "Iv23abc", "html_url": new, "owner": "octocat",
            "owner_type": "User", "installation_id": 7654321, "installation_url": INSTALL_URL,
            "repository_selection": "selected", "bot_login": "isshoni-lander[bot]", "bot_id": 99999999,
            "bot_email": "99999999+isshoni-lander[bot]@users.noreply.github.com"})
        self.assertIn("renamed: factory-octocat -> isshoni-lander\n", self.out)
        self.assertIn(f"App isshoni-lander: {new}", self.out)
        self.assertIn("To remove it: `factory uninstall`", self.out)
        self.assertNotIn("factory-octocat\n", self.out.replace("renamed: factory-octocat -> isshoni-lander\n", ""))
        self.assertEqual(cache.read_text(), '{"token": "t"}')

    def test_an_unchanged_app_prints_no_renamed_line(self):
        self.stored()
        self.assertEqual(self.wizard(), 0, self.err)
        self.assertNotIn("renamed:", self.out)

    def test_app_answer_without_a_slug_writes_nothing(self):
        self.stored()
        before = (self.identity / "app.json").read_text()
        self.assertEqual(self.wizard(github=routes(self.pem, {("GET", "/app"): (200, {"html_url": APP["html_url"]})})), 1)
        self.assertIn("lacks the App's slug", self.err)
        self.assertEqual((self.identity / "app.json").read_text(), before)

    def test_another_owner_is_refused(self):
        self.stored()
        self.assertEqual(self.wizard("--owner", "someone-else"), 1)
        self.assertIn("factory-octocat", self.err)
        self.assertIn("octocat", self.err)
        self.assertEqual(self.gh.calls, [])
        self.assertEqual(self.wizard("--owner", "OCTOCAT"), 0, self.err)

    def test_401_names_the_clock(self):
        self.stored()
        self.wizard(github=routes(self.pem, {("GET", "/app"): (401, {})}))
        self.assertIn("clock is off by minutes", self.err)
        self.wizard(github=routes(self.pem, {("GET", "/app"): (404, {})}))
        self.assertNotIn("clock", self.err)

    def test_loose_modes_are_repaired(self):
        self.stored()
        os.chmod(self.identity / "key.pem", 0o644)
        os.chmod(self.identity, 0o755)
        self.assertEqual(self.wizard(), 0, self.err)
        self.assertEqual(stat.S_IMODE((self.identity / "key.pem").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.identity.stat().st_mode), 0o700)
        self.assertIn("looser permissions", self.out)

    def test_missing_key_is_reported(self):
        self.stored()
        (self.identity / "key.pem").unlink()
        self.assertEqual(self.wizard(), 1)
        self.assertIn("key.pem is missing", self.err)
        self.assertIn("rm -r", self.err)
        self.assertEqual(self.gh.calls, [])

    def test_app_gone_is_reported_and_nothing_changes(self):
        self.stored()
        before = (self.identity / "app.json").read_text()
        for status in (401, 404):
            github = routes(self.pem, {("GET", "/app"): (status, {"message": "Bad credentials"})})
            self.assertEqual(self.wizard(github=github), 1)
            self.assertIn("no longer exists on GitHub or its key was revoked", self.err)
            self.assertIn("https://github.com/settings/apps/factory-octocat/advanced", self.err)
            self.assertFalse(self.gh.called("POST", "/app-manifests/THECODE/conversions"))
            self.assertEqual(self.opened, [])
            self.assertEqual((self.identity / "app.json").read_text(), before)

    def test_not_installed_opens_the_install_page_and_the_next_run_resumes(self):
        self.stored()
        empty = routes(self.pem, {("GET", "/app/installations"): (200, [])})
        self.assertEqual(self.wizard(github=empty, wait=0), 1)
        self.assertEqual(self.opened, [f"{APP['html_url']}/installations/new"])
        self.assertIn("run `factory wizard` again", self.err)
        self.assertEqual(self.files(), ["app.json", "key.pem"])
        self.assertNotIn("installation_id", self.app_json())
        self.assertEqual(self.wizard(), 0, self.err)
        self.assertEqual(self.app_json()["installation_id"], 7654321)

    def test_the_advice_comes_before_the_install_page(self):
        self.stored()
        empty = routes(self.pem, {("GET", "/app/installations"): (200, [])})
        self.wizard(github=empty, wait=0)
        advice = 'On GitHub choose "Only select repositories" and pick the ones the factory works in: with "All repositories" ' \
                 "the bot can write to every repository of octocat."
        self.assertIn(advice, self.out)
        self.assertLess(self.out.index(advice), self.out.index("Install the App on octocat"))

    def test_the_selection_is_stored_and_all_is_warned_about(self):
        everything = {"repository_selection": "all"}
        self.stored()
        github = routes(self.pem, {("GET", "/app/installations"): (200, [{**INSTALLATION, **everything}])})
        self.assertEqual(self.wizard(github=github), 0, self.err)
        self.assertEqual(self.app_json()["repository_selection"], "all")
        self.assertIn(f"WARNING: the App is installed on ALL repositories of octocat (2). Narrow it to the ones the factory "
                      f"works in: {INSTALL_URL}", self.out)
        self.assertIn("reach only the repository a task runs in", self.out)
        self.assertNotIn("octocat/two", self.out)

    def test_a_narrow_installation_is_not_warned_about(self):
        self.stored()
        github = routes(self.pem, {("GET", "/app/installations"): (200, [{**INSTALLATION, "repository_selection": "selected"}])})
        self.assertEqual(self.wizard(github=github), 0, self.err)
        self.assertEqual(self.app_json()["repository_selection"], "selected")
        self.assertNotIn("WARNING", self.out)
        self.assertIn("octocat/two", self.out)

    def test_a_lower_permission_is_warned_about_with_both_urls_and_still_verifies(self):
        self.stored()
        lower = {**INSTALLATION, "permissions": {**INSTALLATION["permissions"], "actions": "read"}}
        self.assertEqual(self.wizard(github=routes(self.pem, {("GET", "/app/installations"): (200, [lower])})), 0, self.err)
        self.assertIn(f"WARNING: the installed App lacks actions: write (it has read). Change it at "
                      f"https://github.com/settings/apps/factory-octocat/permissions, then accept the new permission at {INSTALL_URL}.\n", self.out)
        self.assertEqual(self.out.count("WARNING"), 1)
        self.assertIn("octocat/two", self.out)

    def test_a_missing_permission_and_an_organizations_app_url(self):
        self.stored(owner_type="Organization")
        none = {**INSTALLATION, "permissions": {"contents": "write", "pull_requests": "write", "checks": "read", "statuses": "read"}}
        self.assertEqual(self.wizard(github=routes(self.pem, {("GET", "/app/installations"): (200, [none])})), 0, self.err)
        self.assertIn(f"WARNING: the installed App lacks actions: write (it has none). Change it at "
                      f"https://github.com/organizations/octocat/settings/apps/factory-octocat/permissions, then accept the new "
                      f"permission at {INSTALL_URL}.\n", self.out)

    def test_the_wanted_permissions_print_no_warning(self):
        self.stored()
        self.assertEqual(self.wizard(), 0, self.err)
        self.assertNotIn("WARNING", self.out)

    def test_install_is_polled_until_it_shows_up(self):
        self.stored()
        answers = [(200, []), (200, []), (200, [INSTALLATION])]
        github = routes(self.pem, {("GET", "/app/installations"): lambda path: answers.pop(0)})
        self.assertEqual(self.wizard(github=github), 0, self.err)
        self.assertEqual(len(self.opened), 1)
        self.assertEqual(answers, [])

    def test_zero_repositories(self):
        self.stored()
        github = routes(self.pem, {("GET", "/installation/repositories"): (200, {"total_count": 0, "repositories": []})})
        self.assertEqual(self.wizard(github=github), 1)
        self.assertIn(f"choose some at {INSTALL_URL}", self.err)
        self.assertNotIn("bot_id", self.app_json())

    def test_repositories_are_paged(self):
        self.stored()
        pages = {"1": [{"full_name": f"octocat/r{i}"} for i in range(100)], "2": [{"full_name": "octocat/last"}]}
        github = routes(self.pem, {("GET", "/installation/repositories"): lambda path: (200, {
            "total_count": 101, "repositories": pages[re.search(r"&page=(\d+)", path).group(1)]})})
        self.assertEqual(self.wizard(github=github), 0, self.err)
        self.assertIn("101 repositories", self.out)
        self.assertIn("octocat/last", self.out)


class BrowserTests(unittest.TestCase):
    def opens(self, platform, **env):
        with mock.patch.object(factory.sys, "platform", platform), \
                mock.patch.dict(os.environ, env), mock.patch.object(factory.webbrowser, "open") as opened:
            for name in ("DISPLAY", "WAYLAND_DISPLAY"):
                if name not in env:
                    os.environ.pop(name, None)
            factory.open_browser("http://127.0.0.1:1/")
            return opened.called

    def test_linux_needs_a_display(self):
        self.assertFalse(self.opens("linux"))
        self.assertTrue(self.opens("linux", DISPLAY=":0"))
        self.assertTrue(self.opens("linux", WAYLAND_DISPLAY="wayland-0"))

    def test_other_platforms_always_open(self):
        self.assertTrue(self.opens("darwin"))
