import json, os, re, subprocess, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest

from value_options.cli import main
from value_options.operations import seal_artifact
from value_options.attestation import AttestationReceipt, create_attested_artifact
from value_options.market_data import EvidenceKind, canonical_json
import hashlib
from value_options.sheets import GoogleSheetsAdapter, HEADERS, SheetAttestationBoundary
from value_options.workflow import ReadResult
from value_options.workflow import AtomicCheckpoint, collect_packet, portfolio_collect, seal_ledger_record

UTC=timezone.utc; CONTRACT='AAPL260828P00020000'; AT=datetime(2026,8,7,12,30,tzinfo=UTC)

class TickClock:
    def __init__(self,at): self.value=at
    def __call__(self): self.value+=timedelta(seconds=1); return self.value

class Providers:
    def __init__(self,clock,missing=()): self.ticker=clock; self.calls=[]; self.missing=set(missing); self.stale=False
    def _result(self,name,**n):
        self.calls.append(name)
        if name in self.missing: raise ValueError(name+' provider unavailable')
        timestamp=self.ticker.value-timedelta(minutes=10) if self.stale else self.ticker.value
        n={'timestamp':timestamp.isoformat(),**n}
        return ReadResult('fixture','OPRA' if name in {'option_chain','option_quote'} else 'fixture',{'fixture':n},n)
    def clock(self): return self._result('clock')
    def calendar(self,start,end): return self._result('calendar',session_date=start)
    def underlying_quote(self,symbol): return self._result('underlying_quote',symbol=symbol,bid='20',ask='20.1',bid_size=10,ask_size=10)
    def option_chain(self,underlying): return self._result('option_chain',contracts=[{'symbol':CONTRACT,'underlying':'AAPL','expiration':'2026-08-28','strike':'20','right':'put','multiplier':100}])
    def option_quote(self,symbol): return self._result('option_quote',symbol=symbol,underlying='AAPL',expiration='2026-08-28',strike='20',right='put',multiplier=100,currency='USD',market='US',bid='1',ask='1.1',bid_size=10,ask_size=10)
    def corporate_action(self,u): return self._result('corporate_action',effective_date='2026-08-07',retrieved_at=self.ticker.value.isoformat())
    def dividend(self,u): return self._result('dividend',effective_date='2026-08-07',retrieved_at=self.ticker.value.isoformat())
    def fx(self,p): return self._result('fx',symbol=p,bid='0.79',ask='0.81',mid='0.8')

class EmptyCorporateProviders(Providers):
    def _negative(self,name,u): return ReadResult('alpaca',name,
        {'request':{'symbols':u,'start':'2026-08-07','end':'2026-08-07'},'request_id':'r-'+name,'response':{'corporate_actions':[]}},
        {'symbol':u,'effective_date':'2026-08-07','coverage_start':'2026-08-07','coverage_end':'2026-08-07','records':[],'negative_evidence':True,'request_id':'r-'+name})
    def corporate_action(self,u): return self._negative('corporate_action',u)
    def dividend(self,u): return self._negative('dividend',u)

class MemorySheet:
    def __init__(self): self.rows=[]; self.reads=0; self.fail_read=False
    def read_all(self): return tuple(self.rows)
    def append_row(self,row): self.rows.append(tuple(row)); return f'Attestations!A{len(self.rows)+1}:G{len(self.rows)+1}'
    def read_row(self,location):
        self.reads+=1
        if self.fail_read: raise RuntimeError('interrupted')
        return self.rows[int(re.search(r'!A(\d+):',location).group(1))-2]

os.environ['VALUE_OPTIONS_ENABLE_PAPER_LEDGER_APPEND']='I_AUTHORIZE_APPEND_ONLY_PAPER_LEDGER'
BOUNDARY=SheetAttestationBoundary(MemorySheet(),provenance='test-authenticated')

def write(path,value): path.write_text(json.dumps(value))
def proposal(): return {'operation':{'order_id':'paper-1','side':'sell','quantity':1,'intent':'open','exit_entire_holding':False,'sale_floor':None,'instrument':{'symbol':CONTRACT,'asset_type':'option','issuer':'Apple','sector':'Technology','market':'US','currency':'USD','is_etf':False,'underlying':'AAPL','expiration':'2026-08-28','strike':'20','right':'put','multiplier':100,'adjusted':False,'occ_verified':True}}}


class Trusted:
    def trusts(self,receipt): return receipt.provenance=='test-authenticated'
TRUSTED=Trusted()

def attest(artifact,parents=()):
    receipt=AttestationReceipt.create(artifact_id=artifact['artifact_id'],external_system='fixture-trusted',appended_at=AT,immutable_location='A2:G2',read_back_at=AT,content_sha256=hashlib.sha256(canonical_json(artifact)).hexdigest(),provenance='test-authenticated')
    return create_attested_artifact(artifact,receipt,artifact,parents=parents,trusted_attestor=TRUSTED)

def portfolio_artifact():
    artifact=portfolio_collect(ledger_source([]),initialize=True)
    return attest(artifact).as_json()


def seal_history(rows):
    result=[]
    for sequence,row in enumerate(rows): result.append(seal_ledger_record(row,sequence=sequence,previous_record_id=result[-1]['record_id'] if result else ''))
    return result

def ledger_source(records):
    return {'records':records,'record_count':len(records),'head_record_id':records[-1]['record_id'] if records else ''}

def collect_research(tmp_path,clock,providers):
    source=tmp_path/'candidates.json'; out=tmp_path/'research.json'; write(source,{'candidates':[{'underlying':'AAPL','thesis':'value'}]})
    assert main(['research-collect',str(source),'--output',str(out)],collection_providers=providers,utc_clock=clock,trusted_attestor=TRUSTED,sheet_boundary=BOUNDARY)==0
    return out

def test_successful_research_collection_and_no_option_access(tmp_path):
    clock=TickClock(datetime(2026,8,7,12,30,tzinfo=UTC)); providers=Providers(clock)
    artifact=json.loads(collect_research(tmp_path,clock,providers).read_text())
    assert artifact['original_artifact']['artifact_kind']=='research'
    assert providers.calls==['clock','calendar','underlying_quote']
    assert len(artifact['original_artifact']['payload']['evidence_packets'])==3

def test_missing_research_evidence_quarantines(tmp_path):
    clock=TickClock(datetime(2026,8,7,12,30,tzinfo=UTC)); providers=Providers(clock,{'calendar'})
    source=tmp_path/'in.json'; out=tmp_path/'out.json'; write(source,{'candidates':[{'underlying':'AAPL','thesis':'x'}]})
    assert main(['research-collect',str(source),'--output',str(out)],collection_providers=providers,utc_clock=clock,trusted_attestor=TRUSTED,sheet_boundary=BOUNDARY)==1
    assert json.loads(out.read_text())['quarantined']

def test_collect_rejects_caller_timestamps(tmp_path):
    source=tmp_path/'in.json'; write(source,{'candidates':[{'underlying':'AAPL','thesis':'x'}]})
    with pytest.raises(SystemExit): main(['research-collect',str(source),'--at','2020-01-01T00:00:00Z','--output',str(tmp_path/'x')])

def test_prospective_decision_submission_and_fresh_fill(tmp_path):
    rclock=TickClock(datetime(2026,8,7,12,30,tzinfo=UTC)); research=collect_research(tmp_path,rclock,Providers(rclock))
    clock=TickClock(datetime(2026,8,7,13,40,tzinfo=UTC)); providers=Providers(clock); prop=tmp_path/'proposal.json'; write(prop,proposal()); portfolio=tmp_path/'portfolio.json'; write(portfolio,portfolio_artifact())
    decision,submission=tmp_path/'decision.json',tmp_path/'submission.json'
    assert main(['decision-collect',str(research),str(portfolio),str(prop),'--decision-output',str(decision),'--submission-output',str(submission)],collection_providers=providers,utc_clock=clock,trusted_attestor=TRUSTED,sheet_boundary=BOUNDARY)==0
    assert {'corporate_action','dividend','fx'} <= set(providers.calls)
    sub=json.loads(submission.read_text()); assert sub['original_artifact']['payload']['submitted_at']>sub['original_artifact']['payload']['decision_at']
    fill=tmp_path/'fill.json'
    assert main(['fill-collect',str(research),str(portfolio),str(decision),str(submission),'--output',str(fill)],collection_providers=providers,utc_clock=clock,trusted_attestor=TRUSTED,sheet_boundary=BOUNDARY)==0
    result=json.loads(fill.read_text())['original_artifact']['payload']; assert result['price']=='1' and result['response_received_at']>sub['original_artifact']['payload']['submitted_at']

@pytest.mark.parametrize('missing',[{'corporate_action'},{'dividend'},{'fx'}])
def test_missing_auxiliary_provider_quarantines(tmp_path,missing):
    rc=TickClock(datetime(2026,8,7,12,30,tzinfo=UTC)); research=collect_research(tmp_path,rc,Providers(rc))
    clock=TickClock(datetime(2026,8,7,13,40,tzinfo=UTC)); prop=tmp_path/'p.json'; write(prop,proposal()); portfolio=tmp_path/'portfolio.json'; write(portfolio,portfolio_artifact()); d,s=tmp_path/'d',tmp_path/'s'
    assert main(['decision-collect',str(research),str(portfolio),str(prop),'--decision-output',str(d),'--submission-output',str(s)],collection_providers=Providers(clock,missing),utc_clock=clock,trusted_attestor=TRUSTED,sheet_boundary=BOUNDARY)==1

def test_stale_fill_and_ancestry_substitution_rejected(tmp_path):
    rc=TickClock(datetime(2026,8,7,12,30,tzinfo=UTC)); research=collect_research(tmp_path,rc,Providers(rc))
    clock=TickClock(datetime(2026,8,7,13,40,tzinfo=UTC)); p=tmp_path/'p'; write(p,proposal()); portfolio=tmp_path/'portfolio'; write(portfolio,portfolio_artifact()); d,s=tmp_path/'d',tmp_path/'s'
    main(['decision-collect',str(research),str(portfolio),str(p),'--decision-output',str(d),'--submission-output',str(s)],collection_providers=Providers(clock),utc_clock=clock)
    providers=Providers(clock); providers.stale=True; out=tmp_path/'fill'
    assert main(['fill-collect',str(research),str(portfolio),str(d),str(s),'--output',str(out)],collection_providers=providers,utc_clock=clock,trusted_attestor=TRUSTED,sheet_boundary=BOUNDARY)==1
    forged=seal_artifact('research',{'research_id':'other'}); other=tmp_path/'other'; write(other,forged)
    assert main(['fill-collect',str(other),str(portfolio),str(d),str(s),'--output',str(out)],collection_providers=Providers(clock),utc_clock=clock,trusted_attestor=TRUSTED,sheet_boundary=BOUNDARY)==1

def test_sheet_recovery_after_external_write_without_duplicate():
    port=MemorySheet(); boundary=SheetAttestationBoundary(port); artifact=seal_artifact('research',{'research_id':'x'}); at=datetime(2026,8,7,12,30,tzinfo=UTC); env={'VALUE_OPTIONS_ENABLE_PAPER_LEDGER_APPEND':'I_AUTHORIZE_APPEND_ONLY_PAPER_LEDGER'}
    port.fail_read=True
    with pytest.raises(RuntimeError): boundary.append_activated(artifact,at,environ=env)
    assert len(port.rows)==1
    port.fail_read=False; result=boundary.append_activated(artifact,at+timedelta(seconds=1),environ=env)
    assert not result[3] and result[2][0].verify() and len(port.rows)==1

def test_sheet_preflight_exact_and_rehearsal_nonempty_state(tmp_path):
    adapter=object.__new__(GoogleSheetsAdapter); adapter._spreadsheet,adapter._range='expected','Attestations!A:G'; adapter._request=lambda *a,**k:{'values':[list(HEADERS)]}
    assert adapter.preflight('expected')['writes']==0
    state=portfolio_artifact()
    snapshot=tmp_path/'state.json'; bundle=tmp_path/'bundle.json'; out=tmp_path/'out.json'; write(snapshot,state); write(bundle,{'packets':[]})
    assert main(['workflow-rehearsal',str(bundle),'--state',str(snapshot),'--as-of','2026-08-07T13:40:00Z','--output',str(out)],trusted_attestor=TRUSTED,sheet_boundary=BOUNDARY)==0
    assert json.loads(out.read_text())['persisted_state_unchanged']


def test_checkpoint_repeat_is_idempotent_and_subprocess_quarantines(tmp_path):
    clock=TickClock(datetime(2026,8,7,12,30,tzinfo=UTC)); providers=Providers(clock)
    source=tmp_path/'input.json'; output=tmp_path/'output.json'; checkpoint=tmp_path/'checkpoint.json'
    write(source,{'candidates':[{'underlying':'AAPL','thesis':'value'}]})
    command=['research-collect',str(source),'--output',str(output),'--checkpoint',str(checkpoint)]
    assert main(command,collection_providers=providers,utc_clock=clock,trusted_attestor=TRUSTED,sheet_boundary=BOUNDARY)==0
    calls=tuple(providers.calls)
    assert main(command,collection_providers=providers,utc_clock=clock,trusted_attestor=TRUSTED,sheet_boundary=BOUNDARY)==0
    assert tuple(providers.calls)==calls
    isolated=tmp_path/'isolated.json'
    result=subprocess.run([sys.executable,'-m','value_options.cli','research-collect',
        str(source),'--output',str(isolated)],cwd=Path(__file__).parents[1],
        env={**os.environ,'PYTHONPATH':'src'},capture_output=True,text=True)
    assert result.returncode!=0 and json.loads(isolated.read_text())['quarantined']


def test_checkpoint_binding_tamper_and_observation_after_receipt_are_rejected(tmp_path):
    path=tmp_path/'checkpoint.json'; checkpoint=AtomicCheckpoint(path,{'command_kind':'research','canonical_input_hash':'a'})
    checkpoint.write({'complete':True}); value=json.loads(path.read_text()); value['value']['complete']=False; write(path,value)
    with pytest.raises(ValueError,match='checkpoint'): checkpoint.read()
    clock=TickClock(datetime(2026,8,7,12,30,tzinfo=UTC))
    def future(): return ReadResult('fixture','fixture',{}, {'timestamp':(clock.value+timedelta(minutes=1)).isoformat()})
    with pytest.raises(ValueError,match='after response receipt'):
        collect_packet(EvidenceKind.CLOCK,future,clock,{})


def test_real_alpaca_wire_normalization_has_no_provider_normalized_field():
    from value_options.broker import ProviderResponse
    from value_options.providers import ProductionCollectionProviders
    def response(name,endpoint,feed='iex'):
        raw=json.loads((Path(__file__).parent/'fixtures'/name).read_text())
        timestamp=None
        if endpoint=='clock': timestamp=datetime.fromisoformat(raw['timestamp'].replace('Z','+00:00'))
        return ProviderResponse.capture(endpoint,'fixture-request',feed,timestamp,
            datetime(2026,8,7,13,40,2,tzinfo=UTC),raw)
    normalize=ProductionCollectionProviders._alpaca
    assert normalize(response('alpaca_clock_real.json','clock'),'clock').normalized['timestamp']
    assert 'timestamp' not in normalize(response('alpaca_calendar_real.json','calendar'),'calendar').normalized
    assert normalize(response('alpaca_stock_quote_real.json','underlying_quote'),'underlying_quote').normalized['symbol']=='AAPL'
    assert normalize(response('alpaca_option_quote_real.json','option_quote','opra'),'option_quote').normalized['strike']=='20'
    assert normalize(response('alpaca_option_chain_real.json','option_chain','opra'),'option_chain').normalized['contracts'][0]['symbol']==CONTRACT


def test_production_calendar_is_timestamp_free_and_assesses_session_coverage():
    from value_options.market_data import assess, ingest_response
    raw=json.loads((Path(__file__).parent/'fixtures'/'alpaca_calendar_real.json').read_text())
    normalized={'session_date':'2026-08-07','open':'09:30','close':'16:00'}
    packet=ingest_response(kind=EvidenceKind.CALENDAR,provider='alpaca',feed='alpaca-trading',
        request={'start':'2026-08-07','end':'2026-08-07'},requested_at=AT,
        received_at=AT+timedelta(seconds=1),raw=raw,normalized=normalized)
    checked=assess(packet,as_of=AT+timedelta(seconds=2),cutoff=AT+timedelta(seconds=2),max_age=None)
    assert checked.actionable and 'timestamp' not in packet.normalized


def test_public_commands_produce_complete_envelopes_end_to_end(tmp_path):
    boundary=SheetAttestationBoundary(MemorySheet(),provenance='test-authenticated')
    snapshot_input=tmp_path/'snapshot-input.json'; portfolio=tmp_path/'portfolio-envelope.json'
    write(snapshot_input,ledger_source([]))
    clock=TickClock(datetime(2026,8,7,12,30,tzinfo=UTC))
    assert main(['portfolio-collect',str(snapshot_input),'--initialize','--output',str(portfolio)],
        utc_clock=clock,sheet_boundary=boundary,trusted_attestor=TRUSTED)==0
    source=tmp_path/'research-input.json'; research=tmp_path/'research-envelope.json'
    write(source,{'candidates':[{'underlying':'AAPL','thesis':'value'}]})
    assert main(['research-collect',str(source),'--output',str(research)],
        collection_providers=Providers(clock),utc_clock=clock,sheet_boundary=boundary,trusted_attestor=TRUSTED)==0
    clock.value=datetime(2026,8,7,13,40,tzinfo=UTC)
    operation=tmp_path/'operation.json'; decision=tmp_path/'decision-envelope.json'; submission=tmp_path/'submission-envelope.json'
    write(operation,proposal())
    assert main(['decision-collect',str(research),str(portfolio),str(operation),
        '--decision-output',str(decision),'--submission-output',str(submission)],
        collection_providers=EmptyCorporateProviders(clock),utc_clock=clock,sheet_boundary=boundary,trusted_attestor=TRUSTED)==0
    fill=tmp_path/'fill-envelope.json'
    assert main(['fill-collect',str(research),str(portfolio),str(decision),str(submission),'--output',str(fill)],
        collection_providers=Providers(clock),utc_clock=clock,sheet_boundary=boundary,trusted_attestor=TRUSTED)==0
    values=[json.loads(path.read_text()) for path in (portfolio,research,decision,submission,fill)]
    assert [x['original_artifact']['artifact_kind'] for x in values]==[
        'portfolio_snapshot','research','decision','submission','fill']
    assert values[2]['parent_artifact_ids']==[values[1]['local_artifact_id']]
    assert values[3]['parent_artifact_ids']==[values[2]['local_artifact_id']]
    assert values[4]['parent_artifact_ids']==[values[3]['local_artifact_id']]


def test_portfolio_collect_replays_nonempty_ledger_and_rejects_disagreement(tmp_path):
    boundary=SheetAttestationBoundary(MemorySheet(),provenance='test-authenticated')
    equity={'symbol':'AAPL','asset_type':'equity','issuer':'Apple','sector':'Technology',
        'market':'US','currency':'USD','is_etf':False,'underlying':None,'expiration':None,
        'strike':None,'right':None,'multiplier':1,'adjusted':False,'occ_verified':True}
    ledger={'records':[
        {'record_id':'init','kind':'portfolio_initialized','cash_gbp':'100000','nav_gbp':'100000','peak_nav_gbp':'100000'},
        {'record_id':'cash-1','kind':'cash','amount_gbp':'-1000'},
        {'record_id':'position-1','kind':'position','instrument':equity,'quantity_delta':10,'cost_basis_delta_gbp':'1000'},
        {'record_id':'mark-1','kind':'mark','instrument':equity,'mark_gbp':'110'},
        {'record_id':'valuation-1','kind':'valuation','nav_gbp':'100100','peak_nav_gbp':'100100'}]}
    ledger=ledger_source(seal_history(ledger['records']))
    source=tmp_path/'ledger.json'; output=tmp_path/'portfolio.json'; write(source,ledger)
    clock=TickClock(AT)
    assert main(['portfolio-collect',str(source),'--output',str(output)],utc_clock=clock,
        sheet_boundary=boundary,trusted_attestor=TRUSTED)==0
    payload=json.loads(output.read_text())['original_artifact']['payload']
    assert payload['cash_gbp']=='99000' and payload['positions'][0]['quantity']==10
    assert len(payload['source_ledger_record_ids'])==5 and [x['kind'] for x in payload['source_ledger_records']]==['portfolio_initialized','cash','position','mark','valuation']
    expected=tmp_path/'expected.json'; write(expected,{**payload,'cash_gbp':'1'})
    rejected=tmp_path/'rejected.json'
    assert main(['portfolio-collect',str(source),'--expected-state',str(expected),'--output',str(rejected)],
        utc_clock=clock,sheet_boundary=boundary,trusted_attestor=TRUSTED)==1


def test_negative_corporate_evidence_needs_no_nonstandard_response_fields():
    clock=TickClock(datetime(2026,8,7,13,40,tzinfo=UTC)); provider=EmptyCorporateProviders(clock)
    ca=collect_packet(EvidenceKind.CORPORATE_ACTION,lambda:provider.corporate_action('AAPL'),clock,{'underlying':'AAPL'})
    div=collect_packet(EvidenceKind.DIVIDEND,lambda:provider.dividend('AAPL'),clock,{'underlying':'AAPL'})
    assert ca.normalized['negative_evidence'] and div.normalized['negative_evidence']
    assert ca.raw['response']=={'corporate_actions':[]} and div.raw['response']=={'corporate_actions':[]}


def test_ledger_replay_rejects_falsified_nav_negative_shares_and_cost_basis():
    equity={'symbol':'AAPL','asset_type':'equity','issuer':'Apple','sector':'Technology','market':'US',
        'currency':'USD','is_etf':False,'underlying':None,'expiration':None,'strike':None,
        'right':None,'multiplier':1,'adjusted':False,'occ_verified':True}
    init={'kind':'portfolio_initialized','cash_gbp':'100000','nav_gbp':'100000','peak_nav_gbp':'100000'}
    cases=[
        [init,{'kind':'valuation','nav_gbp':'99999','peak_nav_gbp':'100000'}],
        [init,{'kind':'position','instrument':equity,'quantity_delta':-1,'cost_basis_delta_gbp':'0'}],
        [init,{'kind':'position','instrument':equity,'quantity_delta':1,'cost_basis_delta_gbp':'-1'}]]
    for rows in cases:
        with pytest.raises(ValueError): portfolio_collect(ledger_source(seal_history(rows)))


def test_ledger_replay_rejects_duplicate_pending_and_altered_history():
    submission={'instrument':proposal()['operation']['instrument'],'side':'sell','intent':'open','quantity':1,
        'reserved_cash_gbp':'0','reserved_collateral_gbp':'2000','covered_shares':0,'submission_artifact_id':'submission-1'}
    history=seal_history([{'kind':'portfolio_initialized','cash_gbp':'100000','nav_gbp':'100000','peak_nav_gbp':'100000'}, {'kind':'pending_submission','submission':submission}, {'kind':'pending_submission','submission':submission}])
    with pytest.raises(ValueError,match='duplicate pending'):
        portfolio_collect(ledger_source(history))
    altered=dict(history[1]); altered['submission']={**submission,'quantity':2}
    with pytest.raises(ValueError,match='altered'):
        portfolio_collect(ledger_source([history[0],altered]))
    with pytest.raises(ValueError,match='reordered'):
        portfolio_collect(ledger_source([history[0],history[2],history[1]]))
    missing=ledger_source(history[:-1]); missing['record_count']=len(history)
    missing['head_record_id']=history[-1]['record_id']
    with pytest.raises(ValueError,match='missing'):
        portfolio_collect(missing)
