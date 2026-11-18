import os

# MySQL is the deployment default (docker compose overrides this).
# SQLite is accepted for local drills and tests.
DATABASE_URL = os.getenv('DATABASE_URL', 'sqlite:///./online-index.db')
JWT_SECRET = os.getenv('JWT_SECRET', 'local-online-index-change-before-public-use')
SEED_DEMO = os.getenv('SEED_DEMO', 'true').lower() == 'true'
