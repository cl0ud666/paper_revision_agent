"""The rate-limit identity must match the authentication identity."""

from unittest.mock import Mock, patch

from app.web.queue import login_rate_limit


def test_whitespace_and_case_cannot_create_new_login_counters():
    redis = Mock()
    redis.eval.return_value = 1
    with patch("app.web.queue.redis_client", return_value=redis):
        for username in ("admin", "ADMIN", " admin ", "\tadmin\n"):
            login_rate_limit("192.0.2.1", username)
    keys = [call.args[2] for call in redis.eval.call_args_list]
    assert len(set(keys)) == 1
