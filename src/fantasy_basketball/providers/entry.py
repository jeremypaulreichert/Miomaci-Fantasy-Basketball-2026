"""Explicit diagnostic entry; preparation and local recovery never authorize HTTP.

Approval records are trusted owner-supplied attestations, not signatures. The
CLI checks pinned source before imports; direct Python callers trust this module.
Credentials come only from an explicit callback and are never serialized here.
"""
from dataclasses import asdict, dataclass, fields
import hashlib
import json
from pathlib import Path
import re

from ..core.config import canonical, digest, integer
from ..core.journal import STATE_ROOT
from ..draft.board import Board, Player
from ..draft.trial import Clock, NativeTrial, TrialCase, TrialConfig, time_failure
from .native import NativeProfile, endpoint_for
from .transport import HTTPTransport, LiveAuthorization
from .worker_limits import check_support

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def source_snapshot():
    paths = [PROJECT_ROOT / 'src/fantasy_basketball/__init__.py']
    for directory in ('core', 'draft', 'providers'):
        paths.extend((PROJECT_ROOT / 'src/fantasy_basketball' / directory).glob('*.py'))
    if any(p.resolve()!=p.absolute() for p in paths):
        raise ValueError('source files must not redirect')
    return tuple(sorted((p.relative_to(PROJECT_ROOT).as_posix(), hashlib.sha256(p.read_bytes()).hexdigest()) for p in paths))


@dataclass(frozen=True)
class PreparedDiagnostic:
    config: TrialConfig
    sources: tuple[tuple[str, str], ...]
    documentation_valid_until_ms: int
    unresolved: tuple[str, ...] = ()
    schema: str = 'basketball-controlled-diagnostic/1'

    def __post_init__(self):
        if type(self.config) is not TrialConfig or self.config.mode != 'LIVE_DIAGNOSTIC' or self.schema != 'basketball-controlled-diagnostic/1':
            raise ValueError('diagnostic package required')
        integer(self.documentation_valid_until_ms, 1)
        if type(self.sources) is not tuple or any(type(r) is not tuple or len(r)!=2 for r in self.sources):
            raise ValueError('immutable source snapshot required')
        if type(self.unresolved) is not tuple or any(not isinstance(s,str) or not s.strip() for s in self.unresolved):
            raise ValueError('explicit unresolved evidence required')
        if sum(c.profile.max_attempts*c.profile.bound_micro_usd for c in self.config.cases) > self.config.budget_micro_usd:
            raise ValueError('budget cannot cover frozen request plan')
        for case in self.config.cases:
            check_support(case.profile.request_bytes, case.profile.response_bytes, case.profile.timeout_ms)

    @property
    def hash(self):
        return digest(asdict(self))

    def verify_sources(self):
        # Compare with the fixed module inventory; no user-provided path is read.
        if self.sources != source_snapshot():
            raise ValueError('source snapshot changed')


@dataclass(frozen=True)
class EntryPermit:
    package_hash: str
    config_hash: str
    not_before_ms: int
    expires_ms: int
    budget_micro_usd: int
    max_requests: int
    owner_record: str
    review_record: str
    compatibility_record: str
    account_profile_hashes: tuple[str, ...]
    billing_scope: str
    paid_execution_authorized: bool = False
    schema: str = 'basketball-diagnostic-permit/1'

    def __post_init__(self):
        if self.schema != 'basketball-diagnostic-permit/1' or type(self.paid_execution_authorized) is not bool:
            raise ValueError('literal diagnostic authorization required')
        if self.billing_scope != 'API_METERED_USD_EXCLUDING_TAX':
            raise ValueError('explicit metered-charge scope required; taxes/topups are separate')
        for value in (self.package_hash, self.config_hash):
            if not isinstance(value,str) or not re.fullmatch('[0-9a-f]{64}',value):
                raise ValueError('bound approval hashes required')
        for value in (self.not_before_ms,self.expires_ms,self.budget_micro_usd,self.max_requests):
            integer(value)
        if self.not_before_ms >= self.expires_ms:
            raise ValueError('bounded approval window required')
        for prefix,value in (('owner:',self.owner_record),('review:',self.review_record),('compatibility:',self.compatibility_record)):
            if not isinstance(value,str) or not value.startswith(prefix) or not re.fullmatch('[A-Za-z0-9_.:-]{1,100}',value) or len(value)==len(prefix):
                raise ValueError('non-secret attestation reference required')
        if type(self.account_profile_hashes) is not tuple or len(set(self.account_profile_hashes))!=len(self.account_profile_hashes) or any(not isinstance(v,str) or not re.fullmatch('[0-9a-f]{64}',v) for v in self.account_profile_hashes):
            raise ValueError('explicit account/model/billing attestations required')

    def check(self, package, now):
        integer(now)
        config = package.config
        expected = tuple(sorted({c.profile.hash for c in config.cases}))
        if (self.paid_execution_authorized is not True or package.unresolved
                or self.package_hash != package.hash or self.config_hash != config.hash
                or self.budget_micro_usd != config.budget_micro_usd or self.max_requests != config.max_requests
                or tuple(sorted(self.account_profile_hashes)) != expected
                or not self.not_before_ms <= now < self.expires_ms <= package.documentation_valid_until_ms):
            raise ValueError('paid diagnostic gates incomplete, changed or expired')


def _location(directory, config):
    directory = Path(directory).absolute()
    anchor = STATE_ROOT.absolute()
    if anchor.resolve()!=anchor or directory.resolve()!=directory or not directory.is_relative_to(anchor):
        raise ValueError('basketball state directory required')
    return directory, directory/(config.run_id+'.sqlite3')


def status(package, directory, *, clock=None):
    package.verify_sources()
    directory,path = _location(directory,package.config)
    if not path.exists():
        return {'phase':'NOT_STARTED','config_hash':package.config.hash,'competitive_authorization':False}
    return NativeTrial(directory,package.config,clock=clock).status()


def bound_authorization(package, permit):
    # Existing START/authorization events durably bind the whole non-secret
    # permit and source/package snapshot without a second audit database.
    record=canonical({'owner':permit.owner_record,'permit_hash':digest(asdict(permit)),
                      'package_hash':package.hash,'compatibility':permit.compatibility_record})
    return LiveAuthorization(package.config.hash,permit.expires_ms,record,permit.review_record)


def _verify_recorded_package(package, state):
    recorded=state['authorization']
    if recorded is None and state['started_ms'] is None:
        return
    try:
        audit=json.loads(recorded['owner_record'])
    except (TypeError,ValueError,KeyError):
        raise ValueError('run was not bound by this entry workflow') from None
    if not isinstance(audit,dict) or audit.get('package_hash')!=package.hash:
        raise ValueError('recorded entry package differs; preserve existing run')


def _settle_local(package, trial):
    def local(state):
        # Recheck under the writer lock, including a concurrent first START.
        _verify_recorded_package(package,state)
        if state['pause'] or len(state['results'])==len(trial.config.cases):
            return
        now=trial.clock.now_ms()
        op=state['op'];attempt=op['attempts'][-1] if op and op['attempts'] else None
        if attempt and attempt['state']=='RESPONDED':
            # Journal applies each yielded event before resuming this generator.
            # Settlement and any resulting expiry pause commit atomically.
            yield ('RESOLVE',{'at_ms':now})
        elif attempt and attempt['state']=='DISPATCHED':
            yield ('PAUSE',{'reason':'UNKNOWN_DISPATCH'})
            return
        if state['pause'] or len(state['results'])==len(trial.config.cases) or state['started_ms'] is None:
            return
        reason=time_failure(state,now)
        if reason is None and now>=state['authorization']['expires_ms']:
            reason='AUTHORIZATION_EXPIRED'
        if reason:
            yield ('PAUSE',{'reason':reason})
    trial.journal.transact(local)


def recover(package, directory, decision, *, clock=None):
    """Adjudicate locally under the original package; never collect credentials."""
    package.verify_sources()
    directory,path=_location(directory,package.config)
    if not path.exists():
        raise ValueError('existing entry run required')
    trial=NativeTrial(directory,package.config,clock=clock)
    state=trial.journal.read()
    if state['authorization'] is None:
        raise ValueError('run was not bound by this entry workflow')
    _verify_recorded_package(package,state)
    trial.recover(decision)
    _settle_local(package,trial)
    return trial.status()


def resume(package, directory, permit, credential_provider, *, clock=None):
    package.verify_sources()
    clock = clock or Clock()
    config = package.config
    directory,path = _location(directory,config)
    trial = NativeTrial(directory,config,clock=clock) if path.exists() else None
    if trial:
        _verify_recorded_package(package,trial.journal.read())
        # Never clear a pause, change authority or retry an unknown dispatch.
        _settle_local(package,trial)
        state=trial.status()
        if state['phase'] in {'COMPLETE','PAUSED'}: return state
    if type(permit) is not EntryPermit:
        raise ValueError('explicit permit required before credential access')
    permit.check(package,clock.now_ms())
    authorization=bound_authorization(package,permit)
    generation=None
    if trial:
        prior_state=trial.journal.read()
        generation=len(prior_state['recoveries'])
        prior=prior_state['authorization']
        if prior is not None and prior!=asdict(authorization):
            raise ValueError('recorded authority differs; preserve existing run')
    credentials={}
    transport=None
    try:
        for provider in sorted({c.profile.provider for c in config.cases}):
            # A slow/cancelled prompt cannot extend permission to dispatch.
            permit.check(package,clock.now_ms())
            try:
                credentials[provider]=credential_provider(provider)
            except (Exception,KeyboardInterrupt):
                raise ValueError('credential collection cancelled or failed') from None
        permit.check(package,clock.now_ms())
        package.verify_sources()
        if trial:
            current=trial.journal.read()
            if current['pause'] or len(current['results'])==len(config.cases):
                return trial.status()
            if len(current['recoveries'])!=generation:
                raise ValueError('run adjudicated while collecting credentials')
        transport=HTTPTransport(config,authorization,credentials)
        trial=trial or NativeTrial(directory,config,clock=clock)
        return trial.resume(transport)
    finally:
        credentials.clear()
        if transport is not None:
            transport.credentials.clear()


def decode_profile(document):
    p=dict(document)
    p['accepted_models']=tuple(p['accepted_models'])
    p['documentation_urls']=tuple(p['documentation_urls'])
    return NativeProfile(**p)


def decode_board(document):
    b=dict(document);players=[]
    for original in b.pop('players'):
        player=dict(original);player['positions']=tuple(player['positions']);player['facts']=tuple(tuple(r) for r in player['facts'])
        players.append(Player(**player))
    b.update(players=tuple(players),ownership=tuple(tuple(r) for r in b['ownership']),locked_ids=tuple(b['locked_ids']),slots=tuple((r[0],tuple(r[1])) for r in b['slots']))
    return Board(**b)


def decode_package(document):
    """Strict JSON data only; no imports, factories or executable class names."""
    if not isinstance(document,dict) or set(document)!={f.name for f in fields(PreparedDiagnostic)}:
        raise ValueError('package fields differ')
    data=dict(document);config=dict(data.pop('config'))
    selected=[]
    for row in config.pop('cases'):
        if set(row)!= {'profile','board'}: raise ValueError('case fields differ')
        selected.append(TrialCase(decode_profile(row['profile']),decode_board(row['board'])))
    data.update(config=TrialConfig(cases=tuple(selected),**config),sources=tuple(tuple(r) for r in data['sources']),unresolved=tuple(data['unresolved']))
    return PreparedDiagnostic(**data)
