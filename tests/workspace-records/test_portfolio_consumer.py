"""Event consumer orchestration; no external clients or model calls."""
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / 'skills/workspace-records/scripts'))
import portfolio_consumer as consumer
from core import RecordError


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.box = Mock(directory=pathlib.Path(self.tmp.name))
        self.factory = Mock(side_effect=AssertionError('model must not start'))

    def test_idle_event_queue_never_constructs_model(self):
        with patch.object(consumer.refresh, 'consume_queued', return_value={'outcome': 'idle'}):
            result = consumer.consume_events(self.box, self.factory)
        self.assertEqual(result['model_turns'], 0)
        self.factory.assert_not_called()

    def test_no_change_settles_then_drains_without_model(self):
        def queued(box, callback):
            return {'outcome': 'no_change'} if not getattr(queued, 'ran', False) else {'outcome': 'idle'}
        def first(box, callback):
            if not getattr(queued, 'ran', False):
                queued.ran = True
                return callback(box)
            return {'outcome': 'idle'}
        self.box.probe.return_value = {'outcome': 'no_change'}
        with patch.object(consumer.refresh, 'consume_queued', side_effect=first):
            result = consumer.consume_events(self.box, self.factory)
        self.assertEqual(result['outcome'], 'drained')
        self.assertEqual(result['model_turns'], 0)
        self.factory.assert_not_called()

    def test_unknown_does_not_repeat_consumer(self):
        with patch.object(consumer.refresh, 'consume_queued', return_value={'outcome': 'error', 'code': 'TRANSPORT_UNKNOWN', 'model_turns': 1}) as call:
            result = consumer.consume_events(self.box, self.factory)
        self.assertEqual(result['model_turns'], 1)
        self.assertTrue(result['events_preserved'])
        call.assert_called_once()

    def test_concurrent_invocation_uses_separate_released_os_lock(self):
        gate = consumer.private_child(self.box.directory, 'consumer-gate')
        with consumer.git_store.locked(gate):
            result = consumer.consume_events(self.box, self.factory, lock_timeout=0)
        self.assertEqual(result['outcome'], 'in_flight')
        self.assertEqual(result['model_turns'], 0)
        with patch.object(consumer.refresh, 'consume_queued', return_value={'outcome': 'idle'}):
            self.assertEqual(consumer.consume_events(self.box, self.factory)['outcome'], 'idle')

    def test_bounded_drain_preserves_backlog(self):
        with patch.object(consumer.refresh, 'consume_queued', return_value={'outcome': 'confirmed', 'model_turns': 1}):
            result = consumer.consume_events(self.box, self.factory, max_batches=2)
        self.assertEqual(result['outcome'], 'backlog_pending')
        self.assertEqual(result['model_turns'], 2)
        self.assertTrue(result['events_preserved'])
        with self.assertRaises(RecordError): consumer.consume_events(self.box, self.factory, max_batches=0)


if __name__ == '__main__':
    unittest.main()
