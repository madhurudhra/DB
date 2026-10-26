import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import exit_submission_claim as module

class ClaimTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/"exits.sqlite"
        with sqlite3.connect(self.path) as db:
            db.executescript("""
                CREATE TABLE account_bindings(bot TEXT PRIMARY KEY,account TEXT NOT NULL);
                CREATE TABLE exit_orders(cid TEXT PRIMARY KEY,bot TEXT,account TEXT,state TEXT,broker_id TEXT,last_update TEXT);
                INSERT INTO account_bindings VALUES('crypto','TEST_ACCOUNT');
                INSERT INTO exit_orders VALUES('EXIT_A','crypto','TEST_ACCOUNT','INTENT',NULL,NULL);
            """)
    def tearDown(self):self.temp.cleanup()
    def claim(self,**changes):
        d=dict(cid="EXIT_A",bot="crypto",account="TEST_ACCOUNT")
        d.update(changes)
        return module.claim_exit(self.path,**d)
    def state(self):
        with sqlite3.connect(self.path) as db:
            return db.execute("SELECT state,last_update FROM exit_orders WHERE cid='EXIT_A'").fetchone()
    def test_claim_once(self):
        token=self.claim()
        self.assertIsInstance(token,str)
        self.assertIsNone(self.claim())
        state,payload=self.state()
        self.assertEqual(state,"SUBMITTING")
        self.assertEqual(json.loads(payload)["claim_id"],token)
    def test_twenty_competing_workers(self):
        with ThreadPoolExecutor(max_workers=10) as pool:
            results=list(pool.map(lambda _:self.claim(),range(20)))
        self.assertEqual(sum(token is not None for token in results),1)
    def test_reopen_does_not_retry(self):
        self.claim()
        self.assertIsNone(self.claim())
        self.assertEqual(self.state()[0],"SUBMITTING")
    def test_unknown_or_wrong_identity(self):
        for changes in ({"cid":"UNKNOWN"},{"account":"OTHER"},{"bot":"zerodha"},{"bot":"unknown"}):
            with self.subTest(changes=changes):
                with self.assertRaises(module.ClaimError):self.claim(**changes)
        self.assertEqual(self.state()[0],"INTENT")
    def test_missing_database_not_created(self):
        path=Path(self.temp.name)/"missing.sqlite"
        with self.assertRaises(module.ClaimError):
            module.claim_exit(path,cid="A",bot="crypto",account="TEST_ACCOUNT")
        self.assertFalse(path.exists())
    def test_unknown_schema_blocks(self):
        path=Path(self.temp.name)/"empty.sqlite"
        with sqlite3.connect(path):pass
        with self.assertRaises(module.ClaimError):
            module.claim_exit(path,cid="A",bot="crypto",account="TEST_ACCOUNT")
    def test_pending_uncertain_terminal_not_claimed(self):
        for state in ("PENDING","PARTIALLY_FILLED","UNCERTAIN","FILLED","CANCELED","REJECTED"):
            with self.subTest(state=state):
                with sqlite3.connect(self.path) as db:
                    db.execute("UPDATE exit_orders SET state=? WHERE cid='EXIT_A'",(state,))
                self.assertIsNone(self.claim())
    def test_contradictory_broker_evidence_blocks(self):
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE exit_orders SET broker_id='BROKER_A' WHERE cid='EXIT_A'")
        with self.assertRaises(module.ClaimError):self.claim()
        self.assertEqual(self.state()[0],"INTENT")

if __name__=="__main__":unittest.main()
