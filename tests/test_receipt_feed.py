"""A slow receipt request must not block local event processing."""
from datetime import datetime,timezone
from threading import Event
import unittest

from moqi.ha_gateway import ReceiptFeed


class ReceiptFeedTests(unittest.TestCase):
    def test_poll_returns_while_network_request_is_blocked_and_only_gets_receipts(self):
        entered,release,finished=Event(),Event(),Event()
        class Gateway:
            timeout=2
            def receipt(self,identifier):
                entered.set()
                release.wait(2)
                finished.set()
                return dict(id=identifier,status='service_completed')
        feed=ReceiptFeed(Gateway())
        feed.start()
        try:
            at=datetime.now(timezone.utc)
            self.assertIsNone(feed.poll('fixture',at))
            self.assertTrue(entered.wait(1))
            self.assertIsNone(feed.poll('fixture',at))
            self.assertFalse(finished.is_set())
            release.set()
            self.assertTrue(finished.wait(1))
        finally:
            release.set()
            feed.close()


if __name__=='__main__': unittest.main()
