"""Transactional event log. State is replayed, not separately mutable."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3

from .config import canonical, digest

STATE_ROOT = Path(__file__).resolve().parents[3] / "workstreams" / "build" / "state"


class Journal:
    def __init__(self, directory, config, reducer, initial, *, schema="draft/1", fault=None):
        # Restrict this first offline slice to the Build-owned basketball state tree.
        anchor = STATE_ROOT.absolute()
        if anchor.resolve() != anchor:
            raise ValueError("state anchor must not redirect outside the project")
        directory = Path(directory).resolve()
        if not directory.is_relative_to(anchor):
            raise ValueError("state path outside basketball Build root")
        path = directory / (config.run_id + ".sqlite3")
        if path.resolve() != path or (path.exists() and path.is_symlink()):
            raise ValueError("redirected journal path")
        directory.mkdir(parents=True, exist_ok=True)
        self.path, self.config = path, config
        self.reducer, self.initial, self.fault = reducer, initial, fault
        self.binding = canonical({"schema": schema, "config": config.hash, "experiment": config.experiment_id})
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables:
                if tables != {"binding", "events"}:
                    raise ValueError("foreign database schema")
                self._binding(db)
            else:
                db.execute("CREATE TABLE binding (value TEXT NOT NULL)")
                db.execute("INSERT INTO binding VALUES (?)", (self.binding,))
                db.execute("CREATE TABLE events (sequence INTEGER PRIMARY KEY, document TEXT NOT NULL, hash TEXT NOT NULL)")
                for table in ("binding", "events"):
                    for operation in ("UPDATE", "DELETE"):
                        db.execute(f"CREATE TRIGGER immutable_{table}_{operation} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT, 'immutable journal'); END")
            self._replay(db)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.execute("PRAGMA synchronous=FULL")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _binding(self, db):
        if db.execute("SELECT value FROM binding").fetchall() != [(self.binding,)]:
            raise ValueError("experiment or configuration changed")

    def _replay(self, db, events=None):
        self._binding(db)
        state = self.initial()
        previous, count = None, 0
        for sequence, document, checksum in db.execute("SELECT sequence, document, hash FROM events ORDER BY sequence"):
            count += 1
            event = json.loads(document)
            if (sequence != count or event["sequence"] != count or event["previous"] != previous
                    or event["config"] != self.config.hash or digest(event) != checksum):
                raise ValueError("journal integrity failure")
            self.reducer(state, event["kind"], event["data"], self.config)
            if events is not None:
                events.append(event)
            previous = checksum
        return state, previous, count

    def read(self):
        with self.connection() as db:
            db.execute("BEGIN")
            return self._replay(db)[0]

    def read_with_events(self):
        """One verified snapshot for an observational projection, never a cache."""
        with self.connection() as db:
            db.execute("BEGIN")
            events = []
            state = self._replay(db, events)[0]
            return state, events

    @contextmanager
    def guard(self):
        """Exclude event writers during a short external startup decision.

        The yielded replay is read-only evidence, not a mutable saved state.
        Do not wait for an external response or append events inside this gate.
        """
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            yield self._replay(db)[0]

    def transact(self, decide):
        """decide sees fresh state under a writer lock and returns zero or more events."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            state, previous, count = self._replay(db)
            for kind, data in decide(state):
                self.reducer(state, kind, data, self.config)
                count += 1
                event = {"sequence": count, "previous": previous, "config": self.config.hash, "kind": kind, "data": data}
                previous = digest(event)
                db.execute("INSERT INTO events VALUES (?, ?, ?)", (count, canonical(event), previous))
                if self.fault:
                    self.fault("before_commit:" + kind)
        return state
