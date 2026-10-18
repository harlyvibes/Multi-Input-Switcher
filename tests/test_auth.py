"""SmartThings sign-in refresh rules: only one computer may refresh the shared sign-in."""
import time
import unittest
from unittest import mock

from test_monitors import tis


def oauth_cfg(expires_in=-10):
    return {"mode": "oauth", "client_id": "id", "client_secret": "secret",
            "access_token": "old-access", "refresh_token": "old-refresh",
            "expires_at": time.time() + expires_in}


class RefreshTest(unittest.TestCase):
    def test_refresh_stores_the_new_tokens(self):
        cfg, changes = oauth_cfg(), []
        auth = tis.SmartThingsAuth(cfg, lambda: changes.append(1))
        reply = {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600}
        with mock.patch.object(tis, "http_json", return_value=reply):
            self.assertEqual(auth.token(), "new-access")
        self.assertEqual(cfg["refresh_token"], "new-refresh")
        self.assertTrue(changes)

    def test_valid_token_is_not_refreshed(self):
        auth = tis.SmartThingsAuth(oauth_cfg(expires_in=3600), lambda: None)
        with mock.patch.object(tis, "http_json", side_effect=AssertionError("refreshed")):
            self.assertEqual(auth.token(), "old-access")

    def test_used_up_refresh_token_asks_for_a_new_sign_in(self):
        auth = tis.SmartThingsAuth(oauth_cfg(), lambda: None)
        error = tis.APIError("SmartThings API error 400: Invalid refresh token requested by clientId: x")
        with mock.patch.object(tis, "http_json", side_effect=error):
            with self.assertRaises(tis.AuthError) as ctx:
                auth.token()
        self.assertIn("no longer accepts this sign-in", str(ctx.exception))

    def test_other_api_errors_pass_through(self):
        auth = tis.SmartThingsAuth(oauth_cfg(), lambda: None)
        with mock.patch.object(tis, "http_json", side_effect=tis.APIError("SmartThings API error 500: boom")):
            with self.assertRaises(tis.APIError) as ctx:
                auth.token()
        self.assertNotIsInstance(ctx.exception, tis.AuthError)


class FollowerTest(unittest.TestCase):
    def follower(self, cfg, pulled):
        return tis.SmartThingsAuth(cfg, lambda: None, follower=lambda: True, pull=lambda: pulled)

    def test_follower_takes_tokens_from_the_master_and_never_refreshes(self):
        cfg = oauth_cfg()
        fresh = {"access_token": "master-access", "refresh_token": "master-refresh",
                 "expires_at": time.time() + 3600}
        with mock.patch.object(tis, "http_json", side_effect=AssertionError("follower refreshed")):
            self.assertEqual(self.follower(cfg, fresh).token(), "master-access")
        self.assertEqual(cfg["refresh_token"], "master-refresh")

    def test_follower_without_an_answer_does_not_refresh_either(self):
        for pulled in (None, {"access_token": "x", "expires_at": time.time() - 5}):
            with self.subTest(pulled=pulled), \
                    mock.patch.object(tis, "http_json", side_effect=AssertionError("follower refreshed")):
                with self.assertRaises(tis.AuthError) as ctx:
                    self.follower(oauth_cfg(), pulled).token()
                self.assertIn("master", str(ctx.exception))

    def test_a_computer_with_no_peers_or_the_master_refreshes_itself(self):
        reply = {"access_token": "a", "refresh_token": "b", "expires_in": 3600}
        for follower in (lambda: False, None):
            auth = tis.SmartThingsAuth(oauth_cfg(), lambda: None, follower=follower)
            with mock.patch.object(tis, "http_json", return_value=reply) as call:
                auth.token()
            call.assert_called_once()


class FakeApp:
    def __init__(self, cfg, auth=None):
        self.cfg, self.auth = cfg, auth

    def after(self, _ms, fn):
        fn()


class MasterAnswersTest(unittest.TestCase):
    def test_master_refreshes_before_replying_to_a_request(self):
        order = []
        cfg = {"auth": {"access_token": "t"}, "sync_port": 54975, "master_mode": True}
        auth = mock.Mock()
        auth.token.side_effect = lambda: order.append("refresh")
        server = tis.TokenSyncServer(FakeApp(cfg, auth))
        deadline = time.time() + 3
        while server.listening is None and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(server.listening, server.error)
        got = tis.request_push_from_peers({"peers": ["127.0.0.1:54975"]}, 54975)
        self.assertEqual(got, {"127.0.0.1:54975": {"access_token": "t"}})
        self.assertEqual(order, ["refresh"])


class FollowerRuleTest(unittest.TestCase):
    def test_follower_means_peers_configured_and_not_master(self):
        rule = tis.App._is_follower
        for master, peers, expected in ((False, ["1.2.3.4"], True), (True, ["1.2.3.4"], False),
                                        (False, [], False), (True, [], False)):
            app = mock.Mock(cfg={"master_mode": master, "peers": peers})
            self.assertEqual(rule(app), expected, (master, peers))


if __name__ == "__main__":
    unittest.main()
