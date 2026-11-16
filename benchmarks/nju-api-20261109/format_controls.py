import time,json
import benchmark as b
grades=[]
for name,prompt,expected in b.KNOWLEDGE:
    if name not in ["wood_pick_recipe","fuel"]:continue
    shape={k:("?" if not isinstance(v,bool) else "?boolean") for k,v in expected.items()}
    msgs=[{"role":"system","content":"按 Minecraft Java Edition 1.21 原版生存模式回答。只输出JSON对象，不要附加字段。键名参考给出的结构，但问号代表待你独立求出的答案。"},*b.user(prompt+"\nJSON结构："+json.dumps(shape,ensure_ascii=False))]
    r=b.call("control_"+name+"_no_response_format",msgs,512,**b.NO_THINK)
    try:actual=json.loads(r["content"]);passed=actual==expected
    except Exception:actual=r["content"];passed=False
    grades.append(dict(name=name,variant="same_messages_without_response_format",expected=expected,actual=actual,passed=passed))
    time.sleep(2.6)
    r=b.call("control_"+name+"_free_text",b.user("按 Minecraft Java Edition 1.21 原版生存模式回答。"+prompt+"请用简短自然语言回答。"),512,**b.NO_THINK)
    grades.append(dict(name=name,variant="free_text",actual=r["content"],status=r["status"]))
    time.sleep(2.6)
(b.OUT/"format_controls.json").write_text(json.dumps(grades,ensure_ascii=False,indent=2))

