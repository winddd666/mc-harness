#!/usr/bin/env python3
"""Bounded, authenticated pickup diagnosis using the existing token, never logging in."""
import argparse,json,time,urllib.request
from pathlib import Path
from mc_agent.config import Settings
from mc_agent.auth import TokenStore
from mc_agent.bot import BotSession
from mc_agent.errors import ApiError
from mc_agent.skills.library import MacroSkills

ROOT=Path('/home/xzq/projects/mc')
OUT=ROOT/'experiments/20261115-until-1900/pickup-diagnosis'


def main():
 p=argparse.ArgumentParser();p.add_argument('mode',choices=['inspect','try-pickup','skill-pickup','ground-scan']);args=p.parse_args()
 opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
 with opener.open('http://127.0.0.1:8770/api/state',timeout=8) as r: console=json.load(r)
 if console.get('running'):raise SystemExit('An agent task is active; no diagnosis started.')
 settings=Settings.load(dotenv_path=ROOT/'.env.beast')
 record=TokenStore(settings.state_dir/'tokens.json').get('BeastBot_01')
 if record is None: raise SystemExit('No cached token; refusing to log in.')
 rows=[]
 def call(bot,action,body=None,method='POST'):
  t=time.time()
  try: result={'ok':True,'result':bot.call(action,body,method=method,allow_reauth=False)}
  except ApiError as exc:result={'ok':False,'error':{'code':exc.code,'message':exc.message,**exc.context}}
  row={'time':t,'action':action,'body':body,'duration':time.time()-t,**result};rows.append(row)
  return result.get('result',{})
 with BotSession(record.token,bot_username='BeastBot_01',base_url=settings.mc_api_base_url,timeout=20) as bot:
  state=call(bot,'status',method='GET');call(bot,'inventory',method='GET')
  entities=call(bot,'get_entities',{'radius':16,'types':['item']}).get('entities',[])
  call(bot,'find_blocks',{'types':['grass_block','dirt','birch_log','oak_log'],'radius':16,'limit':15})
  if args.mode=='skill-pickup':
   result=MacroSkills(bot).collect_drops(max_pickups=2)
   rows.append({'time':time.time(),'action':'skill_collect_drops','result':result})
  if args.mode=='ground-scan':
   ground=call(bot,'find_blocks',{'types':['grass_block'],'radius':24,'limit':20}).get('blocks',[])
   for block in ground[:12]:
    x,y,z=block['pos']
    call(bot,'get_block',{'pos':[x,y+1,z]});call(bot,'get_block',{'pos':[x,y+2,z]})
  if args.mode=='try-pickup' and entities:
   entity=entities[0];pos=entity['pos']
   call(bot,'move_to',{'entity_id':entity['id'],'range':0.3,'timeout':8000})
   call(bot,'status',method='GET');call(bot,'inventory',method='GET')
   call(bot,'move_to',{'x':pos[0],'y':pos[1],'z':pos[2],'range':0.3,'timeout':8000})
   time.sleep(0.7)
   call(bot,'status',method='GET');call(bot,'inventory',method='GET')
  if entities:
   pos=entities[0]['pos'];import math
   x,y,z=map(math.floor,pos)
   for dx,dz in [(0,0),(-1,0),(1,0),(0,-1),(0,1)]:
    for dy in (-1,0,1):call(bot,'get_block',{'pos':[x+dx,y+dy,z+dz]})
 OUT.mkdir(parents=True,exist_ok=True)
 dest=OUT/(str(int(time.time()))+'-'+args.mode+'.json');dest.write_text(json.dumps(rows,ensure_ascii=False,indent=2)+'\n')
 for row in rows:
  if row['action']=='status':
   row={**row,'result':{k:v for k,v in row['result'].items() if k in ('pos','health','held','busy')}}
  print(json.dumps(row,ensure_ascii=False)[:2600])
 print('saved',dest)

if __name__=='__main__':main()
