"""Explicit proposed standard Meta Model API profile, not live qualification."""
from .native import NativeProfile

SOURCES=(
    'https://dev.meta.ai/docs/models',
    'https://dev.meta.ai/docs/protocols/chat-completions',
    'https://dev.meta.ai/docs/reasoning',
    'https://dev.meta.ai/docs/pricing-rate-limits',
)


def candidate_profile():
    # Standard mode only. No cache discount, Contributor switch, tools or
    # smaller byte/token estimate. Native combined cap includes reasoning.
    return NativeProfile('qualification-meta-direct','meta_direct','muse-spark-1.3',
                         ('muse-spark-1.3',),'medium',1048576,1048576,16384,
                         '1.25','4.25',600000,1200000,2,4194304,262144,5000,SOURCES,
                         schema='basketball-native-profile/2')


def wonder_candidate_profile(*,effort='high'):
    """Documented standard API candidate; no default/cohort or authority change.

    October18 official model/pricing pages:1,048,576 context, $4 input/$20
    output per million, no long-context premium. Preserve conservative full
    context reservation; do not assume cache discounts or contributor pricing.
    High effort is a candidate setting, not equal-compute or best-task evidence.
    """
    return NativeProfile('qualification-meta-frontier-wonder','meta_direct','muse-wonder-1.0',
                         ('muse-wonder-1.0',),effort,1048576,1048576,16384,
                         '4','20',600000,1200000,2,4194304,262144,5000,SOURCES,
                         schema='basketball-native-profile/2')
