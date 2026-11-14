#!/bin/sh
set -e
echo "seeder: waiting for DB at ${MYSQL_HOST}..."
until mariadb-admin ping -h "$MYSQL_HOST" -uroot -p"$MYSQL_ROOT_PASS" --silent 2>/dev/null; do
  echo "seeder: db not ready, retrying..."; sleep 3
done
echo "seeder: DB up, applying seed"
# --force: apply every statement even if one hits a benign re-run conflict, so a
# single hiccup can never leave the seed half-applied (no partial cohesion).
# Session-token signing key: use a provided value for reproducibility, else a
# fresh random one. INSERT IGNORE in the SQL keeps it stable across reseeds, so a
# re-run's fresh value is discarded once the row exists.
if [ -z "${SIGNING_KEY}" ]; then
  SIGNING_KEY="$(openssl rand -hex 32 2>/dev/null || head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
fi

sed -e "s/__RO_PASS__/${RO_PASS}/g" \
    -e "s/__INGEST_PASS__/${INGEST_PASS}/g" \
    -e "s/__SIGNING_KEY__/${SIGNING_KEY}/g" /seed/live_seed.sql \
  | mariadb --force -h "$MYSQL_HOST" -uroot -p"$MYSQL_ROOT_PASS" openemr

echo "seeder: verifying cohesion..."
# Definitive post-seed verification: doctor persona, encounter/vitals form
# wrappers (what makes data visible in OpenEMR's own UI), lab order-codes, and
# the denial fixture. Printed as the LAST output so the run is self-auditing.
mariadb -h "$MYSQL_HOST" -uroot -p"$MYSQL_ROOT_PASS" openemr -t -e "
SELECT
  (SELECT CONCAT(fname,' ',lname) FROM users WHERE id=1)                                             AS doctor,
  (SELECT COUNT(*) FROM forms WHERE pid IN (101,102) AND formdir='newpatient' AND deleted=0)         AS enc_wrappers,
  (SELECT COUNT(*) FROM forms WHERE pid IN (101,102) AND formdir='vitals' AND deleted=0)             AS vit_wrappers,
  (SELECT COUNT(*) FROM procedure_order_code poc JOIN procedure_order po
     ON po.procedure_order_id=poc.procedure_order_id WHERE po.patient_id IN (101,102,105))           AS lab_codes,
  (SELECT COUNT(*) FROM openemr_postcalendar_events WHERE pc_eventDate=CURDATE() AND pc_aid=1)        AS appts_today,
  (SELECT COUNT(*) FROM openemr_postcalendar_events
     WHERE pc_pid BETWEEN 101 AND 110 AND pc_sharing=1)                                              AS shared_appts,
  (SELECT gl_value FROM globals WHERE gl_name='gbl_time_zone')                                        AS clinic_tz,
  (SELECT providerID FROM patient_data WHERE pid=120)                                                AS p120_provider;"

# Ingest-writer diagnostic: the exact grants + a live write self-test as the
# copilot_ingest user, so an ingest failure can be diagnosed from these logs
# alone (no shell into the DB needed).
echo "seeder: copilot_ingest grants:"
mariadb -h "$MYSQL_HOST" -uroot -p"$MYSQL_ROOT_PASS" -N -e "SHOW GRANTS FOR 'copilot_ingest'@'%';" 2>&1 || true
echo "seeder: copilot_ingest write self-test (UPDATE sequences / documents INSERT-rollback):"
mariadb -h "$MYSQL_HOST" -u copilot_ingest -p"${INGEST_PASS}" openemr -e "
  START TRANSACTION;
  UPDATE sequences SET id = LAST_INSERT_ID(id + 1);
  SELECT LAST_INSERT_ID() INTO @newid;
  INSERT INTO documents (id,type,size,date,url,mimetype,docdate,hash,name,foreign_id,storagemethod,revision,document_data)
    VALUES (@newid,'blob',3,NOW(),'copilot://selftest','application/pdf',CURDATE(),'selftesthash','lab_report.pdf',101,0,NOW(),'AAA');
  ROLLBACK;
  SELECT 'INGEST_WRITE_OK' AS result;" 2>&1 || echo "INGEST_WRITE_FAILED (see error above)"

echo "seeder: SEED_OK — safe to delete this service"
# Keep the container alive briefly so logs are readable, then exit.
sleep 120
