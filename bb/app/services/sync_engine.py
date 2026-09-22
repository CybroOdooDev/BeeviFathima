"""The sync engine: device punches -> Odoo hr.attendance.

    1 fetch      pull punches from each source since its cursor, minus an overlap
    2 ingest     upsert into the ledger — replay-safe by construction
    3 normalise  local wall-clock -> naive UTC
    4 register   record every badge seen, before Odoo is involved at all
    5 map        emp_code -> hr.employee, cached on the mapping row
    6 pair       punch stream -> intervals, carrying the open shift forward
    7 push       create and close hr.attendance within Odoo's constraints
    8 record     ledger state, local mirror, run counters

Two invariants make a run safe to interrupt:

* The ledger is keyed on the vendor's own id scoped to its source, so ingesting
  the same window twice is a no-op.
* The cursor advances only *after* punches are stored, and only to the newest
  punch actually seen. A crash costs a retry, never a gap.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.crypto import encrypt
from app.integrations.base import ProviderError
from app.integrations.odoo import OdooClient, OdooError, parse_dt
from app.models import (
    AttendanceRecord,
    ConnectionStatus,
    Device,
    DeviceSource,
    Direction,
    EmployeeMapping,
    MappingStatus,
    OdooConnection,
    PunchRecord,
    PunchState,
    SyncRun,
    SyncStatus,
    Tenant,
)
from app.services.connections import (
    UnsafeTargetError,
    build_odoo_client,
    build_source_provider,
)
from app.services.pairing import (
    Direction as PairDirection,
    OpenShift,
    PairingConfig,
    PairingMode,
    Punch,
    close_stale_at,
    pair_punches,
)
from app.services.timeutils import local_to_utc, utc_to_local, utcnow_naive

log = logging.getLogger(__name__)

#: How many fetched punches to hold before asking the ledger about them. Large
#: enough that the round trips are few, small enough that the IN list stays an
#: index seek and the buffer stays negligible.
INGEST_BATCH = 500

#: Bound parameters per lookup. SQLite's older default ceiling is 999.
ID_LOOKUP_CHUNK = 500


class SyncAborted(Exception):
    """A configuration problem that makes a run impossible — not an outage.

    Kept distinct because it must not count towards the failure streak: a tenant
    who has not finished onboarding should not be slow-laned or badged degraded.
    """


class AllSourcesUnreachable(SyncAborted):
    """Every configured platform failed. An outage, and it must be counted.

    A subclass because the *run* ends the same way — nothing to do, stop early —
    but the *cause* is the opposite of a configuration problem. Without this it
    inherited SyncAborted's exemption from the failure streak, so a customer
    whose BioTime server was switched off for good would never be badged
    degraded and BioBridge would poll a dead address on the fast lane forever.
    """


class SyncEngine:
    """Runs one full cycle for one tenant."""

    def __init__(self, db: Session, tenant: Tenant, triggered_by: str = "schedule") -> None:
        self.db = db
        self.tenant = tenant
        # Held locally as well as on the row: the run commits mid-cycle, which
        # expires the ORM object, and a reloaded value can come back naive.
        self._started_at = datetime.now(timezone.utc)
        self.run = SyncRun(
            tenant_id=tenant.id,
            status=SyncStatus.running.value,
            triggered_by=triggered_by,
            started_at=self._started_at,
            log=[],
        )
        self._log_lines: list[str] = []

    # -- logging -----------------------------------------------------------
    def _log(self, message: str, level: str = "info") -> None:
        stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self._log_lines.append(f"[{stamp}] {level.upper()}: {message}")
        getattr(log, level, log.info)("[tenant=%s] %s", self.tenant.slug, message)

    def pairing_config(self, mode: str | None = None) -> PairingConfig:
        return PairingConfig(
            mode=PairingMode(mode or self.tenant.pairing_mode),
            day_boundary_hour=self.tenant.day_boundary_hour,
            min_punch_interval_seconds=self.tenant.min_punch_interval_seconds,
            max_shift_hours=self.tenant.max_shift_hours,
            orphan_out_policy=self.tenant.orphan_out_policy,
        )

    # -- entry point -------------------------------------------------------
    def run_cycle(self) -> SyncRun:
        self.db.add(self.run)
        self.db.flush()

        try:
            # The subscription gate, as a backstop.
            #
            # The scheduler filters on this and the console's own button checks
            # it, but the customer's "Sync now" did not — so a suspended
            # account could press it and the run went through. Putting the
            # check here as well means the gate holds for every caller,
            # including any route added later that forgets: there is exactly one
            # place a cycle can start.
            #
            # SyncAborted rather than a bare raise: it records a run saying why,
            # visible to the customer and to staff, and is exempt from the
            # failure streak — a stopped account is not a failing one and must
            # not be badged degraded or slow-laned for it.
            if not self.tenant.syncable:
                raise SyncAborted(
                    f"This account is {self.tenant.status} and does not sync. "
                    f"Nothing has been lost: the cursor stays where it is and "
                    f"the next run picks up from there."
                )

            odoo_conn = self._active_odoo_connection()
            sources = self._active_sources()
            if not sources:
                raise SyncAborted("No attendance platform is connected.")

            # Ingest first, and independently of Odoo. Device platforms prune old
            # transactions, so a punch not captured now may be gone by the next
            # run. Everything lands in the ledger as pending and is pushed once
            # Odoo is reachable: an Odoo outage costs a delay, never data.
            #
            # Each source is isolated. With several sites, letting one failure
            # propagate would skip every other site's punches — data loss
            # disguised as a failed run.
            reachable = 0
            for source in sources:
                try:
                    self._fetch_and_ingest(source)
                    reachable += 1
                except (ProviderError, UnsafeTargetError) as exc:
                    source.status = ConnectionStatus.failed.value
                    source.status_message = str(exc)[:500]
                    source.last_checked_at = datetime.now(timezone.utc)
                    self.run.error_count += 1
                    self._log(f"'{source.name}' unreachable: {exc}", "error")

            if reachable == 0:
                # The run list shows this message and nothing else, so it has to
                # carry the reason — "none could be reached" tells a customer
                # only what they already suspected. The per-source handler above
                # has just written a specific, actionable message; reuse it
                # rather than inventing a vaguer one.
                reasons = [s.status_message for s in sources if s.status_message]
                if len(sources) == 1 and reasons:
                    detail = reasons[0]          # one site: its message *is* the answer
                elif reasons:
                    detail = (
                        f"None of the {len(sources)} connected platforms could be "
                        f"reached. First: {reasons[0]}"
                    )
                else:
                    detail = f"None of the {len(sources)} connected platforms could be reached."
                raise AllSourcesUnreachable(
                    f"{detail} Punches already in the ledger are unaffected."
                )

            # Knowing *which badges punched* needs no Odoo — it is in the punch
            # stream. Registering them now means the Employees page lists
            # everyone awaiting a match even while Odoo is down.
            self._register_badges()
            self.db.commit()

            if odoo_conn is None:
                raise SyncAborted(
                    "Punches captured, but no active Odoo connection is configured. "
                    "Connect Odoo and they will be pushed on the next run."
                )

            odoo = build_odoo_client(self.tenant, odoo_conn)
            odoo.authenticate()
            if odoo_conn.uid_cache != odoo.uid:
                odoo_conn.uid_cache = odoo.uid

            self._resolve_mappings(odoo)
            self._push(odoo, odoo_conn)

            self.tenant.consecutive_failures = 0
            odoo_conn.status = ConnectionStatus.connected.value
            odoo_conn.last_checked_at = datetime.now(timezone.utc)
            self.run.status = (
                SyncStatus.partial.value if self.run.error_count else SyncStatus.success.value
            )

        except AllSourcesUnreachable as exc:
            # Ordered before SyncAborted: it is a subclass, so the broader
            # handler below would swallow it and skip the streak.
            self.tenant.consecutive_failures += 1
            self.run.status = SyncStatus.failed.value
            self.run.error_message = str(exc)
            self._log(str(exc), "error")
            self._mark_degraded_if_needed()
        except SyncAborted as exc:
            self.run.status = SyncStatus.failed.value
            self.run.error_message = str(exc)
            self._log(str(exc), "warning")
        except (ProviderError, OdooError) as exc:
            self.tenant.consecutive_failures += 1
            self.run.status = SyncStatus.failed.value
            self.run.error_message = str(exc)
            self._log(str(exc), "error")
            self._mark_degraded_if_needed()
        except Exception as exc:  # noqa: BLE001 — a worker must never die silently
            self.tenant.consecutive_failures += 1
            self.run.status = SyncStatus.failed.value
            self.run.error_message = f"Unexpected error: {exc}"
            self._log(f"Unexpected error: {exc}", "exception")
            self._mark_degraded_if_needed()
        finally:
            finished = datetime.now(timezone.utc)
            self.run.finished_at = finished
            self.run.duration_ms = int((finished - self._started_at).total_seconds() * 1000)
            self.run.log = self._log_lines[-500:]
            self.db.commit()

        return self.run

    # -- stages 1-3 --------------------------------------------------------
    def _fetch_and_ingest(self, source: DeviceSource) -> None:
        overlap = timedelta(minutes=settings.fetch_overlap_minutes)
        floor = utcnow_naive() - timedelta(days=settings.backfill_limit_days)

        cursor = source.cursor_punch_time or floor
        start_utc = max(cursor - overlap, floor)
        end_utc = utcnow_naive() + timedelta(minutes=5)  # tolerate device clock skew

        self.run.cursor_from = start_utc
        self.run.cursor_to = end_utc

        tz = source.server_timezone or self.tenant.timezone
        start_local = utc_to_local(start_utc, tz)
        end_local = utc_to_local(end_utc, tz)
        self._log(f"Fetching '{source.name}' {start_local} -> {end_local} ({tz})")

        devices = {
            d.serial_number: d
            for d in self.db.scalars(
                select(Device).where(Device.source_id == source.id)
            ).all()
        }
        enabled = {sn for sn, d in devices.items() if d.is_enabled}

        provider = build_source_provider(self.tenant, source)
        newest = source.cursor_punch_time
        fetched = created = 0

        try:
            # Buffered, so the "have we seen this already?" check can be asked
            # about the handful of punches in hand rather than the whole ledger.
            batch: list[tuple[object, datetime]] = []
            for event in provider.fetch_punches(since=start_local, until=end_local):
                fetched += 1

                # A terminal the customer explicitly disabled is skipped, but an
                # unknown one is kept: a device that appeared since the last
                # import should not silently lose its punches.
                if devices and event.terminal_sn and event.terminal_sn not in enabled:
                    if event.terminal_sn in devices:
                        continue

                punch_utc = local_to_utc(event.punch_time_local, tz)
                if punch_utc < floor:
                    continue  # device clock is wrong, or an ancient backfill

                if newest is None or punch_utc > newest:
                    newest = punch_utc

                batch.append((event, punch_utc))
                if len(batch) >= INGEST_BATCH:
                    created += self._ingest_batch(batch, source, devices)
                    batch = []

            if batch:
                created += self._ingest_batch(batch, source, devices)

            cached = getattr(provider, "cached_token", None)
            if cached:
                source.token_enc = encrypt(cached, self.tenant.crypto_key)

            source.status = ConnectionStatus.connected.value
            source.status_message = None
            source.last_checked_at = datetime.now(timezone.utc)
            # Advanced last, and only over punches actually stored. If the
            # provider raised mid-iteration the cursor stays put, the rows
            # already written remain, and the next run re-reads the same window
            # — which the unique index makes free.
            if newest:
                source.cursor_punch_time = newest
        finally:
            provider.close()

        self.db.flush()
        self.run.punches_fetched += fetched
        self.run.punches_new += created
        self._log(f"'{source.name}': {fetched} punch(es) seen, {created} new")

    def _known_external_ids(
        self, source: DeviceSource, external_ids: Collection[str]
    ) -> set[str]:
        """Which of *these* ids the ledger already holds.

        Scoped to the batch on purpose. It used to load every external id the
        tenant had ever stored into a Python set, once per run — so its cost
        grew with the age of the account rather than with the work in front of
        it. Measured on a 5,000-employee tenant: 0.06 s and 2 MB at 10k rows,
        5.3 s and 232 MB at 1M, for a run that had 67 new punches to ingest. At
        a 1-minute interval across 500 tenants that one query was 44 CPU cores,
        against roughly 1.4 for the actual work, and it grew every day the
        system ran.

        Scoped this way it is an index seek on ``uq_punch_external`` over a few
        hundred values, so the cost tracks the batch and stays flat forever.
        """
        ids = list(dict.fromkeys(external_ids))  # de-dupe, keep order for tests
        if not ids:
            return set()

        found: set[str] = set()
        # Chunked to stay under SQLite's bound-parameter ceiling (999 on older
        # builds). Postgres would take the lot, but the limit has to hold for
        # the smallest deployment, not the largest.
        for start in range(0, len(ids), ID_LOOKUP_CHUNK):
            found.update(
                self.db.scalars(
                    select(PunchRecord.external_id).where(
                        PunchRecord.tenant_id == self.tenant.id,
                        PunchRecord.source_id == source.id,
                        PunchRecord.external_id.in_(ids[start:start + ID_LOOKUP_CHUNK]),
                    )
                ).all()
            )
        return found

    def _ingest_batch(
        self, batch: list[tuple[object, datetime]], source: DeviceSource, devices: dict
    ) -> int:
        """Store the new punches in one buffer, and report how many were new."""
        seen = self._known_external_ids(source, [event.external_id for event, _ in batch])
        created = 0
        for event, punch_utc in batch:
            # The in-batch check matters as much as the database one: the fetch
            # window overlaps the previous run deliberately, and a provider may
            # repeat a row across pages, so the same id can appear twice inside
            # a single batch and would otherwise violate the unique index.
            if event.external_id in seen:
                continue
            seen.add(event.external_id)
            self._ingest(event, source, devices, punch_utc)
            created += 1
        return created

    def _ingest(self, event, source: DeviceSource, devices: dict, punch_utc: datetime) -> None:
        direction = Direction.unknown.value
        if event.direction is True:
            direction = Direction.inward.value
        elif event.direction is False:
            direction = Direction.outward.value

        device = devices.get(event.terminal_sn or "")
        self.db.add(
            PunchRecord(
                tenant_id=self.tenant.id,
                source_id=source.id,
                device_id=device.id if device else None,
                # Stamp the run, so "what did this sync bring in" is answerable
                # later from the ledger rather than only from a counter.
                first_seen_run_id=self.run.id,
                external_id=event.external_id,
                emp_code=event.emp_code,
                punch_time_utc=punch_utc,
                punch_time_local=event.punch_time_local,
                direction=direction,
                raw_state=str(event.raw.get("punch_state", "") or "")[:8] or None,
                verify_type=event.verify_type,
                terminal_sn=event.terminal_sn,
                process_state=PunchState.pending.value,
                raw=event.raw,
            )
        )
        if device is not None:
            device.punch_count = (device.punch_count or 0) + 1
            device.last_seen_at = datetime.now(timezone.utc)

    # -- stage 4 -----------------------------------------------------------
    def _register_badges(self) -> None:
        """Create an unmapped row for every badge seen, before Odoo is involved."""
        seen = set(
            self.db.scalars(
                select(PunchRecord.emp_code).where(
                    PunchRecord.tenant_id == self.tenant.id,
                    PunchRecord.process_state.in_(
                        [PunchState.pending.value, PunchState.unmapped.value]
                    ),
                )
            ).all()
        )
        if not seen:
            return

        known = set(
            self.db.scalars(
                select(EmployeeMapping.emp_code).where(
                    EmployeeMapping.tenant_id == self.tenant.id
                )
            ).all()
        )

        added = 0
        for code in sorted(seen - known):
            self.db.add(
                EmployeeMapping(
                    tenant_id=self.tenant.id,
                    emp_code=code,
                    source_name=self._name_for(code),
                    status=MappingStatus.unmapped.value,
                    match_note="Waiting to be matched to an Odoo employee.",
                )
            )
            added += 1

        if added:
            self.db.flush()
            self._log(f"Registered {added} new badge ID(s) awaiting a match")

    def _name_for(self, emp_code: str) -> str | None:
        row = self.db.scalars(
            select(PunchRecord)
            .where(
                PunchRecord.tenant_id == self.tenant.id,
                PunchRecord.emp_code == emp_code,
            )
            .limit(1)
        ).first()
        if row and row.raw:
            name = f"{row.raw.get('first_name') or ''} {row.raw.get('last_name') or ''}"
            return name.strip() or None
        return None

    # -- stage 5 -----------------------------------------------------------
    def _resolve_mappings(self, odoo: OdooClient) -> None:
        pending = set(
            self.db.scalars(
                select(PunchRecord.emp_code).where(
                    PunchRecord.tenant_id == self.tenant.id,
                    PunchRecord.process_state.in_(
                        [PunchState.pending.value, PunchState.unmapped.value]
                    ),
                )
            ).all()
        )
        if not pending:
            return

        existing = {
            m.emp_code: m
            for m in self.db.scalars(
                select(EmployeeMapping).where(
                    EmployeeMapping.tenant_id == self.tenant.id,
                    EmployeeMapping.emp_code.in_(pending),
                )
            ).all()
        }

        # The plan's employee cap, if any. Counted once up front rather than
        # re-queried per code: every new match below increments it in memory,
        # so the loop sees a consistent, monotonically-updated count instead
        # of racing its own writes within the same run. Skipped entirely when
        # there is no cap, which is the common case and the state of every
        # tenant with no plan assigned.
        cap = self.tenant.plan_max_employees
        mapped_count = 0
        if cap is not None:
            mapped_count = self.db.scalar(
                select(func.count(EmployeeMapping.id)).where(
                    EmployeeMapping.tenant_id == self.tenant.id,
                    EmployeeMapping.status == MappingStatus.mapped.value,
                )
            ) or 0

        matched = 0
        for code in sorted(pending):
            mapping = existing.get(code)
            if mapping and mapping.status in (
                MappingStatus.mapped.value,
                MappingStatus.ignored.value,
            ):
                continue
            if mapping is None:
                mapping = EmployeeMapping(tenant_id=self.tenant.id, emp_code=code)
                self.db.add(mapping)
                existing[code] = mapping

            # The cap only holds back *new* matches. A mapping already
            # `mapped` was excluded above and never reaches here, so a plan
            # downgraded under a tenant already over its new cap does not
            # start unmapping people relying on it — see
            # SubscriptionPlan.max_employees. Checked before the Odoo lookup
            # below on purpose: there is no reason to spend an API call, or
            # auto-create an employee in Odoo, for a badge this run is not
            # going to use anyway.
            if cap is not None and mapped_count >= cap:
                mapping.status = MappingStatus.unmapped.value
                mapping.source_name = self._name_for(code)
                mapping.match_note = (
                    f"Not matched: this account's plan allows up to {cap} mapped "
                    f"employee(s), and that many are already in use. Upgrade the "
                    f"plan, or free one up, to match this badge."
                )
                continue

            try:
                emp_id, emp_name, method = odoo.find_employee(code)
            except OdooError as exc:
                mapping.match_note = f"Lookup failed: {exc}"
                continue

            if emp_id:
                mapping.odoo_employee_id = emp_id
                mapping.odoo_employee_name = emp_name
                mapping.status = MappingStatus.mapped.value
                mapping.match_method = method
                mapping.match_note = None
                matched += 1
                mapped_count += 1
            elif method and method.startswith("ambiguous:"):
                mapping.status = MappingStatus.ambiguous.value
                mapping.match_note = (
                    f"Several Odoo employees share {method.split(':')[1]}={code}. "
                    "Pick one in the dashboard."
                )
            elif self.tenant.auto_create_employees:
                name = self._name_for(code) or f"Employee {code}"
                try:
                    mapping.odoo_employee_id = odoo.create_employee(name, code)
                except OdooError as exc:
                    mapping.match_note = f"Auto-create failed: {exc}"
                    continue
                mapping.odoo_employee_name = name
                mapping.status = MappingStatus.mapped.value
                mapping.match_method = "auto_created"
                matched += 1
                mapped_count += 1
            else:
                mapping.status = MappingStatus.unmapped.value
                mapping.source_name = self._name_for(code)
                mapping.match_note = (
                    f"No Odoo employee has barcode / pin / registration number "
                    f"'{code}'. Set it in Odoo, or map by hand."
                )

        self.run.employees_matched = matched
        self.db.flush()
        if matched:
            self._log(f"Matched {matched} new employee(s) to Odoo")

    # -- stages 6-8 --------------------------------------------------------
    def _push(self, odoo: OdooClient, odoo_conn: OdooConnection) -> None:
        mappings = {
            m.emp_code: m
            for m in self.db.scalars(
                select(EmployeeMapping).where(EmployeeMapping.tenant_id == self.tenant.id)
            ).all()
        }

        punches = self.db.scalars(
            select(PunchRecord)
            .where(
                PunchRecord.tenant_id == self.tenant.id,
                PunchRecord.process_state.in_(
                    [
                        PunchState.pending.value,
                        PunchState.unmapped.value,
                        PunchState.error.value,
                    ]
                ),
                PunchRecord.attempts < 5,
            )
            .order_by(PunchRecord.emp_code, PunchRecord.punch_time_utc)
        ).all()

        if not punches:
            self._log("Nothing to push — Odoo is up to date")
            return

        by_employee: dict[str, list[PunchRecord]] = {}
        for punch in punches:
            mapping = mappings.get(punch.emp_code)
            if mapping is None or mapping.status == MappingStatus.unmapped.value:
                punch.process_state = PunchState.unmapped.value
                continue
            if mapping.status in (MappingStatus.ignored.value, MappingStatus.ambiguous.value):
                punch.process_state = PunchState.skipped.value
                punch.error_message = f"Mapping is {mapping.status}"
                continue
            by_employee.setdefault(punch.emp_code, []).append(punch)

        for emp_code, emp_punches in by_employee.items():
            mapping = mappings[emp_code]
            try:
                self._push_employee(odoo, odoo_conn, mapping, emp_punches)
            except OdooError as exc:
                self.run.error_count += 1
                for punch in emp_punches:
                    punch.process_state = PunchState.error.value
                    punch.error_message = str(exc)[:500]
                    punch.attempts += 1
                self._log(f"{emp_code}: {exc}", "error")

        self.db.flush()

    def _push_employee(
        self,
        odoo: OdooClient,
        odoo_conn: OdooConnection,
        mapping: EmployeeMapping,
        punches: list[PunchRecord],
    ) -> None:
        employee_id = mapping.odoo_employee_id
        assert employee_id is not None

        # The shift already open for this person, taken from Odoo so a record
        # closed by hand there is respected, and reconciled with what we stored.
        open_shift = self._current_open_shift(odoo, mapping)

        by_id = {p.id: p for p in punches}
        mode = self._mode_for(punches)
        config = self.pairing_config(mode)

        result = pair_punches(
            [
                Punch(
                    punch_id=p.id,
                    emp_code=p.emp_code,
                    time_utc=p.punch_time_utc,
                    direction=PairDirection(p.direction),
                    terminal_sn=p.terminal_sn,
                )
                for p in punches
            ],
            config,
            open_shift=open_shift,
        )

        for punch_id in result.skipped_punch_ids:
            punch = by_id.get(punch_id)
            if punch is not None:
                punch.process_state = PunchState.skipped.value
                punch.error_message = "Duplicate punch within the minimum interval"

        for warning in result.warnings:
            self._log(warning, "warning")

        for interval in result.intervals:
            if interval.closes_attendance_id is not None:
                # Closes a shift opened in an earlier cycle. No new record is
                # created — this is the case a naive engine turns into a phantom.
                odoo.close_attendance(interval.closes_attendance_id, interval.check_out)
                self.run.attendances_closed += 1
                attendance_id = interval.closes_attendance_id
            else:
                existing = odoo.attendance_exists(employee_id, interval.check_in)
                if existing:
                    if interval.check_out:
                        odoo.close_attendance(existing, interval.check_out)
                        self.run.attendances_closed += 1
                    attendance_id = existing
                elif recovered_id := self._recover_from_lost_close(
                    odoo, employee_id, interval, by_id
                ):
                    # interval.check_in/check_out were just corrected in place
                    # to the real shift's times — see the method's docstring.
                    attendance_id = recovered_id
                    self._log(
                        f"{mapping.emp_code}: recovered a check-out whose local "
                        f"state was lost after an earlier attempt already closed "
                        f"attendance {attendance_id} in Odoo — nothing to write",
                    )
                else:
                    attendance_id = odoo.create_attendance(
                        employee_id,
                        interval.check_in,
                        interval.check_out,
                        biotime_ref=self._ref(interval, by_id)
                        if odoo_conn.has_companion_addon
                        else None,
                    )
                    self.run.attendances_created += 1
                    if interval.check_out:
                        self.run.attendances_closed += 1

            self._mark(by_id, interval, attendance_id)
            self._record_interval(mapping, interval, attendance_id, by_id)

            # Carry the open shift forward for the next cycle. This is the state
            # that makes a later lone check-out resolvable.
            if interval.check_out is None:
                mapping.open_attendance_id = attendance_id
                mapping.open_check_in = interval.check_in
            else:
                mapping.open_attendance_id = None
                mapping.open_check_in = None

        mapping.last_synced_at = datetime.now(timezone.utc)
        if punches:
            mapping.last_punch_at = max(p.punch_time_utc for p in punches)

    def _recover_from_lost_close(
        self,
        odoo: OdooClient,
        employee_id: int,
        interval,
        by_id: dict[str, PunchRecord],
    ) -> int | None:
        """Tell a genuinely new shift apart from a check-out replaying after a
        crash between its Odoo write and the local commit that would have
        marked it synced.

        By the time this runs, two things are already true: there was no
        open shift in Odoo to close (``_current_open_shift`` just checked),
        and ``attendance_exists`` found nothing at this ``check_in`` either.
        For an ordinary new shift that is simply the answer — create it —
        and this function is not even called (see the ``elif`` at the call
        site, which only reaches here when both of those came up empty).

        The one case that still isn't safe to create: pairing has no memory
        across a crash, so a check-out punch that already closed a shift in
        Odoo, but whose "synced" state locally was lost before it committed,
        comes back on retry looking exactly like a lone, unrelated check-in
        — every pairing mode reads it that way once there is no open shift
        left to attach it to. Left alone, that opens a phantom record next
        to the real, already-correctly-closed one.

        The tell, checked first and entirely locally (no Odoo call, so an
        ordinary create pays nothing extra for this): the punch pairing
        used as this interval's check-in was itself recorded with direction
        "out". A real shift's opening punch is never that — either "in", or
        "unknown" on a device that does not report state at all, which is
        deliberately left alone here rather than risk a false positive and
        silently drop what might be a genuine new shift.

        Only then is Odoo asked whether some record already carries this
        exact check-out. A match means the punch already did its job; this
        rewrites ``interval`` in place to the real shift's actual times
        (Odoo's, not the misread ones) so everything downstream — the local
        mirror, lateness scoring, the open-shift carried into next cycle —
        reflects the real shift rather than the phantom reading, and
        returns that record's id so the caller writes nothing further.
        """
        check_in_punch = by_id.get(interval.check_in_punch_id or "")
        if check_in_punch is None or check_in_punch.direction != Direction.outward.value:
            return None

        real = odoo.attendance_closed_at(employee_id, interval.check_in)
        if real is None:
            return None

        real_check_out = interval.check_in
        interval.check_in = parse_dt(real.get("check_in")) or real_check_out
        interval.check_out = real_check_out
        return real["id"]

    def _current_open_shift(self, odoo: OdooClient, mapping: EmployeeMapping) -> OpenShift | None:
        """Reconcile our stored open shift against Odoo's actual state.

        Odoo is authoritative: somebody may have closed the record by hand.
        """
        live = odoo.get_open_attendance(mapping.odoo_employee_id)
        if not live:
            mapping.open_attendance_id = None
            mapping.open_check_in = None
            return None

        check_in = parse_dt(live["check_in"])
        if check_in is None:
            return None
        mapping.open_attendance_id = live["id"]
        mapping.open_check_in = check_in
        return OpenShift(attendance_id=live["id"], check_in=check_in)

    def _mode_for(self, punches: list[PunchRecord]) -> str | None:
        """Per-device pairing override, when every punch came from one terminal.

        Mixing modes inside one employee's stream would produce incoherent
        intervals, so the override applies only when the batch is unambiguous.
        """
        device_ids = {p.device_id for p in punches if p.device_id}
        if len(device_ids) != 1:
            return None
        device = self.db.get(Device, device_ids.pop())
        return device.pairing_override if device else None

    @staticmethod
    def _ref(interval, by_id: dict[str, PunchRecord]) -> str:
        punch = by_id.get(interval.check_in_punch_id or "")
        return f"biotime:{punch.external_id}" if punch else "biotime:derived"

    def _mark(self, by_id: dict[str, PunchRecord], interval, attendance_id: int) -> None:
        for punch_id in (interval.check_in_punch_id, interval.check_out_punch_id):
            punch = by_id.get(punch_id or "")
            if punch is not None:
                punch.process_state = PunchState.synced.value
                punch.odoo_attendance_id = attendance_id
                punch.error_message = None

    def _record_interval(
        self, mapping: EmployeeMapping, interval, attendance_id: int, by_id: dict
    ) -> None:
        """Mirror the interval locally, so reports never call Odoo.

        Keyed on the Odoo attendance id, so re-processing the same shift updates
        the row rather than duplicating it.
        """
        tz = self.tenant.timezone
        check_in_local = utc_to_local(interval.check_in, tz)
        check_out_local = utc_to_local(interval.check_out, tz) if interval.check_out else None
        punch = by_id.get(interval.check_in_punch_id or "")
        shift_date = check_in_local.strftime("%Y-%m-%d")

        existing = self.db.scalars(
            select(AttendanceRecord).where(
                AttendanceRecord.tenant_id == self.tenant.id,
                AttendanceRecord.odoo_attendance_id == attendance_id,
            )
        ).first()

        # Lateness applies to the *arrival*, so only the first interval of a
        # shift-day is eligible. Without this, coming back from lunch at 13:00
        # reads as hours late and everyone looks chronically tardy.
        earlier_today = self.db.scalar(
            select(func.count(AttendanceRecord.id)).where(
                AttendanceRecord.tenant_id == self.tenant.id,
                AttendanceRecord.emp_code == mapping.emp_code,
                AttendanceRecord.shift_date == shift_date,
                AttendanceRecord.check_in < interval.check_in,
                AttendanceRecord.id != (existing.id if existing else ""),
            )
        ) or 0
        late_minutes = 0 if earlier_today else self._late_minutes(check_in_local)

        values = {
            "emp_code": mapping.emp_code,
            "employee_name": mapping.odoo_employee_name or mapping.source_name,
            "department": mapping.department,
            "odoo_employee_id": mapping.odoo_employee_id,
            "check_in": interval.check_in,
            "check_out": interval.check_out,
            "worked_hours": interval.duration_hours,
            "shift_date": shift_date,
            "check_in_local": check_in_local,
            "check_out_local": check_out_local,
            "device_serial": punch.terminal_sn if punch else None,
            "pairing_mode": self.tenant.pairing_mode,
            "is_auto_closed": interval.auto_closed,
            "is_orphan_out": interval.orphan_out,
            "is_late": late_minutes > 0,
            "late_minutes": late_minutes,
            "notes": "; ".join(interval.notes) or None,
        }

        if existing is not None:
            for key, value in values.items():
                setattr(existing, key, value)
        else:
            self.db.add(
                AttendanceRecord(
                    tenant_id=self.tenant.id, odoo_attendance_id=attendance_id, **values
                )
            )

        # The session runs with autoflush off, so without this the next interval
        # of the same day cannot see this one and is scored late a second time.
        self.db.flush()

    def _late_minutes(self, check_in_local: datetime) -> int:
        try:
            hour, minute = (int(p) for p in self.tenant.work_start_time.split(":"))
        except (ValueError, AttributeError):
            return 0
        expected = check_in_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        delta = (check_in_local - expected).total_seconds() / 60
        # A night shift starts before the "work start" time; never call it late.
        if delta <= self.tenant.late_grace_minutes or delta > 12 * 60:
            return 0
        return int(delta)

    # -- helpers -----------------------------------------------------------
    def _active_odoo_connection(self) -> OdooConnection | None:
        return self.db.scalars(
            select(OdooConnection)
            .where(
                OdooConnection.tenant_id == self.tenant.id,
                OdooConnection.is_active.is_(True),
            )
            .limit(1)
        ).first()

    def _active_sources(self) -> list[DeviceSource]:
        return list(
            self.db.scalars(
                select(DeviceSource).where(
                    DeviceSource.tenant_id == self.tenant.id,
                    DeviceSource.is_active.is_(True),
                )
            ).all()
        )

    def _mark_degraded_if_needed(self) -> None:
        if self.tenant.consecutive_failures < settings.max_consecutive_failures:
            return
        for source in self._active_sources():
            source.status = ConnectionStatus.degraded.value
        conn = self._active_odoo_connection()
        if conn:
            conn.status = ConnectionStatus.degraded.value
        self._log(
            f"{self.tenant.consecutive_failures} consecutive failures — connections "
            "marked degraded and polling moved to the slow lane",
            "error",
        )


def close_stale_attendances(db: Session, tenant: Tenant) -> int:
    """Close shifts left open past max_shift_hours.

    Without this, one employee who forgets to badge out holds an ever-growing
    open record that blocks every subsequent check-in for that person.
    """
    engine = SyncEngine(db, tenant, "maintenance")
    conn = engine._active_odoo_connection()  # noqa: SLF001
    if conn is None:
        return 0

    closed = 0
    odoo = build_odoo_client(tenant, conn)
    config = engine.pairing_config()
    mappings = db.scalars(
        select(EmployeeMapping).where(
            EmployeeMapping.tenant_id == tenant.id,
            EmployeeMapping.status == MappingStatus.mapped.value,
        )
    ).all()

    for mapping in mappings:
        try:
            live = odoo.get_open_attendance(mapping.odoo_employee_id)
            if not live:
                continue
            check_in = parse_dt(live["check_in"])
            if check_in is None:
                continue
            auto_close = close_stale_at(check_in, utcnow_naive(), config)
            if auto_close:
                odoo.close_attendance(live["id"], auto_close)
                mapping.open_attendance_id = None
                mapping.open_check_in = None
                closed += 1
        except OdooError as exc:
            log.warning("Stale-close failed for %s: %s", tenant.slug, exc)

    return closed
