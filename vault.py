import base64
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id


# Stable identifiers retained so existing encrypted vaults remain readable.
MAGIC = b"SONDER-VAULT-1\n"
DATABASE_AAD = b"sonder-budget-database-v1"
KEY_AAD = b"sonder-budget-key-v1"
ARGON2_MEMORY_KIB = 64 * 1024
ARGON2_ITERATIONS = 1
ARGON2_LANES = 4


class VaultError(Exception):
    pass


def _encode(value):
    return base64.urlsafe_b64encode(value).decode("ascii")


def _decode(value):
    return base64.urlsafe_b64decode(value.encode("ascii"))


def _derive_key(password, salt, *, memory_cost, iterations, lanes):
    return Argon2id(
        salt=salt,
        length=32,
        iterations=iterations,
        lanes=lanes,
        memory_cost=memory_cost,
    ).derive(password.encode("utf-8"))


def create_key_record(password):
    salt = os.urandom(16)
    wrapping_key = _derive_key(
        password,
        salt,
        memory_cost=ARGON2_MEMORY_KIB,
        iterations=ARGON2_ITERATIONS,
        lanes=ARGON2_LANES,
    )
    data_key = AESGCM.generate_key(bit_length=256)
    nonce = os.urandom(12)
    wrapped_key = AESGCM(wrapping_key).encrypt(nonce, data_key, KEY_AAD)
    return (
        {
            "version": 1,
            "kdf": "argon2id",
            "salt": _encode(salt),
            "memory_cost": ARGON2_MEMORY_KIB,
            "iterations": ARGON2_ITERATIONS,
            "lanes": ARGON2_LANES,
            "nonce": _encode(nonce),
            "wrapped_key": _encode(wrapped_key),
        },
        data_key,
    )


def unlock_key(password, record):
    if record.get("version") != 1 or record.get("kdf") != "argon2id":
        raise VaultError("This vault uses an unsupported key format.")
    try:
        wrapping_key = _derive_key(
            password,
            _decode(record["salt"]),
            memory_cost=int(record["memory_cost"]),
            iterations=int(record["iterations"]),
            lanes=int(record["lanes"]),
        )
        return AESGCM(wrapping_key).decrypt(
            _decode(record["nonce"]),
            _decode(record["wrapped_key"]),
            KEY_AAD,
        )
    except (InvalidTag, KeyError, TypeError, ValueError) as error:
        raise VaultError("The vault could not be unlocked.") from error


class EncryptedDatabase:
    def __init__(self, path):
        self.path = Path(path)
        self._connection = None
        self._key = None
        self._lock = threading.RLock()

    @property
    def exists(self):
        return self.path.exists()

    @property
    def unlocked(self):
        return self._connection is not None and self._key is not None

    def create(self, key, source_path=None):
        if source_path:
            source = Path(source_path)
            raw_database = source.read_bytes()
            self._validate_database(raw_database)
        else:
            connection = sqlite3.connect(":memory:")
            try:
                connection.execute("CREATE TABLE __vault_initialization (value INTEGER)")
                connection.execute("DROP TABLE __vault_initialization")
                connection.commit()
                raw_database = connection.serialize()
            finally:
                connection.close()
        self._write_encrypted(key, raw_database)
        self._verify_encrypted(key)

    def unlock(self, key):
        with self._lock:
            raw_database = self._decrypt(key)
            connection = sqlite3.connect(":memory:", check_same_thread=False)
            try:
                connection.deserialize(raw_database)
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys = ON")
                result = connection.execute("PRAGMA integrity_check").fetchone()[0]
                if result != "ok":
                    raise VaultError(f"Database integrity check failed: {result}")
            except Exception:
                connection.close()
                raise
            self.lock()
            self._key = key
            self._connection = connection

    def lock(self):
        with self._lock:
            if self._connection is not None:
                self._connection.close()
            self._connection = None
            self._key = None

    @contextmanager
    def connection(self):
        with self._lock:
            if not self.unlocked:
                raise VaultError("Unlock the app before accessing its data.")
            before = self._connection.total_changes
            try:
                yield self._connection
            except Exception:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()
                if self._connection.total_changes != before:
                    self.persist()

    def persist(self):
        with self._lock:
            if not self.unlocked:
                raise VaultError("The vault is locked.")
            self._write_encrypted(self._key, self._connection.serialize())

    def _decrypt(self, key):
        payload = self.path.read_bytes()
        if not payload.startswith(MAGIC) or len(payload) <= len(MAGIC) + 12:
            raise VaultError("The vault file is not valid.")
        nonce_start = len(MAGIC)
        nonce = payload[nonce_start : nonce_start + 12]
        ciphertext = payload[nonce_start + 12 :]
        try:
            return AESGCM(key).decrypt(nonce, ciphertext, DATABASE_AAD)
        except InvalidTag as error:
            raise VaultError("The vault is damaged or the key is incorrect.") from error

    def _write_encrypted(self, key, raw_database):
        nonce = os.urandom(12)
        ciphertext = AESGCM(key).encrypt(nonce, raw_database, DATABASE_AAD)
        payload = MAGIC + nonce + ciphertext
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            with temporary.open("wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _verify_encrypted(self, key):
        self._validate_database(self._decrypt(key))

    @staticmethod
    def _validate_database(raw_database):
        connection = sqlite3.connect(":memory:")
        try:
            connection.deserialize(raw_database)
            result = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise VaultError(f"Database integrity check failed: {result}")
        except sqlite3.DatabaseError as error:
            raise VaultError("The source database is not valid.") from error
        finally:
            connection.close()
