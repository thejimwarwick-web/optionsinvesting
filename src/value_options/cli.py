"""Offline sequential operations. No write-side broker or ledger adapters exist."""
import argparse
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path

from .mandate import DEFAULT_MANDATE
from .market_data import EvidenceKind, load_packet, assess
from .models import (AssetType, Instrument, Observation, OptionRight, Order, ResearchCandidate,
                     ResearchPacket, Side, TradingDecision)
from .operations import PaperRun, seal_artifact, verify_artifact
from .risk import PortfolioRisk


def _time(value): return datetime.fromisoformat(value).astimezone(timezone.utc)
def _run(): return PaperRun(DEFAULT_MANDATE, PortfolioRisk(Decimal("100000"), Decimal("100000"), Decimal("100000"), {}, {}))
def _read(path): return json.loads(path.read_text())
def _packets(path): return [load_packet(x) for x in _read(path).get("packets", [])]
def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True); path.write_text(json.dumps(value, sort_keys=True, indent=2, default=str)+"\n")


def _quarantine(mode, reasons):
    return {"mode":mode,"classification":"PAPER ONLY","order_policy":"NO LIVE ORDER","verified":False,
            "actionable":False,"quarantined":True,"excluded":False,"reasons":list(reasons)}


def _research_from_artifact(value):
    ok,reasons=verify_artifact(value,"research"); p=value.get("payload",{})
    if not ok: raise ValueError("; ".join(reasons))
    observations=tuple(Observation(x["name"],x["value"],_time(x["available_at"])) for x in p["evidence_references"])
    candidates=tuple(ResearchCandidate(x["underlying"],x["thesis"]) for x in p["candidates"])
    record=ResearchPacket(p["research_id"],p["mandate_version"],_time(p["research_at"]),candidates,observations,p["rationale"],p["record_seal"])
    if not record.verify(): raise ValueError("research record seal mismatch")
    return record


def _instrument(spec):
    return Instrument(spec["symbol"],AssetType.OPTION,spec["issuer"],spec["sector"],"US",underlying=spec["underlying"],expiry=datetime.fromisoformat(spec["expiration"]).date(),strike=Decimal(spec["strike"]),right=OptionRight(spec["right"]),multiplier=100)


def main(argv=None):
    parser=argparse.ArgumentParser(prog="value-options"); sub=parser.add_subparsers(dest="command",required=True)
    i=sub.add_parser("inspect"); i.add_argument("packet",type=Path); i.add_argument("--as-of",required=True); i.add_argument("--output",type=Path,required=True)
    r=sub.add_parser("research-run"); r.add_argument("input",type=Path); r.add_argument("--at",required=True); r.add_argument("--output",type=Path,required=True)
    d=sub.add_parser("decision-run"); d.add_argument("research",type=Path); d.add_argument("bundle",type=Path); d.add_argument("--at",required=True); d.add_argument("--submitted-at",required=True); d.add_argument("--decision-output",type=Path,required=True); d.add_argument("--submission-output",type=Path,required=True)
    f=sub.add_parser("fill-run"); f.add_argument("submission",type=Path); f.add_argument("quote",type=Path); f.add_argument("--as-of",required=True); f.add_argument("--output",type=Path,required=True)
    rp=sub.add_parser("replay"); rp.add_argument("bundle",type=Path); rp.add_argument("--as-of",required=True); rp.add_argument("--output",type=Path,required=True)
    args=parser.parse_args(argv); run=_run(); artifact=None
    try:
        if args.command=="inspect":
            p=load_packet(_read(args.packet)); result=assess(p,as_of=_time(args.as_of),cutoff=_time(args.as_of),max_age=None); artifact={"mode":"inspection",**asdict(result)}
        elif args.command=="research-run":
            source=_read(args.input); forbidden={"option_chain","option_quote","instrument","order","decision","contract","symbol","strike","expiry","expiration","right","option_right"}
            def scan(v):
                if isinstance(v,dict):
                    return [str(k) for k,x in v.items() if str(k).lower() in forbidden]+sum((scan(x) for x in v.values()),[])
                if isinstance(v,list): return sum((scan(x) for x in v),[])
                return []
            bad=scan(source)
            if bad: raise ValueError("later or exact option information prohibited in research input: "+", ".join(sorted(set(bad))))
            candidates=tuple(ResearchCandidate(x["underlying"],x["thesis"]) for x in source["candidates"])
            refs=tuple(Observation(x["name"],x["value"],_time(x["available_at"])) for x in source.get("evidence_references",[]))
            record=run.create_research(_time(args.at),candidates,refs)
            payload={"research_id":record.packet_id,"mandate_version":record.mandate_version,"research_at":record.research_at.isoformat(),"candidates":[asdict(x) for x in record.shortlist],"evidence_references":[{"name":x.name,"value":x.value,"available_at":x.available_at.isoformat()} for x in record.observations],"rationale":record.rationale,"record_seal":record.seal}
            artifact=seal_artifact("research",payload)
        elif args.command=="decision-run":
            research=_research_from_artifact(_read(args.research)); source=_read(args.bundle); packets=[load_packet(x) for x in source.get("packets",[])]
            run.research_packet=research; spec=source["operation"]["instrument"]; op=source["operation"]
            order=Order(op["order_id"],_instrument(spec),Side(op["side"]),int(op["quantity"]),op["intent"])
            decision=run.decide(_time(args.at),order,packets); submission=run.submit(_time(args.submitted_at))
            rules=asdict(run.rule_results); rules["evaluated_at"]=run.rule_results.evaluated_at.isoformat()
            decision_payload={"decision_id":decision.decision_id,"research_id":decision.research_packet_id,"decision_at":decision.decision_at.isoformat(),"order":op,"record_seal":decision.seal,"rule_results":rules,"evidence_packet_ids":[p.packet_id for p in packets]}
            decision_artifact=seal_artifact("decision",decision_payload); _write(args.decision_output,decision_artifact)
            submission_payload={"decision_artifact_id":decision_artifact["artifact_id"],"decision_at":submission.decision_at.isoformat(),"submitted_at":submission.submitted_at.isoformat(),"order":op}
            artifact=seal_artifact("submission",submission_payload); _write(args.submission_output,artifact)
        elif args.command=="fill-run":
            supplied=_read(args.submission); ok,reasons=verify_artifact(supplied,"submission")
            if not ok: raise ValueError("; ".join(reasons))
            p=supplied["payload"]; op=p["order"]; order=Order(op["order_id"],_instrument(op["instrument"]),Side(op["side"]),int(op["quantity"]),op["intent"])
            run.decision=TradingDecision(p["decision_artifact_id"],"external-sealed-research",_time(p["decision_at"]),order,(),"")
            run.orders.append(__import__('value_options.models',fromlist=['OrderSubmission']).OrderSubmission(p["decision_artifact_id"],_time(p["decision_at"]),order,_time(p["submitted_at"])))
            fill=run.simulate_fill(load_packet(_read(args.quote)),as_of=_time(args.as_of)); artifact=seal_artifact("fill",fill)
        else:
            report=run.excluded_replay(_packets(args.bundle),as_of=_time(args.as_of)); artifact={"mode":"replay",**report.jsonable()}
    except (KeyError,TypeError,ValueError,ArithmeticError) as error:
        artifact=_quarantine(args.command,[str(error) or error.__class__.__name__])
    artifact.update({"classification":"PAPER ONLY","order_policy":"NO LIVE ORDER"})
    output=getattr(args,"output",None)
    if output: _write(output,artifact)
    elif args.command=="decision-run" and artifact.get("quarantined"):
        # A failed stage emits reports at both requested paths and never leaves a
        # misleading partial decision or submission behind.
        _write(args.decision_output,artifact); _write(args.submission_output,artifact)
    print(f"PAPER ONLY | NO LIVE ORDER | {args.command} | actionable={artifact.get('actionable', False)} | reasons={len(artifact.get('reasons',[]))}")
    return 0

if __name__=="__main__": raise SystemExit(main())
