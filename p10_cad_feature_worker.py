"""P10 analytic CAD surface candidates from a verified STEP.

Source file path is passed only by the trusted server as a SHA-checked temporary
copy. A cylindrical face or plane does not imply a specific drawing feature.
"""
from __future__ import annotations
import hashlib
import json
import math
from pathlib import Path
import sys

MAX_STEP_BYTES=8_000_000
MAX_FACES=25000
MAX_FEATURES=300
ROUND_MM=5

def inspect(path):
    from OCP.STEPControl import STEPControl_Reader
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.TopoDS import TopoDS
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopAbs import TopAbs_FACE
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.GeomAbs import GeomAbs_Cylinder, GeomAbs_Plane
    from OCP.collections import Sequence_TCollection_AsciiString
    from OCP.BRepCheck import BRepCheck_Analyzer
    file=Path(path)
    if not file.is_file() or file.stat().st_size>MAX_STEP_BYTES:
        raise ValueError("STEP_MISSING_OR_TOO_LARGE")
    data=file.read_bytes()
    if not data.lstrip().startswith(b"ISO-10303-21;"):
        raise ValueError("NOT_STEP_ENVELOPE")
    digest=hashlib.sha256(data).hexdigest()
    reader=STEPControl_Reader()
    if reader.ReadFile(str(file))!=IFSelect_RetDone:
        raise ValueError("STEP_READ_FAILED")
    lengths=Sequence_TCollection_AsciiString()
    angles=Sequence_TCollection_AsciiString()
    solids=Sequence_TCollection_AsciiString()
    reader.FileUnits(lengths,angles,solids)
    unit_names=[lengths.Value(i).ToCString() for i in range(1,lengths.Length()+1)]
    reader.SetSystemLengthUnit(1.0)
    if reader.TransferRoots()<1:
        raise ValueError("STEP_TRANSFER_FAILED")
    shape=reader.OneShape()
    if shape.IsNull() or not BRepCheck_Analyzer(shape).IsValid():
        raise ValueError("STEP_TOPOLOGY_INVALID")
    features=[]
    # Identical analytic surfaces can be split into several CAD faces; group
    # by radius+axis+location for cylinders and by |normal| for plane angle.
    grouped={}
    exp=TopExp_Explorer(shape,TopAbs_FACE)
    face_index=0
    while exp.More():
        face_index+=1
        if face_index>MAX_FACES:raise ValueError("STEP_FACE_COUNT_LIMIT")
        face=TopoDS.Face(exp.Current())
        surface=BRepAdaptor_Surface(face)
        typ=surface.GetType()
        feature=None
        if typ==GeomAbs_Cylinder:
            cyl=surface.Cylinder()
            direction=cyl.Axis().Direction()
            center=cyl.Location()
            radius=round(float(cyl.Radius()),ROUND_MM)
            xyz=[round(float(getattr(center,axis)()),3) for axis in ("X","Y","Z")]
            vec=[round(float(getattr(direction,axis)()),5) for axis in ("X","Y","Z")]
            if radius>0 and math.isfinite(radius):
                # Axial signs are irrelevant for cylinder identity.
                canonical=tuple(vec) if next((v for v in vec if abs(v)>1e-6),1)>0 else tuple(-v for v in vec)
                feature={"kind":"CYLINDER_RADIUS","value":radius,"unit":"mm",
                         "axis":list(canonical),"axis_location":xyz}
                key=("cylinder",radius,canonical,tuple(xyz))
        elif typ==GeomAbs_Plane:
            plane=surface.Plane()
            normal=plane.Axis().Direction()
            components=tuple(round(abs(float(getattr(normal,a)())),6)
                             for a in ("X","Y","Z"))
            if all(math.isfinite(x) for x in components):
                feature={"kind":"PLANE_DIRECTION",
                         "absolute_normal":list(components),
                         "unit":"degree",
                         "angles_to_axes":{
                            axis:round(math.degrees(math.asin(min(1.0,max(0.0,x)))),5)
                            for axis,x in zip(("x","y","z"),components)}}
                key=("plane",components)
        if feature is not None:
            entry=grouped.get(key)
            if entry:
                entry["face_occurrences"]+=1
                if len(entry["sample_face_indices"])<5:
                    entry["sample_face_indices"].append(face_index)
            else:
                feature["face_occurrences"]=1
                feature["sample_face_indices"]=[face_index]
                grouped[key]=feature
                if len(grouped)>MAX_FEATURES:raise ValueError("STEP_FEATURE_LIMIT")
        exp.Next()
    for key,feature in sorted(grouped.items(),key=lambda item:repr(item[0])):
        payload=json.dumps({"sha256":digest,"key":key},sort_keys=True,separators=(',',':'))
        feature["feature_id"]=hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
        feature["annotation_mapping"]="UNVERIFIED"
        features.append(feature)
    return {
        "state":"ANALYTIC_CAD_CANDIDATES",
        "source_sha256":digest,
        "units":"millimetre" if unit_names==["millimetre"] else "UNKNOWN_OR_MIXED",
        "file_declared_units":unit_names,
        "features":features,"feature_count":len(features),
        "face_count":face_index,
        "manufacturing_authorization":False,
        "warnings":["Analytic face geometry is measured from STEP; which face a drawing annotation points to is not established",
                    "Planar angles compare a selected reference axis with a plane, not arbitrary edge geometry",
                    "No dimensional tolerance, GD&T or approval check is performed"]
    }
if __name__=="__main__":
    try:
        if len(sys.argv)!=2:raise ValueError("STEP_PATH_REQUIRED")
        print("P8_RESULT_JSON:"+json.dumps(inspect(sys.argv[1]),ensure_ascii=True,allow_nan=False))
    except Exception as error:
        print("P8_RESULT_JSON:"+json.dumps({"error":type(error).__name__+": "+str(error)[:160]},ensure_ascii=True))
        sys.exit(2)
