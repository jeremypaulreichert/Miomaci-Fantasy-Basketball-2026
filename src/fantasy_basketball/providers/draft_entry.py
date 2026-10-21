"""Controlled evolving-draft qualification, using the existing native entry.

This namespace never authorizes counted competition. Evidence references are
trusted operator attestations, not signatures or automatically verified results.
"""
from dataclasses import asdict, dataclass, fields
import re

from ..core.config import digest, integer
from ..draft.native_plan import NativeDraftConfig
from ..draft.trial import NativeTrial
from . import entry
from .worker_limits import check_support


@dataclass(frozen=True)
class PreparedDraft:
    config: NativeDraftConfig
    sources: tuple[tuple[str,str], ...]
    documentation_valid_until_ms: int
    criteria_hash: str
    qualification_evidence: tuple[tuple[str,str], ...]
    source_record: str
    rules_record: str
    cohort_record: str
    unresolved: tuple[str, ...] = ()
    schema: str = 'basketball-evolving-draft-qualification/1'

    def __post_init__(self):
        if (type(self.config) is not NativeDraftConfig or self.config.mode!='LIVE_DIAGNOSTIC'
                or self.schema!='basketball-evolving-draft-qualification/1'):
            raise ValueError('explicit evolving-draft qualification package required')
        integer(self.documentation_valid_until_ms,1)
        if not isinstance(self.criteria_hash,str) or not re.fullmatch('[0-9a-f]{64}',self.criteria_hash):
            raise ValueError('bound qualification criteria required')
        if type(self.sources) is not tuple or any(type(r) is not tuple or len(r)!=2 for r in self.sources):
            raise ValueError('immutable source inventory required')
        if type(self.unresolved) is not tuple or any(not isinstance(s,str) or not s.strip() for s in self.unresolved):
            raise ValueError('explicit unresolved evidence required')
        for prefix,value in (('source:',self.source_record),('rules:',self.rules_record),('cohort:',self.cohort_record)):
            if not isinstance(value,str) or not value.startswith(prefix) or not re.fullmatch('[A-Za-z0-9_.:-]{1,100}',value) or len(value)==len(prefix):
                raise ValueError('non-secret freeze reference required')
        evidence=self.qualification_evidence
        if (type(evidence) is not tuple or any(type(r) is not tuple or len(r)!=2 or
                any(not isinstance(v,str) or not re.fullmatch('[0-9a-f]{64}',v) for v in r) for r in evidence)
                or len({r[0] for r in evidence})!=len(evidence)):
            raise ValueError('unique profile-to-evidence hash bindings required')
        expected={p.hash for p in self.config.profiles}
        if {r[0] for r in evidence} != expected and not self.unresolved:
            raise ValueError('missing representative qualification evidence must block execution')
        maximum=self.config.board.roster_size*sum(p.bound_micro_usd*p.max_attempts for p in self.config.profiles)
        if self.config.budget_micro_usd < maximum:
            raise ValueError('draft budget cannot cover frozen plan')
        for p in self.config.profiles:
            check_support(p.request_bytes,p.response_bytes,p.timeout_ms)

    @property
    def hash(self):
        return digest(asdict(self))

    def verify_sources(self):
        if self.sources != entry.source_snapshot():
            raise ValueError('source snapshot changed')


def decode_package(document):
    if not isinstance(document,dict) or set(document)!={f.name for f in fields(PreparedDraft)}:
        raise ValueError('draft package fields differ')
    data=dict(document);config=dict(data.pop('config'))
    config['profiles']=tuple(entry.decode_profile(p) for p in config['profiles'])
    config['board']=entry.decode_board(config['board'])
    data.update(config=NativeDraftConfig(**config),sources=tuple(tuple(r) for r in data['sources']),
                qualification_evidence=tuple(tuple(r) for r in data['qualification_evidence']),
                unresolved=tuple(data['unresolved']))
    return PreparedDraft(**data)


def roster(package,directory):
    """Separate native evidence format; never mislabeled as a simulated release."""
    package.verify_sources()
    directory,path=entry._location(directory,package.config)
    if not path.exists():
        raise ValueError('existing draft required')
    trial=NativeTrial(directory,package.config)
    state=trial.journal.read()
    entry._verify_recorded_package(package,state)
    if state['pause'] or state['op'] or len(state['results'])!=package.config.total_picks:
        raise ValueError('complete settled qualification draft required')
    receipt={'schema':'basketball-native-qualification-roster/1','package_hash':package.hash,
             'config_hash':package.config.hash,'simulated':False,'provisional':True,
             'competitive_authorization':False,'results':state['results']}
    return {**receipt,'receipt_hash':digest(receipt)}
