"""Offline sequential operations. No write-side broker or ledger adapters exist."""
import argparse
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path

from .mandate import DEFAULT_MANDATE
from .market_data import EvidenceKind, load_packet, assess
from .models import (AssetType, Instrument, Observation, OptionRight, Order, OrderSubmission,
                     ResearchCandidate, ResearchPacket, Side, TradingDecision)
from .operations import PaperRun, seal_artifact, verify_artifact
from .market_data import canonical_json
import hashlib
from .risk import PortfolioRisk
from .config import live_read_only_enabled, preflight, redact
from .http import ExternalServiceError


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


def _validate_research_input(source):
    if not isinstance(source,dict) or set(source) - {"candidates","evidence_references"}:
        raise ValueError("research input contains unknown top-level fields")
    if not isinstance(source.get("candidates"),list) or not source["candidates"]:
        raise ValueError("research candidates must be a non-empty list")
    for candidate in source["candidates"]:
        if not isinstance(candidate,dict) or set(candidate)!={"underlying","thesis"}:
            raise ValueError("candidate permits only underlying and thesis")
        if not all(isinstance(candidate[x],str) and candidate[x] for x in ("underlying","thesis")):
            raise ValueError("candidate fields must be non-empty strings")
    refs=source.get("evidence_references",[])
    if not isinstance(refs,list): raise ValueError("evidence_references must be a list")
    for ref in refs:
        if not isinstance(ref,dict) or set(ref)!={"name","value","available_at"}:
            raise ValueError("evidence reference permits only name, value and available_at")
        if not all(isinstance(ref[x],str) and ref[x] for x in ref):
            raise ValueError("evidence-reference fields must be non-empty strings")


def _verify_rule_results(rules):
    required={"mandate_version","evaluated_at","results","seal"}
    if not isinstance(rules,dict) or set(rules)!=required: return False
    body={"mandate_version":rules["mandate_version"],"evaluated_at":rules["evaluated_at"],"results":rules["results"]}
    return rules["seal"]==hashlib.sha256(canonical_json(body)).hexdigest() and all(x[1] is True for x in rules["results"])


def main(argv=None, *, collection_providers=None, utc_clock=None, sheet_boundary=None):
    parser=argparse.ArgumentParser(prog="value-options"); sub=parser.add_subparsers(dest="command",required=True)
    i=sub.add_parser("inspect"); i.add_argument("packet",type=Path); i.add_argument("--as-of",required=True); i.add_argument("--output",type=Path,required=True)
    r=sub.add_parser("research-run"); r.add_argument("input",type=Path); r.add_argument("--at",required=True); r.add_argument("--output",type=Path,required=True)
    rc=sub.add_parser("research-collect"); rc.add_argument("input",type=Path); rc.add_argument("--output",type=Path,required=True); rc.add_argument("--checkpoint",type=Path)
    d=sub.add_parser("decision-run"); d.add_argument("research",type=Path); d.add_argument("bundle",type=Path); d.add_argument("--at",required=True); d.add_argument("--submitted-at",required=True); d.add_argument("--decision-output",type=Path,required=True); d.add_argument("--submission-output",type=Path,required=True)
    dc=sub.add_parser("decision-collect"); dc.add_argument("research",type=Path); dc.add_argument("proposal",type=Path); dc.add_argument("--decision-output",type=Path,required=True); dc.add_argument("--submission-output",type=Path,required=True); dc.add_argument("--checkpoint",type=Path)
    f=sub.add_parser("fill-run"); f.add_argument("decision",type=Path); f.add_argument("submission",type=Path); f.add_argument("quote",type=Path); f.add_argument("--as-of",required=True); f.add_argument("--output",type=Path,required=True)
    fc=sub.add_parser("fill-collect"); fc.add_argument("research",type=Path); fc.add_argument("decision",type=Path); fc.add_argument("submission",type=Path); fc.add_argument("--output",type=Path,required=True); fc.add_argument("--checkpoint",type=Path)
    rp=sub.add_parser("replay"); rp.add_argument("bundle",type=Path); rp.add_argument("--as-of",required=True); rp.add_argument("--output",type=Path,required=True)
    wr=sub.add_parser("workflow-rehearsal"); wr.add_argument("bundle",type=Path); wr.add_argument("--state",type=Path,required=True); wr.add_argument("--as-of",required=True); wr.add_argument("--output",type=Path,required=True)
    pf=sub.add_parser("preflight"); pf.add_argument("--live-read-only",action="store_true")
    wpf=sub.add_parser("workflow-preflight"); wpf.add_argument("--live-read-only",action="store_true")
    args=parser.parse_args(argv); run=_run(); artifact=None
    try:
        if args.command in {"research-collect","decision-collect","fill-collect"}:
            if collection_providers is None or utc_clock is None:
                raise ValueError("prospective collection providers and UTC clock are unavailable")
            from .workflow import (AtomicCheckpoint, decision_collect, fill_collect,
                                   research_collect)
            checkpoint=AtomicCheckpoint(args.checkpoint) if args.checkpoint else None
            recovered=checkpoint.read() if checkpoint else {}
            if recovered.get("complete"):
                artifact=recovered["artifact"]
                if args.command=="decision-collect": _write(args.decision_output,recovered["decision"])
            elif args.command=="research-collect":
                artifact=research_collect(_read(args.input),collection_providers,utc_clock)
            elif args.command=="decision-collect":
                decision_artifact,artifact=decision_collect(_read(args.research),_read(args.proposal),collection_providers,utc_clock)
                _write(args.decision_output,decision_artifact)
            else:
                artifact=fill_collect(_read(args.research),_read(args.decision),_read(args.submission),collection_providers,utc_clock)
            if checkpoint and not recovered.get("complete"):
                data={"complete":True,"artifact":artifact}
                if args.command=="decision-collect": data["decision"]=decision_artifact
                checkpoint.write(data)
            if sheet_boundary is not None:
                # The boundary itself requires the independent ledger sentinel.
                if args.command=="decision-collect":
                    decision_to_append=recovered["decision"] if recovered.get("complete") else decision_artifact
                    sheet_boundary.append_activated(decision_to_append,utc_clock())
                sheet_boundary.append_activated(artifact,utc_clock())
            if args.command=="decision-collect": _write(args.submission_output,artifact)
        elif args.command in {"preflight","workflow-preflight"}:
            configured,artifact=preflight()
            if args.live_read_only:
                if not configured: raise ValueError("live read-only preflight requires all environment credentials")
                if not live_read_only_enabled(): raise ValueError("live integration is disabled; set the documented activation environment value")
                # Deliberately harmless reads only: clock and attestation range.
                from .broker import AlpacaReadOnlyClient
                from .sheets import GoogleSheetsAdapter, google_service_account_token_provider
                clock=AlpacaReadOnlyClient().clock()
                adapter=GoogleSheetsAdapter(token_provider=google_service_account_token_provider())
                sheet=adapter.preflight(__import__("os").environ["GOOGLE_SHEETS_SPREADSHEET_ID"])
                artifact["live_read_only"]={"alpaca_clock_request_id":clock.request_id,
                    "alpaca_evidence_seal":clock.evidence_seal,"sheet":sheet,"writes":0,"orders":0}
                artifact.update(provider_checks_activated=True,sheet_schema_verified=True)
        elif args.command=="inspect":
            p=load_packet(_read(args.packet)); result=assess(p,as_of=_time(args.as_of),cutoff=_time(args.as_of),max_age=None); artifact={"mode":"inspection",**asdict(result)}
        elif args.command=="research-run":
            source=_read(args.input); _validate_research_input(source)
            candidates=tuple(ResearchCandidate(x["underlying"],x["thesis"]) for x in source["candidates"])
            refs=tuple(Observation(x["name"],x["value"],_time(x["available_at"])) for x in source.get("evidence_references",[]))
            record=run.create_research(_time(args.at),candidates,refs)
            payload={"research_id":record.packet_id,"mandate_version":record.mandate_version,"research_at":record.research_at.isoformat(),"candidates":[asdict(x) for x in record.shortlist],"evidence_references":[{"name":x.name,"value":x.value,"available_at":x.available_at.isoformat()} for x in record.observations],"rationale":record.rationale,"record_seal":record.seal}
            artifact=seal_artifact("research",payload)
        elif args.command=="decision-run":
            research_artifact=_read(args.research); research=_research_from_artifact(research_artifact); source=_read(args.bundle); packets=[load_packet(x) for x in source.get("packets",[])]
            run.research_packet=research; spec=source["operation"]["instrument"]; op=source["operation"]
            order=Order(op["order_id"],_instrument(spec),Side(op["side"]),int(op["quantity"]),op["intent"])
            decision=run.decide(_time(args.at),order,packets); submission=run.submit(_time(args.submitted_at))
            rules=asdict(run.rule_results); rules["evaluated_at"]=run.rule_results.evaluated_at.isoformat()
            observations=[{"name":x.name,"value":x.value,"available_at":x.available_at.isoformat()} for x in decision.observations]
            decision_payload={"decision_id":decision.decision_id,"research_id":decision.research_packet_id,"research_artifact_id":research_artifact["artifact_id"],"decision_at":decision.decision_at.isoformat(),"order":op,"record_seal":decision.seal,"rule_results":rules,"evidence_references":observations,"evidence_packet_ids":[p.packet_id for p in packets]}
            decision_artifact=seal_artifact("decision",decision_payload); _write(args.decision_output,decision_artifact)
            submission_payload={"decision_artifact_id":decision_artifact["artifact_id"],"decision_id":decision.decision_id,"decision_at":submission.decision_at.isoformat(),"submitted_at":submission.submitted_at.isoformat(),"order":op,"rule_results_seal":rules["seal"],"evidence_packet_ids":[p.packet_id for p in packets]}
            artifact=seal_artifact("submission",submission_payload); _write(args.submission_output,artifact)
        elif args.command=="fill-run":
            decision_artifact,supplied=_read(args.decision),_read(args.submission)
            dok,dreasons=verify_artifact(decision_artifact,"decision"); sok,sreasons=verify_artifact(supplied,"submission")
            if not dok or not sok: raise ValueError("; ".join(dreasons+sreasons))
            dpay,p=decision_artifact["payload"],supplied["payload"]
            if p["decision_artifact_id"]!=decision_artifact["artifact_id"] or p["decision_id"]!=dpay["decision_id"]: raise ValueError("submission parent decision mismatch")
            if canonical_json(p["order"])!=canonical_json(dpay["order"]): raise ValueError("submission order differs from sealed decision")
            if p["decision_at"]!=dpay["decision_at"] or _time(p["submitted_at"])<=_time(dpay["decision_at"]): raise ValueError("invalid decision/submission chronology")
            if not _verify_rule_results(dpay.get("rule_results")) or p["rule_results_seal"]!=dpay["rule_results"]["seal"]: raise ValueError("rule-results verification failed")
            if not dpay.get("evidence_references") or p.get("evidence_packet_ids")!=dpay.get("evidence_packet_ids"): raise ValueError("evidence references missing or mismatched")
            op=p["order"]; order=Order(op["order_id"],_instrument(op["instrument"]),Side(op["side"]),int(op["quantity"]),op["intent"])
            observations=tuple(Observation(x["name"],x["value"],_time(x["available_at"])) for x in dpay["evidence_references"])
            sealed_decision=TradingDecision(dpay["decision_id"],dpay["research_id"],_time(dpay["decision_at"]),order,observations,dpay["record_seal"])
            if not sealed_decision.verify(): raise ValueError("decision record seal mismatch")
            run.orders.append(OrderSubmission(p["decision_id"],_time(p["decision_at"]),order,_time(p["submitted_at"])))
            fill=run.simulate_fill(load_packet(_read(args.quote)),as_of=_time(args.as_of))
            fill["submission_artifact_id"]=supplied["artifact_id"]
            artifact=seal_artifact("fill",fill)
        else:
            persisted=_read(args.state) if args.command=="workflow-rehearsal" else None
            persisted_before=hashlib.sha256(canonical_json(persisted)).hexdigest() if persisted is not None else None
            before=run._state_fingerprint()
            report=run.excluded_replay(_packets(args.bundle),as_of=_time(args.as_of))
            after=run._state_fingerprint()
            artifact={"mode":"rehearsal" if args.command=="workflow-rehearsal" else "replay",
                **report.jsonable(),"fund_state_unchanged":before==after,
                "cash_nav_orders_positions_launch_unchanged":before==after}
            if persisted is not None:
                persisted_after=hashlib.sha256(canonical_json(_read(args.state))).hexdigest()
                artifact.update(persisted_state_fingerprint=persisted_before,
                    persisted_state_unchanged=persisted_before==persisted_after)
    except (KeyError,TypeError,ValueError,ArithmeticError,RuntimeError,ExternalServiceError) as error:
        artifact=_quarantine(args.command,[redact(str(error)) or error.__class__.__name__])
    artifact.update({"classification":"PAPER ONLY","order_policy":"NO LIVE ORDER"})
    output=getattr(args,"output",None)
    if output: _write(output,artifact)
    elif args.command in {"decision-run","decision-collect"} and artifact.get("quarantined"):
        # A failed stage emits reports at both requested paths and never leaves a
        # misleading partial decision or submission behind.
        _write(args.decision_output,artifact); _write(args.submission_output,artifact)
    print(f"PAPER ONLY | NO LIVE ORDER | {args.command} | configured={configured if args.command in {'preflight','workflow-preflight'} else 'n/a'} | actionable={artifact.get('actionable', False)} | reasons={len(artifact.get('reasons',[]))}")
    if args.command in {"preflight","workflow-preflight"}: return 0 if configured and not artifact.get("quarantined") else 2
    return 1 if args.command in {"research-run","research-collect","decision-run","decision-collect","fill-run","fill-collect"} and (
        artifact.get("quarantined") or not artifact.get("actionable",False)) else 0

if __name__=="__main__": raise SystemExit(main())
