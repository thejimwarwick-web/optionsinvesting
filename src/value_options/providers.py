"""Disabled-by-default production wiring for prospective read-only collection."""
from __future__ import annotations
from datetime import datetime, timezone
import json, os
from typing import Mapping
from urllib.parse import urlsplit
from .broker import AlpacaReadOnlyClient
from .config import live_read_only_enabled
from .http import ExternalServiceError, UrllibTransport
from .workflow import ReadResult

AUX_ENV={'corporate_action':'VALUE_OPTIONS_CORPORATE_ACTION_URL','dividend':'VALUE_OPTIONS_DIVIDEND_URL','fx':'VALUE_OPTIONS_GBPUSD_URL'}
PROVIDER_POLICY_VERSION='read-only-v1'

class ProductionCollectionProviders:
    """Composition of allowlisted Alpaca reads and three configured read-only evidence URLs."""
    def __init__(self,alpaca,aux_urls,transport=None): self.alpaca,self.aux_urls,self.transport=alpaca,dict(aux_urls),transport or UrllibTransport()
    @staticmethod
    def _alpaca(response,kind):
        raw=response.raw; timestamp=response.provider_timestamp
        if kind=='clock': n={'timestamp':timestamp.isoformat()}
        elif kind=='calendar':
            row=raw[0]; n={'timestamp':datetime.fromisoformat(row['date']).replace(tzinfo=timezone.utc).isoformat(),'session_date':row['date']}
        elif kind=='underlying_quote':
            q=raw['quote']; n={'timestamp':q['t'],'symbol':q.get('S') or q.get('symbol'),'bid':q['bp'],'ask':q['ap'],'bid_size':q['bs'],'ask_size':q['as']}
        elif kind=='option_chain': n=raw['normalized']
        else: n=raw['normalized']
        return ReadResult('alpaca',response.feed,raw,n)
    def clock(self): return self._alpaca(self.alpaca.clock(),'clock')
    def calendar(self,start,end): return self._alpaca(self.alpaca.calendar(start,end),'calendar')
    def underlying_quote(self,symbol): return self._alpaca(self.alpaca.underlying_quote(symbol),'underlying_quote')
    def option_chain(self,underlying): return self._alpaca(self.alpaca.option_chain(underlying),'option_chain')
    def option_quote(self,symbol): return self._alpaca(self.alpaca.option_quote(symbol),'option_quote')
    def _aux(self,name,parameter):
        url=self.aux_urls[name]; parsed=urlsplit(url)
        if parsed.scheme!='https' or parsed.username or parsed.password or parsed.fragment: raise ValueError('auxiliary evidence URL is not approved')
        response=self.transport.request('GET',url,headers={'Accept':'application/json'},body=None)
        if response.status!=200 or response.url!=url: raise ExternalServiceError(name+' evidence request failed')
        try: raw=json.loads(response.body)
        except Exception: raise ExternalServiceError(name+' evidence response malformed') from None
        if not isinstance(raw,Mapping) or not isinstance(raw.get('normalized'),Mapping): raise ExternalServiceError(name+' evidence response malformed')
        return ReadResult(name,str(raw.get('feed',name)),raw,raw['normalized'])
    def corporate_action(self,underlying): return self._aux('corporate_action',underlying)
    def dividend(self,underlying): return self._aux('dividend',underlying)
    def fx(self,pair): return self._aux('fx',pair)

def production_provider_status(environ=None):
    env=os.environ if environ is None else environ
    return {'read_only_activated':live_read_only_enabled(env),**{name:bool(env.get(key)) for name,key in AUX_ENV.items()}}

def production_provider_factory(environ=None,*,alpaca=None,transport=None):
    env=os.environ if environ is None else environ; status=production_provider_status(env)
    if not status['read_only_activated']: raise ValueError('read-only collection activation is disabled')
    missing=[name for name in AUX_ENV if not status[name]]
    if missing: raise ValueError('required auxiliary providers unavailable: '+', '.join(missing))
    return ProductionCollectionProviders(alpaca or AlpacaReadOnlyClient(environ=env),{name:env[key] for name,key in AUX_ENV.items()},transport)
