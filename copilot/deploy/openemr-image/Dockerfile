# Clinical Co-Pilot: hardened OpenEMR fork image for Railway.
#  = stock openemr/openemr:8.2.0
#  + our embed module (Co-Pilot card in the patient chart)
#  + a BAKED, pre-configured sqlconf.php ($config = 1, env-based creds)
#  + MANUAL_SETUP=yes
# Result: the entrypoint's auto-installer (which re-installed and WIPED the DB
# on every ephemeral restart) can never fire; boots go straight to Apache
# against the persistent MariaDB.
FROM openemr/openemr:8.2.0

# The in-chart Co-Pilot embed module (event-listener card; zero core edits).
COPY module /var/www/localhost/htdocs/openemr/interface/modules/custom_modules/oe-module-copilot

# Pre-configured sqlconf.php: $config=1 short-circuits run_auto_configure.
COPY --chown=apache:apache sqlconf.php /var/www/localhost/htdocs/openemr/sites/default/sqlconf.php
RUN chmod 0400 /var/www/localhost/htdocs/openemr/sites/default/sqlconf.php

# Belt-and-suspenders: skip setup + per-boot global writes explicitly.
ENV MANUAL_SETUP=yes
