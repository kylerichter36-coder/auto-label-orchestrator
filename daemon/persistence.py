import os
import json
import time
import uuid
import errno
import sqlite3
import logging
import threading
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, List, Optional, Union, Any

from daemon.config import config

logger = logging.getLogger("Persistence")

class PersistenceManager:
    """
    Non-volatile transaction logging and queue persistence engine.
    Ensures power-loss resilience, atomic file writes (.tmp -> fsync -> os.replace),
    structured transaction audit logging (JSONL + SQLite), and graceful fallback
    between the 64GB MicroSD card (/mnt/sdcard_backup) and local non-volatile storage.
    """

    def __init__(
        self,
        sd_mount: Optional[Union[str, Path]] = None,
        fallback_dir: Optional[Union[str, Path]] = None
    ):
        self._lock = threading.RLock()
        self.sd_mount = Path(sd_mount) if sd_mount else config.SDCARD_MOUNT
        self.sdcard_mount = self.sd_mount
        self.fallback_dir = Path(fallback_dir) if fallback_dir else config.FALLBACK_STORAGE_DIR

        # Ensure fallback directory exists
        try:
            self.fallback_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.warning(f"Could not create fallback directory {self.fallback_dir}: {e}")

        # Ensure directory hierarchy on active storage
        self.ensure_topology()

    def is_sdcard_mounted(self) -> bool:
        """
        Validates whether the 64GB MicroSD card mount exists, is accessible, and is writable.
        Returns False if unmounted, missing, or remounted as read-only (EROFS).
        """
        mount_dir = getattr(self, "sdcard_mount", self.sd_mount)
        mount_path = Path(mount_dir)
        if not mount_path.exists() or not mount_path.is_dir():
            return False

        # Verify filesystem is writable and not remounted read-only (EROFS)
        probe_file = os.path.join(
            mount_path,
            f".sdcard_write_probe_{os.getpid()}_{threading.get_ident()}_{uuid.uuid4().hex[:6]}"
        )
        try:
            with open(probe_file, "w", encoding="utf-8") as f:
                f.write("ok")
            return True
        except Exception as e:
            logger.warning(f"MicroSD card mount {mount_path} is not writable ({e}). Using local fallback.")
            return False
        finally:
            try:
                if os.path.exists(probe_file):
                    os.remove(probe_file)
            except Exception:
                pass

    def get_storage_root(self) -> Path:
        """Returns the primary storage directory if available, otherwise the local fallback."""
        if self.is_sdcard_mounted():
            return self.sd_mount
        return self.fallback_dir

    def get_queues_file_path(self) -> Path:
        """Returns the canonical path to active_queues.json on the active storage partition."""
        return self.get_storage_root() / "active_queues.json"

    def get_audit_dir(self) -> Path:
        """Returns the audit_logs directory on the active storage partition."""
        audit_path = self.get_storage_root() / "audit_logs"
        try:
            audit_path.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return audit_path

    def ensure_topology(self, base_dir: Optional[Path] = None) -> None:
        """
        Creates the standardized directory hierarchy:
        - snapshots/
        - audit_logs/ (scans/, prints/)
        - templates_archive/ (golden_master/, custom_user_templates/)
        - rollback_recovery/
        """
        target_root = base_dir if base_dir is not None else self.get_storage_root()
        subdirs = [
            target_root / "snapshots",
            target_root / "audit_logs",
            target_root / "audit_logs" / "scans",
            target_root / "audit_logs" / "prints",
            target_root / "templates_archive",
            target_root / "templates_archive" / "golden_master",
            target_root / "templates_archive" / "custom_user_templates",
            target_root / "rollback_recovery"
        ]
        for d in subdirs:
            try:
                d.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                logger.debug(f"Could not create topology directory {d}: {e}")

    def save_active_queues(
        self,
        queue_mgr_or_data: Any,
        target_file: Optional[Path] = None
    ) -> bool:
        """
        Atomically saves active print queues and outer box sheets to active_queues.json.
        Follows the atomic write protocol (.tmp -> flush -> fsync -> os.replace).
        If the primary destination fails with an OSError (e.g. EROFS), it falls back
        gracefully to local non-volatile storage.
        """
        with self._lock:
            dest_file = target_file if target_file is not None else self.get_queues_file_path()

            # Format payload
            payload = self._serialize_queue_state(queue_mgr_or_data)

            # Attempt write to target file
            success = self._atomic_write_json(dest_file, payload)
            if success:
                return True

            # If writing to primary failed and target was on SD card, attempt fallback
            if target_file is None and self.sd_mount in dest_file.parents:
                fallback_file = self.fallback_dir / "active_queues.json"
                logger.warning(f"Primary queue save failed. Attempting fallback to {fallback_file}")
                return self._atomic_write_json(fallback_file, payload)

            return False

    def load_active_queues(
        self,
        queue_mgr: Any = None,
        source_file: Optional[Path] = None
    ) -> Dict[str, Any]:
        """
        Recovers active queue states and outer boxes from non-volatile storage.
        Cleans up or ignores corrupt/partial .tmp files.
        If queue_mgr is provided, restores jobs and counters directly into it.
        """
        with self._lock:
            files_to_try = []
            if source_file is not None:
                files_to_try.append(Path(source_file))
            else:
                files_to_try.append(self.get_queues_file_path())
                files_to_try.append(self.fallback_dir / "active_queues.json")

            data: Optional[Dict[str, Any]] = None

            for path in files_to_try:
                if not path.exists():
                    continue

                try:
                    with open(path, "r", encoding="utf-8") as f:
                        parsed = json.load(f)
                        if isinstance(parsed, dict) and "queues" in parsed:
                            data = parsed
                            logger.info(f"Loaded active queues state from {path}")
                            break
                except (json.JSONDecodeError, OSError) as e:
                    logger.warning(f"Could not load state from {path}: {e}")

            if data is None:
                # Return empty initialized structure
                data = {
                    "version": 1,
                    "timestamp": time.time(),
                    "job_counter": 0,
                    "queues": {
                        "BLACK": [],
                        "CYAN": [],
                        "MAGENTA": [],
                        "YELLOW": [],
                        "OTHER": []
                    },
                    "outer_boxes": []
                }

            # If a queue manager instance was provided, restore state
            if queue_mgr is not None:
                self._deserialize_into_queue_mgr(queue_mgr, data)

            return data

    def log_transaction(
        self,
        event_type: str,
        details: Dict[str, Any],
        quote_number: Optional[str] = None,
        timestamp: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        Appends a structured transaction record to the monthly audit JSONL log
        and the SQLite transaction database. Operations are thread-safe and fsync'd.
        """
        with self._lock:
            ts = timestamp if timestamp is not None else time.time()
            iso_ts = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
            tx_id = f"TX-{int(ts * 1000)}-{uuid.uuid4().hex[:6]}"

            record = {
                "transaction_id": tx_id,
                "timestamp": ts,
                "iso_timestamp": iso_ts,
                "event": event_type,
                "quote_number": quote_number,
                "details": details
            }

            audit_dir = self.get_audit_dir()
            month_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y%m")
            jsonl_file = audit_dir / f"transactions_{month_str}.jsonl"

            # 1. Append to JSONL with fsync
            try:
                with open(jsonl_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps(record) + "\n")
                    f.flush()
                    os.fsync(f.fileno())
            except OSError as e:
                logger.warning(f"Error appending to JSONL audit log {jsonl_file}: {e}")
                # Fallback to local fallback dir if needed
                if self.sd_mount in jsonl_file.parents:
                    fallback_jsonl = self.fallback_dir / "audit_logs" / f"transactions_{month_str}.jsonl"
                    try:
                        fallback_jsonl.parent.mkdir(parents=True, exist_ok=True)
                        with open(fallback_jsonl, "a", encoding="utf-8") as f:
                            f.write(json.dumps(record) + "\n")
                            f.flush()
                            os.fsync(f.fileno())
                    except Exception as fb_err:
                        logger.error(f"Fallback JSONL logging failed: {fb_err}")

            # 2. Append to SQLite audit database
            db_file = audit_dir / "audit.db"
            try:
                self._insert_sqlite_transaction(db_file, record)
            except Exception as e:
                logger.debug(f"SQLite audit insert notice: {e}")

            return record

    def query_transactions(
        self,
        event_type: Optional[str] = None,
        quote_number: Optional[str] = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        Queries transaction records matching the optional filters.
        Tries SQLite first for efficiency, then falls back to JSONL parsing.
        """
        with self._lock:
            audit_dir = self.get_audit_dir()
            db_file = audit_dir / "audit.db"

            if db_file.exists():
                try:
                    return self._query_sqlite_transactions(db_file, event_type, quote_number, limit)
                except Exception as e:
                    logger.debug(f"SQLite query failed, falling back to JSONL: {e}")

            # Fallback: scan JSONL files in reverse order
            results = []
            jsonl_files = sorted(audit_dir.glob("transactions_*.jsonl"), reverse=True)
            for jf in jsonl_files:
                try:
                    with open(jf, "r", encoding="utf-8") as f:
                        lines = f.readlines()
                    for line in reversed(lines):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                            if event_type and rec.get("event") != event_type:
                                continue
                            if quote_number and rec.get("quote_number") != quote_number:
                                continue
                            results.append(rec)
                            if len(results) >= limit:
                                return results
                        except json.JSONDecodeError:
                            continue
                except OSError:
                    continue

            return results

    def prune_snapshots(
        self,
        snapshot_dir: Optional[Path] = None,
        keep_count: int = 5
    ) -> List[Path]:
        """
        Enforces snapshot retention limit: retains the newest `keep_count` snapshots
        and deletes older archive files.
        """
        with self._lock:
            target_dir = snapshot_dir if snapshot_dir is not None else (self.get_storage_root() / "snapshots")
            if not target_dir.exists():
                return []

            snapshots = sorted(
                target_dir.glob("snapshot_*.tar.gz"),
                key=lambda p: p.stat().st_mtime
            )
            deleted: List[Path] = []
            if len(snapshots) > keep_count:
                to_delete = snapshots[:-keep_count]
                for p in to_delete:
                    try:
                        p.unlink()
                        deleted.append(p)
                        logger.info(f"Pruned old snapshot: {p.name}")
                    except OSError as e:
                        logger.warning(f"Could not prune snapshot {p}: {e}")

            return deleted

    # -------------------------------------------------------------------------
    # Internal Helpers
    # -------------------------------------------------------------------------

    def _atomic_write_json(self, dest_path: Path, data: Dict[str, Any]) -> bool:
        """
        Executes atomic write protocol: writes to .tmp file, fsyncs, and replaces destination.
        Handles EROFS and filesystem errors cleanly.
        """
        tmp_path = dest_path.parent / f".{dest_path.name}.tmp"
        try:
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())

            os.replace(tmp_path, dest_path)
            return True
        except OSError as e:
            logger.warning(f"Atomic write error for {dest_path}: {e}")
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except Exception:
                    pass
            return False

    def _serialize_queue_state(self, queue_mgr_or_data: Any) -> Dict[str, Any]:
        """Normalizes queue manager or dictionary data into a serializable dict."""
        if hasattr(queue_mgr_or_data, "to_dict"):
            base = queue_mgr_or_data.to_dict()
            return {
                "version": 1,
                "timestamp": time.time(),
                "job_counter": base.get("job_counter", 0),
                "queues": base.get("queues", {}),
                "outer_boxes": base.get("pending_outer_boxes", base.get("outer_boxes", []))
            }
        elif hasattr(queue_mgr_or_data, "queues") and hasattr(queue_mgr_or_data, "pending_outer_boxes"):
            queues_dict = {}
            for color, jobs in queue_mgr_or_data.queues.items():
                queues_dict[color] = [
                    j.model_dump() if hasattr(j, "model_dump") else (j.dict() if hasattr(j, "dict") else j)
                    for j in jobs
                ]
            outer_boxes = [
                b.model_dump() if hasattr(b, "model_dump") else (b.dict() if hasattr(b, "dict") else b)
                for b in queue_mgr_or_data.pending_outer_boxes
            ]
            return {
                "version": 1,
                "timestamp": time.time(),
                "job_counter": getattr(queue_mgr_or_data, "job_counter", 0),
                "queues": queues_dict,
                "outer_boxes": outer_boxes
            }
        elif isinstance(queue_mgr_or_data, dict):
            res = dict(queue_mgr_or_data)
            res.setdefault("version", 1)
            res.setdefault("timestamp", time.time())
            res.setdefault("job_counter", 0)
            res.setdefault("queues", {})
            res.setdefault("outer_boxes", [])
            return res
        else:
            return {
                "version": 1,
                "timestamp": time.time(),
                "job_counter": 0,
                "queues": {},
                "outer_boxes": []
            }

    def _deserialize_into_queue_mgr(self, queue_mgr: Any, data: Dict[str, Any]) -> None:
        """Restores serialized data back into a ColorQueueManager instance."""
        from daemon.color_queue import QueuedPrintJob
        from daemon.classifier import OuterBoxSheet

        # Clear existing entries without triggering redundant persistence re-write
        if hasattr(queue_mgr, "queues") and isinstance(queue_mgr.queues, dict):
            for c in queue_mgr.queues:
                queue_mgr.queues[c].clear()
        if hasattr(queue_mgr, "pending_outer_boxes") and isinstance(queue_mgr.pending_outer_boxes, list):
            queue_mgr.pending_outer_boxes.clear()

        # Restore color queues
        raw_queues = data.get("queues", {})
        for color, jobs in raw_queues.items():
            color_upper = color.upper()
            if color_upper not in queue_mgr.queues:
                queue_mgr.queues[color_upper] = []
            for j in jobs:
                try:
                    if isinstance(j, dict):
                        job_obj = QueuedPrintJob.model_validate(j)
                    else:
                        job_obj = j
                    queue_mgr.queues[color_upper].append(job_obj)
                except Exception as e:
                    logger.warning(f"Could not restore queued job: {e}")

        # Restore outer boxes
        raw_boxes = data.get("outer_boxes", data.get("pending_outer_boxes", []))
        for b in raw_boxes:
            try:
                if isinstance(b, dict):
                    box_obj = OuterBoxSheet.model_validate(b)
                else:
                    box_obj = b
                queue_mgr.pending_outer_boxes.append(box_obj)
            except Exception as e:
                logger.warning(f"Could not restore outer box: {e}")

        # Restore counter
        if "job_counter" in data and hasattr(queue_mgr, "job_counter"):
            queue_mgr.job_counter = int(data["job_counter"])

    def _insert_sqlite_transaction(self, db_path: Path, record: Dict[str, Any]) -> None:
        """Appends a record to SQLite audit database."""
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        try:
            with conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS transactions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        transaction_id TEXT UNIQUE,
                        timestamp REAL,
                        iso_timestamp TEXT,
                        event TEXT,
                        quote_number TEXT,
                        details_json TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT OR REPLACE INTO transactions (
                        transaction_id, timestamp, iso_timestamp, event, quote_number, details_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record["transaction_id"],
                        record["timestamp"],
                        record["iso_timestamp"],
                        record["event"],
                        record["quote_number"],
                        json.dumps(record["details"])
                    )
                )
        finally:
            conn.close()

    def _query_sqlite_transactions(
        self,
        db_path: Path,
        event_type: Optional[str],
        quote_number: Optional[str],
        limit: int
    ) -> List[Dict[str, Any]]:
        """Queries records from SQLite audit database."""
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        try:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            query = "SELECT transaction_id, timestamp, iso_timestamp, event, quote_number, details_json FROM transactions"
            clauses = []
            params: List[Any] = []

            if event_type:
                clauses.append("event = ?")
                params.append(event_type)
            if quote_number:
                clauses.append("quote_number = ?")
                params.append(quote_number)

            if clauses:
                query += " WHERE " + " AND ".join(clauses)

            query += " ORDER BY timestamp DESC LIMIT ?"
            params.append(limit)

            cursor.execute(query, params)
            rows = cursor.fetchall()

            records = []
            for r in rows:
                try:
                    details = json.loads(r["details_json"])
                except Exception:
                    details = {}
                records.append({
                    "transaction_id": r["transaction_id"],
                    "timestamp": r["timestamp"],
                    "iso_timestamp": r["iso_timestamp"],
                    "event": r["event"],
                    "quote_number": r["quote_number"],
                    "details": details
                })
            return records
        finally:
            conn.close()


# Module-level singleton
_default_manager: Optional[PersistenceManager] = None
_manager_lock = threading.Lock()

def get_persistence_manager() -> PersistenceManager:
    """Returns the process-wide PersistenceManager singleton."""
    global _default_manager
    with _manager_lock:
        if _default_manager is None:
            _default_manager = PersistenceManager()
        return _default_manager

def save_active_queues(queue_mgr_or_data: Any, target_file: Optional[Path] = None) -> bool:
    """Module-level helper to save active queues."""
    return get_persistence_manager().save_active_queues(queue_mgr_or_data, target_file)

def load_active_queues(queue_mgr: Any = None, source_file: Optional[Path] = None) -> Dict[str, Any]:
    """Module-level helper to load active queues."""
    return get_persistence_manager().load_active_queues(queue_mgr, source_file)

def log_transaction(
    event_type: str,
    details: Dict[str, Any],
    quote_number: Optional[str] = None,
    timestamp: Optional[float] = None
) -> Dict[str, Any]:
    """Module-level helper to log an audit transaction."""
    return get_persistence_manager().log_transaction(event_type, details, quote_number, timestamp)

def query_transactions(
    event_type: Optional[str] = None,
    quote_number: Optional[str] = None,
    limit: int = 100
) -> List[Dict[str, Any]]:
    """Module-level helper to query audit transactions."""
    return get_persistence_manager().query_transactions(event_type, quote_number, limit)

def create_sdcard_directory_topology(base_dir: Optional[Path] = None) -> None:
    """Module-level helper to ensure the standardized directory topology."""
    get_persistence_manager().ensure_topology(base_dir)

def prune_snapshots(snapshot_dir: Optional[Path] = None, keep_count: int = 5) -> List[Path]:
    """Enforces snapshot retention limit, pruning older snapshots beyond keep_count."""
    return get_persistence_manager().prune_snapshots(snapshot_dir, keep_count)
