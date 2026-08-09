"""Prospective, dependency-injected paper workflow; never a brokerage interface."""
from __future__ import annotations
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import fcntl, hashlib, json, os, tempfile
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from .mandate import DEFAULT_MANDATE
from .market_data import EvidenceKind, EvidencePacket, assess, canonical_json, ingest_response
from .accounting import Position
from .models import AssetType, Instrument, Observation, OptionRight, Order, OrderSubmission, ResearchCandidate, Side, TradingDecision
from .operations import PaperRun, seal_artifact, verify_artifact
from .risk import PortfolioRisk

UtcClock=Callable[[],datetime]

@dataclass(frozen=True)
class ReadResult:
    provider: str; feed: str; raw: Any; normalized: Mapping[str,Any]

class CollectionProviders(Protocol):
    def clock(self)->ReadResult: ...
    def calendar(self,start:str,end:str)->ReadResult: ...
    def underlying_quote(self,symbol:str)->ReadResult: ...
    def option_chain(self,underlying:str)->ReadResult: ...
    def option_quote(self,symbol:str)->ReadResult: ...
    def corporate_action(self,underlying:str)->ReadResult: ...
    def dividend(self,underlying:str)->ReadResult: ...
    def fx(self,pair:str)->ReadResult: ...

class AtomicCheckpoint:
    def __init__(self,path:Path,bindings=None): self.path=path; self.bindings=dict(bindings or {})
    def _seal(self,value):
        body={'bindings':self.bindings,'value':value}; checkpoint_id=hashlib.sha256(canonical_json(body)).hexdigest()
        return {'checkpoint_id':checkpoint_id,**body,'seal':hashlib.sha256(canonical_json({'checkpoint_id':checkpoint_id,**body})).hexdigest()}
    def read(self):
        if not self.path.exists(): return {}
        envelope=json.loads(self.path.read_text()); expected=self._seal(envelope.get('value'))
        if envelope!=expected: raise ValueError('checkpoint seal or bound inputs mismatch')
        return envelope['value']
    def write(self,value):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        lock_path=self.path.with_suffix(self.path.suffix+'.lock')
        with lock_path.open('a+') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX); fd,name=tempfile.mkstemp(dir=self.path.parent,prefix=self.path.name+'.')
            try:
                with os.fdopen(fd,'w') as f:
                    f.write(json.dumps(self._seal(value),sort_keys=True,indent=2)+'\n'); f.flush(); os.fsync(f.fileno())
                os.replace(name,self.path)
                directory=os.open(self.path.parent,os.O_RDONLY); os.fsync(directory); os.close(directory)
            finally:
                if os.path.exists(name): os.unlink(name)

def _now(clock):
    value=clock()
    if value.tzinfo is None or value.utcoffset()!=timezone.utc.utcoffset(value): raise ValueError('injected clock must return UTC')
    return value

def collect_packet(kind,call,clock,request):
    started=_now(clock); result=call(); received=_now(clock)
    if not isinstance(result,ReadResult): raise ValueError(f'{kind.value} provider returned malformed response')
    n=dict(result.normalized)
    if kind is not EvidenceKind.CALENDAR:
        if not isinstance(n.get('timestamp'),str): raise ValueError(f'{kind.value} provider observation timestamp unavailable')
        try: observed=_time(n['timestamp'])
        except (TypeError,ValueError): raise ValueError(f'{kind.value} provider observation timestamp malformed') from None
        if observed>received: raise ValueError(f'{kind.value} provider observation is after response receipt')
    packet=ingest_response(kind=kind,provider=result.provider,feed=result.feed,request={**request,'request_started_at':started.isoformat()},requested_at=started,received_at=received,raw=result.raw,normalized=n)
    return packet

def _run(portfolio=None): return PaperRun(DEFAULT_MANDATE,portfolio or PortfolioRisk(Decimal('100000'),Decimal('100000'),Decimal('100000'),{},{}))
def _time(x): return datetime.fromisoformat(x).astimezone(timezone.utc)
def _attested_local(value,kind,trusted_attestor):
    from .attestation import AttestedArtifact, load_attested_artifact, verify_attested_artifact
    envelope=value if isinstance(value,AttestedArtifact) else load_attested_artifact(value)
    checked=verify_attested_artifact(envelope,trusted_attestor=trusted_attestor)
    if not checked.verified or not checked.launch_eligible or envelope.original_artifact.get('artifact_kind')!=kind:
        raise ValueError(f'trusted externally attested {kind} required')
    return envelope,envelope.original_artifact

def _research(value):
    ok,reasons=verify_artifact(value,'research')
    if not ok: raise ValueError('; '.join(reasons))
    p=value['payload']; candidates=tuple(ResearchCandidate(**x) for x in p['candidates'])
    obs=tuple(Observation(x['name'],x['value'],_time(x['available_at'])) for x in p['evidence_references'])
    from .models import ResearchPacket
    record=ResearchPacket(p['research_id'],p['mandate_version'],_time(p['research_at']),candidates,obs,p['rationale'],p['record_seal'])
    if not record.verify(): raise ValueError('research record seal mismatch')
    return record

INSTRUMENT_FIELDS={'symbol','asset_type','issuer','sector','market','currency','is_etf','underlying','expiration','strike','right','multiplier','adjusted','occ_verified'}
ORDER_FIELDS={'order_id','instrument','side','quantity','intent','exit_entire_holding','sale_floor'}
def _instrument(s):
    if set(s)!=INSTRUMENT_FIELDS: raise ValueError('instrument proposal fields must be exact')
    asset=AssetType(s['asset_type'])
    if asset is AssetType.OPTION and s['currency']!='USD': raise ValueError('options require USD currency')
    return Instrument(s['symbol'],asset,s['issuer'],s['sector'],s['market'],is_etf=bool(s['is_etf']),underlying=s['underlying'],expiry=datetime.fromisoformat(s['expiration']).date() if s['expiration'] else None,strike=Decimal(s['strike']) if s['strike'] is not None else None,right=OptionRight(s['right']) if s['right'] else None,multiplier=int(s['multiplier']),adjusted=bool(s['adjusted']),occ_verified=bool(s['occ_verified']))
def _order(op,instrument):
    if set(op)!=ORDER_FIELDS: raise ValueError('operation proposal fields must be exact')
    return Order(op['order_id'],instrument,Side(op['side']),int(op['quantity']),op['intent'],bool(op['exit_entire_holding']),Decimal(op['sale_floor']) if op['sale_floor'] is not None else None)

def portfolio_from_artifact(supplied, trusted_attestor=None):
    from .attestation import AttestedArtifact, load_attested_artifact, verify_attested_artifact
    envelope=supplied if isinstance(supplied,AttestedArtifact) else load_attested_artifact(supplied)
    checked=verify_attested_artifact(envelope,trusted_attestor=trusted_attestor)
    if not checked.verified or not checked.launch_eligible: raise ValueError('trusted externally attested portfolio snapshot required')
    artifact=envelope.original_artifact; p=artifact['payload']
    return _portfolio_payload(p)

def _portfolio_payload(p):
    if set(p)!={'nav_gbp','peak_nav_gbp','cash_gbp','positions','pending_submissions'}: raise ValueError('portfolio snapshot schema mismatch')
    positions={}; marks={}
    for row in p['positions']:
        if set(row)!={'instrument','quantity','cost_basis_gbp','mark_gbp'}: raise ValueError('portfolio position schema mismatch')
        instrument=_instrument(row['instrument'])
        if instrument in positions: raise ValueError('duplicate portfolio instrument')
        quantity=int(row['quantity']); mark=Decimal(row['mark_gbp'])
        if quantity==0 or mark<0 or not mark.is_finite(): raise ValueError('invalid portfolio quantity or mark')
        positions[instrument]=Position(instrument,quantity,Decimal(row['cost_basis_gbp'])); marks[instrument]=mark
    pending_cash=Decimal('0'); pending_collateral=Decimal('0'); pending_covered={}
    for pending in p['pending_submissions']:
        if set(pending)!={'instrument','side','intent','quantity','reserved_cash_gbp','reserved_collateral_gbp','covered_shares','submission_artifact_id'}:
            raise ValueError('pending submission schema mismatch')
        instrument=_instrument(pending['instrument']); quantity=int(pending['quantity'])
        if pending['side'] not in {'buy','sell'} or pending['intent'] not in {'open','close','assign'} or not pending['submission_artifact_id'] or quantity<=0: raise ValueError('invalid pending submission')
        reserved_cash=Decimal(pending['reserved_cash_gbp']); reserved_collateral=Decimal(pending['reserved_collateral_gbp']); covered=int(pending['covered_shares'])
        if min(reserved_cash,reserved_collateral,Decimal(covered))<0: raise ValueError('invalid pending reservation')
        pending_cash+=reserved_cash; pending_collateral+=reserved_collateral
        if covered: pending_covered[instrument.underlying]=pending_covered.get(instrument.underlying,0)+covered
    nav,peak,cash=Decimal(p['nav_gbp']),Decimal(p['peak_nav_gbp']),Decimal(p['cash_gbp'])
    if any(not x.is_finite() or x<0 for x in (nav,peak,cash)) or nav==0 or peak<nav: raise ValueError('invalid portfolio cash or NAV')
    return PortfolioRisk(nav,peak,cash,positions,marks,pending_cash,pending_collateral,pending_covered)

def portfolio_collect(source):
    """Validate and seal a portfolio snapshot before external append/read-back."""
    _portfolio_payload(source)
    return seal_artifact('portfolio_snapshot',source)
def _evidence_refs(packets): return [{'name':p.kind.value,'value':p.packet_id,'available_at':p.received_at.isoformat(),'request_started_at':p.requested_at.isoformat(),'provider_observed_at':p.normalized.get('timestamp')} for p in packets]

def research_collect(source,providers,clock):
    if set(source)!={'candidates'} or not source['candidates']: raise ValueError('collection input permits only candidates')
    candidates=tuple(ResearchCandidate(**x) for x in source['candidates']); packets=[]
    packets.append(collect_packet(EvidenceKind.CLOCK,providers.clock,clock,{}))
    day=_now(clock).date().isoformat(); packets.append(collect_packet(EvidenceKind.CALENDAR,lambda:providers.calendar(day,day),clock,{'start':day,'end':day}))
    for c in candidates: packets.append(collect_packet(EvidenceKind.UNDERLYING_QUOTE,lambda c=c:providers.underlying_quote(c.underlying),clock,{'symbol':c.underlying}))
    at=_now(clock)
    for packet in packets:
        expected=packet.request.get('symbol') if packet.kind is EvidenceKind.UNDERLYING_QUOTE else None
        checked=assess(packet,as_of=at,cutoff=at,max_age=timedelta(seconds=DEFAULT_MANDATE.max_quote_age_seconds) if expected else None,expected_symbol=expected)
        if not checked.actionable: raise ValueError(f'incomplete research evidence: {packet.kind.value}: '+', '.join(checked.reasons))
    run=_run(); refs=tuple(Observation(p.kind.value,p.packet_id,p.received_at) for p in packets)
    record=run.create_research(at,candidates,refs)
    payload={'research_id':record.packet_id,'mandate_version':record.mandate_version,'research_at':at.isoformat(),'candidates':[asdict(x) for x in candidates],'evidence_references':_evidence_refs(packets),'evidence_packets':[p.as_json() for p in packets],'rationale':record.rationale,'record_seal':record.seal}
    return seal_artifact('research',payload)

def decision_collect(research_artifact,portfolio_artifact,proposal,providers,clock,trusted_attestor=None):
    research_envelope,research_local=_attested_local(research_artifact,'research',trusted_attestor)
    research=_research(research_local); op=proposal['operation']; instrument=_instrument(op['instrument'])
    portfolio=portfolio_from_artifact(portfolio_artifact,trusted_attestor)
    portfolio_local=portfolio_artifact.original_artifact if hasattr(portfolio_artifact,'original_artifact') else portfolio_artifact['original_artifact']
    if instrument.underlying not in {x.underlying for x in research.shortlist}: raise ValueError('underlying absent from research shortlist')
    day=_now(clock).date().isoformat(); calls=[
      (EvidenceKind.CLOCK,providers.clock,{}),(EvidenceKind.CALENDAR,lambda:providers.calendar(day,day),{'start':day,'end':day}),
      (EvidenceKind.UNDERLYING_QUOTE,lambda:providers.underlying_quote(instrument.underlying),{'symbol':instrument.underlying}),
      (EvidenceKind.OPTION_CHAIN,lambda:providers.option_chain(instrument.underlying),{'underlying':instrument.underlying}),
      (EvidenceKind.OPTION_QUOTE,lambda:providers.option_quote(instrument.symbol),{'symbol':instrument.symbol}),
      (EvidenceKind.CORPORATE_ACTION,lambda:providers.corporate_action(instrument.underlying),{'underlying':instrument.underlying}),
      (EvidenceKind.DIVIDEND,lambda:providers.dividend(instrument.underlying),{'underlying':instrument.underlying}),
      (EvidenceKind.FX,lambda:providers.fx('GBPUSD'),{'pair':'GBPUSD'})]
    packets=[collect_packet(k,c,clock,r) for k,c,r in calls]
    decision_at=_now(clock); run=_run(portfolio); run.research_packet=research
    order=_order(op,instrument)
    decision=run.decide(decision_at,order,packets)
    rules=asdict(run.rule_results); rules['evaluated_at']=run.rule_results.evaluated_at.isoformat()
    dp={'decision_id':decision.decision_id,'research_id':research.packet_id,'research_artifact_id':research_local['artifact_id'],'portfolio_snapshot_artifact_id':portfolio_local['artifact_id'],'decision_at':decision_at.isoformat(),'order':op,'record_seal':decision.seal,'rule_results':rules,'evidence_references':_evidence_refs(packets),'evidence_packet_ids':[p.packet_id for p in packets]}
    da=seal_artifact('decision',dp); submitted_at=_now(clock); submission=run.submit(submitted_at)
    ledger_event={'event_id':'submission:'+decision.decision_id,'kind':'paper_submission','occurred_at':submission.submitted_at.isoformat(),'applied':False,'requires_launch_authorization':True}
    sp={'decision_artifact_id':da['artifact_id'],'decision_id':decision.decision_id,'decision_at':decision_at.isoformat(),'submitted_at':submission.submitted_at.isoformat(),'order':op,'rule_results_seal':rules['seal'],'evidence_packet_ids':[p.packet_id for p in packets],'paper_ledger_event':ledger_event}
    return da,seal_artifact('submission',sp)

def fill_collect(research_artifact,portfolio_artifact,decision_artifact,submission_artifact,providers,clock,trusted_attestor=None):
    research_envelope,research_local=_attested_local(research_artifact,'research',trusted_attestor)
    decision_envelope,decision_local=_attested_local(decision_artifact,'decision',trusted_attestor)
    submission_envelope,submission_local=_attested_local(submission_artifact,'submission',trusted_attestor)
    research=_research(research_local); d,s=decision_local['payload'],submission_local['payload']
    if d.get('research_artifact_id')!=research_local['artifact_id'] or d.get('research_id')!=research.packet_id: raise ValueError('research ancestry substitution')
    portfolio=portfolio_from_artifact(portfolio_artifact,trusted_attestor)
    portfolio_local=portfolio_artifact.original_artifact if hasattr(portfolio_artifact,'original_artifact') else portfolio_artifact['original_artifact']
    if d.get('portfolio_snapshot_artifact_id')!=portfolio_local['artifact_id']: raise ValueError('portfolio ancestry substitution')
    if s.get('decision_artifact_id')!=decision_local['artifact_id'] or s.get('decision_id')!=d.get('decision_id'): raise ValueError('decision ancestry substitution')
    if canonical_json(s.get('order'))!=canonical_json(d.get('order')): raise ValueError('submission order differs from decision')
    if s.get('decision_at')!=d.get('decision_at') or _time(s['submitted_at'])<=_time(d['decision_at']): raise ValueError('decision/submission chronology invalid')
    if s.get('rule_results_seal')!=d.get('rule_results',{}).get('seal'): raise ValueError('rule-results ancestry substitution')
    op=s['order']; instrument=_instrument(op['instrument']); packet=collect_packet(EvidenceKind.OPTION_QUOTE,lambda:providers.option_quote(instrument.symbol),clock,{'symbol':instrument.symbol})
    run=_run(portfolio); order=_order(op,instrument); run.orders.append(OrderSubmission(s['decision_id'],_time(s['decision_at']),order,_time(s['submitted_at'])))
    filled_at=_now(clock); fill=run.simulate_fill(packet,as_of=filled_at); fill.update(submission_artifact_id=submission_local['artifact_id'],research_artifact_id=research_local['artifact_id'],portfolio_snapshot_artifact_id=portfolio_local['artifact_id'],request_started_at=packet.requested_at.isoformat(),provider_observed_at=packet.normalized['timestamp'],response_received_at=packet.received_at.isoformat(),paper_ledger_event={'event_id':'fill:'+s['decision_id']+':'+packet.packet_id,'kind':'paper_fill','occurred_at':filled_at.isoformat(),'applied':False,'requires_launch_authorization':True})
    return seal_artifact('fill',fill)
