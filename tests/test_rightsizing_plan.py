from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from api.database import FluxDatabase

CHECKOUT_KEY = (
    "/subscriptions/00000000-0000-0000-0000-000000000001/resourcegroups/"
    "flux-demo/providers/microsoft.compute/virtualmachines/checkout-api"
)


class RightsizingPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.database = FluxDatabase(Path(self.temp.name) / "plan.duckdb")
        self.database.init()
        self.database.seed_demo()

    def tearDown(self):
        self.temp.cleanup()

    def test_board_seeds_vms_from_live_inventory(self):
        board = self.database.rightsizing_plan_board()
        names = {vm["name"] for vm in board["vms"]}
        # Only the two virtual machines, never databases/disks/workspaces.
        self.assertEqual(names, {"checkout-api", "batch-worker-01"})
        self.assertEqual(board["summary"]["totalVms"], 2)
        checkout = next(v for v in board["vms"] if v["name"] == "checkout-api")
        self.assertEqual(checkout["vmKey"], CHECKOUT_KEY)
        self.assertEqual(checkout["sku"], "Standard_D4s_v5")
        self.assertEqual(checkout["region"], "eastus2")
        # A blank board id resolves to (and lazily creates) the primary
        # board, and the response says which board it served.
        self.assertTrue(board["boardId"])
        self.assertEqual(board["boardName"], "Default")

    def test_bucket_upsert_updates_in_place(self):
        self.database.save_rightsizing_bucket(
            {"region": "eastus2", "sku": "Standard_D4s_v5",
             "strategy": "1-year reservation", "refMonthlySavings": 100.0},
            updated_by="alice",
        )
        self.database.save_rightsizing_bucket(
            {"region": "eastus2", "sku": "Standard_D4s_v5",
             "strategy": "savings plan", "refMonthlySavings": 150.0},
            updated_by="bob",
        )
        board = self.database.rightsizing_plan_board()
        self.assertEqual(len(board["buckets"]), 1)
        bucket = next(b for b in board["buckets"] if b["sku"] == "Standard_D4s_v5")
        self.assertEqual(bucket["strategy"], "savings plan")
        self.assertEqual(bucket["refMonthlySavings"], 150.0)
        # created_by/created_at are preserved across the upsert; updated_by
        # is implicit in updatedAt advancing.
        self.assertEqual(bucket["createdBy"], "alice")
        self.assertEqual(
            board["summary"]["plannedMonthlySavings"], 150.0,
            "summary must reflect the updated bucket, not both versions",
        )

    def test_bucket_note_roundtrips(self):
        result = self.database.save_rightsizing_bucket(
            {"region": "eastus2", "sku": "Standard_D4s_v5",
             "note": "priced against the July rate card"},
        )
        board = self.database.rightsizing_plan_board()
        bucket = next(b for b in board["buckets"] if b["bucketKey"] == result["bucketKey"])
        self.assertEqual(bucket["note"], "priced against the July rate card")

    def test_move_appends_log_and_preserves_note_on_later_moves(self):
        created = self.database.save_rightsizing_bucket(
            {"region": "eastus2", "sku": "Standard_D4s_v5"}
        )
        self.database.assign_rightsizing_vms(
            [{
                "vmKey": CHECKOUT_KEY, "vmName": "checkout-api",
                "bucketKey": created["bucketKey"],
                "decision": "Confirmed", "note": "sized against p95",
            }],
            actor="alice",
        )
        # A later move that says nothing about decision/note keeps both.
        self.database.assign_rightsizing_vms(
            [{"vmKey": CHECKOUT_KEY, "vmName": "checkout-api",
              "bucketKey": "__savingsplan__"}],
            actor="bob",
        )
        board = self.database.rightsizing_plan_board()
        assignment = board["assignments"][CHECKOUT_KEY]
        self.assertEqual(assignment["bucketKey"], "__savingsplan__")
        self.assertEqual(assignment["decision"], "Confirmed")
        self.assertEqual(assignment["note"], "sized against p95")
        log = self.database.rightsizing_plan_log()
        self.assertEqual(len(log), 2)
        self.assertEqual(log[0]["fromLabel"], created["bucketKey"])
        self.assertEqual(log[0]["toLabel"], "__savingsplan__")
        self.assertEqual(log[0]["actor"], "bob")

    def test_bucket_delete_returns_members_to_unassigned(self):
        created = self.database.save_rightsizing_bucket(
            {"region": "eastus2", "sku": "Standard_D4s_v5"}
        )
        self.database.assign_rightsizing_vms(
            [{"vmKey": CHECKOUT_KEY, "vmName": "checkout-api",
              "bucketKey": created["bucketKey"]}],
            actor="alice",
        )
        result = self.database.delete_rightsizing_bucket(
            created["bucketKey"], actor="alice"
        )
        self.assertEqual(result["movedToUnassigned"], 1)
        board = self.database.rightsizing_plan_board()
        self.assertEqual(board["buckets"], [])
        self.assertEqual(
            board["assignments"][CHECKOUT_KEY]["bucketKey"], "__unassigned__"
        )

    def test_delete_nonexistent_bucket_raises(self):
        with self.assertRaises(ValueError):
            self.database.delete_rightsizing_bucket("nonexistent-key")

    def test_import_maps_by_name_and_preserves_unmatched(self):
        payload = {
            "buckets": {
                "eastus2|Standard_D4s_v5": {
                    "region": "eastus2", "sku": "Standard_D4s_v5",
                    "strategy": "1-year reservation",
                    "refQuantity": 3,
                    "refMonthlyPaygBaseline": "300.50",
                    "refMonthlyRi1YearCost": "200",
                    "refRi1YearUpfrontTotal": "2400",
                    "refMonthlySp1YearCost": "230.10",
                    "refMonthlySavingsVsPayg": "100.50",
                    "refExistingReservationCheck": "TBC",
                },
                # Real export edge case: unpriced fields arrive as "" and
                # crashed the first live import. Blank means absent.
                "westus3|Standard_B2s": {
                    "region": "westus3", "sku": "Standard_B2s",
                    "strategy": "keep on demand",
                    "refMonthlyRi1YearCost": "",
                    "refRi1YearUpfrontTotal": "",
                },
                # Real export edge case: the tool serializes its fixed
                # pseudo-columns as buckets; importing one duplicated the
                # built-in Savings plan column on the live board.
                "__savingsplan__": {
                    "key": "__savingsplan__", "region": "",
                    "sku": "Savings Plan (all eligible VMs)",
                    "refQuantity": 24,
                },
            },
            "assignments": {
                "lm-1": "eastus2|Standard_D4s_v5",
                "lm-2": "__excluded__",
                # Default state with no metadata: carries no signal, skipped.
                "lm-3": "__unassigned__",
            },
            "vmMeta": {
                "lm-1": {"decision": "Confirmed", "note": "keep on RI"},
            },
            "log": [
                {"ts": 1785510822348, "vmId": "lm-1",
                 "vmName": "checkout-api", "from": "Unassigned",
                 "to": "Standard_D4s_v5 - eastus2",
                 "decision": "Confirmed", "note": ""},
            ],
            "vms": [
                {"id": "lm-1", "vmName": "checkout-api",
                 "subscriptionName": "Platform Production"},
                {"id": "lm-2", "vmName": "ghost-vm-gone",
                 "subscriptionName": "Platform Production"},
                {"id": "lm-3", "vmName": "batch-worker-01",
                 "subscriptionName": "Platform Production"},
            ],
        }
        report = self.database.import_rightsizing_plan(payload, actor="import")
        self.assertFalse(report["dryRun"])
        self.assertTrue(report["boardId"])
        self.assertEqual(report["bucketsImported"], 2)
        self.assertEqual(report["bucketsSkipped"], 1)
        self.assertEqual(report["assignmentsImported"], 2)
        self.assertEqual(report["matched"], 1)
        self.assertEqual(report["unmatched"], 1)
        self.assertEqual(report["logImported"], 1)
        self.assertEqual(report["unmatchedSamples"], ["ghost-vm-gone"])
        self.assertEqual(report["inventoryVmCount"], 2)
        self.assertIn("checkout-api", report["inventorySample"])

        board = self.database.rightsizing_plan_board(report["boardId"])
        self.assertEqual(len(board["buckets"]), 2,
                         "the pseudo-column must not become a bucket")
        # checkout-api resolved to its Azure resource id, and its bucketKey
        # is the real (board-prefixed) key, matching an actual bucket row --
        # not the bare "region|sku" string the file used.
        assignment = board["assignments"][CHECKOUT_KEY]
        self.assertEqual(
            assignment["bucketKey"],
            f"{report['boardId']}:eastus2|Standard_D4s_v5",
        )
        self.assertIn(
            assignment["bucketKey"],
            {b["bucketKey"] for b in board["buckets"]},
            "the assignment must reference a real bucket row",
        )
        self.assertEqual(assignment["decision"], "Confirmed")
        self.assertEqual(assignment["note"], "keep on RI")
        # The decommissioned VM is preserved, not dropped.
        unmatched = board["importedUnmatched"]
        self.assertEqual(len(unmatched), 1)
        self.assertEqual(unmatched[0]["vmKey"], "import:lm-2")
        self.assertEqual(unmatched[0]["vmName"], "ghost-vm-gone")
        # Bucket economics mapped from the standalone tool's field names.
        bucket = next(b for b in board["buckets"] if b["sku"] == "Standard_D4s_v5")
        self.assertEqual(bucket["refMonthlyPayg"], 300.50)
        self.assertEqual(bucket["refRi1yUpfront"], 2400.0)
        # Imported log timestamps convert from epoch milliseconds.
        log = self.database.rightsizing_plan_log(report["boardId"])
        imported = [e for e in log if e["actor"] == "import"]
        self.assertEqual(len(imported), 1)
        parsed = datetime.fromisoformat(imported[0]["ts"])
        self.assertEqual(parsed.astimezone(timezone.utc).year, 2026)

    def test_import_is_rerunnable_without_duplicating_assignments(self):
        payload = {
            "buckets": {},
            "assignments": {"lm-1": "__excluded__"},
            "vmMeta": {},
            "log": [
                {"ts": 1785510822348, "vmId": "lm-1",
                 "vmName": "checkout-api", "from": "Unassigned",
                 "to": "Excluded", "decision": "", "note": ""},
            ],
            "vms": [{"id": "lm-1", "vmName": "checkout-api",
                     "subscriptionName": "Platform Production"}],
        }
        first = self.database.import_rightsizing_plan(payload)
        self.database.import_rightsizing_plan(
            payload, board_id=first["boardId"]
        )
        board = self.database.rightsizing_plan_board(first["boardId"])
        self.assertEqual(
            board["assignments"][CHECKOUT_KEY]["bucketKey"], "__excluded__"
        )
        self.assertEqual(len(board["importedUnmatched"]), 0)
        # Imported history is replaced, not appended, on a re-import.
        log = self.database.rightsizing_plan_log(first["boardId"])
        self.assertEqual(
            len([e for e in log if e["actor"].startswith("import")]), 1
        )

    def test_import_matches_fqdn_and_computer_name_and_promotes(self):
        # First import: LogicMonitor knew this VM by a name that does not
        # exist in inventory, so the decision was preserved as historical.
        first = {
            "buckets": {}, "log": [], "vmMeta": {},
            "assignments": {"lm-9": "__savingsplan__"},
            "vms": [{"id": "lm-9", "vmName": "legacy-alias",
                     "subscriptionName": "Platform Production"}],
        }
        report = self.database.import_rightsizing_plan(first)
        board_id = report["boardId"]
        self.assertEqual(report["matched"], 0)
        board = self.database.rightsizing_plan_board(board_id)
        self.assertEqual(board["importedUnmatched"][0]["vmKey"], "import:lm-9")

        # Second import: the seed now carries the guest hostname as a FQDN.
        # The domain-stripped fallback matches it, and the previously
        # preserved import: row is promoted onto the live VM.
        second = {
            "buckets": {}, "log": [], "vmMeta": {},
            "assignments": {"lm-9": "__savingsplan__"},
            "vms": [{"id": "lm-9", "vmName": "legacy-alias",
                     "computerName": "CHECKOUT-API.corp.local",
                     "subscriptionName": "Platform Production"}],
        }
        report = self.database.import_rightsizing_plan(
            second, board_id=board_id
        )
        self.assertEqual(report["matched"], 1)
        board = self.database.rightsizing_plan_board(board_id)
        self.assertEqual(len(board["importedUnmatched"]), 0)
        self.assertEqual(
            board["assignments"][CHECKOUT_KEY]["bucketKey"], "__savingsplan__"
        )

    def test_import_dry_run_does_not_write_and_classifies_changes(self):
        payload = {
            "buckets": {
                "eastus2|Standard_D4s_v5": {
                    "region": "eastus2", "sku": "Standard_D4s_v5",
                    "refMonthlySavingsVsPayg": "100.50",
                },
            },
            "assignments": {"lm-1": "eastus2|Standard_D4s_v5"},
            "vmMeta": {"lm-1": {"decision": "Confirmed", "note": "v1"}},
            "log": [],
            "vms": [{"id": "lm-1", "vmName": "checkout-api",
                     "subscriptionName": "Platform Production"}],
        }
        preview = self.database.import_rightsizing_plan(payload, dry_run=True)
        self.assertTrue(preview["dryRun"])
        self.assertEqual(len(preview["buckets"]["added"]), 1)
        self.assertEqual(preview["buckets"]["changed"], [])
        self.assertEqual(len(preview["assignments"]["added"]), 1)
        self.assertEqual(
            preview["assignments"]["added"][0]["bucketLabel"],
            "Standard_D4s_v5 — eastus2",
        )
        # Nothing was written: no new board, no bucket, no assignment.
        self.assertEqual(len(self.database.rightsizing_boards()), 1)
        board = self.database.rightsizing_plan_board()
        self.assertEqual(board["buckets"], [])
        self.assertNotIn(CHECKOUT_KEY, board["assignments"])

        applied = self.database.import_rightsizing_plan(payload, dry_run=False)
        board_id = applied["boardId"]

        # Re-running the identical file: everything unchanged.
        unchanged_preview = self.database.import_rightsizing_plan(
            payload, board_id=board_id, dry_run=True
        )
        self.assertEqual(unchanged_preview["buckets"]["added"], [])
        self.assertEqual(unchanged_preview["buckets"]["changed"], [])
        self.assertEqual(unchanged_preview["buckets"]["unchanged"], 1)
        self.assertEqual(unchanged_preview["assignments"]["unchanged"], 1)
        self.assertEqual(unchanged_preview["logEntriesReplaced"], 0)

        # An updated file: bucket economics and the VM's note changed.
        updated = {
            "buckets": {
                "eastus2|Standard_D4s_v5": {
                    "region": "eastus2", "sku": "Standard_D4s_v5",
                    "refMonthlySavingsVsPayg": "150.00",
                },
            },
            "assignments": {"lm-1": "eastus2|Standard_D4s_v5"},
            "vmMeta": {"lm-1": {"decision": "Confirmed", "note": "v2"}},
            "log": [],
            "vms": payload["vms"],
        }
        changed_preview = self.database.import_rightsizing_plan(
            updated, board_id=board_id, dry_run=True
        )
        self.assertEqual(len(changed_preview["buckets"]["changed"]), 1)
        fields = {f["field"] for f in changed_preview["buckets"]["changed"][0]["fields"]}
        self.assertEqual(fields, {"refMonthlySavings"})
        self.assertEqual(len(changed_preview["assignments"]["changed"]), 1)
        changed_assignment = changed_preview["assignments"]["changed"][0]
        self.assertEqual(changed_assignment["after"]["note"], "v2")
        self.assertEqual(changed_assignment["before"]["note"], "v1")

        # The dry run must not have written the change.
        untouched = self.database.rightsizing_plan_board(board_id)
        self.assertEqual(untouched["buckets"][0]["refMonthlySavings"], 100.5)

        # Applying the updated file for real does write it.
        self.database.import_rightsizing_plan(
            updated, board_id=board_id, dry_run=False
        )
        after_apply = self.database.rightsizing_plan_board(board_id)
        self.assertEqual(after_apply["buckets"][0]["refMonthlySavings"], 150.0)
        self.assertEqual(
            after_apply["assignments"][CHECKOUT_KEY]["note"], "v2"
        )

    def test_reimport_of_own_export_normalizes_already_prefixed_bucket_key(self):
        # Flux's own "Export" writes assignment values as the real,
        # board-prefixed bucketKey (see exportPlan() in the frontend),
        # unlike the standalone tool's bare "region|sku". Re-importing
        # that file must recognize the already-prefixed key as the same
        # bucket rather than re-prefixing it into a doubly-keyed string
        # that matches no real bucket.
        created = self.database.save_rightsizing_bucket(
            {"region": "eastus2", "sku": "Standard_D4s_v5"}
        )
        board_id = created["boardId"]
        real_bucket_key = created["bucketKey"]
        self.database.assign_rightsizing_vms(
            [{
                "vmKey": CHECKOUT_KEY, "vmName": "checkout-api",
                "bucketKey": real_bucket_key,
                "decision": "Confirmed", "note": "v1",
            }],
            board_id=board_id,
        )

        reexported = {
            "buckets": {
                real_bucket_key: {
                    "region": "eastus2", "sku": "Standard_D4s_v5",
                },
            },
            "assignments": {"lm-1": real_bucket_key},
            "vmMeta": {"lm-1": {"decision": "Confirmed", "note": "v1"}},
            "log": [],
            "vms": [{"id": "lm-1", "vmName": "checkout-api",
                     "subscriptionName": "Platform Production"}],
        }
        preview = self.database.import_rightsizing_plan(
            reexported, board_id=board_id, dry_run=True
        )
        self.assertEqual(preview["assignments"]["added"], [])
        self.assertEqual(preview["assignments"]["changed"], [])
        self.assertEqual(preview["assignments"]["unchanged"], 1)

        # A change that keeps the same already-prefixed key must still
        # resolve to the bucket's human label, not the raw key string.
        reexported["vmMeta"]["lm-1"]["note"] = "v2"
        changed_preview = self.database.import_rightsizing_plan(
            reexported, board_id=board_id, dry_run=True
        )
        self.assertEqual(len(changed_preview["assignments"]["changed"]), 1)
        changed = changed_preview["assignments"]["changed"][0]
        self.assertEqual(
            changed["after"]["bucketLabel"], "Standard_D4s_v5 — eastus2"
        )
        self.assertEqual(changed["after"]["bucketKey"], real_bucket_key)

        # Applying for real must not corrupt the stored bucket_key into
        # a doubly board-prefixed string that matches no real bucket.
        self.database.import_rightsizing_plan(
            reexported, board_id=board_id, dry_run=False
        )
        board = self.database.rightsizing_plan_board(board_id)
        self.assertEqual(
            board["assignments"][CHECKOUT_KEY]["bucketKey"], real_bucket_key
        )
        self.assertEqual(real_bucket_key.count(":"), 1)

    def test_migration_from_pre_board_schema_reprefixes_stored_keys(self):
        # Simulate a real installation that already had rightsizing data
        # before boards existed: a bare bucket_key, a single-column vm_key
        # primary key on assignments, and no board_id anywhere on any of
        # the three tables. _migrate_rightsizing_boards must upgrade all
        # three in place without losing or mis-keying anything -- this is
        # the exact path a real deploy takes, not just a fresh install.
        now = datetime.now(timezone.utc)
        with self.database.operational_connect() as db:
            db.execute("DROP TABLE rightsizing_plan_log")
            db.execute("DROP TABLE rightsizing_plan_assignments")
            db.execute("DROP TABLE rightsizing_plan_buckets")
            db.execute(
                """
                CREATE TABLE rightsizing_plan_buckets (
                    bucket_key VARCHAR PRIMARY KEY,
                    region VARCHAR NOT NULL,
                    sku VARCHAR NOT NULL,
                    strategy VARCHAR NOT NULL DEFAULT '',
                    source VARCHAR NOT NULL DEFAULT '',
                    ref_quantity INTEGER,
                    ref_monthly_payg DOUBLE,
                    ref_monthly_ri_1y DOUBLE,
                    ref_ri_1y_upfront DOUBLE,
                    ref_monthly_sp_1y DOUBLE,
                    ref_monthly_savings DOUBLE,
                    ref_reservation_check VARCHAR NOT NULL DEFAULT '',
                    created_by VARCHAR NOT NULL DEFAULT '',
                    created_at TIMESTAMPTZ NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE rightsizing_plan_assignments (
                    vm_key VARCHAR PRIMARY KEY,
                    vm_name VARCHAR NOT NULL DEFAULT '',
                    subscription_name VARCHAR NOT NULL DEFAULT '',
                    bucket_key VARCHAR NOT NULL DEFAULT '__unassigned__',
                    decision VARCHAR NOT NULL DEFAULT 'Pending',
                    note VARCHAR NOT NULL DEFAULT '',
                    source VARCHAR NOT NULL DEFAULT 'ui',
                    updated_by VARCHAR NOT NULL DEFAULT '',
                    updated_at TIMESTAMPTZ NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE rightsizing_plan_log (
                    id VARCHAR PRIMARY KEY,
                    ts TIMESTAMPTZ NOT NULL,
                    actor VARCHAR NOT NULL DEFAULT '',
                    vm_key VARCHAR NOT NULL DEFAULT '',
                    vm_name VARCHAR NOT NULL DEFAULT '',
                    from_label VARCHAR NOT NULL DEFAULT '',
                    to_label VARCHAR NOT NULL DEFAULT '',
                    decision VARCHAR NOT NULL DEFAULT '',
                    note VARCHAR NOT NULL DEFAULT ''
                )
                """
            )
            db.execute(
                "INSERT INTO rightsizing_plan_buckets "
                "(bucket_key, region, sku, ref_monthly_savings, "
                "created_at, updated_at) VALUES "
                "('eastus2|Standard_D4s_v5', 'eastus2', 'Standard_D4s_v5', "
                "250.0, ?, ?)",
                [now, now],
            )
            db.execute(
                "INSERT INTO rightsizing_plan_assignments "
                "(vm_key, vm_name, bucket_key, decision, note, updated_at) "
                "VALUES (?, 'checkout-api', 'eastus2|Standard_D4s_v5', "
                "'Confirmed', 'v1', ?)",
                [CHECKOUT_KEY, now],
            )
            db.execute(
                "INSERT INTO rightsizing_plan_log "
                "(id, ts, actor, vm_key, vm_name, from_label, to_label, "
                "decision, note) VALUES ('log-1', ?, 'alice', ?, "
                "'checkout-api', '__unassigned__', "
                "'eastus2|Standard_D4s_v5', 'Confirmed', 'v1')",
                [now, CHECKOUT_KEY],
            )
            db.execute(
                "INSERT INTO rightsizing_plan_log "
                "(id, ts, actor, vm_key, vm_name, from_label, to_label, "
                "decision, note) VALUES ('log-2', ?, 'bob', 'lm-9', "
                "'legacy-alias', '', '__savingsplan__', 'Pending', '')",
                [now],
            )
            db.commit()

        # Re-running init() re-triggers the guarded migration against this
        # now-reverted-to-pre-board shape, exactly like a real upgrade.
        self.database._operational.init()

        board = self.database.rightsizing_plan_board()
        self.assertEqual(len(board["buckets"]), 1)
        bucket = board["buckets"][0]
        self.assertTrue(bucket["bucketKey"].startswith(f"{board['boardId']}:"))
        self.assertEqual(
            bucket["bucketKey"].split(":", 1)[1], "eastus2|Standard_D4s_v5"
        )

        # The assignment's bucket_key must be reprefixed to the exact same
        # key as the migrated bucket row -- not just "some" prefix -- or
        # the VM would silently belong to no real bucket at all.
        assignment = board["assignments"][CHECKOUT_KEY]
        self.assertEqual(assignment["bucketKey"], bucket["bucketKey"])
        self.assertEqual(assignment["note"], "v1")

        log = self.database.rightsizing_plan_log()
        by_vm_name = {entry["vmName"]: entry for entry in log}
        # A real bucket reference gets the same reprefixing as the bucket
        # and assignment rows it describes, so the decision log can still
        # resolve it to a human label after the upgrade.
        self.assertEqual(
            by_vm_name["checkout-api"]["toLabel"], bucket["bucketKey"]
        )
        self.assertEqual(by_vm_name["checkout-api"]["fromLabel"], "__unassigned__")
        # Special pseudo-buckets and a blank "no prior bucket" label are
        # never prefixed -- there is no real bucket row for them to match.
        self.assertEqual(by_vm_name["legacy-alias"]["toLabel"], "__savingsplan__")
        self.assertEqual(by_vm_name["legacy-alias"]["fromLabel"], "")

    def test_import_into_new_board_creates_it_only_on_real_apply(self):
        payload = {
            "buckets": {}, "assignments": {}, "vmMeta": {}, "log": [],
            "vms": [],
        }
        preview = self.database.import_rightsizing_plan(
            payload, new_board_name="Q3 migration", dry_run=True
        )
        self.assertIsNone(preview["boardId"])
        self.assertEqual(preview["newBoardName"], "Q3 migration")
        self.assertEqual(len(self.database.rightsizing_boards()), 1)

        report = self.database.import_rightsizing_plan(
            payload, new_board_name="Q3 migration", dry_run=False
        )
        boards = self.database.rightsizing_boards()
        self.assertEqual(len(boards), 2)
        created = next(b for b in boards if b["id"] == report["boardId"])
        self.assertEqual(created["name"], "Q3 migration")
        self.assertFalse(created["isPrimary"])


class RightsizingBoardTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.database = FluxDatabase(Path(self.temp.name) / "boards.duckdb")
        self.database.init()
        self.database.seed_demo()

    def tearDown(self):
        self.temp.cleanup()

    def test_listing_boards_lazily_creates_a_primary_default(self):
        boards = self.database.rightsizing_boards()
        self.assertEqual(len(boards), 1)
        self.assertEqual(boards[0]["name"], "Default")
        self.assertTrue(boards[0]["isPrimary"])
        self.assertEqual(boards[0]["bucketCount"], 0)
        self.assertEqual(boards[0]["assignedCount"], 0)

    def test_create_and_rename_board(self):
        created = self.database.create_rightsizing_board(
            "Migration candidates", "For the Q3 push", actor="alice"
        )
        self.assertFalse(created["isPrimary"])
        renamed = self.database.rename_rightsizing_board(
            created["id"], "Migration candidates (Q3)", "Updated scope"
        )
        self.assertEqual(renamed["name"], "Migration candidates (Q3)")
        boards = {b["id"]: b for b in self.database.rightsizing_boards()}
        self.assertEqual(
            boards[created["id"]]["name"], "Migration candidates (Q3)"
        )

    def test_create_board_requires_a_name(self):
        with self.assertRaises(ValueError):
            self.database.create_rightsizing_board("   ")

    def test_rename_nonexistent_board_raises(self):
        with self.assertRaises(ValueError):
            self.database.rename_rightsizing_board("nope", "New name")

    def test_set_primary_nonexistent_board_raises(self):
        with self.assertRaises(ValueError):
            self.database.set_primary_rightsizing_board("nope")

    def test_boards_are_isolated_from_each_other(self):
        default_board = self.database.rightsizing_boards()[0]
        second = self.database.create_rightsizing_board("Scratch board")
        self.database.save_rightsizing_bucket(
            {"boardId": second["id"], "region": "eastus2",
             "sku": "Standard_D4s_v5"}
        )
        default_state = self.database.rightsizing_plan_board(default_board["id"])
        second_state = self.database.rightsizing_plan_board(second["id"])
        self.assertEqual(default_state["buckets"], [])
        self.assertEqual(len(second_state["buckets"]), 1)

    def test_fiscal_outlook_only_counts_the_primary_board(self):
        default_board = self.database.rightsizing_boards()[0]
        self.database.save_rightsizing_bucket(
            {"boardId": default_board["id"], "region": "eastus2",
             "sku": "Standard_D4s_v5", "refMonthlySavings": 100.0}
        )
        scratch = self.database.create_rightsizing_board("Scratch board")
        self.database.save_rightsizing_bucket(
            {"boardId": scratch["id"], "region": "westus3",
             "sku": "Standard_B2s", "refMonthlySavings": 9999.0}
        )
        self.assertEqual(
            self.database.planned_rightsizing_monthly_savings(), 100.0,
            "a non-primary board must never inflate the fiscal outlook",
        )
        self.database.set_primary_rightsizing_board(scratch["id"])
        self.assertEqual(
            self.database.planned_rightsizing_monthly_savings(), 9999.0,
        )

    def test_cannot_delete_the_primary_board(self):
        default_board = self.database.rightsizing_boards()[0]
        with self.assertRaises(PermissionError):
            self.database.delete_rightsizing_board(default_board["id"])

    def test_delete_nonexistent_board_raises(self):
        with self.assertRaises(ValueError):
            self.database.delete_rightsizing_board("nope")

    def test_delete_board_cascades_buckets_and_assignments(self):
        board = self.database.create_rightsizing_board("Temporary")
        self.database.save_rightsizing_bucket(
            {"boardId": board["id"], "region": "eastus2",
             "sku": "Standard_D4s_v5"}
        )
        self.database.assign_rightsizing_vms(
            [{"vmKey": CHECKOUT_KEY, "vmName": "checkout-api",
              "bucketKey": "__excluded__"}],
            board_id=board["id"],
        )
        result = self.database.delete_rightsizing_board(board["id"])
        self.assertEqual(result["bucketsRemoved"], 1)
        self.assertEqual(result["assignmentsRemoved"], 1)
        remaining = self.database.rightsizing_boards()
        self.assertEqual(len(remaining), 1)
        self.assertTrue(remaining[0]["isPrimary"])


if __name__ == "__main__":
    unittest.main()
