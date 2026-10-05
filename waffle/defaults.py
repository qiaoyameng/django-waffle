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

# --- Explicit exposure confirmation ---------------------------------------

# Dotted path to (or direct) callable invoked once per confirmed exposure.
# It receives a single ExposureEvent argument. It MUST NOT return a flag
# decision; its result is ignored and failures never change the decision.
EXPOSURE_RECORDER = None

# Caller-approved identity fields that may leave the process, e.g.
# ('user_id', 'session_key'). Only keys present here can ever be recorded;
# passing any other key to ``FlagDecision.confirm()`` raises ValueError.
EXPOSURE_IDENTITY_FIELDS: tuple[str, ...] = ()

# Mapping of approved field -> dotted path of a callable used to pull that
# field off the request when a flag is confirmed without explicit identity
# values (e.g. from the ``{% flag %}`` template tag). The resolver receives
# the request and must return a JSON-safe value or None. Fields not listed
# in EXPOSURE_IDENTITY_FIELDS are never resolved.
EXPOSURE_IDENTITY_RESOLVERS: dict[str, str] = {
    'user_id': 'waffle.exposure.default_user_id',
    'session_key': 'waffle.exposure.default_session_key',
}

# When confirmed exposures are submitted if the view raised a regular
# exception (5xx-style response failure).
EXPOSURE_SUBMIT_ON_ERROR = False

# When confirmed exposures are submitted if the request was cancelled
# (e.g. the client disconnected and Django cancelled the view).
EXPOSURE_SUBMIT_ON_CANCEL = False
