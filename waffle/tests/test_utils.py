from django.conf import settings
from django.test import TestCase
from django.test.utils import override_settings

from waffle import defaults
from waffle.utils import get_setting, identity_in_percent, rollout_bucket


class GetSettingTests(TestCase):
    def test_overridden_setting(self):
        prefix = get_setting('CACHE_PREFIX')
        self.assertEqual(settings.WAFFLE_CACHE_PREFIX, prefix)

    def test_default_setting(self):
        age = get_setting('MAX_AGE')
        self.assertEqual(defaults.MAX_AGE, age)

    def test_override_settings(self):
        assert not get_setting('OVERRIDE')
        with override_settings(WAFFLE_OVERRIDE=True):
            assert get_setting('OVERRIDE')


class RolloutBucketTests(TestCase):
    def test_bucket_is_stable_and_bounded(self):
        buckets = [rollout_bucket('42', 'flag', 'salt') for _ in range(10)]
        assert all(b == buckets[0] for b in buckets)
        assert 0 <= buckets[0] < defaults.ROLLOUT_BUCKET_COUNT

    def test_bucket_isolated_per_flag_and_salt(self):
        # Different flags/salts must produce independent bucket keys.
        same_flag_same_salt = rollout_bucket('1', 'f', 's')
        other_flag = rollout_bucket('1', 'g', 's')
        other_salt = rollout_bucket('1', 'f', 't')
        assert same_flag_same_salt != other_flag
        assert same_flag_same_salt != other_salt

    def test_buckets_are_roughly_uniform(self):
        chosen = sum(
            identity_in_percent(
                rollout_bucket(str(i), 'f', 's'), 10)
            for i in range(10000)
        )
        # 10% over a fixed 10k identity set: generous bounds, no randomness.
        assert 800 < chosen < 1200

    def test_higher_percent_is_a_superset(self):
        def selected(percent):
            return {
                i for i in range(3000)
                if identity_in_percent(
                    rollout_bucket(str(i), 'f', 's'), percent)
            }

        at_10 = selected(10)
        at_20 = selected(20)
        assert at_10.issubset(at_20)
