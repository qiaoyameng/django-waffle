COOKIE = 'dwf_%s'
TEST_COOKIE = 'dwft_%s'
SECURE = True
MAX_AGE = 2592000  # 1 month in seconds

CACHE_PREFIX = 'waffle:'
CACHE_NAME = 'default'
FLAG_CACHE_KEY = 'flag:%s'
FLAG_USERS_CACHE_KEY = 'flag:%s:users'
FLAG_GROUPS_CACHE_KEY = 'flag:%s:groups'
ALL_FLAGS_CACHE_KEY = 'flags:all'
SAMPLE_CACHE_KEY = 'sample:%s'
ALL_SAMPLES_CACHE_KEY = 'samples:all'
SWITCH_CACHE_KEY = 'switch:%s'
ALL_SWITCHES_CACHE_KEY = 'switches:all'

FLAG_DEFAULT = False
SAMPLE_DEFAULT = False
SWITCH_DEFAULT = False

READ_FROM_WRITE_DB = False

CREATE_MISSING_FLAGS = False
CREATE_MISSING_SAMPLES = False
CREATE_MISSING_SWITCHES = False

LOG_MISSING_FLAGS = None
LOG_MISSING_SAMPLES = None
LOG_MISSING_SWITCHES = None

OVERRIDE = False
ENABLE_ADMIN_PAGES = True

# Explicit exposure ("impression") tracking. None disables exposure
# registration entirely. A callable or a dotted import path receives one
# record per confirmed flag decision.
EXPOSURE_LOGGER = None

# Callable attributes copied off ``request.user`` when an exposure is
# confirmed without an explicit ``identity`` mapping. Nothing is copied
# unless a project explicitly opts in by listing attribute names here.
EXPOSURE_IDENTITY_ATTRIBUTES = ()

# Whether exposures confirmed during a request whose response signals a
# failure (HTTP status >= 400) are still handed to the logger.
LOG_EXPOSURES_ON_ERROR_RESPONSE = False

# Whether confirmed exposures are handed to the logger when the view
# raised (or was cancelled before a successful response).
LOG_EXPOSURES_ON_EXCEPTION = False
