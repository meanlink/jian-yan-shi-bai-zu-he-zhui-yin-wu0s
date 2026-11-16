import os
DATABASE_URL = os.getenv('DATABASE_URL', 'sqlite:///./local.db')
REDIS_URL = os.getenv('REDIS_URL', 'redis://redis:6379/0')
JWT_SECRET = os.environ.get('JWT_SECRET', 'local-development-change-before-deploy')
ARTIFACT_DIR = os.getenv('ARTIFACT_DIR', '/artifacts')
RECHECK_TZ_OFFSET_MINUTES = int(os.getenv('RECHECK_TZ_OFFSET_MINUTES', '480'))
RECHECK_PREVIEW_TTL_SECONDS = int(os.getenv('RECHECK_PREVIEW_TTL_SECONDS', '300'))
RECHECK_HORIZON_DAYS = int(os.getenv('RECHECK_HORIZON_DAYS', '21'))
RECHECK_DEFAULT_ITEM_MINUTES = int(os.getenv('RECHECK_DEFAULT_ITEM_MINUTES', '10'))
