class VoterRouter:
    """Keep every Django model on local SQLite ('default') and never migrate RDS.

    - 'rds'  (cvl_national)   — voter roll, read-only (also enforced by MySQL session mode).
    - 'ext'  (generic_360_db) — EMS transactions (machinery + audit), written with raw SQL
                                in municipal/machinery.py. Its tables already exist and are
                                shared with CVL-NATIONAL, so Django must never create/alter them.
    No Django model is bound to either RDS database.
    """

    def db_for_read(self, model, **hints):
        return 'default'

    def db_for_write(self, model, **hints):
        return 'default'

    def allow_relation(self, obj1, obj2, **hints):
        return True

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        return db == 'default'
