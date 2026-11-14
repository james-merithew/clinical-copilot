-- One-off LIVE seeder: runs INSIDE Railway's private network against
-- mariadb.railway.internal. Creates the read-only sidecar user + the demo
-- authorization mapping + the 2 curated showcase patients. No public exposure.
SET SESSION sql_mode = '';

-- Read-only DB user for the sidecar (invariant #3). Password injected at runtime.
CREATE USER IF NOT EXISTS 'copilot_ro'@'%' IDENTIFIED BY '__RO_PASS__';
GRANT SELECT ON openemr.* TO 'copilot_ro'@'%';
FLUSH PRIVILEGES;

-- Session-token signing key (AUTH_TOKEN_SPEC.md). Lives in the openemr DB so
-- OpenEMR PHP (native) and the sidecar (copilot_ro SELECT ON openemr.*) both
-- read it — no cross-DB grant, no env-var change on the openemr service. The
-- key is injected at runtime (openssl rand); INSERT IGNORE keeps it STABLE
-- across reseeds so already-issued tokens keep verifying.
CREATE TABLE IF NOT EXISTS copilot_signing_key (
  id         INT NOT NULL PRIMARY KEY,
  secret     VARCHAR(128) NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB;
INSERT IGNORE INTO copilot_signing_key (id, secret) VALUES (1, '__SIGNING_KEY__');

-- Authorize the demo physician (user 1) for the base demo patient; pid 2/3 stay
-- under other providers as natural DENIAL fixtures.
UPDATE patient_data SET providerID = 1 WHERE pid = 1;

-- Clean + insert the 2 showcase patients (pids 101, 102).
DELETE FROM procedure_result WHERE procedure_report_id IN
  (SELECT procedure_report_id FROM procedure_report WHERE procedure_order_id IN
    (SELECT procedure_order_id FROM procedure_order WHERE patient_id IN (101,102)));
DELETE FROM procedure_report WHERE procedure_order_id IN
  (SELECT procedure_order_id FROM procedure_order WHERE patient_id IN (101,102));
DELETE FROM procedure_order WHERE patient_id IN (101,102);
DELETE FROM prescriptions WHERE patient_id IN (101,102);
DELETE FROM lists WHERE pid IN (101,102);
DELETE FROM form_encounter WHERE pid IN (101,102);
DELETE FROM patient_data WHERE pid IN (101,102);

-- Showcase A (pid 101): ED transition, prescriptions<->lists med divergence, A1c trend.
INSERT INTO patient_data (uuid, pid, pubpid, fname, lname, DOB, sex, providerID, date)
VALUES (UNHEX(REPLACE(UUID(),'-','')), 101, 'SHOW-A', 'Marcus', 'Delgado', '1961-04-12', 'Male', 1, NOW());
INSERT INTO lists (uuid, pid, type, title, begdate, activity, date) VALUES
 (UNHEX(REPLACE(UUID(),'-','')), 101, 'medical_problem', 'Type 2 diabetes mellitus', '2019-03-01', 1, NOW()),
 (UNHEX(REPLACE(UUID(),'-','')), 101, 'medical_problem', 'Essential hypertension', '2020-07-01', 1, NOW()),
 (UNHEX(REPLACE(UUID(),'-','')), 101, 'medication', 'Metformin', NULL, 0, NOW());
INSERT INTO prescriptions (uuid, patient_id, provider_id, drug, dosage, unit, active, start_date, date_added) VALUES
 (UNHEX(REPLACE(UUID(),'-','')), 101, 1, 'Metformin', '1000', 'mg', 1, '2024-06-01', NOW()),
 (UNHEX(REPLACE(UUID(),'-','')), 101, 1, 'Lisinopril', '10', 'mg', 1, '2023-02-01', NOW());
INSERT INTO form_encounter (uuid, pid, encounter, date, reason, pos_code, provider_id) VALUES
 (UNHEX(REPLACE(UUID(),'-','')), 101, 90101, '2026-06-14 10:00:00', 'Emergency department visit for chest pain; ACS ruled out', '23', 1),
 (UNHEX(REPLACE(UUID(),'-','')), 101, 90102, '2026-05-01 09:00:00', 'Diabetes follow-up', '11', 1);
INSERT INTO procedure_order (uuid, patient_id, provider_id, date_ordered)
VALUES (UNHEX(REPLACE(UUID(),'-','')), 101, 1, '2026-05-01');
SET @ordA := LAST_INSERT_ID();
INSERT INTO procedure_report (uuid, procedure_order_id, date_report, report_status)
VALUES (UNHEX(REPLACE(UUID(),'-','')), @ordA, '2026-05-01', 'final');
SET @repA := LAST_INSERT_ID();
INSERT INTO procedure_result (uuid, procedure_report_id, result_code, result_text, result, units, `range`, abnormal, date) VALUES
 (UNHEX(REPLACE(UUID(),'-','')), @repA, '4548-4', 'Hemoglobin A1c', '8.2', '%', '4.0-5.6', 'H', '2026-05-01'),
 (UNHEX(REPLACE(UUID(),'-','')), @repA, '4548-4', 'Hemoglobin A1c', '7.5', '%', '4.0-5.6', 'H', '2025-11-01'),
 (UNHEX(REPLACE(UUID(),'-','')), @repA, '4548-4', 'Hemoglobin A1c', '6.9', '%', '4.0-5.6', '', '2025-05-01');

-- Showcase B (pid 102): penicillin allergy + amoxicillin, warfarin + ibuprofen.
INSERT INTO patient_data (uuid, pid, pubpid, fname, lname, DOB, sex, providerID, date)
VALUES (UNHEX(REPLACE(UUID(),'-','')), 102, 'SHOW-B', 'Eleanor', 'Whitfield', '1949-09-30', 'Female', 1, NOW());
INSERT INTO lists (uuid, pid, type, title, reaction, severity_al, activity, date)
VALUES (UNHEX(REPLACE(UUID(),'-','')), 102, 'allergy', 'Penicillin', 'Hives and facial swelling', 'severe', 1, NOW());
INSERT INTO lists (uuid, pid, type, title, begdate, activity, date)
VALUES (UNHEX(REPLACE(UUID(),'-','')), 102, 'medical_problem', 'Atrial fibrillation', '2021-01-01', 1, NOW());
INSERT INTO prescriptions (uuid, patient_id, provider_id, drug, dosage, unit, active, start_date, date_added) VALUES
 (UNHEX(REPLACE(UUID(),'-','')), 102, 1, 'Amoxicillin', '500', 'mg', 1, '2026-06-20', NOW()),
 (UNHEX(REPLACE(UUID(),'-','')), 102, 1, 'Warfarin', '5', 'mg', 1, '2021-02-01', NOW()),
 (UNHEX(REPLACE(UUID(),'-','')), 102, 1, 'Ibuprofen', '400', 'mg', 1, '2026-06-10', NOW());
INSERT INTO form_encounter (uuid, pid, encounter, date, reason, pos_code, provider_id)
VALUES (UNHEX(REPLACE(UUID(),'-','')), 102, 90201, '2026-06-20 11:00:00', 'Acute sinusitis; started on antibiotic', '11', 1);

-- Lightweight demo panel (pids 103-110) + today's schedule -------------------
-- Fills out the "My Day" view: mostly routine, with ONE mild abnormal lab
-- (pid 105) and ONE allergy (pid 107) for variety. All under provider 1.
DELETE FROM procedure_result WHERE procedure_report_id IN
  (SELECT procedure_report_id FROM procedure_report WHERE procedure_order_id IN
    (SELECT procedure_order_id FROM procedure_order WHERE patient_id BETWEEN 103 AND 110));
DELETE FROM procedure_report WHERE procedure_order_id IN
  (SELECT procedure_order_id FROM procedure_order WHERE patient_id BETWEEN 103 AND 110);
DELETE FROM procedure_order WHERE patient_id BETWEEN 103 AND 110;
DELETE FROM prescriptions WHERE patient_id BETWEEN 103 AND 110;
DELETE FROM lists WHERE pid BETWEEN 103 AND 110;
DELETE FROM patient_data WHERE pid BETWEEN 103 AND 110;

INSERT INTO patient_data (uuid, pid, pubpid, fname, lname, DOB, sex, providerID, date) VALUES
 (UNHEX(REPLACE(UUID(),'-','')), 103, 'DEMO-103', 'John',    'Carter',   '1975-08-15', 'Male',   1, NOW()),
 (UNHEX(REPLACE(UUID(),'-','')), 104, 'DEMO-104', 'Maria',   'Gonzalez', '1988-02-20', 'Female', 1, NOW()),
 (UNHEX(REPLACE(UUID(),'-','')), 105, 'DEMO-105', 'Robert',  'Kim',      '1955-11-03', 'Male',   1, NOW()),
 (UNHEX(REPLACE(UUID(),'-','')), 106, 'DEMO-106', 'Linda',   'Nguyen',   '1992-06-25', 'Female', 1, NOW()),
 (UNHEX(REPLACE(UUID(),'-','')), 107, 'DEMO-107', 'David',   'Patel',    '1968-12-10', 'Male',   1, NOW()),
 (UNHEX(REPLACE(UUID(),'-','')), 108, 'DEMO-108', 'Susan',   'Brooks',   '1980-04-05', 'Female', 1, NOW()),
 (UNHEX(REPLACE(UUID(),'-','')), 109, 'DEMO-109', 'James',   'Walker',   '1948-09-18', 'Male',   1, NOW()),
 (UNHEX(REPLACE(UUID(),'-','')), 110, 'DEMO-110', 'Emily',   'Foster',   '2001-01-30', 'Female', 1, NOW());

INSERT INTO lists (uuid, pid, type, title, begdate, activity, date) VALUES
 (UNHEX(REPLACE(UUID(),'-','')), 103, 'medical_problem', 'Hyperlipidemia',      '2018-05-01', 1, NOW()),
 (UNHEX(REPLACE(UUID(),'-','')), 104, 'medical_problem', 'Asthma',              '2010-09-01', 1, NOW()),
 (UNHEX(REPLACE(UUID(),'-','')), 105, 'medical_problem', 'Essential hypertension', '2016-03-01', 1, NOW()),
 (UNHEX(REPLACE(UUID(),'-','')), 106, 'medical_problem', 'Hypothyroidism',      '2019-11-01', 1, NOW()),
 (UNHEX(REPLACE(UUID(),'-','')), 107, 'medical_problem', 'Gastroesophageal reflux disease', '2021-02-01', 1, NOW()),
 (UNHEX(REPLACE(UUID(),'-','')), 108, 'medical_problem', 'Type 2 diabetes mellitus', '2020-07-01', 1, NOW()),
 (UNHEX(REPLACE(UUID(),'-','')), 109, 'medical_problem', 'Osteoarthritis',      '2015-01-01', 1, NOW()),
 (UNHEX(REPLACE(UUID(),'-','')), 110, 'medical_problem', 'Migraine',            '2022-06-01', 1, NOW());

INSERT INTO prescriptions (uuid, patient_id, provider_id, drug, dosage, unit, active, start_date, date_added) VALUES
 (UNHEX(REPLACE(UUID(),'-','')), 103, 1, 'Atorvastatin',  '20',  'mg', 1, '2023-01-10', NOW()),
 (UNHEX(REPLACE(UUID(),'-','')), 104, 1, 'Albuterol',     '90',  'mcg',1, '2022-04-01', NOW()),
 (UNHEX(REPLACE(UUID(),'-','')), 105, 1, 'Amlodipine',    '5',   'mg', 1, '2021-08-15', NOW()),
 (UNHEX(REPLACE(UUID(),'-','')), 106, 1, 'Levothyroxine', '75',  'mcg',1, '2020-01-05', NOW()),
 (UNHEX(REPLACE(UUID(),'-','')), 107, 1, 'Omeprazole',    '20',  'mg', 1, '2023-03-01', NOW()),
 (UNHEX(REPLACE(UUID(),'-','')), 108, 1, 'Metformin',     '500', 'mg', 1, '2021-05-20', NOW()),
 (UNHEX(REPLACE(UUID(),'-','')), 109, 1, 'Acetaminophen', '500', 'mg', 1, '2024-02-01', NOW()),
 (UNHEX(REPLACE(UUID(),'-','')), 110, 1, 'Sumatriptan',   '50',  'mg', 1, '2023-07-01', NOW());

-- pid 107: an allergy (variety; not med-matched so no critical flag).
INSERT INTO lists (uuid, pid, type, title, reaction, severity_al, activity, date)
VALUES (UNHEX(REPLACE(UUID(),'-','')), 107, 'allergy', 'Sulfa drugs', 'Rash', 'moderate', 1, NOW());

-- pid 105: one MILD abnormal lab (LDL slightly high) => a single warning flag.
INSERT INTO procedure_order (uuid, patient_id, provider_id, date_ordered)
VALUES (UNHEX(REPLACE(UUID(),'-','')), 105, 1, '2026-06-10');
SET @ord105 := LAST_INSERT_ID();
INSERT INTO procedure_report (uuid, procedure_order_id, date_report, report_status)
VALUES (UNHEX(REPLACE(UUID(),'-','')), @ord105, '2026-06-10', 'final');
SET @rep105 := LAST_INSERT_ID();
INSERT INTO procedure_result (uuid, procedure_report_id, result_code, result_text, result, units, `range`, abnormal, date)
VALUES (UNHEX(REPLACE(UUID(),'-','')), @rep105, '13457-7', 'LDL Cholesterol', '142', 'mg/dL', '0-100', 'H', '2026-06-10');

-- Schedule WINDOW (CURDATE() .. +13 days), provider 1, one Office Visit per
-- patient per day — "My Day" is populated on ANY presentation day.
DELETE FROM openemr_postcalendar_events
 WHERE pc_pid IN (101,102,103,104,105,106,107,108,109,110);
INSERT INTO openemr_postcalendar_events
 (pc_catid, pc_aid, pc_pid, pc_title, pc_hometext, pc_eventDate, pc_startTime, pc_endTime, pc_duration, pc_eventstatus, pc_apptstatus, pc_facility)
SELECT 5, 1, t.pid, 'Office Visit', t.reason, DATE_ADD(CURDATE(), INTERVAL d.n DAY), t.st, t.et, 1800, 1, '-', 3
FROM (
        SELECT 101 AS pid, 'Post-ED follow-up; chest pain workup review' AS reason, '08:00:00' AS st, '08:30:00' AS et
  UNION ALL SELECT 102, 'Sinusitis recheck; anticoagulation review',   '08:30:00', '09:00:00'
  UNION ALL SELECT 103, 'Lipid management follow-up',   '09:00:00', '09:30:00'
  UNION ALL SELECT 104, 'Asthma control check',   '09:30:00', '10:00:00'
  UNION ALL SELECT 105, 'Hypertension follow-up; review recent labs',   '10:00:00', '10:30:00'
  UNION ALL SELECT 106, 'Thyroid follow-up',   '10:30:00', '11:00:00'
  UNION ALL SELECT 107, 'GERD symptom review',   '11:00:00', '11:30:00'
  UNION ALL SELECT 108, 'Diabetes follow-up',   '11:30:00', '12:00:00'
  UNION ALL SELECT 109, 'Knee osteoarthritis; pain management',   '13:00:00', '13:30:00'
  UNION ALL SELECT 110, 'Migraine follow-up',   '13:30:00', '14:00:00'
) t
CROSS JOIN (
        SELECT -1 AS n UNION ALL SELECT 0 UNION ALL SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3
  UNION ALL SELECT 4 UNION ALL SELECT 5 UNION ALL SELECT 6 UNION ALL SELECT 7
  UNION ALL SELECT 8 UNION ALL SELECT 9 UNION ALL SELECT 10 UNION ALL SELECT 11
  UNION ALL SELECT 12 UNION ALL SELECT 13
) d;

-- Restricted patient (authz-denied demo fixture): belongs to another provider.
DELETE FROM patient_data WHERE pid = 120;
INSERT INTO patient_data (uuid, pid, pubpid, fname, lname, DOB, sex, providerID, date)
VALUES (UNHEX(REPLACE(UUID(),'-','')), 120, 'DEMO-120', 'Rachel', 'Osei', '1985-03-22', 'Female', 999, NOW());

-- Auto-enable the OpenEMR Co-Pilot module (InstModuleTable register + enable).
DELETE FROM modules WHERE mod_directory='oe-module-copilot';
INSERT INTO modules SET mod_name='Clinical Co-Pilot', mod_active=1, mod_ui_name='Copilot',
  mod_relative_link='oe-module-copilot', mod_directory='oe-module-copilot', type=0, date=NOW();

SELECT CONCAT('SEED_OK patients=', (SELECT COUNT(*) FROM patient_data WHERE pid IN (1,101,102))) AS status;

-- Vitals + extra labs for the showcase metric cards --------------------------
DELETE FROM form_vitals WHERE pid IN (101,102);
INSERT INTO form_vitals (uuid, pid, date, activity, bps, bpd, pulse, weight, height, BMI) VALUES
 (UNHEX(REPLACE(UUID(),'-','')), 101, '2025-05-01 09:00:00', 1, 132, 84, 80, 93, 178, 29.0),
 (UNHEX(REPLACE(UUID(),'-','')), 101, '2025-11-01 09:00:00', 1, 142, 90, 84, 93, 178, 29.2),
 (UNHEX(REPLACE(UUID(),'-','')), 101, '2026-05-01 09:00:00', 1, 150, 95, 86, 93, 178, 29.3),
 (UNHEX(REPLACE(UUID(),'-','')), 101, '2026-06-14 10:00:00', 1, 155, 99, 88, 93, 178, 29.4),
 (UNHEX(REPLACE(UUID(),'-','')), 102, '2026-06-20 11:00:00', 1, 122, 78, 72, 68, 165, 24.0);
INSERT INTO procedure_order (uuid, patient_id, provider_id, date_ordered)
VALUES (UNHEX(REPLACE(UUID(),'-','')), 101, 1, '2026-05-01');
SET @ordK := LAST_INSERT_ID();
INSERT INTO procedure_report (uuid, procedure_order_id, date_report, report_status)
VALUES (UNHEX(REPLACE(UUID(),'-','')), @ordK, '2026-05-01', 'final');
SET @repK := LAST_INSERT_ID();
INSERT INTO procedure_result (uuid, procedure_report_id, result_code, result_text, result, units, `range`, abnormal, date) VALUES
 (UNHEX(REPLACE(UUID(),'-','')), @repK, '48642-3', 'eGFR', '64', 'mL/min', '>=60', '', '2025-11-01'),
 (UNHEX(REPLACE(UUID(),'-','')), @repK, '48642-3', 'eGFR', '61', 'mL/min', '>=60', '', '2026-05-01'),
 (UNHEX(REPLACE(UUID(),'-','')), @repK, '13457-7', 'LDL Cholesterol', '158', 'mg/dL', '<100', 'H', '2025-11-01'),
 (UNHEX(REPLACE(UUID(),'-','')), @repK, '13457-7', 'LDL Cholesterol', '165', 'mg/dL', '<100', 'H', '2026-05-01');

-- ===== SEED CONTRACT: make all seeded data visible in OpenEMR's own UI =====
-- (forms wrappers, procedure_order_code linkage, doctor persona, calendar fields)

-- Doctor persona: the login user (id 1) IS Dr. Sarah Chen, MD.
UPDATE users SET fname='Sarah', lname='Chen', title='MD', authorized=1, calendar=1,
  active=1, facility='Your Clinic Name Here', facility_id=3, see_auth=3 WHERE id=1;

-- Second display-only provider (no login); owns the restricted patient 120.
DELETE FROM users WHERE username='pnatarajan';
INSERT INTO users (username, password, fname, lname, title, authorized, calendar, active, facility, facility_id, see_auth)
VALUES ('pnatarajan', 'NoLogin', 'Priya', 'Natarajan', 'MD', 1, 1, 1, 'Your Clinic Name Here', 3, 3);
SET @drp := LAST_INSERT_ID();
UPDATE patient_data SET providerID=@drp WHERE pid=120;

-- Encounters: correct facility/category/class, plus the two visits that carry
-- the older vitals readings for Marcus (90103/90104).
DELETE FROM form_encounter WHERE pid=101 AND encounter IN (90103,90104);
INSERT INTO form_encounter (uuid, pid, encounter, date, reason, pos_code, provider_id, facility_id, facility, pc_catid, class_code) VALUES
 (UNHEX(REPLACE(UUID(),'-','')), 101, 90103, '2025-05-01 09:00:00', 'Diabetes follow-up', '11', 1, 3, 'Your Clinic Name Here', 5, 'AMB'),
 (UNHEX(REPLACE(UUID(),'-','')), 101, 90104, '2025-11-01 09:00:00', 'Chronic care visit', '11', 1, 3, 'Your Clinic Name Here', 5, 'AMB');
UPDATE form_encounter SET facility_id=3, facility='Your Clinic Name Here', pc_catid=5, class_code='AMB'
 WHERE pid IN (101,102);

-- forms wrappers: encounters (formdir=newpatient) + vitals (formdir=vitals).
DELETE FROM forms WHERE pid IN (101,102) AND formdir IN ('newpatient','vitals');
INSERT INTO forms (date, encounter, form_name, form_id, pid, user, groupname, authorized, deleted, formdir)
SELECT fe.date, fe.encounter, 'New Patient Encounter', fe.id, fe.pid, 'admin', 'Default', 1, 0, 'newpatient'
FROM form_encounter fe WHERE fe.pid IN (101,102);
UPDATE form_vitals SET activity=1 WHERE pid IN (101,102);
INSERT INTO forms (date, encounter, form_name, form_id, pid, user, groupname, authorized, deleted, formdir)
SELECT v.date, fe.encounter, 'Vitals', v.id, v.pid, 'admin', 'Default', 1, 0, 'vitals'
FROM form_vitals v JOIN form_encounter fe ON fe.pid = v.pid AND DATE(fe.date) = DATE(v.date)
WHERE v.pid IN (101,102);

-- Labs visible: order codes (INNER JOIN requirement) + seq/status alignment.
DELETE poc FROM procedure_order_code poc
  JOIN procedure_order po ON po.procedure_order_id = poc.procedure_order_id
 WHERE po.patient_id IN (101,102,105);
INSERT INTO procedure_order_code (procedure_order_id, procedure_order_seq, procedure_code, procedure_name)
SELECT po.procedure_order_id, 1, 'LABPANEL',
       SUBSTRING(GROUP_CONCAT(DISTINCT pr.result_text ORDER BY pr.result_text SEPARATOR ', '),1,80)
FROM procedure_order po
JOIN procedure_report prep ON prep.procedure_order_id = po.procedure_order_id
JOIN procedure_result pr ON pr.procedure_report_id = prep.procedure_report_id
WHERE po.patient_id IN (101,102,105)
GROUP BY po.procedure_order_id;
UPDATE procedure_report prep
  JOIN procedure_order po ON po.procedure_order_id = prep.procedure_order_id
   SET prep.procedure_order_seq = 1, prep.report_status='final',
       prep.date_collected = COALESCE(prep.date_collected, prep.date_report)
 WHERE po.patient_id IN (101,102,105);
UPDATE procedure_order po
  LEFT JOIN form_encounter fe ON fe.pid = po.patient_id AND DATE(fe.date) = po.date_ordered
   SET po.encounter_id = COALESCE(fe.encounter, 0), po.activity = 1, po.provider_id = 1
 WHERE po.patient_id IN (101,102,105);
UPDATE procedure_result pr
  JOIN procedure_report prep ON prep.procedure_report_id = pr.procedure_report_id
  JOIN procedure_order po ON po.procedure_order_id = prep.procedure_order_id
   SET pr.result_status='final'
 WHERE po.patient_id IN (101,102,105);

-- Calendar-query correctness for the seeded appointments.
UPDATE openemr_postcalendar_events
   SET pc_recurrtype=0, pc_endDate=pc_eventDate, pc_multiple=0, pc_sharing=1
 WHERE pc_pid IN (101,102,103,104,105,106,107,108,109,110);

-- Clinic timezone: without this OpenEMR runs in UTC, so its calendar's "today"
-- can be a day ahead of the physician's actual day (and mismatch My Day).
-- America/Chicago = the demo clinic's timezone (Central).
INSERT INTO globals (gl_name, gl_index, gl_value) VALUES ('gbl_time_zone', 0, 'America/Chicago')
  ON DUPLICATE KEY UPDATE gl_value='America/Chicago';

-- ============================================================================
-- Week 2 (Lane A) — document ingestion: `copilot` schema + least-privilege
-- writer identity. Idempotent: safe to re-run on every seed.
-- ============================================================================
CREATE DATABASE IF NOT EXISTS copilot;

-- One row per PERSISTED upload. UNIQUE(content_hash, pid) is the idempotency
-- key: re-uploading the same bytes for the same patient = zero new rows.
CREATE TABLE IF NOT EXISTS copilot.ingestion_run (
  run_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  correlation_id VARCHAR(64) NOT NULL,
  pid BIGINT NOT NULL,
  doc_type VARCHAR(32) NOT NULL,
  content_hash CHAR(128) NOT NULL,
  document_id BIGINT NULL,
  status VARCHAR(32) NOT NULL,
  prompt_version VARCHAR(64) NOT NULL DEFAULT '',
  fields_total INT NOT NULL DEFAULT 0,
  fields_extracted INT NOT NULL DEFAULT 0,
  fields_not_found INT NOT NULL DEFAULT 0,
  fields_failed_verification INT NOT NULL DEFAULT 0,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (run_id),
  UNIQUE KEY uq_ingestion_hash_pid (content_hash, pid)
) ENGINE=InnoDB;

-- Field-level lineage: which chart row came from which document region.
CREATE TABLE IF NOT EXISTS copilot.provenance (
  provenance_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  run_id BIGINT UNSIGNED NOT NULL,
  document_id BIGINT NOT NULL,
  field_id VARCHAR(64) NOT NULL,
  target_table VARCHAR(64) NULL,
  target_pk BIGINT NULL,
  page INT NULL,
  bbox_x0 DOUBLE NULL,
  bbox_y0 DOUBLE NULL,
  bbox_x1 DOUBLE NULL,
  bbox_y1 DOUBLE NULL,
  bbox_origin VARCHAR(16) NULL,
  confidence DOUBLE NULL,
  quote TEXT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (provenance_id),
  UNIQUE KEY uq_provenance_doc_field (document_id, field_id),
  KEY idx_provenance_target (target_table, target_pk)
) ENGINE=InnoDB;

-- Sidecar-owned intake facts (chief concern, family history) + demographic
-- compare-and-flag mismatches (patient_data is NEVER overwritten).
CREATE TABLE IF NOT EXISTS copilot.intake_fact (
  fact_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  run_id BIGINT UNSIGNED NOT NULL,
  document_id BIGINT NOT NULL,
  pid BIGINT NOT NULL,
  kind VARCHAR(32) NOT NULL,
  field_id VARCHAR(64) NOT NULL,
  value TEXT NULL,
  chart_value TEXT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (fact_id),
  KEY idx_intake_fact_pid (pid)
) ENGINE=InnoDB;

-- INSERT-only writer identity (invariant #3 preserved: copilot_ro untouched;
-- the in-code writer allowlist enforces the same boundary a second time).
CREATE USER IF NOT EXISTS 'copilot_ingest'@'%' IDENTIFIED BY '__INGEST_PASS__';
-- Force the password every run: CREATE USER IF NOT EXISTS is a no-op when the
-- user already exists (e.g. an earlier seed), so it would NOT pick up a rotated
-- password — ALTER USER makes the credential authoritative each seed.
ALTER USER 'copilot_ingest'@'%' IDENTIFIED BY '__INGEST_PASS__';
GRANT INSERT ON openemr.procedure_order TO 'copilot_ingest'@'%';
GRANT INSERT ON openemr.procedure_order_code TO 'copilot_ingest'@'%';
GRANT INSERT ON openemr.procedure_report TO 'copilot_ingest'@'%';
GRANT INSERT ON openemr.procedure_result TO 'copilot_ingest'@'%';
GRANT INSERT ON openemr.lists TO 'copilot_ingest'@'%';
-- documents fallback path (flag-gated in code) + OpenEMR's sequences id idiom:
GRANT INSERT ON openemr.documents TO 'copilot_ingest'@'%';
GRANT UPDATE ON openemr.sequences TO 'copilot_ingest'@'%';
-- dedup / documents.id recovery / demographics compare reads:
GRANT SELECT ON openemr.* TO 'copilot_ingest'@'%';
GRANT ALL PRIVILEGES ON copilot.* TO 'copilot_ingest'@'%';
-- read tools may surface provenance alongside chart rows:
GRANT SELECT ON copilot.* TO 'copilot_ro'@'%';
FLUSH PRIVILEGES;
