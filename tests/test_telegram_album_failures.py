"""Independent additive regressions for real pacing failure and cleanup isolation."""
import asyncio
import contextlib
import io
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_telegram_albums as fixtures

tgs, tgs_one = fixtures.tgs, fixtures.tgs_one


class AlbumFailures(unittest.TestCase):
    def test_actual_pace_io_failure_is_unknown_after_ids(self):
        class Client:
            async def send_file(self, *args, **kwargs):
                return [SimpleNamespace(id=73101), SimpleNamespace(id=73102)]

        for core in (tgs, tgs_one.tgs):
            with self.subTest(core=core.__name__), tempfile.TemporaryDirectory() as directory:
                block = Path(directory) / 'not-a-directory'
                block.write_text('synthetic')
                out, err = io.StringIO(), io.StringIO()
                with patch.object(core, 'PACE_STATE_PATH', block / 'pace.json'), \
                        contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    status, ids = asyncio.run(core.send_document_album(
                        Client(), SimpleNamespace(id=111), [block, block], 'caption',
                        reply_to=None, silent=False, account='default'))
                self.assertEqual(status, 4, 'TARGET: actual pace persistence failure must be unknown')
                self.assertEqual(ids, [73101, 73102])
                self.assertIn('73101', out.getvalue())
                self.assertIn('73102', out.getvalue())
                self.assertNotIn('OK:', out.getvalue())
                self.assertNotIn(str(block), err.getvalue(), 'TARGET: album errors expose class only')

    def test_external_cancel_not_masked_by_cleanup_failure(self):
        async def cancelled_send(*args, **kwargs):
            asyncio.current_task().cancel()
            await asyncio.sleep(0)

        for route, core in [('label', tgs), ('explicit', tgs_one.tgs)]:
            fixture = fixtures.Albums()
            fixture.setUp()
            try:
                with patch.object(core, 'send_document_album', cancelled_send):
                    with self.assertRaises(asyncio.CancelledError,
                                           msg='TARGET: cleanup cannot replace external cancellation with retryable status'):
                        fixture.exercise(route, fixture.files[:2], disconnect_error=True)
            finally:
                fixture.doCleanups()

    def test_overlapping_album_cleanup_keeps_safe_error_scope(self):
        async def overlap(core):
            first_entered, first_release = asyncio.Event(), asyncio.Event()
            second_entered, second_release = asyncio.Event(), asyncio.Event()

            class First:
                async def disconnect(self):
                    first_entered.set()
                    await first_release.wait()

            class Second:
                async def disconnect(self):
                    second_entered.set()
                    await second_release.wait()
                    raise RuntimeError('SYNTHETIC_PRIVATE_ERROR')

            first = asyncio.create_task(core.disconnect_album(First()))
            await first_entered.wait()
            second = asyncio.create_task(core.disconnect_album(Second()))
            await second_entered.wait()
            first_release.set()
            self.assertTrue(await first)
            second_release.set()
            self.assertFalse(await second)

        for core in (tgs, tgs_one.tgs):
            out = io.StringIO()
            with contextlib.redirect_stderr(out):
                asyncio.run(overlap(core))
            self.assertIn('RuntimeError', out.getvalue())
            self.assertNotIn('SYNTHETIC_PRIVATE_ERROR', out.getvalue(),
                             'TARGET: concurrent album cleanup cannot leak exception content')


if __name__ == '__main__':
    unittest.main()
