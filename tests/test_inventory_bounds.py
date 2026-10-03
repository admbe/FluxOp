"""Issue #100: the virtual-tag inventory filter must not scan the estate
unbounded. The old path sent LIMIT 1000000 and materialized every row into
Python before filtering; an authenticated reader could OOM the instance with
?virtualTagKey=env. The scan is now bounded relative to the requested page
and streamed in batches."""
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from api.database import FluxDatabase


class InventoryScanBoundTests(unittest.TestCase):
    def _spied(self, database: FluxDatabase, captured: list) -> None:
        real_connect = database.connect

        class Spy:
            def __init__(self, inner):
                self._inner = inner

            def execute(self, sql, params=None):
                captured.append((sql, params))
                if params is None:
                    return self._inner.execute(sql)
                return self._inner.execute(sql, params)

            def __getattr__(self, name):
                return getattr(self._inner, name)

        @contextmanager
        def spying_connect(read_only=False):
            with real_connect(read_only=read_only) as db:
                yield Spy(db)

        database.connect = spying_connect  # type: ignore[method-assign]

    def test_tag_filter_scan_limit_derives_from_page_not_a_million(self):
        with TemporaryDirectory() as temp:
            database = FluxDatabase(Path(temp) / "inv.duckdb")
            database.init()
            captured: list = []
            self._spied(database, captured)

            result = database.inventory(virtual_tag_key="env", limit=250)

            page_query = next(
                (params for sql, params in captured
                 if params and "LIMIT ? OFFSET ?" in sql),
                None,
            )
            self.assertIsNotNone(page_query, "paged inventory query not captured")
            self.assertLessEqual(page_query[-2], 2500)
            self.assertEqual(page_query[-1], 0)
            self.assertEqual(result["items"], [])
            self.assertFalse(result["scanTruncated"])

    def test_plain_inventory_keeps_exact_page_limit(self):
        with TemporaryDirectory() as temp:
            database = FluxDatabase(Path(temp) / "inv2.duckdb")
            database.init()
            captured: list = []
            self._spied(database, captured)

            result = database.inventory(limit=25, offset=50)

            page_query = next(
                (params for sql, params in captured
                 if params and "LIMIT ? OFFSET ?" in sql),
                None,
            )
            self.assertIsNotNone(page_query)
            self.assertEqual(page_query[-2], 25)
            self.assertEqual(page_query[-1], 50)
            self.assertFalse(result["scanTruncated"])


if __name__ == "__main__":
    unittest.main()
