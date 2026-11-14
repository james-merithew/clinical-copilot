# One-off seeder — runs on Railway's private network, seeds the live DB, exits.
# Delete this service after SEED_OK appears in the logs.
FROM mariadb:11.8.8
COPY live_seed.sql /seed/live_seed.sql
COPY run.sh /seed/run.sh
RUN chmod +x /seed/run.sh
CMD ["/seed/run.sh"]
