import unittest

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")

from ledgepill.ui.activity import Activity, ActivityQueue
from ledgepill.ui.island import Island


def activity(priority=0, key=None):
    return Activity(lambda: None, priority=priority, key=key)


class ActivityQueueTest(unittest.TestCase):
    def _drain(self, queue):
        items = []
        while True:
            nxt = queue.pop_next()
            if nxt is None:
                return items
            items.append(nxt)

    def test_highest_priority_first_with_fifo_ties(self):
        queue = ActivityQueue()
        first = activity(priority=30)
        second = activity(priority=30)
        queue.push(activity(priority=10))
        queue.push(first)
        queue.push(second)
        queue.push(activity(priority=50))

        self.assertEqual(queue.pop_next().priority, 50)
        self.assertIs(queue.pop_next(), first)
        self.assertIs(queue.pop_next(), second)
        self.assertEqual(queue.pop_next().priority, 10)

    def test_ties_pop_in_insertion_order(self):
        queue = ActivityQueue()
        oldest = activity(priority=5)
        middle = activity(priority=5)
        newest = activity(priority=5)
        for item in (oldest, middle, newest):
            queue.push(item)

        self.assertIs(queue.pop_next(), oldest)
        self.assertIs(queue.pop_next(), middle)
        self.assertIs(queue.pop_next(), newest)

    def test_same_key_replaces_queued_activity(self):
        queue = ActivityQueue()
        queue.push(activity(priority=10, key="volume"))
        replacement = activity(priority=90, key="volume")
        queue.push(replacement)

        self.assertEqual(len(queue), 1)
        self.assertIs(queue.pop_next(), replacement)

    def test_overflow_drops_lowest_priority(self):
        queue = ActivityQueue(max_size=2)
        queue.push(activity(priority=10))
        queue.push(activity(priority=50))
        queue.push(activity(priority=30))

        self.assertEqual(len(queue), 2)
        self.assertEqual([a.priority for a in self._drain(queue)], [50, 30])

    def test_overflow_tie_drops_oldest(self):
        queue = ActivityQueue(max_size=2)
        oldest = activity(priority=10)
        newer = activity(priority=10)
        queue.push(oldest)
        queue.push(newer)
        queue.push(activity(priority=20))

        remaining = self._drain(queue)
        self.assertNotIn(oldest, remaining)
        self.assertIn(newer, remaining)

    def test_pop_next_empties_queue(self):
        queue = ActivityQueue()
        queue.push(activity(priority=1))
        queue.push(activity(priority=2))

        queue.pop_next()
        queue.pop_next()

        self.assertEqual(len(queue), 0)
        self.assertIsNone(queue.pop_next())

    def test_remove_key_and_clear(self):
        queue = ActivityQueue()
        queue.push(activity(key="a"))
        queue.push(activity(key="b"))
        queue.remove_key("a")
        self.assertEqual(len(queue), 1)

        queue.clear()
        self.assertEqual(len(queue), 0)

    def test_higher_priority_peek_preserves_interrupted_peek(self):
        interrupted = activity(priority=10, key="volume")

        class PeekHarness:
            class PillHarness:
                @staticmethod
                def show_queue_count(_count):
                    pass

            def __init__(self):
                self.cfg = {"activity_queue": True}
                self.state = "peek"
                self._peek_seq = 0
                self._peek_timeout = 1
                self._current_peek = interrupted
                self._peek_queue = ActivityQueue()
                self._shown = []
                self.pill = self.PillHarness()

            def _show_peek_now(self, item):
                self._current_peek = item
                self._shown.append(item)

        harness = PeekHarness()
        harness._peek_queue = ActivityQueue()
        harness._shown = []

        def setter():
            return None

        Island.show_peek(harness, setter, priority=90, key="call")

        self.assertIs(harness._shown[0].setter, setter)
        self.assertIs(harness._peek_queue.pop_next(), interrupted)


if __name__ == "__main__":
    unittest.main()
