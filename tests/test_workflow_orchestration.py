import json, os, re, subprocess, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest

from value_options.cli import main
from value_options.operations import seal_artifact
from value_options.market_data import EvidenceKind, canonical_json
import hashlib
from value_options.sheets import GoogleSheetsAdapter, HEADERS, SheetAttestationBoundary
from value_options.workflow import ReadResult
from value_options.workflow import AtomicCheckpoint, collect_packet

UTC=timezone.utc; CONTRACT='AAPL260828P00020000'

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

class MemorySheet:
    def __init__(self): self.rows=[]; self.reads=0; self.fail_read=False
    def read_all(self): return tuple(self.rows)
    def append_row(self,row): self.rows.append(tuple(row)); return f'Attestations!A{len(self.rows)+1}:G{len(self.rows)+1}'
    def read_row(self,location):
        self.reads+=1
        if self.fail_read: raise RuntimeError('interrupted')
        return self.rows[int(re.search(r'!A(\d+):',location).group(1))-2]

def write(path,value): path.write_text(json.dumps(value))
def proposal(): return {'operation':{'order_id':'paper-1','side':'sell','quantity':1,'intent':'open','exit_entire_holding':False,'sale_floor':None,'instrument':{'symbol':CONTRACT,'asset_type':'option','issuer':'Apple','sector':'Technology','market':'US','currency':'USD','is_etf':False,'underlying':'AAPL','expiration':'2026-08-28','strike':'20','right':'put','multiplier':100,'adjusted':False,'occ_verified':True}}}


def portfolio_artifact():
    payload={'nav_gbp':'100000','peak_nav_gbp':'100000','cash_gbp':'100000','positions':[],'externally_reconciled':True}
    payload['reconciliation_seal']=hashlib.sha256(canonical_json(payload)).hexdigest()
    return seal_artifact('portfolio_snapshot',payload)

def collect_research(tmp_path,clock,providers):
    source=tmp_path/'candidates.json'; out=tmp_path/'research.json'; write(source,{'candidates':[{'underlying':'AAPL','thesis':'value'}]})
    assert main(['research-collect',str(source),'--output',str(out)],collection_providers=providers,utc_clock=clock)==0
    return out

def test_successful_research_collection_and_no_option_access(tmp_path):
    clock=TickClock(datetime(2026,8,7,12,30,tzinfo=UTC)); providers=Providers(clock)
    artifact=json.loads(collect_research(tmp_path,clock,providers).read_text())
    assert artifact['artifact_kind']=='research'
    assert providers.calls==['clock','calendar','underlying_quote']
    assert len(artifact['payload']['evidence_packets'])==3

def test_missing_research_evidence_quarantines(tmp_path):
    clock=TickClock(datetime(2026,8,7,12,30,tzinfo=UTC)); providers=Providers(clock,{'calendar'})
    source=tmp_path/'in.json'; out=tmp_path/'out.json'; write(source,{'candidates':[{'underlying':'AAPL','thesis':'x'}]})
    assert main(['research-collect',str(source),'--output',str(out)],collection_providers=providers,utc_clock=clock)==1
    assert json.loads(out.read_text())['quarantined']

def test_collect_rejects_caller_timestamps(tmp_path):
    source=tmp_path/'in.json'; write(source,{'candidates':[{'underlying':'AAPL','thesis':'x'}]})
    with pytest.raises(SystemExit): main(['research-collect',str(source),'--at','2020-01-01T00:00:00Z','--output',str(tmp_path/'x')])

def test_prospective_decision_submission_and_fresh_fill(tmp_path):
    rclock=TickClock(datetime(2026,8,7,12,30,tzinfo=UTC)); research=collect_research(tmp_path,rclock,Providers(rclock))
    clock=TickClock(datetime(2026,8,7,13,40,tzinfo=UTC)); providers=Providers(clock); prop=tmp_path/'proposal.json'; write(prop,proposal()); portfolio=tmp_path/'portfolio.json'; write(portfolio,portfolio_artifact())
    decision,submission=tmp_path/'decision.json',tmp_path/'submission.json'
    assert main(['decision-collect',str(research),str(portfolio),str(prop),'--decision-output',str(decision),'--submission-output',str(submission)],collection_providers=providers,utc_clock=clock)==0
    assert {'corporate_action','dividend','fx'} <= set(providers.calls)
    sub=json.loads(submission.read_text()); assert sub['payload']['submitted_at']>sub['payload']['decision_at']
    fill=tmp_path/'fill.json'
    assert main(['fill-collect',str(research),str(portfolio),str(decision),str(submission),'--output',str(fill)],collection_providers=providers,utc_clock=clock)==0
    result=json.loads(fill.read_text())['payload']; assert result['price']=='1' and result['response_received_at']>sub['payload']['submitted_at']

@pytest.mark.parametrize('missing',[{'corporate_action'},{'dividend'},{'fx'}])
def test_missing_auxiliary_provider_quarantines(tmp_path,missing):
    rc=TickClock(datetime(2026,8,7,12,30,tzinfo=UTC)); research=collect_research(tmp_path,rc,Providers(rc))
    clock=TickClock(datetime(2026,8,7,13,40,tzinfo=UTC)); prop=tmp_path/'p.json'; write(prop,proposal()); portfolio=tmp_path/'portfolio.json'; write(portfolio,portfolio_artifact()); d,s=tmp_path/'d',tmp_path/'s'
    assert main(['decision-collect',str(research),str(portfolio),str(prop),'--decision-output',str(d),'--submission-output',str(s)],collection_providers=Providers(clock,missing),utc_clock=clock)==1

def test_stale_fill_and_ancestry_substitution_rejected(tmp_path):
    rc=TickClock(datetime(2026,8,7,12,30,tzinfo=UTC)); research=collect_research(tmp_path,rc,Providers(rc))
    clock=TickClock(datetime(2026,8,7,13,40,tzinfo=UTC)); p=tmp_path/'p'; write(p,proposal()); portfolio=tmp_path/'portfolio'; write(portfolio,portfolio_artifact()); d,s=tmp_path/'d',tmp_path/'s'
    main(['decision-collect',str(research),str(portfolio),str(p),'--decision-output',str(d),'--submission-output',str(s)],collection_providers=Providers(clock),utc_clock=clock)
    providers=Providers(clock); providers.stale=True; out=tmp_path/'fill'
    assert main(['fill-collect',str(research),str(portfolio),str(d),str(s),'--output',str(out)],collection_providers=providers,utc_clock=clock)==1
    forged=seal_artifact('research',{'research_id':'other'}); other=tmp_path/'other'; write(other,forged)
    assert main(['fill-collect',str(other),str(portfolio),str(d),str(s),'--output',str(out)],collection_providers=Providers(clock),utc_clock=clock)==1

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
    assert main(['workflow-rehearsal',str(bundle),'--state',str(snapshot),'--as-of','2026-08-07T13:40:00Z','--output',str(out)])==0
    assert json.loads(out.read_text())['persisted_state_unchanged']


def test_checkpoint_repeat_is_idempotent_and_subprocess_quarantines(tmp_path):
    clock=TickClock(datetime(2026,8,7,12,30,tzinfo=UTC)); providers=Providers(clock)
    source=tmp_path/'input.json'; output=tmp_path/'output.json'; checkpoint=tmp_path/'checkpoint.json'
    write(source,{'candidates':[{'underlying':'AAPL','thesis':'value'}]})
    command=['research-collect',str(source),'--output',str(output),'--checkpoint',str(checkpoint)]
    assert main(command,collection_providers=providers,utc_clock=clock)==0
    calls=tuple(providers.calls)
    assert main(command,collection_providers=providers,utc_clock=clock)==0
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
