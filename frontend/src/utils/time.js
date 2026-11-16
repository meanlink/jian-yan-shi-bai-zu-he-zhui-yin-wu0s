export function localStamp(iso){return iso?iso.slice(0,16).replace('T',' '):'—';}
export function minutesLabel(m){if(!Number.isFinite(Number(m)))return '—';const v=Number(m);return v>=60?Math.floor(v/60)+' 小时'+(v%60?' '+v%60+' 分':''):v+' 分钟';}
export function shiftLabel(s){if(!s)return '未绑定班次';const pad=n=>String(n).padStart(2,'0');return s.name+' '+pad(Math.floor(s.start_minute/60))+':'+pad(s.start_minute%60)+'–'+pad(Math.floor(s.end_minute/60))+':'+pad(s.end_minute%60);}
export function nowLocalInput(offsetMinutes=480,addMinutes=0){const d=new Date(Date.now()+addMinutes*60000+offsetMinutes*60000);return d.toISOString().slice(0,16);}
export function toEpoch(local){return local?Math.floor(new Date(local).getTime()/1000):null;}
/** 业务时区（固定偏移）下的 datetime-local 字符串 → epoch 秒，不依赖浏览器时区。 */
export function localInputToEpoch(value,offsetMinutes=480){
 if(!value)return null;
 const [datePart,timePart='00:00']=value.split('T');
 const [y,m,d]=datePart.split('-').map(Number);
 const [hh,mm]=timePart.split(':').map(Number);
 return Date.UTC(y,m-1,d,hh,mm)/1000-offsetMinutes*60;
}
