import sqlite3
import tempfile
import unittest
from pathlib import Path

from vault import EncryptedDatabase, VaultError, create_key_record, unlock_key


class VaultTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source.db"
        connection = sqlite3.connect(self.source)
        try:
            connection.execute("CREATE TABLE sample (value TEXT NOT NULL)")
            connection.execute("INSERT INTO sample VALUES ('private data')")
            connection.commit()
        finally:
            connection.close()

    def tearDown(self):
        self.temporary.cleanup()

    def test_round_trip_and_persistence(self):
        record, key = create_key_record("a long test password")
        database = EncryptedDatabase(self.root / "finance.vault")
        database.create(key, self.source)
        database.unlock(unlock_key("a long test password", record))

        with database.connection() as connection:
            self.assertEqual(
                connection.execute("SELECT value FROM sample").fetchone()[0],
                "private data",
            )
            connection.execute("INSERT INTO sample VALUES ('second value')")

        database.lock()
        database.unlock(unlock_key("a long test password", record))
        with database.connection() as connection:
            count = connection.execute("SELECT COUNT(*) FROM sample").fetchone()[0]
        self.assertEqual(count, 2)
        self.assertNotIn(b"private data", database.path.read_bytes())
        database.lock()

    def test_blank_database_can_be_created(self):
        record, key = create_key_record("a blank database password")
        database = EncryptedDatabase(self.root / "blank.vault")
        database.create(key)
        database.unlock(unlock_key("a blank database password", record))

        with database.connection() as connection:
            tables = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        self.assertEqual(tables, [])
        database.lock()

    def test_wrong_password_does_not_unwrap_key(self):
        record, _ = create_key_record("correct password")
        with self.assertRaises(VaultError):
            unlock_key("wrong password", record)

    def test_tampering_is_detected(self):
        _, key = create_key_record("password")
        database = EncryptedDatabase(self.root / "finance.vault")
        database.create(key, self.source)
        payload = bytearray(database.path.read_bytes())
        payload[-1] ^= 1
        database.path.write_bytes(payload)
        with self.assertRaises(VaultError):
            database.unlock(key)


if __name__ == "__main__":
    unittest.main()
