import {api} from './client.js';
export const datasets={list:()=>api('/datasets'),get:id=>api('/datasets/'+id),create:data=>api('/datasets',{method:'POST',body:JSON.stringify(data)})};
export const jobs={list:()=>api('/jobs'),get:id=>api('/jobs/'+id),create:data=>api('/jobs',{method:'POST',body:JSON.stringify(data)}),approve:id=>api('/jobs/'+id+'/approve',{method:'POST'})};
export const reports={list:()=>api('/reports'),create:data=>api('/reports',{method:'POST',body:JSON.stringify(data)}),toggle:id=>api('/reports/'+id+'/toggle',{method:'POST'}),deliveries:()=>api('/deliveries')};
export const recheck={
 catalog:()=>api('/recheck/catalog'),
 shifts:()=>api('/recheck/shifts'),createShift:data=>api('/recheck/shifts',{method:'POST',body:JSON.stringify(data)}),
 instruments:()=>api('/recheck/instruments'),createInstrument:data=>api('/recheck/instruments',{method:'POST',body:JSON.stringify(data)}),patchInstrument:(id,data)=>api('/recheck/instruments/'+id,{method:'PATCH',body:JSON.stringify(data)}),
 inspectors:()=>api('/recheck/inspectors'),createInspector:data=>api('/recheck/inspectors',{method:'POST',body:JSON.stringify(data)}),patchInspector:(id,data)=>api('/recheck/inspectors/'+id,{method:'PATCH',body:JSON.stringify(data)}),
 orders:()=>api('/recheck/orders'),order:id=>api('/recheck/orders/'+id),createOrder:data=>api('/recheck/orders',{method:'POST',body:JSON.stringify(data)}),
 preview:id=>api('/recheck/orders/'+id+'/preview',{method:'POST'}),
 confirm:(id,data)=>api('/recheck/orders/'+id+'/confirm',{method:'POST',body:JSON.stringify(data)}),
 claim:(id,data)=>api('/recheck/orders/'+id+'/claims',{method:'POST',body:JSON.stringify(data)}),
 startClaim:(id,token)=>api('/recheck/claims/'+id+'/start',{method:'POST',body:JSON.stringify({token})}),
 completeClaim:id=>api('/recheck/claims/'+id+'/complete',{method:'POST'}),
 dispose:(id,data)=>api('/recheck/orders/'+id+'/dispose',{method:'POST',body:JSON.stringify(data)}),cancelOrder:id=>api('/recheck/orders/'+id+'/cancel',{method:'POST'}),
 downtimes:()=>api('/recheck/downtimes'),downtime:data=>api('/recheck/downtimes',{method:'POST',body:JSON.stringify(data)}),resolveDowntime:(id,restore=true)=>api('/recheck/downtimes/'+id+'/resolve?restore_original='+restore,{method:'POST'}),
 revisions:()=>api('/recheck/revisions')
};
