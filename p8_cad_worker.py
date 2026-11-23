"""Run only as a short-lived, bounded subprocess on validated public STEP files.

OpenCascade computes actual BREP topology and bounding dimensions, not a
CARTESIAN_POINT text approximation. This still does not compare to PDF drawings.
"""
from __future__ import annotations
import json
import pathlib
import sys

MAX_STEP_BYTES=8_000_000

def inspect(path):
    from OCP.STEPControl import STEPControl_Reader
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopAbs import TopAbs_SOLID,TopAbs_FACE
    file=pathlib.Path(path)
    if not file.is_file() or file.stat().st_size>MAX_STEP_BYTES:
        raise ValueError("STEP missing or exceeds size limit")
    header=file.open("rb").read(2048)
    if not header.lstrip().startswith(b"ISO-10303-21;"):
        raise ValueError("Invalid STEP envelope")
    from OCP.collections import Sequence_TCollection_AsciiString
    reader=STEPControl_Reader()
    status=reader.ReadFile(str(file))
    if status!=IFSelect_RetDone:
        return {"state":"STEP_READER_FAILED","geometry_verified":False}
    raw_lengths=Sequence_TCollection_AsciiString()
    raw_angles=Sequence_TCollection_AsciiString()
    raw_solids=Sequence_TCollection_AsciiString()
    reader.FileUnits(raw_lengths,raw_angles,raw_solids)
    length_units=[raw_lengths.Value(i).ToCString() for i in range(1,raw_lengths.Length()+1)]
    # OCCT's system-length unit of 1.0 corresponds to millimetres.
    # Use it explicitly and never compare if the original declares mixed units.
    reader.SetSystemLengthUnit(1.0)
    exact_mm=(length_units==["millimetre"] and abs(reader.SystemLengthUnit()-1.0)<1e-12)
    if reader.TransferRoots()<1:
        return {"state":"STEP_TRANSFER_FAILED","geometry_verified":False}
    shape=reader.OneShape()
    if shape.IsNull():
        return {"state":"STEP_NULL_SHAPE","geometry_verified":False}
    valid=BRepCheck_Analyzer(shape).IsValid()
    bounding=Bnd_Box()
    BRepBndLib.Add_s(shape,bounding,False)
    if bounding.IsVoid() or bounding.IsOpen():
        raise ValueError("Unbounded geometry")
    xmin,ymin,zmin,xmax,ymax,zmax=(
        bounding.GetXMin(),bounding.GetYMin(),bounding.GetZMin(),
        bounding.GetXMax(),bounding.GetYMax(),bounding.GetZMax())
    xmin,ymin,zmin,xmax,ymax,zmax=map(float,(xmin,ymin,zmin,xmax,ymax,zmax))
    dimensions=[round(xmax-xmin,5),round(ymax-ymin,5),round(zmax-zmin,5)]
    solids=0
    explorer=TopExp_Explorer(shape,TopAbs_SOLID)
    while explorer.More():
        solids+=1
        if solids>5000: raise ValueError("Too many solids")
        explorer.Next()
    faces=0
    explorer=TopExp_Explorer(shape,TopAbs_FACE)
    while explorer.More():
        faces+=1
        if faces>250000: raise ValueError("Too many faces")
        explorer.Next()
    props=GProp_GProps()
    BRepGProp.VolumeProperties_s(shape,props)
    volume=float(props.Mass())
    usable=(valid and solids>=1 and all(x>0 for x in dimensions) and volume>0)
    return {
        "state":"BREP_VALID" if usable else "BREP_REVIEW_REQUIRED",
        "reader":"OpenCascade",
        "geometry_loaded":True,
        "geometry_verified":bool(usable),
        "comparison_to_drawing":"NOT_PERFORMED",
        "units":"millimetre" if exact_mm else "UNKNOWN_OR_MIXED",
        "file_declared_length_units":length_units,
        "occt_system_length_unit_mm":reader.SystemLengthUnit(),
        "raw_xyz_bbox":[round(v,5) for v in (xmin,ymin,zmin,xmax,ymax,zmax)],
        "xyz_extent":dimensions,
        "solid_count":solids,
        "face_count":faces,
        "volume_in_cad_units_cubed":round(volume,5),
        "brep_valid":bool(valid),
        "manufacturing_authorization":False
    }

if __name__=="__main__":
    try:
        if len(sys.argv)!=2:raise ValueError("Provide STEP path")
        print('P8_RESULT_JSON:'+json.dumps(inspect(sys.argv[1]),ensure_ascii=True,allow_nan=False))
    except Exception as error:
        print('P8_RESULT_JSON:'+json.dumps({"error":type(error).__name__+": "+str(error)[:180]},ensure_ascii=True))
        sys.exit(2)
