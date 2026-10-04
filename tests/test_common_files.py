"""Windows sharing conflicts are retried without hiding invalid local state."""
import io
import json
import unittest
from unittest.mock import patch

from expman import common


class DurableReadTests(unittest.TestCase):
    def test_windows_transient_sharing_conflict_keeps_actual_owner(self):
        with patch.object(common.os, 'name', 'nt'), patch.object(common.time, 'sleep') as sleep, \
                patch('builtins.open', side_effect=[PermissionError('replacing'),
                      io.StringIO('{"pid":123,"nonce":"current-owner"}')]) as read:
            self.assertEqual(common.read_json('owner.json', {}), {'pid': 123, 'nonce': 'current-owner'})
            self.assertEqual(read.call_count, 2)
            sleep.assert_called_once()

    def test_permanent_windows_permission_failure_never_returns_empty_owner(self):
        with patch.object(common.os, 'name', 'nt'), patch.object(common.time, 'sleep') as sleep, \
                patch('builtins.open', side_effect=PermissionError('denied')) as read:
            with self.assertRaises(PermissionError):
                common.read_json('owner.json', {})
            self.assertEqual(read.call_count, 4)
            self.assertEqual(sleep.call_count, 3)

    def test_other_platform_permission_failure_is_immediate(self):
        with patch.object(common.os, 'name', 'posix'), patch.object(common.time, 'sleep') as sleep, \
                patch('builtins.open', side_effect=PermissionError('denied')) as read:
            with self.assertRaises(PermissionError):
                common.read_json('owner.json', {})
            read.assert_called_once()
            sleep.assert_not_called()

    def test_invalid_json_never_becomes_an_empty_owner_or_gets_retried(self):
        with patch.object(common.os, 'name', 'nt'), patch.object(common.time, 'sleep') as sleep, \
                patch('builtins.open', return_value=io.StringIO('{broken')) as read:
            with self.assertRaises(json.JSONDecodeError):
                common.read_json('owner.json', {})
            read.assert_called_once()
            sleep.assert_not_called()


if __name__ == '__main__':
    unittest.main()
