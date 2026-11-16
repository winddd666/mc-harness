#!/usr/bin/env python3
"""Follow-up controls for MC mistakes and image-input availability."""
import json,base64,struct,zlib
import benchmark as b
def knowledge_controls():
    grades=[]
    for name,prompt,expected in b.KNOWLEDGE:
        if name not in ["wood_pick_recipe","fuel"]:continue
        shape={k:("?" if not isinstance(v,bool) else "?boolean") for k,v in expected.items()}
        msgs=[{"role":"system","content":"按 Minecraft Java Edition 1.21 原版生存模式回答。只输出JSON对象，不要附加字段。键名参考给出的结构，但问号代表待你独立求出的答案。"},*b.user(prompt+"\nJSON结构："+json.dumps(shape,ensure_ascii=False))]
        for mode in ["fast_repeat","thinking"]:
            r=b.call("control_"+name+"_"+mode,msgs,2048,response_format={"type":"json_object"},**(b.NO_THINK if mode=="fast_repeat" else {}))
            try:actual=json.loads(r["content"]);passed=actual==expected
            except Exception:actual=r["content"];passed=False
            grades.append({"name":name,"mode":mode,"expected":expected,"actual":actual,"passed":passed,"completion_tokens":r["usage"].get("completion_tokens"),"elapsed_s":r["elapsed_s"]})
    (b.OUT/"control_grades.json").write_text(json.dumps(grades,ensure_ascii=False,indent=2))
def png_grid():
    # Pure generated diagnostic image, not a Minecraft screenshot.
    colors=[[(255,0,0),(0,0,255),(0,255,0)],[(0,0,255),(255,0,0),(0,0,255)],[(0,255,0),(0,0,255),(255,0,0)]]
    size=192
    raw=b"".join(b"\0"+b"".join(bytes(colors[y//64][x//64]) for x in range(size)) for y in range(size))
    def chunk(typ,data):return struct.pack(">I",len(data))+typ+data+struct.pack(">I",zlib.crc32(typ+data)&0xffffffff)
    png=b"\x89PNG\r\n\x1a\n"+chunk(b"IHDR",struct.pack(">IIBBBBB",size,size,8,2,0,0,0))+chunk(b"IDAT",zlib.compress(raw))+chunk(b"IEND",b"")
    url="data:image/png;base64,"+base64.b64encode(png).decode()
    r=b.call("vision_grid_probe",[{"role":"user","content":[{"type":"text","text":'图像为3x3色块矩阵，只观察图像。输出JSON：center_color的值为red/green/blue之一，red_count为红色色块数量。不要猜。'},{"type":"image_url","image_url":{"url":url}}]}],512,response_format={"type":"json_object"},**b.NO_THINK)
    try:passed=json.loads(r["content"])=={"center_color":"red","red_count":3}
    except Exception:passed=False
    (b.OUT/"vision_grade.json").write_text(json.dumps({"status":r["status"],"passed":passed,"actual":r["content"],"error":r.get("error"),"limitation":"Synthetic color grid only; not gameplay or Minecraft perception validation."},ensure_ascii=False,indent=2))
if __name__=="__main__":
    knowledge_controls()
    png_grid()

