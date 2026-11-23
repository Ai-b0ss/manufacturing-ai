"""P13 PDF vector witness: genuine line/arrow/arc geometry, no fabricated registration.

Reads only SHA-verified temporary copies of source PDFs. Finds real drafting
arrowhead triangles in path geometry (closed or implicitly closed via fill),
then looks for either a dimension leader connected near the text (radius) or
an angle arc whose first and last vertices touch the arrowhead pair.

Geometry is DOCUMENT evidence only, not PDF-to-STEP registration.
"""
from __future__ import annotations
import base64
import hashlib
import json
import math
from pathlib import Path
import sys

MAX_PDF_BYTES=9_000_000
MAX_PAGES=75
MAX_PATHS=2000
MAX_ITEMS=90_000
MAX_PIXELS=1_150_000
MAX_PNG=2_000_000

class VectorFailure(ValueError):pass

def distance(a,b):
    return math.hypot(a[0]-b[0],a[1]-b[1])

def line_length(seg):
    return distance(seg[0],seg[1])

def box_distance(point,bbox):
    x,y=point
    return math.hypot(max(bbox[0]-x,0,x-bbox[2]),
                      max(bbox[1]-y,0,y-bbox[3]))

def blue(rgb):
    return (rgb is not None and len(rgb)==3 and
            rgb[2]>0.75 and 0.3<rgb[1]<0.73 and rgb[0]<0.20)

def segs_for_path(path):
    rows=[]
    for item in path["items"]:
        if item[0]=="l":
            p,q=item[1],item[2]
            pts=((float(p.x),float(p.y)),(float(q.x),float(q.y)))
            if all(math.isfinite(v) for xy in pts for v in xy):
                rows.append(pts)
        else:
            rows.append(None)  # never connect independent path item types
    return rows

def arrowheads(path,path_number,rect):
    if not (blue(path["color"]) or blue(path["fill"])):return []
    segments=segs_for_path(path)
    found=[]
    for i in range(len(segments)-1):
        a=segments[i];b=segments[i+1]
        if a is None or b is None or distance(a[1],b[0])>.6:continue
        # Native drafting arrows commonly use 9.4 mm half-legs and 2.9 mm base.
        long_side=line_length(a);base=line_length(b)
        if not (5.0<long_side<17.0 and 1.0<base<5.1):continue
        tip=a[0]
        if not (distance(tip,b[1])>5 and
                abs(distance(tip,b[1])-long_side)<2.0):continue
        closed=(i+2<len(segments) and segments[i+2] is not None and
                distance(segments[i+2][0],b[1])<.6 and
                distance(segments[i+2][1],tip)<.6)
        if not closed and path["type"]!="f":continue
        if box_distance(tip,rect)>160:continue
        if all(distance(tip,old["point"])>0.85 for old in found):
            found.append({"point":tip,"path_index":path_number,
                          "triangle_item_index":i,
                          "filled":bool(blue(path["fill"]))})
    return found

def nearby_leader(tip,paths,rect):
    """Require vector connectivity at a tip and an end near label, not proximity alone."""
    choices=[]
    for index,path in enumerate(paths):
        if not blue(path["color"]):continue
        for seg in segs_for_path(path):
            if seg is None or line_length(seg)<14:continue
            if distance(seg[0],tip)<1.2:
                other=seg[1]
            elif distance(seg[1],tip)<1.2:
                other=seg[0]
            else:continue
            d=box_distance(other,rect)
            if d<=24:
                choices.append((d,index,other))
    return min(choices,default=None,key=lambda x:x[0])

def point_segment_distance(point,a,b):
    abx=b[0]-a[0];aby=b[1]-a[1]
    denom=abx*abx+aby*aby
    t=max(0.,min(1.,((point[0]-a[0])*abx+(point[1]-a[1])*aby)/denom)) if denom>1e-12 else 0.
    return distance(point,(a[0]+t*abx,a[1]+t*aby))

def contour_contact(point,paths):
    nearby=[]
    for index,path in enumerate(paths):
        color=path["color"]
        if color is None or any(abs(float(c))>.12 for c in color):continue
        segments=[seg for seg in segs_for_path(path) if seg is not None]
        # Only physically drawn complex part outlines; not one-segment black
        # borders/text glyphs, dimension lines or dotted markers.
        if len(segments)<24:continue
        minimum=min((point_segment_distance(point,*seg) for seg in segments),default=1e6)
        if minimum<8:nearby.append((minimum,index))
    return min(nearby,default=None)

def angle_arc(p,path,tips,rect):
    """A single continuous chain must connect two arrow tips, not scattered triangles."""
    if not blue(path["color"]):return None
    segs=segs_for_path(path)
    tipset=[h["point"] for h in tips if h["path_index"]==p]
    if len(tipset)!=2:return None
    # Arc starts after two 3-line triangles and progresses in same vector path.
    candidates=[s for s in segs if s is not None and 0.25<line_length(s)<6.5]
    if len(candidates)<13:return None
    # Search a connected contiguous run of short segments between both tips.
    best=None
    for start in range(len(segs)):
        first=segs[start]
        if first is None or line_length(first)>=6.5:continue
        if min(distance(first[0],t) for t in tipset)>1.5:continue
        chain=[first]
        for idx in range(start+1,len(segs)):
            nextseg=segs[idx]
            if nextseg is None or not (0.2<line_length(nextseg)<6.5) or distance(chain[-1][1],nextseg[0])>0.8:break
            chain.append(nextseg)
        if len(chain)<13:continue
        end=chain[-1][1]
        if (min(distance(end,t) for t in tipset)>1.5 or
                distance(chain[0][0],end)<18):continue
        if box_distance(chain[len(chain)//2][0],rect)>75:continue
        if best is None or len(chain)>len(best):
            best=chain
    return best

def inspect(filename,page_number,bbox,kind):
    import fitz
    f=Path(filename)
    if not f.is_file() or f.stat().st_size>MAX_PDF_BYTES:raise VectorFailure("PDF_UNAVAILABLE")
    raw=f.read_bytes()
    sha=hashlib.sha256(raw).hexdigest()
    if not isinstance(page_number,int) or not 1<=page_number<=MAX_PAGES:
        raise VectorFailure("PAGE_INVALID")
    if kind not in ("RADIUS","ANGULAR"):raise VectorFailure("ANNOTATION_KIND_INVALID")
    if not isinstance(bbox,list) or len(bbox)!=4 or any(type(v) not in (int,float) or
        not math.isfinite(v) or not 0<=v<=1 for v in bbox) or bbox[0]>=bbox[2] or bbox[1]>=bbox[3]:
        raise VectorFailure("BBOX_INVALID")
    with fitz.open(stream=raw,filetype="pdf") as doc:
        if doc.needs_pass or len(doc)>MAX_PAGES or page_number>len(doc):
            raise VectorFailure("PDF_PAGE_UNAVAILABLE")
        page=doc[page_number-1]
        w,h=page.rect.width,page.rect.height
        if not 30<w<5000 or not 30<h<5000:raise VectorFailure("PAGE_SIZE_INVALID")
        rect=(bbox[0]*w,bbox[1]*h,bbox[2]*w,bbox[3]*h)
        paths=page.get_drawings()
        if len(paths)>MAX_PATHS or sum(len(path["items"]) for path in paths)>MAX_ITEMS:
            raise VectorFailure("VECTOR_PATH_LIMIT")
        tips=[]
        for i,path in enumerate(paths):
            for candidate in arrowheads(path,i,rect):
                if all(distance(candidate["point"],old["point"])>1.1 for old in tips):
                    tips.append(candidate)
        if len(tips)>150:raise VectorFailure("ARROWHEAD_LIMIT")
        choices=[]
        if kind=="RADIUS":
            for tip in tips:
                lead=nearby_leader(tip["point"],paths,rect)
                if lead is None:continue
                d,path_id,other=lead
                choices.append({"tip":tip,"start":other,"score":round(d,4),
                                "path_id":path_id,"geometry":"LINE_LEADER"})
            choices.sort(key=lambda x:x["score"])
            if len(choices)>1 and abs(choices[0]["score"]-choices[1]["score"])<6:
                choices=choices[:4];state="MULTIPLE_VECTOR_LEADERS"
            elif choices:
                choices=choices[:1];state="VECTOR_LEADER_OBSERVED"
            else:state="LEADER_NOT_DETERMINED"
        else:
            for i,path in enumerate(paths):
                arc=angle_arc(i,path,tips,rect)
                if arc is None:continue
                first=arc[0][0];last=arc[-1][1]
                score=min(box_distance(pt,rect) for edge in arc for pt in edge)
                choices.append({"start":first,"tip":tips[next(k for k,t in enumerate(tips)
                               if distance(t["point"],first)<1.5)],
                                "second_tip":last,
                                "score":round(score,4),"path_id":i,
                                "geometry":"ANGULAR_ARC","arc":arc})
            choices.sort(key=lambda x:x["score"])
            if len(choices)>1 and abs(choices[0]["score"]-choices[1]["score"])<8:
                choices=choices[:3];state="MULTIPLE_VECTOR_ARCS"
            elif choices:
                choices=choices[:1];state="VECTOR_ANGLE_ARC_OBSERVED"
            else:state="ANGLE_ARC_NOT_DETERMINED"
        evidence=[]
        for choice in choices:
            row={
                "kind":choice["geometry"],
                "arrow_tip_normalized":[round(choice["tip"]["point"][0]/w,6),
                                        round(choice["tip"]["point"][1]/h,6)],
                "arrow_tip_page_points":[round(v,3) for v in choice["tip"]["point"]],
                "document_path_index":choice["path_id"],
                "label_distance_pt":round(float(choice["score"]),3)
            }
            if choice["geometry"]=="LINE_LEADER":
                contact=contour_contact(choice["tip"]["point"],paths)
                row["vector_contour_contact_pt"]=round(contact[0],3) if contact else None
                row["vector_contour_path_index"]=contact[1] if contact else None
                row["targets_drawing_outline"]=bool(contact and contact[0]<=1.8)
                row["label_connection_normalized"]=[round(choice["start"][0]/w,6),
                                                    round(choice["start"][1]/h,6)]
            else:
                row["second_arrow_tip_normalized"]=[round(choice["second_tip"][0]/w,6),
                                                   round(choice["second_tip"][1]/h,6)]
                row["arc_segment_count"]=len(choice["arc"])
            evidence.append(row)
        result={
            "source_sha256":sha,"page":page_number,
            "annotation_bbox":bbox,"evidence_state":state,
            "arrows_total_nearby":len(tips),
            "vector_witnesses":evidence,
            "drawing_to_cad_registration":"NOT_ESTABLISHED",
            "manufacturing_authorization":False
        }
        # Overlay is derived from document vectors and never alters stored PDF.
        xcoords=[rect[0],rect[2]];ycoords=[rect[1],rect[3]]
        for choice in choices:
            points=[choice["tip"]["point"],choice["start"]]
            if "second_tip" in choice:points.append(choice["second_tip"])
            for x,y in points:xcoords.append(x);ycoords.append(y)
        clip=fitz.Rect(min(xcoords)-62,min(ycoords)-62,max(xcoords)+62,max(ycoords)+62)&page.rect
        ratio=min(2.3,math.sqrt(MAX_PIXELS/max(1,clip.width*clip.height)))
        if ratio<0.7:raise VectorFailure("VECTOR_PREVIEW_TOO_LARGE")
        page.draw_rect(fitz.Rect(*rect),color=(0.96,0.33,0.04),width=2.2,overlay=True)
        for choice in choices:
            tip=fitz.Point(*choice["tip"]["point"])
            page.draw_circle(tip,4.1,color=(0.9,0.15,0.04),fill=(1,0.91,0.23),width=1.5,overlay=True)
            if "arc" in choice:
                for a,b in choice["arc"]:
                    page.draw_line(fitz.Point(*a),fitz.Point(*b),
                                   color=(0.92,0.2,0.06),width=1.45,overlay=True)
                page.draw_circle(fitz.Point(*choice["second_tip"]),4.1,
                                 color=(0.9,0.15,0.04),fill=(1,0.91,0.23),width=1.5,overlay=True)
            else:
                page.draw_line(tip,fitz.Point(*choice["start"]),
                               color=(0.92,0.2,0.06),width=1.65,overlay=True)
        pix=page.get_pixmap(matrix=fitz.Matrix(ratio,ratio),clip=clip,alpha=False,
                            colorspace=fitz.csRGB)
        png=pix.tobytes("png")
        if not png.startswith(b"\x89PNG") or len(png)>MAX_PNG:
            raise VectorFailure("PREVIEW_PNG_LIMIT")
        result.update({
            "preview_width":pix.width,"preview_height":pix.height,
            "preview_png_base64":base64.b64encode(png).decode("ascii"),
            "crop_normalized":[round(clip.x0/w,6),round(clip.y0/h,6),
                               round(clip.x1/w,6),round(clip.y1/h,6)]
        })
        return result

if __name__=="__main__":
    try:
        if len(sys.argv)!=5:raise VectorFailure("EXPECTED_PDF_PAGE_BBOX_KIND")
        result=inspect(sys.argv[1],int(sys.argv[2]),json.loads(sys.argv[3]),sys.argv[4])
        print("P13_VECTOR_JSON:"+json.dumps(result,ensure_ascii=True,separators=(",",":"),allow_nan=False))
    except Exception as exc:
        print("P13_VECTOR_JSON:"+json.dumps({"error":type(exc).__name__,"detail":str(exc)[:90]}))
        sys.exit(2)
