"""Disabled-by-default production wiring for prospective read-only collection."""
from __future__ import annotations
from datetime import datetime, timezone
import json, os, re
from typing import Mapping
from urllib.parse import urlencode, urlsplit
from .broker import AlpacaReadOnlyClient
from .config import live_read_only_enabled
from .http import ExternalServiceError, UrllibTransport
from .workflow import ReadResult

AUX_ENV={'corporate_action':'VALUE_OPTIONS_ENABLE_CORPORATE_ACTION_READS','dividend':'VALUE_OPTIONS_ENABLE_DIVIDEND_READS','fx':'VALUE_OPTIONS_ENABLE_GBPUSD_READS'}
AUX_SENTINEL='I_UNDERSTAND_AUXILIARY_READ_ONLY_ACCESS'
AUX_ENDPOINTS={'corporate_action':('data.alpaca.markets','/v1/corporate-actions'),
               'dividend':('data.alpaca.markets','/v1/corporate-actions'),
               'fx':('data.alpaca.markets','/v1beta1/forex/latest/quotes')}
PROVIDER_POLICY_VERSION='read-only-v1'

class ProductionCollectionProviders:
    """Composition of allowlisted Alpaca reads and three configured read-only evidence URLs."""
    def __init__(self,alpaca,credentials,transport=None): self.alpaca,self.credentials,self.transport=alpaca,credentials,transport or UrllibTransport()
    @staticmethod
    def _alpaca(response,kind):
        raw=response.raw; timestamp=response.provider_timestamp
        if kind=='clock': n={'timestamp':timestamp.isoformat()}
        elif kind=='calendar':
            if len(raw)!=1: raise ExternalServiceError('calendar response must cover exactly one session')
            row=raw[0]; n={'session_date':row['date'],'open':row.get('open'),'close':row.get('close')}
        elif kind=='underlying_quote':
            q=raw['quote']; n={'timestamp':q['t'],'symbol':raw.get('symbol'),'bid':q['bp'],'ask':q['ap'],'bid_size':q['bs'],'ask_size':q['as']}
        elif kind=='option_chain':
            contracts=[]; timestamps=[]
            pages=raw.get('pages'); snapshots={}
            if pages:
                for page in pages: snapshots.update(page['raw']['snapshots'])
            else: snapshots.update(raw['snapshots'])
            for symbol,snapshot in snapshots.items():
                quote=snapshot.get('latestQuote') or {}; match=re.fullmatch(r'([A-Z.]+)(\d{6})([CP])(\d{8})',symbol)
                if not match: raise ExternalServiceError('option chain contains a non-OCC contract')
                underlying,date,right,strike=match.groups()
                timestamps.append(quote.get('t'))
                contracts.append({'symbol':symbol,'underlying':underlying,
                    'expiration':f'20{date[:2]}-{date[2:4]}-{date[4:]}',
                    'strike':str(int(strike)/1000).rstrip('0').rstrip('.'),
                    'right':'call' if right=='C' else 'put','multiplier':100})
            n={'timestamp':max(x for x in timestamps if x),'contracts':contracts}
        else:
            if len(raw['quotes'])!=1: raise ExternalServiceError('exact option quote response required')
            symbol,q=next(iter(raw['quotes'].items())); match=re.fullmatch(r'([A-Z.]+)(\d{6})([CP])(\d{8})',symbol)
            if not match: raise ExternalServiceError('option symbol is not an exact OCC contract')
            underlying,date,right,strike=match.groups(); expiration=f'20{date[:2]}-{date[2:4]}-{date[4:]}'
            n={'timestamp':q['t'],'symbol':symbol,'underlying':underlying,
               'expiration':expiration,'strike':str(int(strike)/1000).rstrip('0').rstrip('.'),
               'right':'call' if right=='C' else 'put','multiplier':100,
               'currency':'USD','market':'US','bid':q['bp'],'ask':q['ap'],
               'bid_size':q['bs'],'ask_size':q['as']}
        return ReadResult('alpaca',response.feed,raw,n)
    def clock(self): return self._alpaca(self.alpaca.clock(),'clock')
    def calendar(self,start,end): return self._alpaca(self.alpaca.calendar(start,end),'calendar')
    def underlying_quote(self,symbol): return self._alpaca(self.alpaca.underlying_quote(symbol),'underlying_quote')
    def option_chain(self,underlying): return self._alpaca(self.alpaca.option_chain(underlying),'option_chain')
    def option_quote(self,symbol): return self._alpaca(self.alpaca.option_quote(symbol),'option_quote')
    def _aux(self,name,parameter):
        host,path=AUX_ENDPOINTS[name]
        coverage=datetime.now(timezone.utc).date().isoformat()
        query=({'symbols':parameter,'types':'cash_dividend' if name=='dividend' else 'merger,spinoff,split','start':coverage,'end':coverage,'limit':1000,'sort':'asc'}
               if name!='fx' else {'symbols':parameter})
        url=f'https://{host}{path}?{urlencode(query)}'
        response=self.transport.request('GET',url,headers={'Accept':'application/json',
            'APCA-API-KEY-ID':self.credentials[0],'APCA-API-SECRET-KEY':self.credentials[1]},body=None)
        if response.status!=200 or response.url!=url: raise ExternalServiceError(name+' evidence request failed')
        try: raw=json.loads(response.body)
        except Exception: raise ExternalServiceError(name+' evidence response malformed') from None
        if name=='fx':
            if not isinstance(raw,Mapping) or len(raw.get('quotes',{}))!=1: raise ExternalServiceError('fx evidence response malformed')
            symbol,q=next(iter(raw['quotes'].items()))
            if symbol.replace('/','')!='GBPUSD' or parameter!='GBPUSD': raise ExternalServiceError('fx evidence response mismatched request')
            bid,ask=q.get('bp'),q.get('ap'); normalized={'symbol':'GBPUSD','timestamp':q.get('t'),
                'bid':bid,'ask':ask,'mid':str((float(bid)+float(ask))/2)}
        else:
            rows=raw.get('corporate_actions') if isinstance(raw,Mapping) else None
            if not isinstance(rows,list): raise ExternalServiceError(name+' evidence response malformed')
            matched=[x for x in rows if isinstance(x,Mapping) and x.get('symbol')==parameter]
            if not matched and rows: raise ExternalServiceError(name+' evidence response mismatched request')
            if not matched:
                if raw.get('symbol')!=parameter or raw.get('start')!=coverage or raw.get('end')!=coverage or not raw.get('as_of'):
                    raise ExternalServiceError(name+' negative evidence lacks authenticated coverage')
                normalized={'symbol':parameter,'timestamp':raw['as_of'],'retrieved_at':raw['as_of'],
                    'effective_date':coverage,'coverage_start':coverage,'coverage_end':coverage,'records':[],'negative_evidence':True}
            else:
                latest=max(matched,key=lambda x:x.get('updated_at','')); normalized={'symbol':parameter,
                    'timestamp':latest.get('updated_at'),'retrieved_at':latest.get('updated_at'),
                    'effective_date':latest.get('ex_date') or latest.get('effective_date'),
                    'coverage_start':coverage,'coverage_end':coverage,'records':matched,'negative_evidence':False}
        return ReadResult('alpaca',str(raw.get('feed',name)),raw,normalized)
    def corporate_action(self,underlying): return self._aux('corporate_action',underlying)
    def dividend(self,underlying): return self._aux('dividend',underlying)
    def fx(self,pair): return self._aux('fx',pair)

def production_provider_status(environ=None):
    env=os.environ if environ is None else environ
    return {'read_only_activated':live_read_only_enabled(env),**{name:env.get(key)==AUX_SENTINEL for name,key in AUX_ENV.items()}}

def production_provider_factory(environ=None,*,alpaca=None,transport=None):
    env=os.environ if environ is None else environ; status=production_provider_status(env)
    if not status['read_only_activated']: raise ValueError('read-only collection activation is disabled')
    missing=[name for name in AUX_ENV if not status[name]]
    if missing: raise ValueError('required auxiliary providers unavailable: '+', '.join(missing))
    credentials=(env.get('ALPACA_API_KEY_ID',''),env.get('ALPACA_API_SECRET_KEY',''))
    if not all(credentials): raise ValueError('Alpaca read-only credentials unavailable')
    return ProductionCollectionProviders(alpaca or AlpacaReadOnlyClient(environ=env),credentials,transport)
