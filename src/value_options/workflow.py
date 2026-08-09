"""Prospective, dependency-injected paper workflow; never a brokerage interface."""
from __future__ import annotations
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import fcntl, hashlib, json, os, tempfile
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from .mandate import DEFAULT_MANDATE
from .market_data import EvidenceKind, EvidencePacket, assess, canonical_json, ingest_response, load_packet
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
    timestamp_optional=(kind is EvidenceKind.CALENDAR or
        kind in {EvidenceKind.CORPORATE_ACTION,EvidenceKind.DIVIDEND} and n.get('negative_evidence') is True)
    if not timestamp_optional:
        if not isinstance(n.get('timestamp'),str): raise ValueError(f'{kind.value} provider observation timestamp unavailable')
        try: observed=_time(n['timestamp'])
        except (TypeError,ValueError): raise ValueError(f'{kind.value} provider observation timestamp malformed') from None
        if observed>received: raise ValueError(f'{kind.value} provider observation is after response receipt')
    if timestamp_optional and kind in {EvidenceKind.CORPORATE_ACTION,EvidenceKind.DIVIDEND}:
        n['retrieved_at']=received.isoformat()
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
    required={'nav_gbp','peak_nav_gbp','cash_gbp','positions','pending_submissions',
              'drawdown','source_ledger_record_ids','source_ledger_records',
              'ledger_ancestry_sha256','initialization_mode','fx_evidence',
              'valuation_at','anchor_artifact_id'}
    if set(p)!=required: raise ValueError('portfolio snapshot schema mismatch')
    positions={}; marks={}
    for row in p['positions']:
        if set(row)!={'instrument','quantity','cost_basis_gbp','mark_gbp'}: raise ValueError('portfolio position schema mismatch')
        instrument=_instrument(row['instrument'])
        if instrument in positions: raise ValueError('duplicate portfolio instrument')
        quantity=int(row['quantity']); mark=Decimal(row['mark_gbp'])
        if quantity==0 or mark<0 or not mark.is_finite(): raise ValueError('invalid portfolio quantity or mark')
        cost=Decimal(row['cost_basis_gbp'])
        if not cost.is_finite() or cost<0: raise ValueError('invalid position cost basis')
        positions[instrument]=Position(instrument,quantity,cost); marks[instrument]=mark
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
    drawdown=max(Decimal('0'),(peak-nav)/peak)
    if Decimal(p['drawdown'])!=drawdown: raise ValueError('portfolio drawdown mismatch')
    refs=p['source_ledger_record_ids']
    if not isinstance(refs,list) or len(refs)!=len(set(refs)) or any(not isinstance(x,str) or not x for x in refs): raise ValueError('invalid source ledger references')
    if not isinstance(p['initialization_mode'],bool): raise ValueError('invalid initialization mode')
    if not isinstance(p['source_ledger_records'],list) or [x.get('record_id') for x in p['source_ledger_records']]!=refs: raise ValueError('ledger ancestry order mismatch')
    if p['ledger_ancestry_sha256']!=hashlib.sha256(canonical_json(p['source_ledger_records'])).hexdigest(): raise ValueError('ledger ancestry hash mismatch')
    if p['fx_evidence'] is not None:
        fx=load_packet(p['fx_evidence'])
        if not fx.verify() or fx.kind is not EvidenceKind.FX or fx.normalized.get('symbol')!='GBPUSD': raise ValueError('invalid snapshot FX evidence')
    if p['valuation_at'] is not None: _time(p['valuation_at'])
    if p['initialization_mode'] != (p['anchor_artifact_id'] is None): raise ValueError('portfolio anchor/initialization mismatch')
    return PortfolioRisk(nav,peak,cash,positions,marks,pending_cash,pending_collateral,pending_covered)

def seal_ledger_record(value, *, sequence=0, previous_record_id=""):
    """Create immutable identity for fixture/import preparation, never mutate one."""
    body=dict(value); body.pop('record_id',None); body.pop('seal',None)
    body['sequence']=sequence; body['previous_record_id']=previous_record_id
    record_id=hashlib.sha256(canonical_json(body)).hexdigest()
    return {'record_id':record_id,**body,'seal':hashlib.sha256(canonical_json({'record_id':record_id,**body})).hexdigest()}

def _verify_ledger_record(record):
    if not isinstance(record,dict) or not record.get('record_id') or not record.get('seal'): return False
    body={k:v for k,v in record.items() if k not in {'record_id','seal'}}
    expected=seal_ledger_record(body,sequence=body.pop('sequence',None),previous_record_id=body.pop('previous_record_id',None))
    return record==expected

def portfolio_collect(source, *, initialize=False, expected_state=None, trusted_attestor=None):
    """Replay append-only paper-ledger records into a reconciled snapshot."""
    if not isinstance(source,dict) or set(source)!={'records','record_count','head_record_id','anchor'} or not isinstance(source['records'],list): raise ValueError('paper ledger input schema mismatch')
    records=source['records']; ids=[]; cash=Decimal('0'); peak=Decimal('0'); positions={}; marks={}; pending={}; initialized=False; fx_packet=None; valuation_at=None; anchor_id=None
    if source['record_count']!=len(records) or (records and source['head_record_id']!=records[-1].get('record_id')) or (not records and source['head_record_id']!=''):
        raise ValueError('paper ledger history is missing or has the wrong head')
    if records:
        if source['anchor'] is None: raise ValueError('non-bootstrap ledger history requires an external anchor')
        anchor_envelope,anchor_local=_attested_local(source['anchor'],'portfolio_snapshot',trusted_attestor)
        anchor_id=anchor_local['artifact_id']
        anchored=anchor_local['payload']; count=len(anchored['source_ledger_records'])
        if count>len(records) or records[:count]!=anchored['source_ledger_records'] or anchored['source_ledger_record_ids'][-1]!=records[count-1]['record_id']:
            raise ValueError('ledger history does not continue its externally anchored head')
    elif source['anchor'] is not None: raise ValueError('empty initialization cannot replace an anchored ledger')
    def marked_nav():
        value=cash
        for instrument,row in positions.items(): value+=Decimal(row['quantity'])*instrument.multiplier*marks.get(instrument,Decimal('0'))
        return value
    previous=""
    for expected_sequence,record in enumerate(records):
        if not _verify_ledger_record(record) or record['record_id'] in ids: raise ValueError('unverified, altered or duplicate ledger record')
        if record.get('sequence')!=expected_sequence or record.get('previous_record_id')!=previous: raise ValueError('ledger record ancestry reordered or missing')
        ids.append(record['record_id']); kind=record.get('kind')
        previous=record['record_id']
        common={'record_id','seal','sequence','previous_record_id','kind'}
        schemas={'portfolio_initialized':common|{'cash_gbp','nav_gbp','peak_nav_gbp'},
            'cash':common|{'amount_gbp'},'position':common|{'instrument','quantity_delta','cost_basis_delta_gbp'},
            'mark':common|{'instrument','mark_gbp'},'pending_submission':common|{'submission'},
            'submission_resolved':common|{'submission_artifact_id'},
            'valuation':common|{'nav_gbp','peak_nav_gbp','valued_at'},'fx':common|{'evidence_packet'}}
        if kind not in schemas or set(record)!=schemas[kind]: raise ValueError('paper ledger record schema mismatch')
        if kind=='portfolio_initialized':
            if initialized or record.get('cash_gbp')!='100000' or record.get('nav_gbp')!='100000' or record.get('peak_nav_gbp')!='100000': raise ValueError('invalid portfolio initialization record')
            cash=peak=Decimal('100000'); initialized=True
        elif not initialized: raise ValueError('ledger history lacks initialization')
        elif kind=='cash':
            cash+=Decimal(record['amount_gbp'])
            if not cash.is_finite() or cash<0: raise ValueError('paper ledger cash cannot be negative or non-finite')
        elif kind=='position':
            instrument=_instrument(record['instrument']); row=positions.setdefault(instrument,{'instrument':record['instrument'],'quantity':0,'cost_basis_gbp':'0','mark_gbp':'0'})
            before=row['quantity']; after=before+int(record['quantity_delta']); cost=Decimal(row['cost_basis_gbp'])+Decimal(record.get('cost_basis_delta_gbp','0'))
            if before and after and before*after<0: raise ValueError('impossible position transition through zero')
            if instrument.asset_type is AssetType.EQUITY and after<0: raise ValueError('negative share quantity prohibited')
            if not cost.is_finite() or cost<0: raise ValueError('invalid position cost basis')
            if after==0 and cost!=0: raise ValueError('closed position retains impossible cost basis')
            row['quantity']=after; row['cost_basis_gbp']=str(cost)
            if not row['quantity']: positions.pop(instrument)
        elif kind=='mark':
            instrument=_instrument(record['instrument']); mark=Decimal(record['mark_gbp'])
            if not mark.is_finite() or mark<0: raise ValueError('invalid ledger mark')
            marks[instrument]=mark
        elif kind=='pending_submission':
            key=record['submission']['submission_artifact_id']
            if key in pending: raise ValueError('duplicate pending submission ID')
            pending[key]=record['submission']
        elif kind=='submission_resolved':
            if record['submission_artifact_id'] not in pending: raise ValueError('unknown pending submission resolution')
            pending.pop(record['submission_artifact_id'])
        elif kind=='valuation':
            computed=marked_nav(); stated=Decimal(record['nav_gbp']); stated_peak=Decimal(record['peak_nav_gbp'])
            if stated!=computed: raise ValueError('valuation NAV disagrees with reconstructed state')
            peak=max(peak,computed)
            if stated_peak!=peak: raise ValueError('valuation peak NAV disagrees with reconstructed state')
            valuation_at=_time(record['valued_at'])
        elif kind=='fx':
            candidate=load_packet(record['evidence_packet'])
            if not candidate.verify() or candidate.kind is not EvidenceKind.FX or candidate.normalized.get('symbol')!='GBPUSD': raise ValueError('sealed GBP/USD FX evidence required')
            fx_packet=candidate
    if not records:
        if not initialize: raise ValueError('empty ledger requires explicit initialization mode')
        bootstrap=seal_ledger_record({'kind':'portfolio_initialized','cash_gbp':'100000','nav_gbp':'100000','peak_nav_gbp':'100000'},sequence=0,previous_record_id='')
        records=[bootstrap]; ids=[bootstrap['record_id']]; cash=peak=Decimal('100000'); initialized=True
    elif initialize: raise ValueError('initialization mode is one-time and requires an empty ledger')
    for instrument,row in positions.items():
        if instrument not in marks: raise ValueError('position is missing a ledger mark')
        row['mark_gbp']=str(marks[instrument])
    # Validate final option shorts against actual secured cash/share cover.
    put_collateral=Decimal('0')
    for instrument,row in positions.items():
        if row['quantity']<0 and instrument.asset_type is AssetType.OPTION:
            if instrument.right is OptionRight.PUT: put_collateral+=abs(row['quantity'])*instrument.multiplier*instrument.strike
            elif instrument.right is OptionRight.CALL:
                shares=sum(x['quantity'] for i,x in positions.items() if i.asset_type is AssetType.EQUITY and i.symbol==instrument.underlying)
                if shares<abs(row['quantity'])*instrument.multiplier: raise ValueError('uncovered short call in ledger')
            else: raise ValueError('invalid short option position')
        elif row['quantity']>0 and instrument.asset_type is AssetType.OPTION:
            raise ValueError('long option position prohibited by mandate')
    if put_collateral:
        if fx_packet is None or valuation_at is None: raise ValueError('sealed GBP/USD FX evidence and valuation time required for put collateral')
        checked=assess(fx_packet,as_of=valuation_at,cutoff=valuation_at,
            max_age=timedelta(seconds=DEFAULT_MANDATE.max_quote_age_seconds),expected_symbol='GBPUSD')
        if not checked.actionable: raise ValueError('GBP/USD FX evidence is stale, malformed or not two-sided: '+', '.join(checked.reasons))
        mid=Decimal(str(fx_packet.normalized['mid']))
        if not mid.is_finite() or mid<=0: raise ValueError('GBP/USD midpoint must be finite and positive')
        put_collateral/=mid
    if put_collateral>cash: raise ValueError('unsecured short put in ledger')
    if put_collateral+sum(Decimal(x['reserved_collateral_gbp']) for x in pending.values())>marked_nav()*DEFAULT_MANDATE.csp_collateral_limit:
        raise ValueError('cash-secured-put collateral limit exceeded in ledger')
    for instrument,row in positions.items():
        if instrument.asset_type is AssetType.OPTION and instrument.right is OptionRight.CALL and row['quantity']<0:
            shares=sum(x['quantity'] for i,x in positions.items() if i.asset_type is AssetType.EQUITY and i.symbol==instrument.underlying)
            pending_cover=sum(int(x['covered_shares']) for x in pending.values() if x['instrument']['underlying']==instrument.underlying)
            if abs(row['quantity'])*instrument.multiplier+pending_cover>int(shares*DEFAULT_MANDATE.covered_call_fraction):
                raise ValueError('covered-call fraction exceeded in ledger')
    nav=marked_nav(); peak=max(peak,nav)
    initialization_mode=not source['records']
    payload={'nav_gbp':str(nav),'peak_nav_gbp':str(peak),'cash_gbp':str(cash),'positions':list(positions.values()),
        'pending_submissions':list(pending.values()),'drawdown':str(max(Decimal('0'),(peak-nav)/peak)),
        'source_ledger_record_ids':ids,'source_ledger_records':records,
        'ledger_ancestry_sha256':hashlib.sha256(canonical_json(records)).hexdigest(),'initialization_mode':initialization_mode,
        'fx_evidence':fx_packet.as_json() if fx_packet else None,
        'valuation_at':valuation_at.isoformat() if valuation_at else None,'anchor_artifact_id':anchor_id}
    _portfolio_payload(payload)
    if expected_state is not None and canonical_json(expected_state)!=canonical_json(payload): raise ValueError('supplied state disagrees with reconstructed ledger state')
    return seal_artifact('portfolio_snapshot',payload)
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
