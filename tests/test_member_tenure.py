import unittest
from datetime import datetime, timedelta, timezone

from core.member_tenure import MINIMUM_SERVER_TENURE, has_minimum_server_tenure


class MemberTenureTests(unittest.TestCase):
    def test_rejects_member_before_fifteen_full_days(self):
        now = datetime(2026, 10, 24, tzinfo=timezone.utc)
        self.assertFalse(has_minimum_server_tenure(
            now - MINIMUM_SERVER_TENURE + timedelta(seconds=1), now,
        ))

    def test_accepts_member_at_fifteen_full_days(self):
        now = datetime(2026, 10, 24, tzinfo=timezone.utc)
        self.assertTrue(has_minimum_server_tenure(
            now - MINIMUM_SERVER_TENURE, now,
        ))

    def test_future_join_date_is_not_eligible(self):
        now = datetime(2026, 10, 24, tzinfo=timezone.utc)
        self.assertFalse(has_minimum_server_tenure(now + timedelta(seconds=1), now))
